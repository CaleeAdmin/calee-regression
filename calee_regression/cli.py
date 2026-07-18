from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
from . import selector_evidence as selector_evidence_mod
from . import selector_provenance as selector_provenance_mod
from . import sync_smoke
from . import toolchain_verify as toolchain_verify_mod
from .appium_driver import CaleeDriver
from .consolidated_report import (
    STATUS_BLOCKED,
    STATUS_PASS,
    ManualCheck,
    build_release_report,
    component_from_caleemobile_sha_agreement,
    component_from_identity_stability,
    component_from_release_intent,
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
        features = release_platforms.load_release_features()
        profile = {
            "tablet": platforms.tablet,
            "mobile_android": platforms.mobile_android,
            "mobile_ios": platforms.mobile_ios,
            "synchronization": features.synchronization,
            "meals": features.meals,
            "onboarding": features.onboarding,
            "google_calendar": features.google_calendar,
            "kiosk_admin": features.kiosk_admin,
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


def _verified_backend_from_environment(
    workspace: run_context.RunWorkspace, run_id: str
) -> "str | None":
    """This run's prepared-and-verified backend, read from
    reports/runs/<run-id>/environment/results.json (prepare's output), or None.

    Only a backend the regression fixture was actually verified against
    (``fixtureVerificationStatus == "ok"``) for THIS run id is returned -- sync
    must talk to the SAME verified backend the rest of the release run did, not
    an arbitrary or unverified one (Workstream 1). Read-only; never creates the
    workspace, so the credential/backend guard below can still fire before
    anything is written.
    """
    path = workspace.component_report_path("environment")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if run_context.extract_report_run_id(data) != run_id:
        return None
    if data.get("fixtureVerificationStatus") != "ok":
        return None
    backend = data.get("targetEnvironment")
    return backend or None


def _write_sync_marker(
    workspace: run_context.RunWorkspace, run_id: str, *, status: str, mandatory: bool, detail: "list[str]"
) -> Path:
    """Write an explicit sync marker report (no flows) + record the component.

    Used when the flows are not run: an intentionally excluded (optional)
    release, or no in-scope mobile platform / verified backend for a mandatory
    one. The marker keeps sync from being silently omitted from consolidation --
    it appears as an explicit optional/blocked component. See
    consolidated_report.component_from_sync_report.
    """
    workspace.ensure_created()
    report_dir = workspace.component_dir("sync")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "results.json"
    payload = {"runId": run_id, "mandatory": mandatory, "status": status, "flows": [], "detail": detail}
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    exit_code = {
        STATUS_PASS: EXIT_SUCCESS,
        "not_run": EXIT_SUCCESS,
        "fail": EXIT_REGRESSION,
    }.get(status, EXIT_BLOCKED)
    manifest = _load_or_init_manifest(workspace)
    manifest.record_component("sync", report_path=str(report_path), exit_code=exit_code)
    manifest.write(workspace.manifest_path)
    return report_path


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
    "--platform", type=click.Choice(["android", "ios", "none"]), default="android",
    help="Which CaleeMobile platform runs the mobile legs (sync_task_complete_test.dart / "
         "sync_chore_complete_test.dart). 'none' means no in-scope mobile platform is available -- "
         "a mandatory sync then BLOCKS (it needs a mobile surface to verify against).",
)
@click.option(
    "--mandatory/--optional", "mandatory_opt", default=None,
    help="Whether cross-device synchronization is release-gating for this run. Defaults to "
         "config/release-platforms.yaml (release_features.synchronization), or True if absent. "
         "An excluded (optional) sync is recorded as an explicit optional component, never run.",
)
@click.option(
    "--task-id", default=None,
    help="REG-TASK-OPEN-001's server-assigned id, for the task flow's API-based cleanup fallback. "
         "Optional -- without it, that fallback is recorded BLOCKED instead of guessing an id.",
)
def sync_smoke_cmd(config_path, run_id_opt, base_url, email, password, platform, mandatory_opt, task_id):
    """Cross-device sync-smoke: event/task/chore flows across the API, CaleeMobile, and the tablet.

    Release-gating (Workstream 1): for a full Calee solution release
    synchronization defaults to mandatory, is invoked by the full launcher
    after the mobile UI legs and before manual checks, reuses this run's
    verified backend + fixture + credentials and the same CALEE_RUN_ID, and
    writes reports/runs/<run-id>/sync/results.json which `consolidate`
    auto-discovers and gates on.

    The event and task flows still include one genuinely BLOCKED step because
    tablet-side mutation isn't possible yet (unconfirmed resource ids -- see
    docs/TABLET_MUTATION_COVERAGE_GAPS.md); every other leg runs for real. That
    BLOCKED step means a mandatory sync currently BLOCKS the release (never a
    false PASS) until the tablet-mutation gap closes and a real device verifies
    it -- which is the intended safety property, not a silent non-gate.
    """
    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r} (expected letters/digits/._- only).", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    run_id = run_id_opt

    # Mandatory-ness: explicit flag wins, else the release feature profile
    # (release_features.synchronization), which defaults to True when no config
    # file is present -- an omitted feature must never silently become optional.
    if mandatory_opt is None:
        try:
            mandatory = release_platforms.load_release_features().synchronization
        except release_platforms.ReleasePlatformsError:
            mandatory = True
    else:
        mandatory = mandatory_opt

    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)

    # Reuse this run's verified backend (from prepare's environment report) when
    # one wasn't passed explicitly -- proving sync talks to the SAME
    # fixture-verified backend as the rest of the run.
    if not base_url:
        base_url = _verified_backend_from_environment(workspace, run_id)

    # Excluded (optional) sync: record an explicit optional marker so it is
    # never silently omitted from consolidation, and do not run the flows.
    if not mandatory:
        _write_sync_marker(
            workspace, run_id, status="not_run", mandatory=False,
            detail=[
                "Cross-device synchronization is optional for this release "
                "(release_features.synchronization=false) and was not run."
            ],
        )
        click.echo("Cross-device synchronization is OPTIONAL for this release — recorded as optional, not run.")
        raise SystemExit(EXIT_SUCCESS)

    # No in-scope CaleeMobile platform to drive the sync mobile legs: a
    # mandatory sync BLOCKS (it has no mobile surface to verify against).
    if platform == "none":
        _write_sync_marker(
            workspace, run_id, status=STATUS_BLOCKED, mandatory=True,
            detail=[
                "No in-scope CaleeMobile platform (Android/iOS) available to run the synchronization "
                "mobile legs -- cannot verify cross-device sync for this release."
            ],
        )
        click.echo("BLOCKED: no in-scope CaleeMobile platform for cross-device synchronization.", err=True)
        raise SystemExit(EXIT_BLOCKED)

    if not base_url or not email or not password:
        click.echo(
            "BLOCKED: sync-smoke needs --base-url/--email/--password (or CALEE_EXPECTED_BACKEND/"
            "CALEE_TEST_EMAIL/CALEE_TEST_PASSWORD) to reach the Calee Client API and CaleeMobile.",
            err=True,
        )
        raise SystemExit(EXIT_BLOCKED)

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
        json.dump(
            {"runId": run_id, "mandatory": True, "flows": [r.to_dict() for r in results]}, f, indent=2
        )
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


# ── Kiosk/admin physical suite gating (Workstream 4) ──────────────────────────

KIOSK_COMPONENT = "kiosk-admin"
KIOSK_FEATURE = "kiosk_admin"


def _write_kiosk_marker(
    workspace: run_context.RunWorkspace, run_id: str, *, status: str, mandatory: bool,
    steps: "list[dict]", detail: "list[str]",
    caleeshell_version: "str | None" = None, tablet: "dict | None" = None,
) -> "tuple[Path, int]":
    """Write the kiosk-admin component report + record it (Workstream 4).

    The report carries feature-tagged steps (feature="kiosk_admin") so the
    consolidator's independent kiosk/admin feature component (Workstream 3) reads
    it exactly like the mobile UI reports. A mandatory kiosk/admin that could not
    run the real physical PIN/escape suite is BLOCKED here -- never a PASS from
    the insufficient find.text("Admin") probe."""
    workspace.ensure_created()
    report_dir = workspace.component_dir(KIOSK_COMPONENT)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "results.json"
    payload = {
        "runId": run_id,
        "mandatory": mandatory,
        "status": status,
        "feature": KIOSK_FEATURE,
        "caleeShellVersion": caleeshell_version,
        "tablet": tablet or {},
        "steps": steps,
        "detail": detail,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    exit_code = {STATUS_PASS: EXIT_SUCCESS, "not_run": EXIT_SUCCESS, "fail": EXIT_REGRESSION}.get(status, EXIT_BLOCKED)
    manifest = _load_or_init_manifest(workspace)
    manifest.record_component(KIOSK_COMPONENT, report_path=str(report_path), exit_code=exit_code)
    manifest.write(workspace.manifest_path)
    return report_path, exit_code


def _detect_disposable_tablet(serial: "str | None") -> "dict | None":
    """Best-effort, NON-DESTRUCTIVE adb detection of a connected disposable
    tablet and its device-owner/admin state. Returns a dict of identity/state or
    None when adb is unavailable, no device is connected, or the match is
    ambiguous. Never issues a device-owner/factory-reset/wipe command -- only
    read-only `adb devices` / `dumpsys device_policy` / `getprop` calls."""
    adb = shutil.which("adb")
    if adb is None:
        return None
    try:
        listed = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    serials = [
        line.split("\t")[0]
        for line in listed.stdout.splitlines()[1:]
        if "\tdevice" in line
    ]
    if serial:
        if serial not in serials:
            return None
        chosen = serial
    elif len(serials) == 1:
        chosen = serials[0]
    else:
        # Zero or ambiguous (>1) -- never guess which tablet is the disposable one.
        return None

    def _shell(*args: str) -> "str | None":
        try:
            r = subprocess.run([adb, "-s", chosen, "shell", *args], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None

    device_policy = _shell("dumpsys", "device_policy") or ""
    return {
        "serial": chosen,
        "model": _shell("getprop", "ro.product.model"),
        "androidRelease": _shell("getprop", "ro.build.version.release"),
        # Read-only device-owner/admin snapshot for the record (truncated).
        "deviceOwnerState": device_policy[:2000],
        "hasDeviceOwner": "Device Owner:" in device_policy,
    }


@main.command("kiosk-admin")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True,
    help="Shared release run ID (see run_context.py).",
)
@click.option(
    "--mandatory/--optional", "mandatory_opt", default=None,
    help="Whether CaleeShell kiosk/admin is release-gating for this run. Defaults to "
         "config/release-platforms.yaml (release_features.kiosk_admin), or True if absent. "
         "An excluded (optional) kiosk/admin is recorded as an explicit optional component, never run.",
)
@click.option(
    "--confirm-technical", is_flag=True, default=False,
    help="Required to run the physical kiosk/admin suite (it drives a real, disposable, "
         "device-owner-provisioned tablet). Without it -- or allow_release_technical in the config -- "
         "a mandatory kiosk/admin BLOCKS.",
)
@click.option("--tablet-serial", envvar="CALEE_KIOSK_TABLET_SERIAL", default=None,
              help="adb serial of the disposable kiosk tablet. Auto-detected when exactly one device is connected.")
@click.option("--caleeshell-version", envvar="CALEESHELL_VERSION", default=None,
              help="CaleeShell version installed on the kiosk tablet, recorded in the evidence.")
def kiosk_admin_cmd(config_path, run_id_opt, mandatory_opt, confirm_technical, tablet_serial, caleeshell_version):
    """CaleeShell kiosk/admin physical-suite gating (Workstream 4).

    When kiosk/admin is mandatory for this release, a real result requires the
    physical kiosk suite on a disposable, device-owner tablet: the real
    admin-entry gesture + PIN flow (incorrect and correct PIN), return-to-kiosk,
    and Home/Back/Recents/notification-shade/Android-Settings escape attempts,
    plus a record of the device-owner/admin state, CaleeShell version and tablet
    identity. Until that confirmed physical suite exists and runs, a mandatory
    kiosk/admin BLOCKS with the specific unmet prerequisite -- it never PASSes
    from the insufficient optional find.text("Admin") probe. No destructive
    device-owner or factory-reset operations are ever issued.
    """
    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r} (expected letters/digits/._- only).", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    run_id = run_id_opt

    if mandatory_opt is None:
        try:
            mandatory = release_platforms.load_release_features().kiosk_admin
        except release_platforms.ReleasePlatformsError:
            mandatory = True
    else:
        mandatory = mandatory_opt

    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
    step_name = "CaleeShell kiosk/admin physical suite"

    # Excluded (optional): record an explicit optional not-run marker; never run.
    if not mandatory:
        _write_kiosk_marker(
            workspace, run_id, status="not_run", mandatory=False,
            steps=[{
                "name": step_name, "status": "SKIP", "mandatory": False,
                "skipCategory": "optional_feature", "feature": KIOSK_FEATURE,
                "detail": "kiosk/admin is optional for this release "
                          "(release_features.kiosk_admin=false) and was not run.",
            }],
            detail=["kiosk/admin is optional for this release and was not run."],
            caleeshell_version=caleeshell_version,
        )
        click.echo("Kiosk/admin is OPTIONAL for this release — recorded as optional, not run.")
        raise SystemExit(EXIT_SUCCESS)

    # Mandatory. Require the destructive-physical confirmation first.
    confirmed = confirm_technical
    if config_path and not confirmed:
        try:
            confirmed = bool(getattr(config_mod.load_config(config_path), "allow_release_technical", False))
        except Exception:  # noqa: BLE001 - a broken config is "not confirmed", still BLOCKS below
            confirmed = False

    def _block(reason: str) -> None:
        _write_kiosk_marker(
            workspace, run_id, status=STATUS_BLOCKED, mandatory=True,
            steps=[{
                "name": step_name, "status": "BLOCKED", "mandatory": True,
                "skipCategory": None, "feature": KIOSK_FEATURE, "detail": reason,
            }],
            detail=[reason], caleeshell_version=caleeshell_version, tablet=tablet,
        )
        click.echo(f"BLOCKED: kiosk/admin — {reason}", err=True)
        raise SystemExit(EXIT_BLOCKED)

    tablet = None
    if not confirmed:
        _block(
            "kiosk/admin is mandatory for this release, but the physical kiosk suite was not "
            "confirmed. Re-run with --confirm-technical (or set allow_release_technical in the "
            "config) on a disposable, device-owner tablet you are willing to have driven."
        )

    tablet = _detect_disposable_tablet(tablet_serial)
    if tablet is None:
        _block(
            "kiosk/admin is mandatory for this release, but no suitable disposable physical tablet "
            "is connected (adb detected zero or an ambiguous number of devices, or adb is "
            "unavailable). Connect exactly one disposable kiosk tablet, or pass --tablet-serial."
        )

    # Confirmed + a disposable tablet is present, but the real admin-entry
    # gesture + PIN + escape-attempt suite is not yet implemented with CONFIRMED
    # selectors (the existing find.text("Admin") probe is an optional scaffold
    # and is explicitly insufficient -- Workstream 4/5). So a mandatory
    # kiosk/admin still BLOCKS, now with the tablet identity + device-owner state
    # recorded as evidence, rather than PASSing on an insufficient probe.
    _block(
        "kiosk/admin is mandatory and a disposable tablet is connected, but the real physical "
        "kiosk suite (admin-entry gesture, incorrect+correct PIN, return-to-kiosk, and "
        "Home/Back/Recents/notification-shade/Android-Settings escape attempts) is not yet "
        "implemented with confirmed CaleeShell selectors. The optional find.text(\"Admin\") probe "
        "is insufficient and must not produce a kiosk/admin PASS (Workstream 4/5)."
    )


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

    Two flavours of the same single source of truth are emitted:

      * Plain ``RELEASE_PLATFORM_*`` / ``RELEASE_FEATURE_*`` shell variables --
        for the launcher's own branching (which platform/feature legs to run,
        which mandatory/optional flags to pass to `consolidate`).
      * Exported ``CALEE_RELEASE_FEATURE_*`` environment variables (Workstream 1)
        -- so the feature scope PROPAGATES to every child process the launcher
        spawns (scripts/test_caleemobile.sh -> run_ui_suite.py -> the Dart
        integration-test process) without each of them re-parsing the YAML.
        The values come from the same parsed config/release-platforms.yaml the
        consolidator uses, never a second bash/ad-hoc parse.
    """
    try:
        platforms = release_platforms.load_release_platforms()
        features = release_platforms.load_release_features()
    except release_platforms.ReleasePlatformsError as exc:
        click.echo(f"echo '{exc}' >&2; exit {EXIT_INVALID_CONFIG}")
        raise SystemExit(EXIT_INVALID_CONFIG)
    click.echo(f"RELEASE_PLATFORM_TABLET={'true' if platforms.tablet else 'false'}")
    click.echo(f"RELEASE_PLATFORM_ANDROID={'true' if platforms.mobile_android else 'false'}")
    click.echo(f"RELEASE_PLATFORM_IOS={'true' if platforms.mobile_ios else 'false'}")
    # Feature scope (Workstream 2). A full-solution launcher branches on
    # $RELEASE_FEATURE_SYNCHRONIZATION (and the others) to decide whether that
    # feature's leg is mandatory this release, the same way it branches on the
    # platform flags above -- without parsing YAML in bash.
    click.echo(f"RELEASE_FEATURE_SYNCHRONIZATION={'true' if features.synchronization else 'false'}")
    click.echo(f"RELEASE_FEATURE_MEALS={'true' if features.meals else 'false'}")
    click.echo(f"RELEASE_FEATURE_ONBOARDING={'true' if features.onboarding else 'false'}")
    click.echo(f"RELEASE_FEATURE_GOOGLE_CALENDAR={'true' if features.google_calendar else 'false'}")
    click.echo(f"RELEASE_FEATURE_KIOSK_ADMIN={'true' if features.kiosk_admin else 'false'}")
    # Exported CALEE_RELEASE_FEATURE_* (Workstream 1): the feature scope that
    # propagates down to the mobile/tablet test processes. `export` (not a bare
    # assignment) so a child `bash scripts/test_caleemobile.sh` and, in turn,
    # run_ui_suite.py and the Dart process all inherit it. Consolidation reads
    # the same feature profile directly from the YAML, so the report and the
    # executed scope can never disagree about which features were in scope.
    click.echo(f"export CALEE_RELEASE_FEATURE_SYNCHRONIZATION={'true' if features.synchronization else 'false'}")
    click.echo(f"export CALEE_RELEASE_FEATURE_MEALS={'true' if features.meals else 'false'}")
    click.echo(f"export CALEE_RELEASE_FEATURE_ONBOARDING={'true' if features.onboarding else 'false'}")
    click.echo(f"export CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR={'true' if features.google_calendar else 'false'}")
    click.echo(f"export CALEE_RELEASE_FEATURE_KIOSK_ADMIN={'true' if features.kiosk_admin else 'false'}")
    raise SystemExit(EXIT_SUCCESS)


@main.command("verify-selector-evidence")
@click.option(
    "--evidence", "evidence_path", required=True,
    help="Path to a CaleeMobile-Regression selector-contract result JSON "
         "(see selector_evidence.py / config/release-platforms.example.yaml).",
)
@click.option(
    "--expected-sha", "expected_sha", default=None,
    help="Expected full CaleeMobile release Git SHA. Defaults to the "
         "caleemobile_git_sha in config/release-platforms.yaml when set.",
)
@click.option(
    "--expected-version", "expected_version", default=None,
    help="Expected CaleeMobile release version (pubspec version+build). Defaults "
         "to the caleemobile_build_version in config/release-platforms.yaml when set.",
)
@click.option(
    "--expected-ref", "expected_ref", default=None,
    help="Expected CaleeMobile ref (non-blocking note only; SHA/version are authoritative).",
)
def verify_selector_evidence_cmd(evidence_path, expected_sha, expected_version, expected_ref):
    """Reject CaleeMobile selector-contract evidence gathered against a
    DIFFERENT build than the one being released (Workstream 1).

    Selectors passing for commit X are not evidence about commit Y. This reads
    the machine-readable selector-contract result and BLOCKS (exit 3) when the
    contract didn't PASS, the tested SHA/version is missing/malformed, or the
    tested SHA/version differs from the expected CaleeMobile release identity.
    When --expected-sha/--expected-version are omitted they fall back to the
    expected identity in config/release-platforms.yaml, so a release run
    verifies against the same intended target the consolidator does.
    """
    if expected_sha is None or expected_version is None:
        try:
            configured = release_platforms.load_expected_build_identity()
        except release_platforms.ReleasePlatformsError as exc:
            click.echo(f"Invalid release-platforms config: {exc}", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        if expected_sha is None:
            expected_sha = configured.caleemobile_git_sha
        if expected_version is None:
            expected_version = configured.caleemobile_build_version

    try:
        result = selector_evidence_mod.load_selector_contract_result(evidence_path)
    except selector_evidence_mod.SelectorEvidenceError as exc:
        # A missing/malformed evidence file is a framework/pipeline fault, not a
        # product regression -- BLOCKED, never a fabricated pass.
        click.echo(f"Selector-contract evidence could not be read: {exc}", err=True)
        raise SystemExit(EXIT_BLOCKED)

    verdict = selector_evidence_mod.verify_selector_contract_evidence(
        result,
        expected_git_sha=expected_sha,
        expected_version=expected_version,
        expected_ref=expected_ref,
    )
    for problem in verdict.problems:
        click.echo(("  - " + problem), err=not verdict.ok)
    click.echo(verdict.summary())
    raise SystemExit(EXIT_SUCCESS if verdict.ok else EXIT_BLOCKED)


@main.command("selector-contract")
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True,
    help="Shared release run ID. The evidence is recorded at "
         "reports/runs/<run-id>/selector-contract/results.json and stamped with this ID.",
)
@click.option(
    "--source", "source_path", type=click.Path(exists=True), default=None,
    help="Existing selector-contract result JSON to adopt (e.g. a CI artifact "
         "downloaded for the exact release commit). When omitted, evidence is "
         "generated locally from the CaleeMobile-Regression + CaleeMobile checkouts.",
)
@click.option(
    "--expected-sha", "expected_sha", default=None,
    help="Expected full CaleeMobile release SHA. Defaults to config/release-platforms.yaml "
         "(caleemobile_git_sha), then the detected CaleeMobile checkout HEAD.",
)
@click.option(
    "--expected-version", "expected_version", default=None,
    help="Expected CaleeMobile release version. Defaults to config "
         "(caleemobile_build_version), then the detected checkout pubspec version.",
)
@click.option(
    "--expected-ref", "expected_ref", default=None,
    help="Expected CaleeMobile ref (non-blocking note; SHA/version are authoritative).",
)
@click.option(
    "--caleemobile-source", "caleemobile_source_opt", default=None,
    help="CaleeMobile checkout (default: ../CaleeMobile). Used to generate evidence "
         "and/or resolve the detected release identity.",
)
@click.option(
    "--regression-source", "regression_source_opt", default=None,
    help="CaleeMobile-Regression checkout (default: ../CaleeMobile-Regression). Its "
         "ui/selector_contract.py generates the evidence when --source is omitted.",
)
@click.option(
    "--flutter-version", "flutter_version_opt", default=None,
    help="Flutter toolchain the local generation must ACTUALLY report (default: the "
         "pinned version the schema requires). This is verified against `flutter "
         "--version`, never recorded on the toolchain's behalf.",
)
@click.option(
    "--production/--development", "production_opt", default=None,
    help="Production release profile (Priority 1, Problem A): production accepts ONLY "
         "a CI-produced selector artifact (--source with generatedBy=ci); local "
         "generation is refused. Defaults to config/release-platforms.yaml.",
)
@click.option(
    "--source-artifact-id", "source_artifact_id", default=None,
    help="GitHub artifact ID of the adopted --source (retained for traceability).",
)
@click.option(
    "--source-artifact-digest", "source_artifact_digest", default=None,
    help="GitHub-provided digest of the adopted --source artifact (retained for traceability).",
)
@click.option(
    "--adopted-by", "adopted_by", default=None,
    help="Who/what is adopting the evidence (recorded in adoption provenance; "
         "default: the selector-contract gate).",
)
@click.option(
    "--mandatory/--optional", "mandatory", default=True,
    help="Whether this selector contract is release-gating (default: mandatory).",
)
def selector_contract_cmd(
    run_id_opt, source_path, expected_sha, expected_version, expected_ref,
    caleemobile_source_opt, regression_source_opt, flutter_version_opt,
    production_opt, source_artifact_id, source_artifact_digest, adopted_by, mandatory,
):
    """Release gate: obtain/generate CaleeMobile selector evidence for the EXACT
    release build, validate it, and record it under this run BEFORE any mobile
    functional test (Priority 1).

    Resolves the expected CaleeMobile SHA+version (flags -> release profile ->
    detected checkout), obtains evidence (an adopted --source artifact, else a
    fresh local generation), stamps release-run provenance, and validates it with
    the hardened schema. Records the result at
    reports/runs/<run-id>/selector-contract/results.json (stamped with this run
    ID so consolidation trusts it) and exits:

      * SUCCESS (0)  -- valid selector evidence for the exact build being released;
      * BLOCKED (3)  -- evidence missing, unreadable, malformed, stale, for another
                        SHA/version, produced with the wrong Flutter version, not
                        PASS, or reporting any missing selector.

    A release can never PASS without valid selector evidence for the build being
    released -- this gate is why.
    """
    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    run_id = run_id_opt
    workspace = run_context.RunWorkspace(REPO_ROOT, run_id)
    component_dir = workspace.component_dir("selector-contract")
    component_dir.mkdir(parents=True, exist_ok=True)
    report_path = workspace.component_report_path("selector-contract")

    # Production release profile (Problem A): production accepts ONLY a
    # CI-produced selector artifact; local generation is refused. An explicit
    # --production/--development wins over config/release-platforms.yaml.
    try:
        eff_production = (
            production_opt if production_opt is not None
            else release_platforms.load_expected_build_identity().production
        )
    except release_platforms.ReleasePlatformsError:
        eff_production = bool(production_opt)

    def _finish(status, detail, problems, evidence, source_label, provenance=None):
        report = {
            "component": "caleemobile-selector-contract-gate",
            "releaseRunId": run_id,
            "runId": run_id,
            "status": status,  # "passed" | "blocked"
            "mandatory": mandatory,
            "production": eff_production,
            "expectedSha": expected_sha,
            "expectedVersion": expected_version,
            "source": source_label,
            "detail": list(detail),
            "problems": list(problems),
            # The evidence used for build-identity verification. This is the
            # source artifact preserved byte-for-byte -- the gate never mutates
            # a source artifact's own provenance (Problem B).
            "evidence": evidence,
            # Immutable source provenance + release adoption (Problem B). When
            # present, consolidation re-verifies its content digest and rules.
            "provenance": provenance,
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        # Also write the raw source evidence under a clear filename so the
        # release bundle can retain it as a downloadable artifact
        # (evidence/selector-contract-result.json) rather than a generic name.
        if evidence is not None:
            (component_dir / "selector-contract-result.json").write_text(
                json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
            )
        # Retain the immutable provenance record (source + adoption + digest +
        # any local toolchain verification) alongside it, so the bundle carries
        # both the original evidence and how it was adopted (Problem B).
        if provenance is not None:
            (component_dir / "selector-contract-provenance.json").write_text(
                json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
            )
        exit_code = EXIT_SUCCESS if status == "passed" else EXIT_BLOCKED
        try:
            manifest = _load_or_init_manifest(workspace)
            manifest.record_component("selector-contract", report_path=str(report_path), exit_code=exit_code)
            if evidence and evidence.get("testedSha"):
                manifest.git_shas["caleemobile-selector"] = evidence["testedSha"]
            manifest.write(workspace.manifest_path)
        except Exception:  # noqa: BLE001 - the report file is the authoritative artifact
            pass
        for line in problems:
            click.echo(f"  - {line}", err=(status != "passed"))
        for line in detail:
            click.echo(line)
        click.echo(f"Selector-contract gate: {status.upper()} (run {run_id})")
        click.echo(f"Recorded: {report_path}")
        raise SystemExit(exit_code)

    # 1. Resolve the expected CaleeMobile identity: flags -> release profile ->
    #    the detected checkout HEAD. Without a concrete SHA+version there is no
    #    "exact build" to prove selectors against, so an unresolved identity is
    #    itself a BLOCK.
    cm_source = Path(caleemobile_source_opt) if caleemobile_source_opt else (REPO_ROOT.parent / "CaleeMobile")
    detected = build_identity_mod.collect_caleemobile_identity(cm_source)
    if expected_sha is None or expected_version is None:
        try:
            configured = release_platforms.load_expected_build_identity()
        except release_platforms.ReleasePlatformsError as exc:
            _finish("blocked", [], [f"Invalid release-platforms config: {exc}"], None, "unresolved")
        if expected_sha is None:
            expected_sha = configured.caleemobile_git_sha or (detected.git_sha if detected.available else None)
        if expected_version is None:
            expected_version = configured.caleemobile_build_version or (
                detected.build_version if detected.available else None
            )
    if not expected_sha or not expected_version:
        _finish(
            "blocked", [],
            ["Cannot resolve the expected CaleeMobile release identity (need both SHA and version). "
             "Configure expected_build_identity in config/release-platforms.yaml, pass "
             "--expected-sha/--expected-version, or provide a CaleeMobile checkout."],
            None, "unresolved",
        )

    # 2. Obtain evidence: adopt a provided CI artifact, else generate locally
    #    (development only). The required Flutter version below is what the
    #    toolchain must ACTUALLY report -- it is never recorded on the toolchain's
    #    behalf (Problem A).
    required_flutter = flutter_version_opt or selector_evidence_mod.EXPECTED_FLUTTER_VERSION
    adopted_by_label = adopted_by or "caleemobile-selector-contract-gate"
    adopted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    local_verification = None  # verified toolchain evidence, for local generation

    if source_path:
        # --- Adopt a provided artifact (the ONLY path allowed in production) ---
        source_label = str(source_path)
        try:
            source_evidence = json.loads(Path(source_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _finish("blocked", [], [f"Could not read selector evidence from {source_path}: {exc}"], None, source_label)
        if not isinstance(source_evidence, dict):
            _finish("blocked", [], [f"Selector evidence at {source_path} is not a JSON object."], None, source_label)
        if eff_production and (source_evidence.get("generatedBy") or "").strip() != selector_provenance_mod.GENERATED_BY_CI:
            _finish(
                "blocked", [],
                ["Production release accepts ONLY a CI-produced selector artifact "
                 f"(generatedBy='ci'); this --source declares generatedBy="
                 f"{source_evidence.get('generatedBy')!r}."],
                source_evidence, source_label,
            )
    else:
        # --- Generate locally (development fallback; refused in production) ----
        source_label = "local-generation"
        if eff_production:
            _finish(
                "blocked", [],
                ["Production release accepts ONLY a CI-produced selector artifact. Local "
                 "generation cannot prove the release toolchain; provide the CI artifact "
                 "via --source (generatedBy='ci' with a workflowRunId)."],
                None, source_label,
            )
        reg_source = (
            Path(regression_source_opt) if regression_source_opt else (REPO_ROOT.parent / "CaleeMobile-Regression")
        )
        script = reg_source / "ui" / "selector_contract.py"
        if not script.is_file():
            _finish(
                "blocked", [],
                [f"Cannot generate selector evidence: {script} not found. Provide --source or a "
                 f"CaleeMobile-Regression checkout via --regression-source."],
                None, source_label,
            )
        if not cm_source.is_dir():
            _finish(
                "blocked", [],
                [f"Cannot generate selector evidence: CaleeMobile checkout not found at {cm_source}."],
                None, source_label,
            )
        # Actually run the Flutter toolchain against the exact CaleeMobile
        # checkout BEFORE trusting any generated evidence. A caller-supplied
        # Flutter string must never become proof of the installed toolchain
        # (Problem A): the recorded flutterVersion is the one the real
        # `flutter --version` reports, and generation is refused if the
        # toolchain cannot be verified.
        tv = toolchain_verify_mod.verify_local_toolchain(
            cm_source, reg_source, expected_flutter_version=required_flutter,
        )
        local_verification = tv.to_dict()
        if not tv.ok:
            _finish(
                "blocked", [],
                ["Local toolchain verification failed -- cannot back locally-generated "
                 "selector evidence with a real Flutter toolchain:", *tv.problems],
                None, source_label, selector_provenance_mod.build_provenance_record(
                    {"generatedBy": selector_provenance_mod.GENERATED_BY_LOCAL},
                    release_run_id=run_id, adopted_at=adopted_at, adopted_by=adopted_by_label,
                    source_path=source_label, local_verification=local_verification,
                ),
            )
        verified_flutter = tv.flutter_version  # the ACTUAL version, from `flutter --version`

        out_file = component_dir / "generated-evidence.json"
        env = dict(os.environ)
        env["CALEE_MOBILE_REPO_PATH"] = str(cm_source)
        env["CALEE_RUN_ID"] = run_id
        cmd = [
            sys.executable or "python3", str(script),
            "--ref", (expected_ref or expected_sha),
            "--flutter-version", verified_flutter,  # verified, not caller-supplied
            "--release-run-id", run_id,
            "--generated-by", selector_provenance_mod.GENERATED_BY_LOCAL,
            "--out", str(out_file),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, cwd=str(script.parent), env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _finish("blocked", [], [f"Selector-contract generation failed to run: {exc}"], None, source_label)
        # selector_contract.py exits non-zero when the contract FAILED but still
        # writes the evidence file; read it regardless and let the verifier BLOCK
        # on a FAIL. Only a missing/unreadable file is a generation failure.
        try:
            source_evidence = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _finish(
                "blocked", [],
                [f"Selector-contract generation produced no readable evidence: {exc}",
                 (proc.stderr or "").strip() or "(no stderr)"],
                None, source_label,
            )

    # 3. Build the immutable source-provenance + adoption record (Problem B).
    #    The source artifact is preserved byte-for-byte; a SHA-256 content
    #    digest is computed and verified now (and re-verified at consolidation);
    #    this run's adoption context is recorded SEPARATELY, never by overwriting
    #    the source's own provenance fields.
    provenance = selector_provenance_mod.build_provenance_record(
        source_evidence,
        release_run_id=run_id,
        adopted_at=adopted_at,
        adopted_by=adopted_by_label,
        source_path=source_label,
        source_artifact_id=source_artifact_id,
        source_artifact_digest=source_artifact_digest,
        local_verification=local_verification,
    )
    prov_problems = selector_provenance_mod.validate_source_provenance(
        source_evidence, local_verification=local_verification,
    )
    if prov_problems:
        _finish("blocked", [], prov_problems, source_evidence, source_label, provenance)

    # 4. Validate: hardened schema + exact-build identity. Run provenance is
    #    enforced via the adoption record above, so build identity is verified
    #    against the preserved source evidence directly (no in-place mutation).
    try:
        result = selector_evidence_mod.parse_selector_contract_result(source_evidence)
    except selector_evidence_mod.SelectorEvidenceError as exc:
        _finish("blocked", [], [f"Selector evidence is malformed: {exc}"], source_evidence, source_label, provenance)

    verdict = selector_evidence_mod.verify_selector_contract_evidence(
        result,
        expected_git_sha=expected_sha,
        expected_version=expected_version,
        expected_ref=expected_ref,
        expected_flutter_version=required_flutter,
    )
    if verdict.ok:
        detail = [
            f"Selector contract PASS for CaleeMobile {result.pubspec_version} @ {result.tested_sha} "
            f"({result.selectors_present}/{result.selectors_checked} selectors present, "
            f"Flutter {result.flutter_version}). Evidence source: {source_label}; "
            f"adopted by {adopted_by_label} for run {run_id}."
        ]
        _finish("passed", detail, verdict.problems, source_evidence, source_label, provenance)
    _finish("blocked", [], verdict.problems, source_evidence, source_label, provenance)


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


def _report_build_sha(report: "dict | None") -> "str | None":
    """The CaleeMobile Git SHA embedded in a per-platform UI report at
    execution time (report["buildIdentity"]["gitSha"]), or None. See Phase 5
    and CaleeMobile-Regression/ui/run_ui_suite.py."""
    if isinstance(report, dict):
        identity = report.get("buildIdentity")
        if isinstance(identity, dict):
            return identity.get("gitSha")
    return None


def _snapshot_caleemobile_sha(snapshot: "dict | None") -> "str | None":
    """The CaleeMobile Git SHA from a pre/post identity snapshot
    (snapshot["caleemobile"]["gitSha"]), or None."""
    if isinstance(snapshot, dict):
        caleemobile = snapshot.get("caleemobile")
        if isinstance(caleemobile, dict):
            return caleemobile.get("gitSha")
    return None


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
@click.option("--sync-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's sync/results.json")
@click.option("--kiosk-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's kiosk-admin/results.json (kiosk/admin feature evidence)")
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
@click.option(
    "--sync-mandatory/--sync-optional", "sync_mandatory", default=None,
    help="Whether cross-device synchronization is release-gating. Defaults to "
         "config/release-platforms.yaml (release_features.synchronization), or True if absent. "
         "An optional sync is still shown in the report, just never blocks a PASS.",
)
@click.option(
    "--selector-contract-mandatory/--selector-contract-optional", "selector_contract_mandatory", default=None,
    help="Whether CaleeMobile selector evidence is release-gating (Priority 1). The full "
         "launcher passes --selector-contract-mandatory. When omitted, the component is still "
         "auto-included as mandatory if a selector-contract report exists for this run; a release "
         "can never PASS without valid selector evidence for the exact CaleeMobile build.",
)
# Independent release-feature gating (Workstream 3). Each defaults to the
# release feature profile (config/release-platforms.yaml release_features.*),
# or True if absent -- an omitted feature is mandatory. An optional feature is
# still shown as an explicit component, just never blocks a PASS.
@click.option("--meals-mandatory/--meals-optional", "meals_mandatory", default=None,
              help="Whether the CaleeMobile Meals feature is release-gating. Defaults to release_features.meals.")
@click.option("--onboarding-mandatory/--onboarding-optional", "onboarding_mandatory", default=None,
              help="Whether onboarding + display/mobile handoff is release-gating. Defaults to release_features.onboarding.")
@click.option("--google-calendar-mandatory/--google-calendar-optional", "google_calendar_mandatory", default=None,
              help="Whether Google Calendar connection is release-gating. Defaults to release_features.google_calendar.")
@click.option("--kiosk-admin-mandatory/--kiosk-admin-optional", "kiosk_admin_mandatory", default=None,
              help="Whether CaleeShell kiosk/admin is release-gating. Defaults to release_features.kiosk_admin.")
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
@click.option("--production/--development", "production", default=None, help="Production release profile (Workstream 3): the expected identity below becomes REQUIRED, and a dirty tree needs a named waiver. Defaults to config/release-platforms.yaml (expected_build_identity.production).")
@click.option("--expected-calee-application-id", default=None, help="Production: expected Calee tablet application id; mismatch/missing BLOCKS")
@click.option("--expected-calee-version-code", default=None, help="Production: expected Calee tablet installed versionCode; mismatch/missing BLOCKS")
@click.option("--expected-caleeshell-version", default=None, help="Production: expected CaleeShell version when CaleeShell is in scope; mismatch/missing BLOCKS")
@click.option("--waiver-reason", default=None, help="Named waiver reason (Workstream 3): approves a dirty tree in a production release")
@click.option("--waiver-approver", default=None, help="Named waiver approver")
@click.option("--waiver-timestamp", default=None, help="Named waiver timestamp")
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
    sync_report, kiosk_report, manual_checks_path, environment_report, android_mandatory, ios_mandatory, sync_mandatory,
    selector_contract_mandatory,
    meals_mandatory, onboarding_mandatory, google_calendar_mandatory, kiosk_admin_mandatory,
    build_version, calee_build_version, expected_calee_build_version,
    caleemobile_build_version, expected_caleemobile_build_version, caleeshell_version,
    calee_git_sha, expected_calee_git_sha, caleemobile_git_sha, expected_caleemobile_git_sha,
    production, expected_calee_application_id, expected_calee_version_code, expected_caleeshell_version,
    waiver_reason, waiver_approver, waiver_timestamp,
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
    sync = resolve("sync", sync_report)
    kiosk = resolve("kiosk-admin", kiosk_report)
    # CaleeMobile selector contract (Priority 1). Auto-discovered from this run's
    # workspace and run-ID-validated like every other component. An explicit
    # --selector-contract-mandatory/--optional wins; otherwise a present report is
    # always release-gating (a recorded selector gate must never be silently
    # ignored), and its absence leaves the component out only for legacy/ad-hoc
    # consolidation that never ran the gate.
    selector_contract_report = resolve("selector-contract", None)
    if selector_contract_mandatory is not None:
        selector_contract_gating = selector_contract_mandatory
    elif selector_contract_report is not None:
        selector_contract_gating = True
    else:
        selector_contract_gating = None

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
        features = release_platforms.load_release_features()
        expected_identity = release_platforms.load_expected_build_identity()
    except release_platforms.ReleasePlatformsError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    android_gating = android_mandatory if android_mandatory is not None else platforms.mobile_android
    ios_gating = ios_mandatory if ios_mandatory is not None else platforms.mobile_ios
    # Cross-device synchronization gating (Workstream 1): explicit
    # --sync-mandatory/--sync-optional wins, else the release feature profile
    # (release_features.synchronization), which defaults to True.
    sync_gating = sync_mandatory if sync_mandatory is not None else features.synchronization

    # Independent release-feature gating (Workstream 3): explicit
    # --<feature>-mandatory/--<feature>-optional wins, else the release feature
    # profile (release_features.<feature>), which defaults to True. Each feature
    # gets its OWN consolidated component built from feature-tagged step
    # evidence -- a mandatory feature with no evidence BLOCKS.
    feature_profile = {
        "meals": meals_mandatory if meals_mandatory is not None else features.meals,
        "onboarding": onboarding_mandatory if onboarding_mandatory is not None else features.onboarding,
        "google_calendar": (
            google_calendar_mandatory if google_calendar_mandatory is not None else features.google_calendar
        ),
        "kiosk_admin": kiosk_admin_mandatory if kiosk_admin_mandatory is not None else features.kiosk_admin,
    }

    # Expected build identity: an explicit CLI flag wins over the release
    # profile (config/release-platforms.yaml's expected_build_identity).
    eff_expected_calee_build = expected_calee_build_version or expected_identity.calee_build_version
    eff_expected_calee_sha = expected_calee_git_sha or expected_identity.calee_git_sha
    eff_expected_caleemobile_build = expected_caleemobile_build_version or expected_identity.caleemobile_build_version
    eff_expected_caleemobile_sha = expected_caleemobile_git_sha or expected_identity.caleemobile_git_sha
    eff_expected_calee_app_id = expected_calee_application_id or expected_identity.calee_application_id
    eff_expected_calee_version_code = expected_calee_version_code or expected_identity.calee_version_code
    eff_expected_caleeshell_version = expected_caleeshell_version or expected_identity.caleeshell_version

    # Production release profile (Workstream 3): the expected identity becomes
    # REQUIRED (a missing expectation BLOCKS), and a dirty tree needs a named
    # waiver -- allow_dirty alone is not sufficient. An explicit --production/
    # --development wins over the profile.
    eff_production = production if production is not None else expected_identity.production
    try:
        profile_waiver = release_platforms.load_waiver()
    except release_platforms.ReleasePlatformsError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    # A CLI-supplied waiver (build pipeline) wins over the profile's, per field.
    waiver = {
        "reason": waiver_reason or profile_waiver.reason,
        "approver": waiver_approver or profile_waiver.approver,
        "timestamp": waiver_timestamp or profile_waiver.timestamp,
    }
    waiver_is_valid = all(str(waiver.get(k) or "").strip() for k in ("reason", "approver", "timestamp"))

    if eff_production:
        # In production a dirty tree is approved ONLY by a valid named waiver;
        # --allow-dirty / allow_dirty:true cannot bypass the waiver requirement.
        allow_dirty = waiver_is_valid
    else:
        allow_dirty = allow_dirty_opt if allow_dirty_opt is not None else expected_identity.allow_dirty

    # An app's identity is required (mandatory-to-know) when that app is in this
    # release's scope. A PASS may never leave an in-scope build's identity
    # unknown (Phase 3).
    #
    # The Calee tablet is UNCONDITIONALLY in scope for the consolidated
    # Calee-solution report: the tablet suite is always mandatory
    # (component_from_tablet_report is always mandatory=True) and is
    # unconditionally executed by the full launcher, so its build identity is
    # unconditionally required whenever build identity is required at all --
    # execution scope and consolidation scope must never disagree (Workstream 2).
    # The release_platforms `tablet` flag is therefore NOT a full-solution
    # opt-out; it never gated execution or the tablet component's mandatoriness,
    # and it must not silently relax the tablet's identity requirement either.
    require_calee_identity = require_build_identity
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

    # CaleeMobile commit-SHA agreement (Phase 5): the exact SHA embedded into
    # each Android/iOS UI report at execution time must agree with the pre/post
    # snapshots, the expected release SHA, and the detected SHA -- all full,
    # unambiguous 40-char SHAs. A version alone (e.g. 0.0.22+22) spans many
    # commits, so an in-scope CaleeMobile run gates on the exact commit.
    caleemobile_sha_values = {
        "Android UI report": _report_build_sha(mobile_android),
        "iPhone UI report": _report_build_sha(mobile_ios),
        "pre-run snapshot": _snapshot_caleemobile_sha(identity_pre),
        "post-run snapshot": _snapshot_caleemobile_sha(identity_post),
        "expected release": eff_expected_caleemobile_sha,
        "detected": caleemobile_git_sha,
    }
    sha_agreement = component_from_caleemobile_sha_agreement(
        caleemobile_sha_values, required=require_caleemobile_identity,
    )

    # Release identity intent (Workstream 3): for a production profile the
    # *expected* identity must be stated up front (missing -> BLOCKED) and a
    # dirty tree needs a named waiver. CaleeShell is in scope when the tablet is
    # and the kiosk/admin feature is included. The tablet source SHA is only
    # required where the pipeline can provide it (a source SHA was detected).
    caleeshell_in_scope = require_calee_identity and features.kiosk_admin
    release_intent = component_from_release_intent(
        production=eff_production,
        caleemobile_in_scope=require_caleemobile_identity,
        tablet_in_scope=require_calee_identity,
        caleeshell_in_scope=caleeshell_in_scope,
        expected_caleemobile_build_version=eff_expected_caleemobile_build,
        expected_caleemobile_git_sha=eff_expected_caleemobile_sha,
        expected_calee_build_version=eff_expected_calee_build,
        expected_calee_git_sha=eff_expected_calee_sha,
        expected_calee_application_id=eff_expected_calee_app_id,
        expected_calee_version_code=eff_expected_calee_version_code,
        expected_caleeshell_version=eff_expected_caleeshell_version,
        detected_calee_application_id=calee_application_id,
        detected_calee_version_code=calee_version_code,
        detected_caleeshell_version=caleeshell_version,
        tablet_source_sha_available=bool(calee_git_sha),
        caleemobile_dirty=caleemobile_dirty,
        calee_dirty=calee_dirty,
        waiver=waiver,
    )

    extra_components = []
    if identity_stability is not None:
        extra_components.append(identity_stability)
    if sha_agreement is not None:
        extra_components.append(sha_agreement)
    if release_intent is not None:
        extra_components.append(release_intent)

    report = build_release_report(
        environment=env_report,
        tablet=tablet,
        mobile_api=mobile_api,
        mobile_android_ui=mobile_android,
        mobile_ios_ui=mobile_ios,
        sync=sync,
        kiosk_admin=kiosk,
        feature_profile=feature_profile,
        manual_checks=manual_checks_list,
        meta=meta,
        android_mandatory=android_gating,
        ios_mandatory=ios_gating,
        sync_mandatory=sync_gating,
        selector_contract=selector_contract_report,
        selector_contract_mandatory=selector_contract_gating,
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
        require_calee_package_identity=require_calee_identity,
        require_caleemobile_identity=require_caleemobile_identity,
        require_caleemobile_git_sha=require_caleemobile_identity,
        allow_dirty=allow_dirty,
        mobile_exit_floors=mobile_exit_floors,
        extra_components=extra_components,
    )

    out = Path(out_dir) if out_dir else workspace.consolidated_dir
    # Retain the raw selector-contract evidence inside the release ZIP so the
    # selector proof travels with the bundle as a downloadable artifact
    # (Priority 1: "Include it ... in ... ZIP outputs").
    evidence_paths = []
    if selector_contract_gating is not None:
        selector_dir = workspace.component_dir("selector-contract")
        raw_evidence = selector_dir / "selector-contract-result.json"
        # The immutable source-provenance + adoption record (Problem B): the
        # original evidence and its adoption metadata travel with the bundle.
        provenance_evidence = selector_dir / "selector-contract-provenance.json"
        selector_report_path = workspace.component_report_path("selector-contract")
        for candidate in (raw_evidence, provenance_evidence, selector_report_path):
            if candidate.is_file():
                evidence_paths.append(candidate)
    bundle_path = write_release_bundle(
        report, out, build_label=build_version, evidence_paths=evidence_paths or None
    )

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
