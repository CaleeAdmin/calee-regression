from __future__ import annotations

import json
import re
import time
from pathlib import Path

import click

from . import appium_lifecycle
from . import config as config_mod
from . import manual_checks as manual_checks_mod
from . import preflight, release_platforms, reporting, suites
from .consolidated_report import ManualCheck, build_release_report, decide_status, write_release_bundle
from .fixture_bridge import FixtureBridgeError, run_fixture_action
from .models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_REGRESSION, EXIT_SUCCESS
from .runner import ScenarioRunner

REPO_ROOT = suites.REPO_ROOT

_STYLE = {"ok": "green", "warning": "yellow", "error": "red"}
_LABEL = {"ok": "[OK]", "warning": "[WARN]", "error": "[ERROR]"}


@click.group()
def main():
    """Calee regression testing framework."""


def _load_config_or_exit(config_path):
    if not config_path:
        click.echo("No config given. Pass --config path/to/file.yaml or set CALEE_TEST_CONFIG.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    try:
        return config_mod.load_config(config_path)
    except config_mod.ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)


_STATUS_TO_EXIT_CODE = {
    "pass": EXIT_SUCCESS,
    "fail": EXIT_REGRESSION,
    "blocked": EXIT_BLOCKED,
}


def _exit_code_for(result) -> int:
    """Map a SuiteResult to the framework's exit-code contract.

    Delegates the actual PASS/FAIL/BLOCKED decision to
    consolidated_report.decide_status so this CLI and the consolidated
    cross-repo report can never disagree about what a given set of counts
    means. A mandatory (release-critical) scenario that ended up SKIPPED
    is folded into the same "blocked" bucket as an outright blocked
    scenario -- a required scenario that never ran must never let the
    suite read as an overall pass just because everything that *did* run
    happened to pass.
    """
    status = decide_status(
        passed=result.passed_count,
        failed=result.failed_count,
        blocked=result.blocked_count + result.mandatory_skipped_count,
        total=len(result.scenarios),
    )
    return _STATUS_TO_EXIT_CODE[status]


def _resolve_scenario_path(scenario_arg: str) -> Path:
    p = Path(scenario_arg)
    if p.is_absolute():
        return p
    if (Path.cwd() / p).exists():
        return Path.cwd() / p
    return REPO_ROOT / p


@main.command()
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
def doctor(config_path):
    """Check the local Appium/adb/config setup for common mistakes."""
    cfg = _load_config_or_exit(config_path)
    checks = preflight.run_doctor(cfg)
    for check in checks:
        label = _LABEL.get(check.status, f"[{check.status.upper()}]")
        color = _STYLE.get(check.status, None)
        line = f"{label} {check.name}: {check.message}"
        click.echo(click.style(line, fg=color) if color else line)
        if check.hint:
            click.echo(f"       hint: {check.hint}")
    # A failed preflight check means the test environment isn't ready to run
    # anything meaningful yet — that's a blocked run, not a product failure.
    raise SystemExit(EXIT_BLOCKED if preflight.has_errors(checks) else EXIT_SUCCESS)


_FIXTURE_VERSION_RE = re.compile(r"version=([^,\s\)]+)")


def _extract_fixture_version(output: "str | None") -> "str | None":
    if not output:
        return None
    match = _FIXTURE_VERSION_RE.search(output)
    return match.group(1) if match else None


def _environment_status_path() -> Path:
    return REPO_ROOT / "reports" / "environment-status-latest.json"


def _appium_log_path() -> Path:
    return REPO_ROOT / "reports" / "appium.log"


def _appium_pid_path() -> Path:
    return REPO_ROOT / "reports" / "appium.pid"


def _manual_checks_latest_path() -> Path:
    return REPO_ROOT / "reports" / "manual-checks-latest.json"


def _ensure_appium_or_echo_blocked(cfg, *, ready_timeout_seconds: float = 60) -> bool:
    """Auto-starts Appium if the configured endpoint isn't already
    healthy, so the tester never has to open a separate Terminal (see
    Workstream 8). Returns True if Appium is (now) reachable."""
    click.echo(f"\nChecking Appium at {cfg.appium_url} ...")
    try:
        handle = appium_lifecycle.ensure_appium_running(
            base_url=cfg.appium_url, log_path=_appium_log_path(), pid_file=_appium_pid_path(),
            ready_timeout_seconds=ready_timeout_seconds,
        )
    except appium_lifecycle.AppiumLifecycleError as exc:
        click.echo(f"BLOCKED: could not start Appium automatically: {exc}", err=True)
        return False
    if handle.started_by_us:
        click.echo(f"[OK] Appium started automatically (log: {_appium_log_path()})")
    else:
        click.echo("[OK] Appium was already running")
    return True


def _write_environment_status(
    *,
    target_environment: "str | None",
    fixture_version: "str | None",
    fixture_reset_status: str,
    fixture_verification_status: str,
    suite_name: "str | None",
) -> Path:
    """Records fixture/environment status for the consolidated report.

    Never includes the email/password/access token -- only the target base
    URL (not a secret) and status labels. See docs/RELEASE_POLICY.md and
    Workstream 2's "the report must record" requirement.
    """
    path = _environment_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "targetEnvironment": target_environment,
        "fixtureVersion": fixture_version,
        "fixtureResetStatus": fixture_reset_status,
        "fixtureVerificationStatus": fixture_verification_status,
        "suite": suite_name,
        "preparedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


@main.command()
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option("--fixture-base-url", envvar="CALEE_API_BASE", default=None)
@click.option("--fixture-email", envvar="CALEE_TEST_EMAIL", default=None)
@click.option("--fixture-password", envvar="CALEE_TEST_PASSWORD", default=None)
@click.option(
    "--suite", "suite_name", default=None,
    help="The suite this environment is being prepared for (e.g. tablet-full). Used only to "
         "reject --skip-fixture/--allow-no-fixture for a suite that actually needs the fixture.",
)
@click.option(
    "--skip-fixture", "--allow-no-fixture", "skip_fixture", is_flag=True, default=False,
    help="Explicit technical-owner opt-out: proceed without resetting the deterministic REG-* "
         "fixture. Refused (BLOCKED) for a --suite that depends on the fixture (e.g. tablet-full, "
         "full-release, calendar) -- see docs/TEST_DATA_RESET_CONTRACT.md.",
)
def prepare(config_path, fixture_base_url, fixture_email, fixture_password, suite_name, skip_fixture):
    """Check the local environment and reset+verify the deterministic REG-* fixture.

    This is what "01 Prepare Test Environment" runs. It never claims READY
    it can't back up: any preflight error, any fixture-reset/verify failure,
    or (for a release-gating profile) missing fixture credentials all exit
    BLOCKED -- see Workstream 2's fixture-preparation-integrity requirement.
    Only a real preflight pass plus (fixture credentials given and both
    reset and verify succeeding, or an explicit --skip-fixture/
    --allow-no-fixture for a suite that doesn't need the fixture) exits
    READY.
    """
    cfg = _load_config_or_exit(config_path)

    if not _ensure_appium_or_echo_blocked(cfg):
        raise SystemExit(EXIT_BLOCKED)

    checks = preflight.run_doctor(cfg)
    for check in checks:
        label = _LABEL.get(check.status, f"[{check.status.upper()}]")
        color = _STYLE.get(check.status, None)
        line = f"{label} {check.name}: {check.message}"
        click.echo(click.style(line, fg=color) if color else line)
        if check.hint:
            click.echo(f"       hint: {check.hint}")

    if preflight.has_errors(checks):
        click.echo("\nEnvironment is not ready — fix the [ERROR] items above and run this again.", err=True)
        raise SystemExit(EXIT_BLOCKED)

    suite_requires_fixture = False
    if suite_name:
        try:
            suite_requires_fixture = suites.suite_requires_fixture(suite_name)
        except suites.SuiteError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)

    if skip_fixture:
        if suite_requires_fixture:
            click.echo(
                f"\nBLOCKED: --skip-fixture/--allow-no-fixture was used with --suite {suite_name!r}, "
                f"which depends on the deterministic REG-* fixture (see "
                f"docs/TEST_DATA_RESET_CONTRACT.md). Fixture preparation cannot be silently skipped "
                f"for this suite — configure fixture credentials, or choose a suite that doesn't "
                f"need the fixture.",
                err=True,
            )
            raise SystemExit(EXIT_BLOCKED)
        click.echo("\nFixture reset skipped (--skip-fixture/--allow-no-fixture).")
        _write_environment_status(
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="skipped", fixture_verification_status="skipped", suite_name=suite_name,
        )
        raise SystemExit(EXIT_SUCCESS)

    if not (fixture_base_url and fixture_email and fixture_password):
        click.echo(
            "\nBLOCKED: fixture credentials are not configured (set CALEE_API_BASE, "
            "CALEE_TEST_EMAIL, CALEE_TEST_PASSWORD, or pass --fixture-base-url/--fixture-email/"
            "--fixture-password). Release-gating scenarios that require the deterministic fixture "
            "(e.g. the calendar suite) cannot be trusted without it. If you are deliberately "
            "running a suite that doesn't need the fixture, pass --allow-no-fixture "
            "--suite <suite-name>.",
            err=True,
        )
        _write_environment_status(
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="blocked_missing_credentials", fixture_verification_status="blocked_missing_credentials",
            suite_name=suite_name,
        )
        raise SystemExit(EXIT_BLOCKED)

    click.echo(f"\nResetting the regression fixture at {fixture_base_url} ...")
    try:
        reset_output = run_fixture_action(
            "reset", repo_root=REPO_ROOT, base_url=fixture_base_url, email=fixture_email, password=fixture_password,
        )
    except FixtureBridgeError as exc:
        click.echo(f"\n=== Blocked: fixture reset failed: {exc} ===", err=True)
        _write_environment_status(
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="blocked", fixture_verification_status="not_run", suite_name=suite_name,
        )
        raise SystemExit(EXIT_BLOCKED)
    click.echo(reset_output)

    click.echo(f"\nVerifying the regression fixture at {fixture_base_url} ...")
    try:
        verify_output = run_fixture_action(
            "verify", repo_root=REPO_ROOT, base_url=fixture_base_url, email=fixture_email, password=fixture_password,
        )
    except FixtureBridgeError as exc:
        click.echo(f"\n=== Blocked: fixture verification failed: {exc} ===", err=True)
        _write_environment_status(
            target_environment=fixture_base_url,
            fixture_version=_extract_fixture_version(reset_output),
            fixture_reset_status="ok", fixture_verification_status="blocked", suite_name=suite_name,
        )
        raise SystemExit(EXIT_BLOCKED)
    click.echo(verify_output)

    fixture_version = _extract_fixture_version(verify_output) or _extract_fixture_version(reset_output)
    status_path = _write_environment_status(
        target_environment=fixture_base_url, fixture_version=fixture_version,
        fixture_reset_status="ok", fixture_verification_status="ok", suite_name=suite_name,
    )
    click.echo(f"\nEnvironment ready. Fixture version: {fixture_version or 'unknown'}.")
    click.echo(f"Environment status: {status_path}")
    raise SystemExit(EXIT_SUCCESS)


@main.command("stop-appium")
def stop_appium_cmd():
    """Stops Appium, but only if THIS framework started it (tracked via
    reports/appium.pid) -- a no-op if Appium was already running before
    `prepare` touched it, or if nothing was ever auto-started. Run at the
    end of "06 Test Full Calee Solution" so a multi-step tester session
    doesn't restart Appium between every launcher, but the very last step
    still cleans up.
    """
    stopped = appium_lifecycle.stop_appium_from_pid_file(_appium_pid_path())
    if stopped:
        click.echo("[OK] Appium (started by this framework) stopped.")
    else:
        click.echo("Nothing to stop (Appium was not auto-started by this framework, or is not running).")
    raise SystemExit(EXIT_SUCCESS)


@main.command("record-manual-checks")
@click.option(
    "--checks", "checks_path", type=click.Path(), default=None,
    help="Manual check definitions JSON. Defaults to config/manual-checks.json, falling back to "
         "config/manual-checks.example.json if the real one hasn't been set up yet.",
)
@click.option("--out", "out_path", type=click.Path(), default=None, help="Where to write the recorded results")
def record_manual_checks(checks_path, out_path):
    """Guided terminal menu for recording manual checks -- "05 Record Manual Checks".

    The tester only ever types a single digit (1-6); nothing here requires
    editing JSON/YAML. Unanswered mandatory checks are recorded with
    status=null, which consolidate/component_from_manual_checks already
    treats as BLOCKED -- an unanswered mandatory check can never silently
    read as a pass.
    """
    default_checks = REPO_ROOT / "config" / "manual-checks.json"
    example_checks = REPO_ROOT / "config" / "manual-checks.example.json"
    resolved_checks_path = Path(checks_path) if checks_path else (
        default_checks if default_checks.is_file() else example_checks
    )

    try:
        definitions = manual_checks_mod.load_check_definitions(resolved_checks_path)
    except manual_checks_mod.ManualChecksDefinitionError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    click.echo(f"Recording manual checks from: {resolved_checks_path}")
    if resolved_checks_path == example_checks:
        click.echo(
            "(Using the example checklist -- ask your technical owner to set up "
            "config/manual-checks.json with your release's real checks.)"
        )

    results = manual_checks_mod.run_recorder(definitions)

    out = Path(out_path) if out_path else manual_checks_mod.default_output_path(REPO_ROOT / "reports")
    manual_checks_mod.write_results(results, out)
    manual_checks_mod.write_results(results, _manual_checks_latest_path())

    click.echo(manual_checks_mod.summarize(results))
    click.echo(f"\nSaved: {out}")

    unanswered_mandatory = any(r["status"] is None and r["mandatory"] for r in results)
    failed_mandatory = any(r["status"] == manual_checks_mod.STATUS_FAIL and r["mandatory"] for r in results)
    if failed_mandatory:
        raise SystemExit(EXIT_REGRESSION)
    if unanswered_mandatory:
        raise SystemExit(EXIT_BLOCKED)
    blocked_mandatory = any(r["status"] == manual_checks_mod.STATUS_BLOCKED and r["mandatory"] for r in results)
    raise SystemExit(EXIT_BLOCKED if blocked_mandatory else EXIT_SUCCESS)


@main.command("list-suites")
def list_suites_cmd():
    """List all available suites and the scenario files each one resolves to."""
    for name in suites.all_suite_names():
        physical_marker = " [physical-only scenarios included]" if suites.suite_includes_physical(name) else ""
        composite = " (alias)" if name in suites.SUITE_ALIASES else (
            " (composite)" if name in suites.COMPOSITE_SUITES else ""
        )
        click.echo(f"{name}{composite}{physical_marker}")
        for path in suites.resolve_suite(name):
            click.echo(f"    {path.relative_to(REPO_ROOT)}")


@main.command()
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option("--scenario", "scenario_arg", required=True)
def run(config_path, scenario_arg):
    """Run a single scenario YAML file."""
    cfg = _load_config_or_exit(config_path)
    scenario_path = _resolve_scenario_path(scenario_arg)
    rb = reporting.ReportBuilder(cfg, run_name=scenario_path.stem)
    result = ScenarioRunner(cfg, report_builder=rb).run_scenarios([scenario_path], suite_name=scenario_path.stem)
    report_dir = rb.write(result)
    click.echo(
        f"Passed: {result.passed_count}  Failed: {result.failed_count}  "
        f"Skipped: {result.skipped_count}  Blocked: {result.blocked_count}"
    )
    click.echo(f"Report: {report_dir}")
    raise SystemExit(_exit_code_for(result))


@main.command()
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option("--suite", "suite_name", required=True)
@click.option(
    "--confirm-technical", is_flag=True, default=False,
    help="Required (or set allow_release_technical: true in your config) to run a suite containing "
         "physical-tablet-only scenarios.",
)
def suite(config_path, suite_name, confirm_technical):
    """Run a named suite of scenarios."""
    cfg = _load_config_or_exit(config_path)
    try:
        scenario_paths = suites.resolve_suite(suite_name)
    except suites.SuiteError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    if suites.suite_includes_physical(suite_name) and not (confirm_technical or cfg.allow_release_technical):
        click.echo(
            f"Suite '{suite_name}' includes physical-tablet-only scenarios (kiosk/admin/system-receiver "
            f"tests). Re-run with --confirm-technical, or set allow_release_technical: true in your "
            f"config, once you have a real tablet ready.",
            err=True,
        )
        raise SystemExit(EXIT_INVALID_CONFIG)

    rb = reporting.ReportBuilder(cfg, run_name=suite_name)
    result = ScenarioRunner(cfg, report_builder=rb).run_scenarios(scenario_paths, suite_name=suite_name)
    report_dir = rb.write(result)
    click.echo(
        f"Passed: {result.passed_count}  Failed: {result.failed_count}  "
        f"Skipped: {result.skipped_count}  Blocked: {result.blocked_count}"
    )
    click.echo(f"Report: {report_dir}")
    raise SystemExit(_exit_code_for(result))


def _load_json_report(path: "str | None") -> "dict | None":
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@main.command("release-platforms")
def release_platforms_cmd():
    """Prints the resolved release-platform profile as shell variable
    assignments, so a launcher script can `eval "$(python -m
    calee_regression release-platforms)"` and branch on
    $RELEASE_PLATFORM_ANDROID / $RELEASE_PLATFORM_IOS / $RELEASE_PLATFORM_TABLET
    without parsing YAML in bash. See release_platforms.py and
    config/release-platforms.example.yaml.
    """
    try:
        platforms = release_platforms.load_release_platforms()
    except release_platforms.ReleasePlatformsError as exc:
        click.echo(f"echo '{exc}' >&2; exit {EXIT_INVALID_CONFIG}")
        raise SystemExit(EXIT_INVALID_CONFIG)
    click.echo(f"RELEASE_PLATFORM_TABLET={'true' if platforms.tablet else 'false'}")
    click.echo(f"RELEASE_PLATFORM_ANDROID={'true' if platforms.mobile_android else 'false'}")
    click.echo(f"RELEASE_PLATFORM_IOS={'true' if platforms.mobile_ios else 'false'}")
    raise SystemExit(EXIT_SUCCESS)


def _load_manual_checks(path: "str | None") -> "list[ManualCheck] | None":
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [
        ManualCheck(
            title=item["title"],
            instruction=item["instruction"],
            expected_result=item.get("expectedResult", ""),
            status=item.get("status"),
            note=item.get("note", ""),
            screenshot_ref=item.get("screenshotRef"),
            mandatory=item.get("mandatory", True),
        )
        for item in raw
    ]


@main.command()
@click.option("--tablet-report", type=click.Path(exists=True), default=None, help="calee-regression results.json")
@click.option("--mobile-api-report", type=click.Path(exists=True), default=None, help="CaleeMobile-Regression api --report json")
@click.option("--mobile-android-report", type=click.Path(exists=True), default=None, help="CaleeMobile-Regression ui Android --report json")
@click.option("--mobile-ios-report", type=click.Path(exists=True), default=None, help="CaleeMobile-Regression ui iOS --report json")
@click.option("--manual-checks", "manual_checks_path", type=click.Path(exists=True), default=None, help="JSON list of manual guided check results")
@click.option(
    "--environment-report", type=click.Path(exists=True), default=None,
    help="prepare's reports/environment-status-latest.json (fixture version/reset/verify status). "
         "Auto-discovered from reports/ if not given and present.",
)
@click.option(
    "--android-mandatory/--android-optional", "android_mandatory", default=None,
    help="Whether the Android UI report is release-gating. Defaults to config/release-platforms.yaml "
         "(mobile_android), or True if that file is absent.",
)
@click.option(
    "--ios-mandatory/--ios-optional", "ios_mandatory", default=None,
    help="Whether the iPhone UI report is release-gating. Defaults to config/release-platforms.yaml "
         "(mobile_ios), or True if that file is absent.",
)
@click.option("--build-version", default="unknown", help="Combined/overall application build label (used for the bundle filename)")
@click.option("--calee-build-version", default=None, help="Calee tablet app package version under test")
@click.option("--expected-calee-build-version", default=None, help="Technical-owner-configured expected Calee build; mismatch BLOCKS")
@click.option("--caleemobile-build-version", default=None, help="CaleeMobile app version/build under test")
@click.option("--expected-caleemobile-build-version", default=None, help="Technical-owner-configured expected CaleeMobile build; mismatch BLOCKS")
@click.option("--caleeshell-version", default=None, help="CaleeShell version, where available")
@click.option("--calee-git-sha", default=None, help="Calee repo commit SHA under test")
@click.option("--caleemobile-git-sha", default=None, help="CaleeMobile repo commit SHA under test")
@click.option("--android-device-id", default=None, help="Android device/emulator identifier used for the Android UI run")
@click.option("--ios-device-id", default=None, help="iOS device/simulator identifier used for the iOS UI run")
@click.option("--test-environment", default="", help="Target environment URL")
@click.option("--tester", default="", help="Tester name/identifier")
@click.option("--out-dir", type=click.Path(), default=None, help="Where to write the consolidated bundle (default: reports/)")
def consolidate(
    tablet_report, mobile_api_report, mobile_android_report, mobile_ios_report,
    manual_checks_path, environment_report, android_mandatory, ios_mandatory,
    build_version, calee_build_version, expected_calee_build_version,
    caleemobile_build_version, expected_caleemobile_build_version, caleeshell_version,
    calee_git_sha, caleemobile_git_sha, android_device_id, ios_device_id,
    test_environment, tester, out_dir,
):
    """Combine per-framework JSON reports into one consolidated release report.

    Reads already-produced report files -- it does not run anything itself.
    Any report not given is treated as "not executed", which blocks an
    overall PASS for any component that is mandatory for this release (see
    docs/RELEASE_POLICY.md and config/release-platforms.example.yaml).
    """
    env_report_path = environment_report or (
        str(_environment_status_path()) if _environment_status_path().is_file() else None
    )
    env_status = _load_json_report(env_report_path) or {}
    meta = {
        "buildVersion": build_version,
        "caleeBuildVersion": calee_build_version,
        "caleeMobileBuildVersion": caleemobile_build_version,
        "caleeShellVersion": caleeshell_version,
        "caleeGitSha": calee_git_sha,
        "caleeMobileGitSha": caleemobile_git_sha,
        "androidDeviceId": android_device_id,
        "iosDeviceId": ios_device_id,
        "testEnvironment": test_environment,
        "tester": tester,
    }
    if env_status:
        meta["fixtureVersion"] = env_status.get("fixtureVersion")
        meta["fixtureTargetEnvironment"] = env_status.get("targetEnvironment")
        meta["fixtureResetStatus"] = env_status.get("fixtureResetStatus")
        meta["fixtureVerificationStatus"] = env_status.get("fixtureVerificationStatus")
    meta = {k: v for k, v in meta.items() if v not in (None, "")}

    try:
        platforms = release_platforms.load_release_platforms()
    except release_platforms.ReleasePlatformsError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    android_gating = android_mandatory if android_mandatory is not None else platforms.mobile_android
    ios_gating = ios_mandatory if ios_mandatory is not None else platforms.mobile_ios

    report = build_release_report(
        tablet=_load_json_report(tablet_report),
        mobile_api=_load_json_report(mobile_api_report),
        mobile_android_ui=_load_json_report(mobile_android_report),
        mobile_ios_ui=_load_json_report(mobile_ios_report),
        manual_checks=_load_manual_checks(manual_checks_path),
        meta=meta,
        android_mandatory=android_gating,
        ios_mandatory=ios_gating,
        calee_build_version=calee_build_version,
        expected_calee_build_version=expected_calee_build_version,
        caleemobile_build_version=caleemobile_build_version,
        expected_caleemobile_build_version=expected_caleemobile_build_version,
    )

    out = Path(out_dir) if out_dir else REPO_ROOT / "reports"
    bundle_dir = out / f"consolidated-{time.strftime('%Y%m%d-%H%M%S')}"
    bundle_path = write_release_bundle(report, bundle_dir, build_label=build_version)

    click.echo(f"Overall: {report.overall_status.upper()}")
    for component in report.components:
        marker = "" if component.mandatory else " (optional)"
        click.echo(f"  {component.name}{marker}: {component.status.upper()}")
    click.echo(f"Suggested next action: {report.summary.get('suggestedNextAction', '')}")
    click.echo(f"Bundle: {bundle_path}")

    raise SystemExit(_STATUS_TO_EXIT_CODE[report.overall_status])


if __name__ == "__main__":
    main()
