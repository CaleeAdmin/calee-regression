from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import NamedTuple

import click

from . import appium_lifecycle
from . import build_identity as build_identity_mod
from . import config as config_mod
from . import manual_checks as manual_checks_mod
from . import preflight, release_platforms, reporting, suites
from . import run_context
from . import sync_smoke
from .appium_driver import CaleeDriver
from .consolidated_report import (
    STATUS_BLOCKED,
    STATUS_PASS,
    ManualCheck,
    build_release_report,
    component_from_identity_stability,
    decide_status,
    write_release_bundle,
)
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


def _resolve_run_id(run_id: "str | None") -> str:
    """Every `prepare` invocation operates inside a run workspace, whether
    or not it's part of an orchestrated "06 Test Full Calee Solution" run
    -- a standalone "01 Prepare Test Environment" run just gets a fresh
    run ID of its own instead of overwriting a shared "-latest" file (see
    run_context.py's module docstring for why that pattern was the bug).
    """
    if run_id:
        if not run_context.is_valid_run_id(run_id):
            click.echo(f"Invalid --run-id {run_id!r} (expected letters/digits/._- only).", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        return run_id
    env_run_id = os.environ.get("CALEE_RUN_ID")
    if env_run_id:
        return _resolve_run_id(env_run_id)
    return run_context.generate_run_id()


def _load_or_init_manifest(
    workspace: run_context.RunWorkspace, *, suite_name: "str | None" = None, tester: "str | None" = None
) -> run_context.RunManifest:
    if workspace.manifest_path.is_file():
        return run_context.RunManifest.load(workspace.manifest_path)
    try:
        platforms = release_platforms.load_release_platforms()
        profile = {
            "tablet": platforms.tablet,
            "mobile_android": platforms.mobile_android,
            "mobile_ios": platforms.mobile_ios,
        }
    except release_platforms.ReleasePlatformsError:
        profile = {}
    return run_context.RunManifest(
        run_id=workspace.run_id,
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        expected_components=list(run_context.COMPONENT_NAMES),
        release_platform_profile=profile,
        tester=tester or os.environ.get("CALEE_TESTER_ID") or None,
    )


def _appium_log_path() -> Path:
    return REPO_ROOT / "reports" / "appium.log"


def _appium_pid_path() -> Path:
    return REPO_ROOT / "reports" / "appium.pid"


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


def _write_environment_report(
    workspace: run_context.RunWorkspace,
    *,
    status: str,
    detail: "list[str]",
    target_environment: "str | None",
    fixture_version: "str | None",
    fixture_reset_status: str,
    fixture_verification_status: str,
    suite_name: "str | None",
) -> Path:
    """Records fixture/environment status as this run's mandatory "Test
    environment and regression fixture" component (see
    consolidated_report.component_from_environment_report). `status` is
    "pass" or "blocked" -- Prepare has no concept of a product FAIL, only
    "ready" or "not ready" -- see docs/RELEASE_POLICY.md.

    Never includes the email/password/access token -- only the target base
    URL (not a secret) and status labels.
    """
    path = workspace.component_report_path("environment")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "runId": workspace.run_id,
        "status": status,
        "detail": detail,
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
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", default=None,
    help="Shared release run ID (see run_context.py). Auto-generated when omitted, so a "
         "standalone 'Prepare Test Environment' run still gets its own workspace instead of "
         "overwriting a shared '-latest' file.",
)
@click.option("--tester", "tester_opt", envvar="CALEE_TESTER_ID", default=None)
def prepare(config_path, fixture_base_url, fixture_email, fixture_password, suite_name, skip_fixture, run_id_opt, tester_opt):
    """Check the local environment and reset+verify the deterministic REG-* fixture.

    This is what "01 Prepare Test Environment" runs, and step 1 of "06 Test
    Full Calee Solution". It never claims READY it can't back up: any
    preflight error, any fixture-reset/verify failure, or (for a
    release-gating profile) missing fixture credentials all exit BLOCKED.
    Only a real preflight pass plus (fixture credentials given and both
    reset and verify succeeding, or an explicit --skip-fixture/
    --allow-no-fixture for a suite that doesn't need the fixture) exits
    READY.

    Every outcome -- including the earliest failures (Appium won't start,
    preflight errors) -- is recorded as this run's mandatory "Test
    environment and regression fixture" component at
    reports/runs/<run-id>/environment/results.json, so a release run's
    Prepare step is always traceable even when it fails before reaching
    the fixture-reset stage.
    """
    cfg = _load_config_or_exit(config_path)
    run_id = _resolve_run_id(run_id_opt)
    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
    workspace.ensure_created()
    manifest = _load_or_init_manifest(workspace, suite_name=suite_name, tester=tester_opt)
    if fixture_base_url:
        manifest.target_backend = fixture_base_url
    manifest.write(workspace.manifest_path)
    click.echo(f"Run ID: {run_id}")

    def _finish(*, status: str, detail: "list[str]", exit_code: int, **status_kwargs) -> "None":
        _write_environment_report(workspace, status=status, detail=detail, **status_kwargs)
        manifest.record_component("environment", report_path=str(workspace.component_report_path("environment")), exit_code=exit_code)
        manifest.fixture_version = status_kwargs.get("fixture_version") or manifest.fixture_version
        manifest.write(workspace.manifest_path)
        raise SystemExit(exit_code)

    if not _ensure_appium_or_echo_blocked(cfg):
        _finish(
            status=STATUS_BLOCKED, detail=["Appium could not be started or reached."], exit_code=EXIT_BLOCKED,
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="not_run", fixture_verification_status="not_run", suite_name=suite_name,
        )

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
        _finish(
            status=STATUS_BLOCKED,
            detail=[f"{c.name}: {c.message}" for c in checks if c.status == "error"],
            exit_code=EXIT_BLOCKED,
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="not_run", fixture_verification_status="not_run", suite_name=suite_name,
        )

    suite_requires_fixture = False
    if suite_name:
        try:
            suite_requires_fixture = suites.suite_requires_fixture(suite_name)
        except suites.SuiteError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)

    if skip_fixture:
        if suite_requires_fixture:
            detail = [
                f"--skip-fixture/--allow-no-fixture was used with --suite {suite_name!r}, which depends "
                f"on the deterministic REG-* fixture (see docs/TEST_DATA_RESET_CONTRACT.md). Fixture "
                f"preparation cannot be silently skipped for this suite."
            ]
            click.echo(f"\nBLOCKED: {detail[0]}", err=True)
            _finish(
                status=STATUS_BLOCKED, detail=detail, exit_code=EXIT_BLOCKED,
                target_environment=fixture_base_url, fixture_version=None,
                fixture_reset_status="not_run", fixture_verification_status="not_run", suite_name=suite_name,
            )
        click.echo("\nFixture reset skipped (--skip-fixture/--allow-no-fixture).")
        _finish(
            status=STATUS_PASS, detail=["Fixture reset explicitly skipped for a suite that doesn't need it."],
            exit_code=EXIT_SUCCESS,
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="skipped", fixture_verification_status="skipped", suite_name=suite_name,
        )

    if not (fixture_base_url and fixture_email and fixture_password):
        detail = [
            "Fixture credentials are not configured (set CALEE_API_BASE, CALEE_TEST_EMAIL, "
            "CALEE_TEST_PASSWORD, or pass --fixture-base-url/--fixture-email/--fixture-password)."
        ]
        click.echo(
            f"\nBLOCKED: {detail[0]} Release-gating scenarios that require the deterministic "
            "fixture (e.g. the calendar suite) cannot be trusted without it. If you are "
            "deliberately running a suite that doesn't need the fixture, pass --allow-no-fixture "
            "--suite <suite-name>.",
            err=True,
        )
        _finish(
            status=STATUS_BLOCKED, detail=detail, exit_code=EXIT_BLOCKED,
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="blocked_missing_credentials", fixture_verification_status="blocked_missing_credentials",
            suite_name=suite_name,
        )

    click.echo(f"\nResetting the regression fixture at {fixture_base_url} ...")
    try:
        reset_output = run_fixture_action(
            "reset", repo_root=REPO_ROOT, base_url=fixture_base_url, email=fixture_email, password=fixture_password,
        )
    except FixtureBridgeError as exc:
        click.echo(f"\n=== Blocked: fixture reset failed: {exc} ===", err=True)
        _finish(
            status=STATUS_BLOCKED, detail=[f"Fixture reset failed: {exc}"], exit_code=EXIT_BLOCKED,
            target_environment=fixture_base_url, fixture_version=None,
            fixture_reset_status="blocked", fixture_verification_status="not_run", suite_name=suite_name,
        )
    click.echo(reset_output)

    click.echo(f"\nVerifying the regression fixture at {fixture_base_url} ...")
    try:
        verify_output = run_fixture_action(
            "verify", repo_root=REPO_ROOT, base_url=fixture_base_url, email=fixture_email, password=fixture_password,
        )
    except FixtureBridgeError as exc:
        click.echo(f"\n=== Blocked: fixture verification failed: {exc} ===", err=True)
        _finish(
            status=STATUS_BLOCKED, detail=[f"Fixture verification failed: {exc}"], exit_code=EXIT_BLOCKED,
            target_environment=fixture_base_url,
            fixture_version=_extract_fixture_version(reset_output),
            fixture_reset_status="ok", fixture_verification_status="blocked", suite_name=suite_name,
        )
    click.echo(verify_output)

    fixture_version = _extract_fixture_version(verify_output) or _extract_fixture_version(reset_output)
    click.echo(f"\nEnvironment ready. Fixture version: {fixture_version or 'unknown'}.")
    click.echo(f"Environment report: {workspace.component_report_path('environment')}")
    _finish(
        status=STATUS_PASS, detail=["Environment and fixture ready."], exit_code=EXIT_SUCCESS,
        target_environment=fixture_base_url, fixture_version=fixture_version,
        fixture_reset_status="ok", fixture_verification_status="ok", suite_name=suite_name,
    )


@main.command("record-component")
@click.option("--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True)
@click.option("--component", "component", required=True, type=click.Choice(run_context.COMPONENT_NAMES))
@click.option("--report-path", default=None, help="Path to this component's results.json, if produced")
@click.option("--exit-code", type=int, default=None, help="This component's own process exit code")
@click.option("--device-id", default=None)
@click.option("--build-version", default=None)
@click.option("--git-sha", default=None)
def record_component_cmd(run_id_opt, component, report_path, exit_code, device_id, build_version, git_sha):
    """Records one component's outcome into an existing run's manifest.

    For components not driven directly through this CLI (e.g. CaleeMobile's
    mobile-api/mobile-android/mobile-ios checks, run from
    CaleeMobile-Regression's own scripts) -- see scripts/test_caleemobile.sh.
    Requires the run workspace to already exist (created by `prepare`).
    """
    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    workspace = run_context.RunWorkspace(REPO_ROOT, run_id_opt)
    if not workspace.root.is_dir():
        click.echo(f"No run workspace found for run ID {run_id_opt!r} at {workspace.root}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    manifest = _load_or_init_manifest(workspace)
    manifest.record_component(
        component, report_path=report_path, exit_code=exit_code,
        device_id=device_id, build_version=build_version, git_sha=git_sha,
    )
    manifest.write(workspace.manifest_path)
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
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", default=None,
    help="Shared release run ID (see run_context.py). When given, results are also written to "
         "this run's workspace (reports/runs/<run-id>/manual-checks/results.json) for consolidation.",
)
def record_manual_checks(checks_path, out_path, run_id_opt):
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

    if run_id_opt:
        run_id = _resolve_run_id(run_id_opt)
        workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
        workspace.ensure_created()
        manual_checks_mod.write_results(results, workspace.component_report_path("manual-checks"), run_id=run_id)
        manifest = _load_or_init_manifest(workspace)
        manifest.record_component("manual-checks", report_path=str(workspace.component_report_path("manual-checks")))
        manifest.write(workspace.manifest_path)

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
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", default=None,
    help="Shared release run ID (see run_context.py). When given, writes into this run's "
         "workspace (reports/runs/<run-id>/tablet/results.json) instead of a standalone "
         "timestamped report directory.",
)
def run(config_path, scenario_arg, run_id_opt):
    """Run a single scenario YAML file."""
    cfg = _load_config_or_exit(config_path)
    scenario_path = _resolve_scenario_path(scenario_arg)
    out_dir, run_id = _tablet_out_dir(run_id_opt)
    rb = reporting.ReportBuilder(cfg, run_name=scenario_path.stem, out_dir=out_dir)
    result = ScenarioRunner(cfg, report_builder=rb).run_scenarios([scenario_path], suite_name=scenario_path.stem)
    if run_id:
        result.run_id = run_id
    report_dir = rb.write(result)
    _record_tablet_component(run_id, report_dir, result)
    click.echo(
        f"Passed: {result.passed_count}  Failed: {result.failed_count}  "
        f"Skipped: {result.skipped_count}  Blocked: {result.blocked_count}"
    )
    click.echo(f"Report: {report_dir}")
    raise SystemExit(_exit_code_for(result))


def _tablet_out_dir(run_id_opt: "str | None") -> "tuple[Path | None, str | None]":
    if not run_id_opt:
        return None, None
    run_id = _resolve_run_id(run_id_opt)
    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
    workspace.ensure_created()
    return workspace.component_dir("tablet"), run_id


def _record_tablet_component(run_id: "str | None", report_dir: Path, result) -> None:
    if not run_id:
        return
    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
    manifest = _load_or_init_manifest(workspace)
    manifest.record_component("tablet", report_path=str(report_dir / "results.json"), exit_code=_exit_code_for(result))
    manifest.write(workspace.manifest_path)


@main.command()
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option("--suite", "suite_name", required=True)
@click.option(
    "--confirm-technical", is_flag=True, default=False,
    help="Required (or set allow_release_technical: true in your config) to run a suite containing "
         "physical-tablet-only scenarios.",
)
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", default=None,
    help="Shared release run ID (see run_context.py). When given, writes into this run's "
         "workspace (reports/runs/<run-id>/tablet/results.json) instead of a standalone "
         "timestamped report directory.",
)
def suite(config_path, suite_name, confirm_technical, run_id_opt):
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

    out_dir, run_id = _tablet_out_dir(run_id_opt)
    rb = reporting.ReportBuilder(cfg, run_name=suite_name, out_dir=out_dir)
    result = ScenarioRunner(cfg, report_builder=rb).run_scenarios(scenario_paths, suite_name=suite_name)
    if run_id:
        result.run_id = run_id
    report_dir = rb.write(result)
    _record_tablet_component(run_id, report_dir, result)
    click.echo(
        f"Passed: {result.passed_count}  Failed: {result.failed_count}  "
        f"Skipped: {result.skipped_count}  Blocked: {result.blocked_count}"
    )
    click.echo(f"Report: {report_dir}")
    raise SystemExit(_exit_code_for(result))


@main.command("sync-smoke")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True,
    help="Shared release run ID (see run_context.py). Every sync-smoke run belongs to a workspace.",
)
@click.option("--base-url", envvar="CALEE_EXPECTED_BACKEND", default=None, help="Calee Client API base URL.")
@click.option("--email", envvar="CALEE_TEST_EMAIL", default=None)
@click.option("--password", envvar="CALEE_TEST_PASSWORD", default=None)
@click.option(
    "--platform", type=click.Choice(["android", "ios"]), default="android",
    help="Which CaleeMobile platform runs the mobile legs (sync_task_complete_test.dart / sync_chore_complete_test.dart).",
)
@click.option(
    "--task-id", default=None,
    help="REG-TASK-OPEN-001's server-assigned id, for the task flow's API-based cleanup fallback. "
         "Optional -- without it, that fallback is recorded BLOCKED instead of guessing an id.",
)
def sync_smoke_cmd(config_path, run_id_opt, base_url, email, password, platform, task_id):
    """Cross-device sync-smoke: event/task/chore flows across the API, CaleeMobile, and the tablet.

    NOT release-gating today -- see docs/TABLET_MUTATION_COVERAGE_GAPS.md.
    The event and task flows always include one genuinely BLOCKED step
    because tablet-side mutation isn't possible yet (unconfirmed resource
    ids); every other leg runs for real. Writes reports/runs/<run-id>/sync/
    results.json but is not yet auto-discovered by `consolidate` -- this is
    deliberate, matching how the Workstream 10 draft scenarios are kept out
    of a real release's mandatory components until that gap closes.
    """
    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r} (expected letters/digits/._- only).", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    run_id = run_id_opt

    if not base_url or not email or not password:
        click.echo(
            "BLOCKED: sync-smoke needs --base-url/--email/--password (or CALEE_EXPECTED_BACKEND/"
            "CALEE_TEST_EMAIL/CALEE_TEST_PASSWORD) to reach the Calee Client API and CaleeMobile.",
            err=True,
        )
        raise SystemExit(EXIT_BLOCKED)

    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
    workspace.ensure_created()
    report_dir = workspace.component_dir("sync")
    report_dir.mkdir(parents=True, exist_ok=True)

    cfg = config_mod.load_config(config_path) if config_path else None
    tablet_driver = None
    if cfg is not None:
        driver = CaleeDriver(cfg)
        try:
            driver.start_session()
            tablet_driver = driver
        except Exception as exc:
            click.echo(
                f"[WARN] Could not start a tablet Appium session ({exc}) -- tablet-leg checks in this "
                f"run will all be recorded as real failed polls, not skipped or faked.",
                err=True,
            )

    try:
        env = sync_smoke.build_real_environment(
            repo_root=REPO_ROOT, base_url=base_url, email=email, password=password, platform=platform,
            report_dir=report_dir, tablet_driver=tablet_driver,
            device_id=cfg.udid if cfg is not None else None,
        )
        results = sync_smoke.run_all_sync_flows(env, run_id=run_id, task_id=task_id)
    finally:
        if tablet_driver is not None:
            tablet_driver.quit()

    report_path = report_dir / "results.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"runId": run_id, "flows": [r.to_dict() for r in results]}, f, indent=2)
        f.write("\n")

    for result in results:
        click.echo(f"{result.flow}: {result.status.upper()}")
    click.echo(f"Report: {report_path}")

    if any(r.status == "failed" for r in results):
        overall_exit = EXIT_REGRESSION
    elif any(r.status == "blocked" for r in results):
        overall_exit = EXIT_BLOCKED
    else:
        overall_exit = EXIT_SUCCESS

    manifest = _load_or_init_manifest(workspace)
    manifest.record_component("sync", report_path=str(report_path), exit_code=overall_exit)
    manifest.write(workspace.manifest_path)

    raise SystemExit(overall_exit)


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


@main.command("build-identity")
@click.option(
    "--caleemobile-source", default=None,
    help="Path to the CaleeMobile checkout. Defaults to ../CaleeMobile next to this repo.",
)
@click.option(
    "--calee-source", default=None,
    help="Path to the Calee tablet source checkout, where available (for its Git SHA).",
)
@click.option(
    "--android-package", default=None, envvar="CALEE_TABLET_PACKAGE",
    help="Calee tablet Android application id to query via `adb dumpsys package` for the installed version.",
)
@click.option("--caleeshell-version", default=None, envvar="CALEESHELL_VERSION")
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", default=None,
    help="Shared release run ID. Required together with --phase to also write a "
         "pre/post identity snapshot into this run's workspace.",
)
@click.option(
    "--phase", type=click.Choice(["pre", "post"]), default=None,
    help="When given (with --run-id), also write this run's build-identity snapshot to "
         "reports/runs/<run-id>/identity/<phase>.json, so consolidation can prove the "
         "build was stable across the run (Phase 4).",
)
def build_identity_cmd(caleemobile_source, calee_source, android_package, caleeshell_version, run_id_opt, phase):
    """Automatically collect build identity and print it as shell assignments.

    A launcher can `eval "$(python -m calee_regression build-identity)"` and
    then prefer any value a technical owner set manually
    (`${CALEEMOBILE_BUILD_VERSION:-$AUTO_CALEEMOBILE_BUILD_VERSION}`), falling
    back to the auto-detected one. This replaces the old "only when an env var
    was manually provided" behaviour -- see Phase 3 and docs/RELEASE_POLICY.md.

    With --run-id and --phase (pre|post) it ALSO writes the collected identity
    to reports/runs/<run-id>/identity/<phase>.json. The full launcher collects
    a `pre` snapshot before testing and a `post` snapshot after, and
    consolidation BLOCKS when an in-scope app's identity changed between them
    (Phase 4). Writing the snapshot never changes the shell output.

    Never fails the run: an unreachable device/adb or a missing checkout just
    yields AUTO_*_IDENTITY_AVAILABLE=false, which the consolidator turns into
    BLOCKED when that app is in scope (never a fabricated pass).
    """
    cm_source = Path(caleemobile_source) if caleemobile_source else (REPO_ROOT.parent / "CaleeMobile")
    caleemobile = build_identity_mod.collect_caleemobile_identity(cm_source)
    tablet = build_identity_mod.collect_calee_tablet_identity(
        source_dir=calee_source, android_package=android_package, caleeshell_version=caleeshell_version,
    )
    click.echo(caleemobile.to_shell("CALEEMOBILE"))
    click.echo(tablet.to_shell("CALEE"))

    if phase and run_id_opt:
        if not run_context.is_valid_run_id(run_id_opt):
            click.echo(f"Invalid --run-id {run_id_opt!r}.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        workspace = run_context.RunWorkspace(REPO_ROOT, run_id_opt)
        identity_dir = workspace.root / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "runId": run_id_opt,
            "phase": phase,
            "capturedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "caleemobile": caleemobile.to_dict(),
            "tablet": tablet.to_dict(),
        }
        snapshot_path = identity_dir / f"{phase}.json"
        snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
        click.echo(f"[identity] wrote {phase}-run snapshot: {snapshot_path}", err=True)
    elif phase and not run_id_opt:
        click.echo("--phase also needs --run-id (or CALEE_RUN_ID) to write an identity snapshot.", err=True)

    raise SystemExit(EXIT_SUCCESS)


def _manual_checks_from_list(raw: list) -> "list[ManualCheck]":
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


def _load_manual_checks(path: "str | None") -> "list[ManualCheck] | None":
    """Legacy/ad-hoc entry point: loads a bare JSON list with no run-ID
    validation. Run-scoped consolidation uses _resolve_component instead
    (see consolidate below), which also accepts the {"runId":...,
    "checks":[...]} shape manual_checks.write_results produces when given
    a run_id."""
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        raw = raw.get("checks", [])
    return _manual_checks_from_list(raw)


def _load_identity_snapshot(workspace: run_context.RunWorkspace, phase: str) -> "dict | None":
    """Load this run's pre/post build-identity snapshot
    (reports/runs/<run-id>/identity/<phase>.json), or None if it was never
    written or is unreadable. Written by `build-identity --phase pre|post`;
    consumed by component_from_identity_stability (Phase 4)."""
    path = workspace.root / "identity" / f"{phase}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class _ConsolidationProblem(NamedTuple):
    component: str
    message: str


def _resolve_component(
    component: str,
    explicit_path: "str | None",
    *,
    workspace: run_context.RunWorkspace,
    run_id: str,
    run_started_at_epoch: "float | None",
    problems: "list[_ConsolidationProblem]",
) -> "dict | None":
    """Resolves one component's report: an explicit --foo-report path if
    given, else the fixed workspace location. Missing is "not executed"
    (not a problem -- component_from_* already renders that as blocked for
    a mandatory component). A file that exists but fails run-ID/workspace/
    freshness validation is recorded in `problems` and treated as if it
    were never produced -- a report that can't be trusted must never be
    silently used, but the CLI still finishes and reports every problem it
    found rather than crashing on the first one.
    """
    path = Path(explicit_path) if explicit_path else workspace.component_report_path(component)
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(_ConsolidationProblem(component, f"could not read {path}: {exc}"))
        return None
    try:
        run_context.validate_component_report(
            report, report_path=path, run_id=run_id, workspace=workspace,
            component=component, run_started_at_epoch=run_started_at_epoch,
        )
    except run_context.RunIdError as exc:
        problems.append(_ConsolidationProblem(component, str(exc)))
        return None
    return report


@main.command()
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True,
    help="Shared release run ID for this consolidation (see run_context.py). Every component "
         "report must carry this same run ID -- consolidation refuses to guess.",
)
@click.option("--tablet-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's tablet/results.json")
@click.option("--mobile-api-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's mobile-api/results.json")
@click.option("--mobile-android-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's mobile-android/results.json")
@click.option("--mobile-ios-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's mobile-ios/results.json")
@click.option("--manual-checks", "manual_checks_path", type=click.Path(exists=True), default=None, help="Override: defaults to this run's manual-checks/results.json")
@click.option(
    "--environment-report", type=click.Path(exists=True), default=None,
    help="Override: defaults to this run's environment/results.json (prepare's output).",
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
@click.option("--calee-git-sha", default=None, help="Calee tablet commit SHA under test")
@click.option("--expected-calee-git-sha", default=None, help="Technical-owner-configured expected Calee tablet commit; mismatch BLOCKS")
@click.option("--caleemobile-git-sha", default=None, help="CaleeMobile commit SHA under test")
@click.option("--expected-caleemobile-git-sha", default=None, help="Technical-owner-configured expected CaleeMobile commit; mismatch BLOCKS")
@click.option("--calee-version-code", default=None, help="Calee tablet installed versionCode (from adb), where available")
@click.option("--calee-application-id", default=None, help="Calee tablet Android application id, where available")
@click.option("--caleemobile-dirty/--caleemobile-clean", "caleemobile_dirty", default=False, help="CaleeMobile build has uncommitted changes (BLOCKS unless --allow-dirty)")
@click.option("--calee-dirty/--calee-clean", "calee_dirty", default=False, help="Calee tablet build has uncommitted changes (BLOCKS unless --allow-dirty)")
@click.option("--caleemobile-identity-available/--caleemobile-identity-unavailable", "caleemobile_identity_available", default=True, help="Whether CaleeMobile build identity could be determined")
@click.option("--calee-identity-available/--calee-identity-unavailable", "calee_identity_available", default=True, help="Whether Calee tablet build identity could be determined")
@click.option("--require-build-identity/--allow-unknown-build-identity", "require_build_identity", default=True, help="Whether an in-scope app's build identity must be known (default: required -- a PASS must prove which build was tested)")
@click.option("--allow-dirty/--no-allow-dirty", "allow_dirty_opt", default=None, help="Explicitly approve testing an uncommitted build. Defaults to config/release-platforms.yaml (expected_build_identity.allow_dirty).")
@click.option("--android-device-id", default=None, help="Android device/emulator identifier used for the Android UI run")
@click.option("--ios-device-id", default=None, help="iOS device/simulator identifier used for the iOS UI run")
@click.option("--test-environment", default="", help="Target environment URL")
@click.option("--tester", default="", help="Tester name/identifier")
@click.option("--out-dir", type=click.Path(), default=None, help="Where to write the consolidated bundle (default: this run's workspace)")
def consolidate(
    run_id_opt, tablet_report, mobile_api_report, mobile_android_report, mobile_ios_report,
    manual_checks_path, environment_report, android_mandatory, ios_mandatory,
    build_version, calee_build_version, expected_calee_build_version,
    caleemobile_build_version, expected_caleemobile_build_version, caleeshell_version,
    calee_git_sha, expected_calee_git_sha, caleemobile_git_sha, expected_caleemobile_git_sha,
    calee_version_code, calee_application_id, caleemobile_dirty, calee_dirty,
    caleemobile_identity_available, calee_identity_available, require_build_identity, allow_dirty_opt,
    android_device_id, ios_device_id,
    test_environment, tester, out_dir,
):
    """Combine this run's per-component JSON reports into one consolidated
    release report.

    Reads already-produced report files -- it does not run anything
    itself. Every report (explicit --foo-report or auto-discovered from
    the run workspace) is validated against --run-id before it's trusted:
    a missing run ID, a mismatched run ID, a path outside this run's
    workspace, or a report that predates this run's start are all treated
    as if that component was never executed (and reported as a specific
    problem), never silently accepted. Any component with no valid report
    at all is "not executed", which blocks an overall PASS for any
    component that is mandatory for this release (see
    docs/RELEASE_POLICY.md and config/release-platforms.example.yaml).
    """
    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r} (expected letters/digits/._- only).", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    run_id = run_id_opt
    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
    if not workspace.root.is_dir():
        click.echo(
            f"No run workspace found for run ID {run_id!r} at {workspace.root}. "
            f"Run 'prepare --run-id {run_id}' first to create it.",
            err=True,
        )
        raise SystemExit(EXIT_INVALID_CONFIG)

    manifest = _load_or_init_manifest(workspace)
    run_started_at_epoch = None
    if manifest.started_at:
        try:
            run_started_at_epoch = time.mktime(time.strptime(manifest.started_at, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            run_started_at_epoch = None

    problems: "list[_ConsolidationProblem]" = []
    resolve = lambda component, explicit: _resolve_component(  # noqa: E731
        component, explicit, workspace=workspace, run_id=run_id,
        run_started_at_epoch=run_started_at_epoch, problems=problems,
    )
    env_report = resolve("environment", environment_report)
    tablet = resolve("tablet", tablet_report)
    mobile_api = resolve("mobile-api", mobile_api_report)
    mobile_android = resolve("mobile-android", mobile_android_report)
    mobile_ios = resolve("mobile-ios", mobile_ios_report)

    manual_checks_raw = resolve("manual-checks", manual_checks_path)
    manual_checks_list = None
    if manual_checks_raw is not None:
        manual_checks_list = _manual_checks_from_list(manual_checks_raw.get("checks", []))
    elif manual_checks_path:
        # An explicit --manual-checks path outside a run workspace (legacy/
        # ad-hoc use, e.g. from existing tests) has no run ID to validate --
        # still usable, just not run-scoped.
        manual_checks_list = _load_manual_checks(manual_checks_path)

    for problem in problems:
        click.echo(f"BLOCKED: {problem.component} report rejected: {problem.message}", err=True)

    meta = {
        "runId": run_id,
        "buildVersion": build_version,
        "caleeBuildVersion": calee_build_version,
        "caleeMobileBuildVersion": caleemobile_build_version,
        "caleeShellVersion": caleeshell_version,
        "caleeGitSha": calee_git_sha,
        "caleeMobileGitSha": caleemobile_git_sha,
        "caleeVersionCode": calee_version_code,
        "caleeApplicationId": calee_application_id,
        "androidDeviceId": android_device_id,
        "iosDeviceId": ios_device_id,
        "testEnvironment": test_environment,
        "tester": tester or manifest.tester,
    }
    if env_report:
        meta["fixtureVersion"] = env_report.get("fixtureVersion")
        meta["fixtureTargetEnvironment"] = env_report.get("targetEnvironment")
        meta["fixtureResetStatus"] = env_report.get("fixtureResetStatus")
        meta["fixtureVerificationStatus"] = env_report.get("fixtureVerificationStatus")
    meta = {k: v for k, v in meta.items() if v not in (None, "")}

    try:
        platforms = release_platforms.load_release_platforms()
        expected_identity = release_platforms.load_expected_build_identity()
    except release_platforms.ReleasePlatformsError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    android_gating = android_mandatory if android_mandatory is not None else platforms.mobile_android
    ios_gating = ios_mandatory if ios_mandatory is not None else platforms.mobile_ios

    # Expected build identity: an explicit CLI flag wins over the release
    # profile (config/release-platforms.yaml's expected_build_identity).
    eff_expected_calee_build = expected_calee_build_version or expected_identity.calee_build_version
    eff_expected_calee_sha = expected_calee_git_sha or expected_identity.calee_git_sha
    eff_expected_caleemobile_build = expected_caleemobile_build_version or expected_identity.caleemobile_build_version
    eff_expected_caleemobile_sha = expected_caleemobile_git_sha or expected_identity.caleemobile_git_sha
    allow_dirty = allow_dirty_opt if allow_dirty_opt is not None else expected_identity.allow_dirty

    # An app's identity is required (mandatory-to-know) only when that app is
    # in this release's scope: the tablet when the tablet is gating, CaleeMobile
    # when at least one mobile platform is gating. A PASS then may never leave
    # an in-scope build's identity unknown (Phase 3).
    require_calee_identity = require_build_identity and platforms.tablet
    require_caleemobile_identity = require_build_identity and (android_gating or ios_gating)

    # The manifest's worst-wins effective exit codes for the mobile components
    # are passed as floors: a component's report that reads better than the
    # worst result recorded for it during the run (e.g. a later platform run
    # overwrote an earlier FAIL) is downgraded back to the recorded result.
    # See build_release_report / _apply_exit_floor and Phase 3.
    mobile_exit_floors = {
        key: manifest.effective_exit_code(key)
        for key in ("mobile-api", "mobile-android", "mobile-ios")
    }

    # Pre/post build-identity stability (Phase 4): a snapshot captured before
    # testing and one captured after. An in-scope app whose identity changed
    # between them BLOCKS -- what was tested is not what is being certified.
    # Absent snapshots (legacy/ad-hoc consolidation) add no component; the full
    # launcher always captures both.
    identity_pre = _load_identity_snapshot(workspace, "pre")
    identity_post = _load_identity_snapshot(workspace, "post")
    identity_stability = component_from_identity_stability(
        identity_pre, identity_post,
        require_caleemobile=require_caleemobile_identity,
        require_calee=require_calee_identity,
    )
    identity_stability_components = [identity_stability] if identity_stability is not None else []

    report = build_release_report(
        environment=env_report,
        tablet=tablet,
        mobile_api=mobile_api,
        mobile_android_ui=mobile_android,
        mobile_ios_ui=mobile_ios,
        manual_checks=manual_checks_list,
        meta=meta,
        android_mandatory=android_gating,
        ios_mandatory=ios_gating,
        calee_build_version=calee_build_version,
        expected_calee_build_version=eff_expected_calee_build,
        caleemobile_build_version=caleemobile_build_version,
        expected_caleemobile_build_version=eff_expected_caleemobile_build,
        calee_git_sha=calee_git_sha,
        expected_calee_git_sha=eff_expected_calee_sha,
        caleemobile_git_sha=caleemobile_git_sha,
        expected_caleemobile_git_sha=eff_expected_caleemobile_sha,
        calee_dirty=calee_dirty,
        caleemobile_dirty=caleemobile_dirty,
        calee_identity_available=calee_identity_available,
        caleemobile_identity_available=caleemobile_identity_available,
        calee_version_code=calee_version_code,
        calee_application_id=calee_application_id,
        caleeshell_version=caleeshell_version,
        require_calee_identity=require_calee_identity,
        require_caleemobile_identity=require_caleemobile_identity,
        allow_dirty=allow_dirty,
        mobile_exit_floors=mobile_exit_floors,
        extra_components=identity_stability_components,
    )

    out = Path(out_dir) if out_dir else workspace.consolidated_dir
    bundle_path = write_release_bundle(report, out, build_label=build_version)

    click.echo(f"Run ID: {run_id}")
    click.echo(f"Overall: {report.overall_status.upper()}")
    for component in report.components:
        marker = "" if component.mandatory else " (optional)"
        click.echo(f"  {component.name}{marker}: {component.status.upper()}")
    click.echo(f"Suggested next action: {report.summary.get('suggestedNextAction', '')}")
    click.echo(f"Bundle: {bundle_path}")

    exit_code = _STATUS_TO_EXIT_CODE[report.overall_status]
    manifest.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest.record_component("consolidated", report_path=str(bundle_path), exit_code=exit_code)
    if calee_build_version:
        manifest.build_versions["calee"] = calee_build_version
    if caleemobile_build_version:
        manifest.build_versions["caleemobile"] = caleemobile_build_version
    if calee_git_sha:
        manifest.git_shas["calee"] = calee_git_sha
    if caleemobile_git_sha:
        manifest.git_shas["caleemobile"] = caleemobile_git_sha
    if android_device_id:
        manifest.device_ids["android"] = android_device_id
    if ios_device_id:
        manifest.device_ids["ios"] = ios_device_id
    if test_environment:
        manifest.target_backend = test_environment
    if meta.get("fixtureVersion"):
        manifest.fixture_version = meta["fixtureVersion"]
    manifest.write(workspace.manifest_path)

    if exit_code == EXIT_SUCCESS or out_dir is None:
        # A convenience pointer to the most recent run, created only now
        # that the run has actually finished -- never a consolidation
        # input (see run_context.py). Refreshed on every consolidate call
        # (not just PASS) so "open latest report" also works for a
        # FAIL/BLOCKED run the tester needs to inspect.
        latest_link = REPO_ROOT / "reports" / "latest-run"
        try:
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()
            latest_link.symlink_to(Path("runs") / run_id, target_is_directory=True)
        except OSError:
            pass  # Best-effort convenience link; never fail the run over it.

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
