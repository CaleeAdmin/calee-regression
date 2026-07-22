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
from . import build_provenance as build_provenance_mod
from . import config as config_mod
from . import credentials as credentials_mod
from . import distributed_build_acceptance as distributed_build_acceptance_mod
from . import manual_checks as manual_checks_mod
from . import provider_evidence as provider_evidence_mod
from . import preflight, release_platforms, reporting, suites
from . import report_root as report_root_mod
from . import run_context
from . import github_artifact as github_artifact_mod
from . import selector_evidence as selector_evidence_mod
from . import selector_provenance as selector_provenance_mod
from . import sync_smoke
from . import toolchain_verify as toolchain_verify_mod
from .appium_driver import CaleeDriver
from .consolidated_report import (
    DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT_NAME,
    STATUS_BLOCKED,
    STATUS_PASS,
    ManualCheck,
    build_release_report,
    collect_step_diagnostic_paths,
    component_from_caleemobile_sha_agreement,
    component_from_distributed_build_acceptance_report,
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

# ComponentResult.name (the human-readable label build_release_report hard-
# codes for each component) -> the run_context.COMPONENT_NAMES slug it came
# from. Used only to attach resume provenance (see resume_release.py) onto
# the right ComponentResult after build_release_report returns -- kept next
# to _STATUS_TO_EXIT_CODE as another "one place both sides must agree" map.
_CONSOLIDATED_COMPONENT_SLUGS = {
    "Test environment and regression fixture": "environment",
    "Calee tablet": "tablet",
    "CaleeMobile Client API": "mobile-api",
    "CaleeMobile Android UI": "mobile-android",
    "CaleeMobile iPhone UI": "mobile-ios",
    "manual checks": "manual-checks",
    "CaleeMobile selector contract": "selector-contract",
    "CaleeMobile cross-device synchronization": "sync",
    "Subscribed-calendar fixture": "subscribed-fixture",
    "Calee tablet release installation": "installation",
    "Release configuration (machine + release-candidate composition)": "release-config",
    "Machine configuration (config/machine.local.yaml)": "machine-config",
    "Distributed-build acceptance": "distributed-build-acceptance",
    "CaleeShell kiosk/admin": "kiosk-admin",
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
    return _resolved_report_root() / "reports" / "appium.log"


def _appium_pid_path() -> Path:
    return _resolved_report_root() / "reports" / "appium.pid"


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


def _fill_credentials_from_providers(email, password):
    """Fill a missing regression email/password from the environment and then the
    macOS Keychain (credentials.default_resolver's chain: injected CLI value ->
    env -> Keychain). An explicit CLI/env value always wins; anything still
    unresolved stays None so the caller's existing BLOCKED guard fires.

    Returns ``(email, password, resolver)`` -- ``resolver.secret_values()`` is
    the set of every secret actually resolved, fed to ``credentials.redact``
    before any report/log text is written so a secret can never leak (Priority
    3). Never places a secret on a command line and never prints the resolver
    (its repr is secret-free by construction)."""
    injected = {}
    if email:
        injected[credentials_mod.REGRESSION_USERNAME.name] = email
    if password:
        injected[credentials_mod.REGRESSION_PASSWORD.name] = password
    resolver = credentials_mod.default_resolver(injected=injected or None)
    resolved_email = resolver.get(credentials_mod.REGRESSION_USERNAME)
    resolved_password = resolver.get(credentials_mod.REGRESSION_PASSWORD)
    # Optional secrets (API token, AI-analysis key) are resolved too so their
    # values are in the redaction set even though they are never required here.
    for optional in credentials_mod.OPTIONAL_SECRETS:
        resolver.get(optional)
    return resolved_email, resolved_password, resolver


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
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)
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

    # Integrate the environment + macOS Keychain credential providers (Priority
    # 3): a fixture email/password not passed on the CLI/env can still resolve
    # from the login Keychain. Anything still unresolved falls through to the
    # BLOCKED guard below -- a required credential is never silently empty.
    fixture_email, fixture_password, _cred_resolver = _fill_credentials_from_providers(
        fixture_email, fixture_password
    )
    if not (fixture_base_url and fixture_email and fixture_password):
        detail = [
            "Fixture credentials are not configured (set CALEE_API_BASE, CALEE_TEST_EMAIL, "
            "CALEE_TEST_PASSWORD, the macOS Keychain, or pass --fixture-base-url/--fixture-email/"
            "--fixture-password)."
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
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
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

    out = Path(out_path) if out_path else manual_checks_mod.default_output_path(_resolved_report_root() / "reports")
    manual_checks_mod.write_results(results, out)

    if run_id_opt:
        run_id = _resolve_run_id(run_id_opt)
        workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)
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
    variables = _load_run_scenario_variables(run_id)
    result = ScenarioRunner(cfg, report_builder=rb, variables=variables).run_scenarios([scenario_path], suite_name=scenario_path.stem)
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
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)
    workspace.ensure_created()
    return workspace.component_dir("tablet"), run_id


def _load_run_scenario_variables(run_id: "str | None") -> "dict | None":
    """Load run-scoped scenario variables (Priority 6): the today-relative
    subscribed-event titles that prepare-subscribed-fixture recorded, so a
    scenario's ${VAR} placeholders resolve to THIS run's provisioned events.
    None when there is no run or no subscribed-fixture evidence."""
    if not run_id or not run_context.is_valid_run_id(run_id):
        return None
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)
    evidence = workspace.component_report_path("subscribed-fixture")
    if not evidence.is_file():
        return None
    try:
        data = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    variables = data.get("variables")
    return variables if isinstance(variables, dict) and variables else None


def _record_tablet_component(run_id: "str | None", report_dir: Path, result) -> None:
    if not run_id:
        return
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)
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
    variables = _load_run_scenario_variables(run_id)
    result = ScenarioRunner(cfg, report_builder=rb, variables=variables).run_scenarios(scenario_paths, suite_name=suite_name)
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
    help="Whether cross-device synchronization is release-gating for this run. Defaults to this "
         "run's own schema-v2 release-config feature scope when one was composed (never the legacy "
         "file in that case), else config/release-platforms.yaml (release_features.synchronization), "
         "or True if neither is available. An excluded (optional) sync is recorded as an explicit "
         "optional component, never run.",
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
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)

    # Mandatory-ness (Priority 2): explicit flag wins; else THIS run's own
    # schema-v2 release-config feature scope (never the legacy file, once
    # composed); else the release feature profile (release_features.
    # synchronization), which defaults to True when no config file is present
    # -- an omitted feature must never silently become optional.
    if mandatory_opt is None:
        release_config_dict = _load_release_config_dict(workspace)
        if release_config_dict is not None and release_config_dict.get("schemaVersion") == 2:
            _, v2_features, _ = _v2_platforms_features_expected(release_config_dict)
            mandatory = v2_features.synchronization
        else:
            try:
                mandatory = release_platforms.load_release_features().synchronization
            except release_platforms.ReleasePlatformsError:
                mandatory = True
    else:
        mandatory = mandatory_opt

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

    # Integrate the environment + macOS Keychain credential providers (Priority
    # 3): email/password not on the CLI/env can still resolve from the Keychain.
    # Resolved BEFORE any workspace/report directory is created, so a
    # missing-credential invocation stays BLOCKED without leaving a half-formed
    # reports/runs/<id>/ behind.
    email, password, cred_resolver = _fill_credentials_from_providers(email, password)
    if not base_url or not email or not password:
        click.echo(
            "BLOCKED: sync-smoke needs --base-url/--email/--password (or CALEE_EXPECTED_BACKEND/"
            "CALEE_TEST_EMAIL/CALEE_TEST_PASSWORD, or the macOS Keychain) to reach the Calee Client "
            "API and CaleeMobile.",
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
    payload = {"runId": run_id, "mandatory": True, "flows": [r.to_dict() for r in results]}
    # Redact any resolved secret value from the serialized report before it is
    # written to disk (Priority 3): even though the flows never intentionally
    # record credentials, a subprocess error excerpt could carry one.
    report_text = credentials_mod.redact(json.dumps(payload, indent=2), cred_resolver.secret_values())
    report_path.write_text(report_text + "\n", encoding="utf-8")

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
    help="Whether CaleeShell kiosk/admin is release-gating for this run. Defaults to this run's own "
         "schema-v2 release-config feature scope when one was composed (never the legacy file in "
         "that case), else config/release-platforms.yaml (release_features.kiosk_admin), or True if "
         "neither is available. An excluded (optional) kiosk/admin is recorded as an explicit "
         "optional component, never run.",
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
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)

    # Mandatory-ness (Priority 2): explicit flag wins; else THIS run's own
    # schema-v2 release-config feature scope (never the legacy file, once
    # composed); else config/release-platforms.yaml, defaulting to True.
    if mandatory_opt is None:
        release_config_dict = _load_release_config_dict(workspace)
        if release_config_dict is not None and release_config_dict.get("schemaVersion") == 2:
            _, v2_features, _ = _v2_platforms_features_expected(release_config_dict)
            mandatory = v2_features.kiosk_admin
        else:
            try:
                mandatory = release_platforms.load_release_features().kiosk_admin
            except release_platforms.ReleasePlatformsError:
                mandatory = True
    else:
        mandatory = mandatory_opt

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
        expected = release_platforms.load_expected_build_identity()
    except release_platforms.ReleasePlatformsError as exc:
        click.echo(f"echo '{exc}' >&2; exit {EXIT_INVALID_CONFIG}")
        raise SystemExit(EXIT_INVALID_CONFIG)
    click.echo(f"RELEASE_PLATFORM_TABLET={'true' if platforms.tablet else 'false'}")
    click.echo(f"RELEASE_PLATFORM_ANDROID={'true' if platforms.mobile_android else 'false'}")
    click.echo(f"RELEASE_PLATFORM_IOS={'true' if platforms.mobile_ios else 'false'}")
    # Priority 2 (this session): same resolved selector-evidence policy as
    # _emit_release_config_vars, for the no-machine-config (legacy-only)
    # path -- schema-v1/legacy policy: mandatory whenever a mobile platform
    # is in scope, unconditionally mandatory in production.
    from . import release_config as _rc_mod

    resolved_selector_required = _rc_mod.resolve_selector_evidence_required(
        profile=("production" if expected.production else "staging"),
        enabled_platforms=[p for p, on in (("android", platforms.mobile_android), ("ios", platforms.mobile_ios)) if on],
        schema_version=1,
        manifest_required=None,
    )
    click.echo(f"RELEASE_SELECTOR_EVIDENCE_REQUIRED={'true' if resolved_selector_required else 'false'}")
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

    This is a standalone spot-check utility -- it is NOT part of the
    `consolidate`/`selector-contract` release-gating pipeline (grep the
    codebase: nothing else calls it) and it has NO schema-v2 awareness: when
    --expected-sha/--expected-version are omitted they fall back ONLY to
    config/release-platforms.yaml's expected identity, even for a schema-v2
    release whose actual expected identity comes from the release-candidate
    bundle instead. For the identity the real release run actually enforces,
    see `selector-contract` (which IS schema-v2-aware) and `consolidate`.
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
    help="Expected full CaleeMobile release SHA. Defaults to this run's own schema-v2 "
         "release-config identity when one was composed (never the legacy file in that case), else "
         "config/release-platforms.yaml (caleemobile_git_sha), then the detected CaleeMobile "
         "checkout HEAD.",
)
@click.option(
    "--expected-version", "expected_version", default=None,
    help="Expected CaleeMobile release version. Defaults to this run's own schema-v2 release-config "
         "identity when one was composed (never the legacy file in that case), else config "
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
         "generation is refused. Defaults to this run's own schema-v2 release-config profile when "
         "one was composed (never the legacy file in that case), else config/release-platforms.yaml.",
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
    "--github-run-id", "github_run_id", default=None,
    help="GitHub Actions workflow run ID that produced the selector artifact "
         "(Priority 2). Required, with --github-artifact-id, for a PRODUCTION release: "
         "the run/job/artifact ownership and the artifact digest are verified against "
         "GitHub before the evidence is accepted. A bare --source is refused in production.",
)
@click.option(
    "--github-artifact-id", "github_artifact_id", default=None,
    help="GitHub Actions artifact ID of the selector-contract-result artifact (Priority 2). "
         "Its ZIP bytes are downloaded and hashed against GitHub's recorded digest.",
)
@click.option(
    "--github-artifact-zip", "github_artifact_zip", type=click.Path(exists=True), default=None,
    help="An already-downloaded artifact ZIP to authenticate locally (Priority 2). Its bytes "
         "are still hashed against GitHub's digest; run/artifact metadata is still verified "
         "over the API (so credentials are still required for the ownership checks).",
)
@click.option(
    "--dirty-waiver", "dirty_waiver_opt", default=None,
    help="Named development waiver (Priority 4) permitting local generation from a "
         "dirty CaleeMobile/CaleeMobile-Regression worktree. Recorded in the local "
         "verification record; without it, a dirty worktree BLOCKS local generation.",
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
@click.option(
    "--expected-release-id", "expected_release_id", envvar="CALEE_RELEASE_ID", default=None,
    help="Priority 8: when set, this is a RELEASE-CERTIFICATION request, not ordinary PR selector "
         "checking -- the adopted evidence must carry a matching releaseId (missing release identity, "
         "or a mismatched one, fails certification even if SHA/version match). Defaults to the "
         "release-config composition's releaseId when this run already composed one.",
)
def selector_contract_cmd(
    run_id_opt, source_path, expected_sha, expected_version, expected_ref,
    caleemobile_source_opt, regression_source_opt, flutter_version_opt,
    production_opt, source_artifact_id, source_artifact_digest,
    github_run_id, github_artifact_id, github_artifact_zip, dirty_waiver_opt,
    adopted_by, mandatory, expected_release_id,
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
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)
    component_dir = workspace.component_dir("selector-contract")
    component_dir.mkdir(parents=True, exist_ok=True)
    report_path = workspace.component_report_path("selector-contract")

    # Priority 8: an explicit --expected-release-id wins; else, when this run
    # already composed its release-config (launcher "00" always does before
    # this gate runs), adopt ITS releaseId -- the same release the whole run
    # is for. No release-config composed for this run at all (a bare/dev
    # invocation) leaves this None, so ordinary PR selector checking is
    # completely unaffected (requirement 1).
    release_config_dict = _load_release_config_dict(workspace)
    if expected_release_id is None and release_config_dict is not None:
        expected_release_id = release_config_dict.get("releaseId")

    # Priority 2: once this run has composed a schema-v2 release-config, ITS
    # profile and expected CaleeMobile identity are authoritative -- config/
    # release-platforms.yaml is not consulted at all for such a run. This is
    # what makes requirement 6 ("a schema-v2 production release must never
    # permit local selector generation") hold even when the legacy file is
    # stale, absent, or declares a different profile.
    v2_expected_identity = None
    if release_config_dict is not None and release_config_dict.get("schemaVersion") == 2:
        _, _, v2_expected_identity = _v2_platforms_features_expected(release_config_dict)

    # Production release profile (Problem A): production accepts ONLY a
    # CI-produced selector artifact; local generation is refused. An explicit
    # --production/--development wins over the schema-v2 release-config or,
    # absent one, config/release-platforms.yaml (schema v1 / bare invocation).
    if production_opt is not None:
        eff_production = production_opt
    elif v2_expected_identity is not None:
        eff_production = v2_expected_identity.production
    else:
        try:
            eff_production = release_platforms.load_expected_build_identity().production
        except release_platforms.ReleasePlatformsError:
            eff_production = False

    def _finish(status, detail, problems, evidence, source_label, provenance=None,
                raw_result_bytes=None, raw_zip_bytes=None):
        report = {
            "component": "caleemobile-selector-contract-gate",
            "releaseRunId": run_id,
            "runId": run_id,
            "status": status,  # "passed" | "blocked"
            "mandatory": mandatory,
            "production": eff_production,
            "expectedSha": expected_sha,
            "expectedVersion": expected_version,
            "expectedReleaseId": expected_release_id,
            "source": source_label,
            "detail": list(detail),
            "problems": list(problems),
            # The evidence used for build-identity verification -- the parsed
            # (semantic) view of the source artifact. The gate never mutates a
            # source artifact's own provenance (Problem B). The exact source
            # bytes are preserved separately in the evidence bundle below.
            "evidence": evidence,
            # Immutable source provenance + release adoption (Problem B/P3). When
            # present, consolidation re-verifies its envelope + content digests
            # and rules.
            "provenance": provenance,
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        # Also write the semantic source evidence under a clear filename (kept
        # for backward compatibility with existing consumers).
        if evidence is not None:
            (component_dir / "selector-contract-result.json").write_text(
                json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
            )
        if provenance is not None:
            (component_dir / "selector-contract-provenance.json").write_text(
                json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
            )
            # Priority 3 evidence bundle: raw ZIP + raw JSON bytes (unmodified),
            # their raw-byte sha256 sidecars, and the envelope-protected
            # provenance.json. Written only when the raw source bytes exist (the
            # GitHub artifact chain); local generation has no source ZIP.
            try:
                selector_provenance_mod.write_evidence_bundle(
                    component_dir, provenance,
                    result_bytes=raw_result_bytes, zip_bytes=raw_zip_bytes,
                )
            except Exception:  # noqa: BLE001 - the report file is authoritative
                pass
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
        if v2_expected_identity is not None:
            # Schema v2: never fall back to the legacy file (requirement 7 --
            # a malformed release-platforms.yaml must not block a valid v2
            # bundle's selector resolution).
            configured = v2_expected_identity
        else:
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

    # 2. Obtain evidence. Policy (Priority 2):
    #    * PRODUCTION accepts ONLY the GitHub artifact authenticity chain
    #      (--github-run-id + --github-artifact-id [+ --github-artifact-zip]). A
    #      bare --source JSON -- even one self-declaring generatedBy='ci' with a
    #      workflowRunId -- is REFUSED: any file can claim that; only GitHub's own
    #      run/job/artifact record and the artifact digest are proof.
    #    * DEVELOPMENT: the GitHub chain if given, else an adopted --source
    #      artifact, else a fresh local generation (real toolchain verified).
    #    The required Flutter version below is what the toolchain must ACTUALLY
    #    report -- never recorded on the toolchain's behalf (Problem A).
    required_flutter = flutter_version_opt or selector_evidence_mod.EXPECTED_FLUTTER_VERSION
    adopted_by_label = adopted_by or "caleemobile-selector-contract-gate"
    adopted_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    local_verification = None  # verified toolchain evidence, for local generation
    raw_result_bytes = None    # exact source-result.json bytes (GitHub chain)
    raw_zip_bytes = None       # exact source-artifact.zip bytes (GitHub chain)
    use_github = bool(github_run_id or github_artifact_id or github_artifact_zip)

    if eff_production and not use_github:
        _finish(
            "blocked", [],
            ["Production release accepts ONLY a CI-produced selector artifact authenticated "
             "against GitHub (Priority 2): pass --github-run-id and --github-artifact-id "
             "(optionally --github-artifact-zip). A bare --source JSON cannot be authenticated "
             "-- any file can self-declare a CI-produced generatedBy='ci' with a workflowRunId "
             "-- so it is refused for a production release."],
            None, "production-requires-github-chain",
        )

    if use_github:
        # --- Authenticate a GitHub-produced artifact (the ONLY production path) ---
        source_label = f"github-artifact:{github_artifact_id or '?'}@run:{github_run_id or '?'}"
        try:
            chain = github_artifact_mod.acquire_github_artifact(
                run_id=github_run_id,
                artifact_id=github_artifact_id,
                local_zip_path=github_artifact_zip,
                expected_tested_sha=expected_sha,
                expected_version=expected_version,
            )
        except github_artifact_mod.GithubArtifactError as exc:
            # Missing credentials / unreadable metadata / malformed ZIP -> BLOCKED,
            # naming the exact missing secret where that is the cause.
            _finish("blocked", [], [str(exc)], None, source_label)
        if not chain.ok:
            _finish("blocked", [], list(chain.problems), chain.result, source_label)
        source_evidence = chain.result
        raw_result_bytes = chain.result_bytes
        raw_zip_bytes = chain.zip_bytes
        # Prefer GitHub's verified artifact identity for the provenance record.
        if chain.artifact is not None:
            source_artifact_id = source_artifact_id or chain.artifact.artifact_id
            source_artifact_digest = source_artifact_digest or chain.artifact.digest
    elif source_path:
        # --- Adopt a provided artifact (development only) ---
        source_label = str(source_path)
        try:
            source_evidence = json.loads(Path(source_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _finish("blocked", [], [f"Could not read selector evidence from {source_path}: {exc}"], None, source_label)
        if not isinstance(source_evidence, dict):
            _finish("blocked", [], [f"Selector evidence at {source_path} is not a JSON object."], None, source_label)
    else:
        # --- Generate locally (development fallback; refused in production) ----
        source_label = "local-generation"
        if eff_production:
            _finish(
                "blocked", [],
                ["Production release accepts ONLY a CI-produced selector artifact. Local "
                 "generation cannot prove the release toolchain; provide the GitHub-authenticated "
                 "CI artifact via --github-run-id/--github-artifact-id."],
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
            dirty_waiver=dirty_waiver_opt,
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

    # 3. Build the immutable source-provenance + adoption record (Problem B/P3).
    #    The semantic evidence is preserved with a canonical content digest; when
    #    the exact source bytes came from the GitHub chain, their raw-byte digests
    #    are recorded too, and the whole envelope is digest-protected. This run's
    #    adoption context is recorded SEPARATELY, never by overwriting the
    #    source's own provenance fields.
    provenance = selector_provenance_mod.build_provenance_record(
        source_evidence,
        release_run_id=run_id,
        adopted_at=adopted_at,
        adopted_by=adopted_by_label,
        source_path=source_label,
        source_artifact_id=source_artifact_id,
        source_artifact_digest=source_artifact_digest,
        local_verification=local_verification,
        raw_result_bytes=raw_result_bytes,
        raw_zip_bytes=raw_zip_bytes,
    )
    prov_problems = selector_provenance_mod.validate_source_provenance(
        source_evidence, local_verification=local_verification,
    )
    if prov_problems:
        _finish("blocked", [], prov_problems, source_evidence, source_label, provenance,
                raw_result_bytes, raw_zip_bytes)

    # 4. Validate: hardened schema + exact-build identity. Run provenance is
    #    enforced via the adoption record above, so build identity is verified
    #    against the preserved source evidence directly (no in-place mutation).
    try:
        result = selector_evidence_mod.parse_selector_contract_result(source_evidence)
    except selector_evidence_mod.SelectorEvidenceError as exc:
        _finish("blocked", [], [f"Selector evidence is malformed: {exc}"], source_evidence, source_label,
                provenance, raw_result_bytes, raw_zip_bytes)

    verdict = selector_evidence_mod.verify_selector_contract_evidence(
        result,
        expected_git_sha=expected_sha,
        expected_version=expected_version,
        expected_ref=expected_ref,
        expected_flutter_version=required_flutter,
        expected_release_id=expected_release_id,
    )
    if verdict.ok:
        detail = [
            f"Selector contract PASS for CaleeMobile {result.pubspec_version} @ {result.tested_sha} "
            f"({result.selectors_present}/{result.selectors_checked} selectors present, "
            f"Flutter {result.flutter_version}). Evidence source: {source_label}; "
            f"adopted by {adopted_by_label} for run {run_id}."
        ]
        _finish("passed", detail, verdict.problems, source_evidence, source_label, provenance,
                raw_result_bytes, raw_zip_bytes)
    _finish("blocked", [], verdict.problems, source_evidence, source_label, provenance,
            raw_result_bytes, raw_zip_bytes)


@main.command("record-distributed-build-acceptance")
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True,
    help="Shared release run ID. Evidence is recorded at "
         "reports/runs/<run-id>/distributed-build-acceptance/results.json.",
)
@click.option(
    "--provider", "live_provider", default=None,
    type=click.Choice(sorted([provider_evidence_mod.PROVIDER_APP_STORE_CONNECT, provider_evidence_mod.PROVIDER_PLAY_CONSOLE])),
    help="Priority 3: perform a LIVE authenticated collection from this provider's API right now, "
         "using credentials resolved via credentials.py (tier provider-api-live -- the strongest "
         "evidence tier). Requires --app-id/--build-version (app_store_connect) or "
         "--package-name/--track (play_console).",
)
@click.option("--app-id", default=None, help="App Store Connect app id (--provider app_store_connect).")
@click.option("--build-version", "asc_build_version", default=None, help="TestFlight/App Store Connect build/version number to match (--provider app_store_connect).")
@click.option("--package-name", default=None, help="Play Console package name (--provider play_console).")
@click.option("--track", "play_track", default=None, help="Play Console release track, e.g. 'internal' (--provider play_console).")
@click.option(
    "--play-edit-id", "play_edit_id", default=None,
    help="Priority 3: an existing Play Console edit session id, used strictly read-only (never created "
         "or deleted by this process). The Play Developer API only exposes track state through an edit "
         "session; supplying one you already control is the safe way to read it without risking another "
         "open edit. Mutually exclusive with --allow-create-play-edit-session.",
)
@click.option(
    "--allow-create-play-edit-session", "allow_create_play_edit_session", is_flag=True, default=False,
    help="Priority 3: explicit opt-in to create a TEMPORARY Play Console edit session for this read and "
         "delete it immediately afterward. Creating an edit can invalidate another edit already open for "
         "this app -- without --play-edit-id or this flag, Play live collection BLOCKS rather than "
         "silently creating one.",
)
@click.option(
    "--signed-export", "signed_export_payload_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Priority 3: path to a signed-export evidence PAYLOAD JSON (the unsigned canonical claim). "
         "Requires --export-signature. Produces tier verified-signed-export ONLY if the detached "
         "signature cryptographically verifies against the configured trusted public key -- a claim "
         "with no genuine signature is rejected outright, never recorded as a weaker pass.",
)
@click.option(
    "--export-signature", "signed_export_signature_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Path to the raw detached signature bytes over --signed-export's exact canonical payload bytes.",
)
@click.option("--signer-fingerprint", default=None, help="Human-readable operator label for the signer key, recorded alongside the VERIFIED key's own computed fingerprint for cross-reference only -- never itself trusted as the signer identity.")
@click.option(
    "--trusted-public-key-file", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Priority 4: DIAGNOSTIC-ONLY per-command override of the trusted public key. Its result is "
         "ALWAYS recorded as blocked-unverified (tier diagnostic-unpinned-key), never a release-gating "
         "PASS, however genuine the signature verification against it turns out to be -- use this only "
         "to troubleshoot a signature offline. A real PASS requires the key pinned in machine "
         "configuration (see --config) and resolved from CALEE_SIGNED_EXPORT_PUBLIC_KEY.",
)
@click.option(
    "--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(),
    help="Path to machine.local.yaml (defaults to config/machine.local.yaml) -- read for the pinned "
         "trusted_signed_export_public_key_sha256 fingerprint (--signed-export path only).",
)
@click.option(
    "--github-run-id", default=None,
    help="Priority 3: GitHub Actions workflow run id that produced a retained distributed-build-"
         "evidence artifact (tier github-authenticated-artifact) -- the CI run itself must have "
         "collected the nested evidence live via one of the collectors above; a hand-typed claim "
         "smuggled through an otherwise-real artifact is still rejected. Requires "
         "--github-artifact-id/--github-repository/--github-workflow-file/--github-artifact-name/"
         "--github-result-filename.",
)
@click.option("--github-artifact-id", default=None, help="GitHub Actions artifact id within --github-run-id.")
@click.option("--github-repository", default=None, help="owner/repo the run/artifact belong to.")
@click.option("--github-workflow-file", default=None, help="Expected workflow file path, e.g. .github/workflows/collect-distributed-build-evidence.yml.")
@click.option("--github-artifact-name", default=None, help="Expected artifact name.")
@click.option("--github-result-filename", default=None, help="Expected JSON filename inside the artifact ZIP.")
@click.option(
    "--github-artifact-zip", "github_artifact_zip_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="An already-downloaded artifact ZIP -- metadata (run/artifact ownership, digest) is still "
         "authenticated live via the GitHub API; an operator-supplied ZIP alone is never sufficient.",
)
@click.option(
    "--build-provenance-github-run-id", "bp_github_run_id", default=None,
    help="Priority 1: GitHub Actions workflow run id (in the CaleeMobile PRODUCT repository, not this "
         "one) that produced a retained build-provenance artifact proving the source Git SHA/version/"
         "platform build number a specific CI build produced. Combine with --provider/--github-run-id "
         "above to JOIN a provider observation with this build provenance -- neither side alone can ever "
         "PASS. Requires --build-provenance-github-artifact-id/-repository/-workflow-file/-artifact-name/"
         "-result-filename.",
)
@click.option("--build-provenance-github-artifact-id", "bp_github_artifact_id", default=None, help="GitHub Actions artifact id within --build-provenance-github-run-id.")
@click.option("--build-provenance-github-repository", "bp_github_repository", default=None, help="owner/repo the build-provenance run/artifact belong to (e.g. CaleeAdmin/CaleeMobile).")
@click.option("--build-provenance-github-workflow-file", "bp_github_workflow_file", default=None, help="Expected build-provenance workflow file path.")
@click.option("--build-provenance-github-artifact-name", "bp_github_artifact_name", default=None, help="Expected build-provenance artifact name.")
@click.option("--build-provenance-github-result-filename", "bp_github_result_filename", default=None, help="Expected JSON filename inside the build-provenance artifact ZIP.")
@click.option(
    "--build-provenance-github-artifact-zip", "bp_github_artifact_zip_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="An already-downloaded build-provenance artifact ZIP -- metadata is still authenticated live "
         "via the GitHub API.",
)
@click.option(
    "--build-provenance-signed-export", "bp_signed_export_payload_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Priority 1: path to a signed BUILD-PROVENANCE payload JSON (source Git SHA/version/platform "
         "build number). Requires --build-provenance-signature. Verified against the PINNED trust root "
         "(see --config), never a per-command key. Combine with --provider/--github-run-id above to JOIN.",
)
@click.option(
    "--build-provenance-signature", "bp_signature_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Path to the raw detached signature bytes over --build-provenance-signed-export's exact "
         "canonical payload bytes.",
)
@click.option(
    "--source", "source_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Records an operator-supplied evidence JSON file for audit purposes. This process never "
         "independently authenticates it -- it can NEVER produce a PASS, regardless of how "
         "well-formed/self-consistent the content looks (always recorded as blocked-unverified). Use "
         "--provider / --signed-export / --github-run-id above to reach a PASS.",
)
@click.option("--adopted-by", default=None, help="Who/what is adopting this evidence (defaults to the OS user).")
@click.option(
    "--channel", required=False, default=None,
    type=click.Choice(sorted(distributed_build_acceptance_mod.VALID_CHANNELS)),
    help="DEPRECATED legacy path (no --source/live mode): which distribution channel this manual claim is for.",
)
@click.option(
    "--distributed-build-id", "distributed_build_id", default=None,
    help="DEPRECATED legacy path: the distributed build's own identifier.",
)
@click.option("--tested-git-sha", "tested_git_sha", default=None, help="DEPRECATED legacy path: full 40-character CaleeMobile Git SHA.")
@click.option("--tested-version", "tested_version", default=None, help="DEPRECATED legacy path: CaleeMobile version.")
@click.option(
    "--verified-via", "verified_via", default=None,
    help="DEPRECATED legacy path: an operator-typed label. This can NEVER produce a PASS any more -- "
         "see --provider/--signed-export/--github-run-id. Retained only so a legacy/manual claim can "
         "still be recorded as an explicit, clearly-labelled blocked-unverified (or informational, "
         "when not required) component, never silently omitted.",
)
@click.option("--release-id", "release_id_opt", envvar="CALEE_RELEASE_ID", default=None, help="The release ID this evidence is bound to.")
@click.option("--expected-git-sha", "expected_git_sha", default=None, help="Expected CaleeMobile release SHA; a mismatch BLOCKS.")
@click.option("--expected-version", "expected_version", default=None, help="Expected CaleeMobile release version; a mismatch BLOCKS.")
@click.option("--expected-release-id", "expected_release_id", default=None, help="Expected release ID; defaults to this run's own release-config releaseId.")
def record_distributed_build_acceptance_cmd(
    run_id_opt, live_provider, app_id, asc_build_version, package_name, play_track,
    play_edit_id, allow_create_play_edit_session,
    signed_export_payload_path, signed_export_signature_path, signer_fingerprint, trusted_public_key_file,
    config_path,
    github_run_id, github_artifact_id, github_repository, github_workflow_file, github_artifact_name,
    github_result_filename, github_artifact_zip_path,
    bp_github_run_id, bp_github_artifact_id, bp_github_repository, bp_github_workflow_file,
    bp_github_artifact_name, bp_github_result_filename, bp_github_artifact_zip_path,
    bp_signed_export_payload_path, bp_signature_path,
    source_path, adopted_by, channel, distributed_build_id, tested_git_sha, tested_version,
    verified_via, release_id_opt, expected_git_sha, expected_version, expected_release_id,
):
    """Records distributed-build acceptance evidence for THIS release run
    (Priority 3): explicit, externally-verifiable proof that a distributed/
    TestFlight/store build's identity matches the release candidate.

    A PASS is produced ONLY from evidence THIS PROCESS itself independently
    authenticated: a live provider-API collection (``--provider``), a
    cryptographically-verified signed export (``--signed-export``), or an
    authenticated GitHub CI artifact whose nested content was itself
    live-collected (``--github-run-id``). Operator-supplied ``--source``
    JSON and the legacy ``--channel``/``--verified-via`` flags can, at best,
    record an explicit ``blocked-unverified`` component -- never a PASS, no
    matter how well-formed the content looks. With no evidence at all, the
    component stays BLOCKED.
    """
    from . import distributed_build_provenance as dbp_mod
    pe_mod = provider_evidence_mod

    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
    workspace.ensure_created()
    report_path = workspace.component_report_path("distributed-build-acceptance")

    if expected_release_id is None:
        release_config_dict = _load_release_config_dict(workspace)
        if release_config_dict is not None:
            expected_release_id = release_config_dict.get("releaseId")

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    provider_side_modes = [
        name for name, given in (
            ("--provider", live_provider is not None), ("--github-run-id", github_run_id is not None),
        ) if given
    ]
    if len(provider_side_modes) > 1:
        click.echo(f"Only one of --provider / --github-run-id (the provider side) may be used at a time; got {provider_side_modes}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    build_side_modes = [
        name for name, given in (
            ("--build-provenance-github-run-id", bp_github_run_id is not None),
            ("--build-provenance-signed-export", bp_signed_export_payload_path is not None),
        ) if given
    ]
    if len(build_side_modes) > 1:
        click.echo(
            f"Only one of --build-provenance-github-run-id / --build-provenance-signed-export (the "
            f"build-provenance side) may be used at a time; got {build_side_modes}.", err=True,
        )
        raise SystemExit(EXIT_INVALID_CONFIG)
    provider_side_requested = bool(provider_side_modes)
    build_side_requested = bool(build_side_modes)
    if signed_export_payload_path is not None and (provider_side_requested or build_side_requested):
        click.echo(
            "Only one of --signed-export (a complete, independently-signed distributed-build claim) or "
            "--provider/--github-run-id/--build-provenance-* (which build a JOINED identity chain from "
            "two separately-authenticated sides) may be used at a time.",
            err=True,
        )
        raise SystemExit(EXIT_INVALID_CONFIG)
    if play_edit_id is not None and allow_create_play_edit_session:
        click.echo(
            "Only one of --play-edit-id (read-only against an edit you already control) or "
            "--allow-create-play-edit-session (creates and deletes a temporary one) may be used at a time.",
            err=True,
        )
        raise SystemExit(EXIT_INVALID_CONFIG)

    _UNSET = object()

    def _record_authenticated(
        evidence: dict, *, raw_bytes: bytes, evidence_tier: str, source_label: str, verify_version=_UNSET,
    ) -> None:
        # Priority 1/2: the distributed-build identity JOIN's testedVersion
        # is the MARKETING-only part (build_provenance.application_version)
        # -- --expected-version is the CaleeMobile FULL "marketing+build"
        # form, and the join already verified both parts separately (via
        # identity_format.split_marketing_version_and_build_number). Re-
        # checking the full form against the marketing-only value here would
        # spuriously fail a join that already passed, so the join call site
        # passes ``verify_version=None`` to skip this redundant (and, for
        # that shape, incorrect) re-check; every other tier's evidence still
        # carries the version in the SAME full form --expected-version uses,
        # so they keep the default (this closure's own expected_version).
        version_to_verify = expected_version if verify_version is _UNSET else verify_version
        adopted_by_label = adopted_by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        record = dbp_mod.build_provenance_record(
            evidence, release_run_id=run_id_opt, adopted_at=timestamp, adopted_by=adopted_by_label,
            source_path=source_label, raw_source_bytes=raw_bytes, evidence_tier=evidence_tier,
        )
        problems = dbp_mod.verify_provenance_record(
            record, source_bytes=raw_bytes, expected_release_run_id=run_id_opt,
            expected_git_sha=expected_git_sha, expected_version=version_to_verify,
            expected_release_id=expected_release_id,
        )
        if evidence_tier not in pe_mod.AUTHENTICATED_TIERS:
            # Defence in depth: every call site below only ever passes an
            # authenticated tier, but this keeps the CLI's own immediate
            # verdict correct even if that ever changes, matching
            # consolidated_report.py's independent re-check at consolidation.
            problems = list(problems) + [
                f"evidence tier {evidence_tier!r} is not an independently-authenticated tier."
            ]
        status = "passed" if not problems else "blocked"
        report = {
            "runId": run_id_opt,
            "component": dbp_mod.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
            "status": status,
            "evidenceTier": evidence_tier,
            "provenance": record,
            "problems": list(problems),
            "generatedAt": timestamp,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        component_dir = workspace.component_dir("distributed-build-acceptance")
        dbp_mod.write_evidence_bundle(component_dir, record, source_bytes=raw_bytes)
        for problem in problems:
            click.echo(f"  - {problem}", err=status != "passed")
        if status == "passed":
            ev = record["sourceEvidence"]
            click.echo(
                f"Distributed-build acceptance PASS via {ev.get('provider')}/{ev.get('generatedBy')} "
                f"[{evidence_tier}] ({ev.get('channel')} build {ev.get('distributedBuildId')}) for "
                f"CaleeMobile {ev.get('testedVersion')} @ {ev.get('testedGitSha')}."
            )
        else:
            click.echo("Distributed-build acceptance evidence REJECTED: " + "; ".join(problems))
        click.echo(f"Recorded: {report_path}")
        raise SystemExit(EXIT_SUCCESS if status == "passed" else EXIT_BLOCKED)

    def _collect_provider_side() -> "tuple[dict, bytes, str]":
        """Priority 1: acquire the PROVIDER OBSERVATION half of the identity
        chain, from whichever of --provider (live collection) /
        --github-run-id (CI artifact) was requested. Returns (evidence,
        raw_bytes, source_label); exits directly (INVALID_CONFIG/BLOCKED) on
        failure, matching every other collection path in this command."""
        if live_provider is not None:
            try:
                if live_provider == pe_mod.PROVIDER_APP_STORE_CONNECT:
                    if not app_id or not asc_build_version:
                        click.echo("--provider app_store_connect requires --app-id and --build-version.", err=True)
                        raise SystemExit(EXIT_INVALID_CONFIG)
                    record = pe_mod.collect_app_store_connect_evidence(
                        app_id=app_id, build_version=asc_build_version, requested_git_sha=expected_git_sha,
                        requested_version=expected_version, release_id=expected_release_id,
                        collection_run_id=run_id_opt,
                    )
                else:
                    if not package_name or not play_track:
                        click.echo("--provider play_console requires --package-name and --track.", err=True)
                        raise SystemExit(EXIT_INVALID_CONFIG)
                    record = pe_mod.collect_play_console_evidence(
                        package_name=package_name, track=play_track, requested_git_sha=expected_git_sha,
                        requested_version=expected_version, release_id=expected_release_id,
                        collection_run_id=run_id_opt,
                        edit_id=play_edit_id, allow_create_edit_session=allow_create_play_edit_session,
                    )
            except pe_mod.ProviderEvidenceError as exc:
                click.echo(f"Distributed-build acceptance BLOCKED: {exc}", err=True)
                raise SystemExit(EXIT_BLOCKED)
            return record.to_provider_observation_dict(), record.raw_response_bytes, f"live:{live_provider}"

        # --github-run-id: an authenticated CI artifact whose contained
        # result is itself a provider observation.
        required = {
            "--github-artifact-id": github_artifact_id, "--github-repository": github_repository,
            "--github-workflow-file": github_workflow_file, "--github-artifact-name": github_artifact_name,
            "--github-result-filename": github_result_filename,
        }
        missing_github = [name for name, value in required.items() if not value]
        if missing_github:
            click.echo(f"--github-run-id requires {sorted(required)}. Missing: {missing_github}.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        try:
            chain = pe_mod.acquire_provider_ci_artifact(
                repository=github_repository, workflow_path=github_workflow_file, run_id=github_run_id,
                artifact_id=github_artifact_id, expected_artifact_name=github_artifact_name,
                expected_result_filename=github_result_filename, local_zip_path=github_artifact_zip_path,
                expected_release_id=expected_release_id,
            )
        except pe_mod.ProviderEvidenceError as exc:
            click.echo(f"Distributed-build acceptance BLOCKED: {exc}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        if not chain.ok:
            click.echo("Distributed-build acceptance BLOCKED -- provider CI artifact chain rejected:", err=True)
            for problem in chain.problems:
                click.echo(f"  - {problem}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        return chain.result, chain.result_bytes, f"github-artifact:{github_repository}#run={github_run_id}&artifact={github_artifact_id}"

    def _collect_build_provenance_side():
        """Priority 1: acquire the BUILD PROVENANCE half of the identity
        chain, from whichever of --build-provenance-github-run-id /
        --build-provenance-signed-export was requested. Returns
        (BuildProvenanceRecord, raw_bytes, source_label); exits directly on
        failure."""
        if bp_github_run_id is not None:
            required = {
                "--build-provenance-github-artifact-id": bp_github_artifact_id,
                "--build-provenance-github-repository": bp_github_repository,
                "--build-provenance-github-workflow-file": bp_github_workflow_file,
                "--build-provenance-github-artifact-name": bp_github_artifact_name,
                "--build-provenance-github-result-filename": bp_github_result_filename,
            }
            missing_bp = [name for name, value in required.items() if not value]
            if missing_bp:
                click.echo(f"--build-provenance-github-run-id requires {sorted(required)}. Missing: {missing_bp}.", err=True)
                raise SystemExit(EXIT_INVALID_CONFIG)
            try:
                chain = build_provenance_mod.acquire_build_provenance_artifact(
                    repository=bp_github_repository, workflow_path=bp_github_workflow_file, run_id=bp_github_run_id,
                    artifact_id=bp_github_artifact_id, expected_artifact_name=bp_github_artifact_name,
                    expected_result_filename=bp_github_result_filename, local_zip_path=bp_github_artifact_zip_path,
                )
            except build_provenance_mod.BuildProvenanceError as exc:
                click.echo(f"Distributed-build acceptance BLOCKED: {exc}", err=True)
                raise SystemExit(EXIT_BLOCKED)
            if not chain.ok:
                click.echo("Distributed-build acceptance BLOCKED -- build-provenance CI artifact chain rejected:", err=True)
                for problem in chain.problems:
                    click.echo(f"  - {problem}", err=True)
                raise SystemExit(EXIT_BLOCKED)
            return chain.record, chain.record.raw_bytes, f"build-provenance-github-artifact:{bp_github_repository}#run={bp_github_run_id}&artifact={bp_github_artifact_id}"

        # --build-provenance-signed-export
        if bp_signature_path is None:
            click.echo("--build-provenance-signed-export requires --build-provenance-signature.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        try:
            bp_payload = json.loads(Path(bp_signed_export_payload_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            click.echo(f"--build-provenance-signed-export {bp_signed_export_payload_path!r} is not valid JSON: {exc}", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        if not isinstance(bp_payload, dict):
            click.echo(f"--build-provenance-signed-export {bp_signed_export_payload_path!r} must contain a JSON object.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        bp_signature_bytes = Path(bp_signature_path).read_bytes()
        # Priority 4: the SAME pinned trust root as the complete --signed-export
        # path -- never a per-command key.
        default_machine_config_path = REPO_ROOT / "config" / "machine.local.yaml"
        machine_config_file = Path(config_path) if config_path else default_machine_config_path
        pinned_fingerprint = None
        if machine_config_file.is_file():
            from . import machine_config as machine_config_mod

            try:
                pinned_fingerprint = machine_config_mod.load_machine_config(
                    machine_config_file
                ).trusted_signed_export_public_key_sha256
            except machine_config_mod.MachineConfigError as exc:
                click.echo(f"Distributed-build acceptance BLOCKED: could not load machine config: {exc}", err=True)
                raise SystemExit(EXIT_BLOCKED)
        try:
            trusted_public_key_pem, _fingerprint = pe_mod.resolve_pinned_trusted_public_key(pinned_fingerprint=pinned_fingerprint)
        except pe_mod.ProviderEvidenceError as exc:
            click.echo(f"Distributed-build acceptance BLOCKED: {exc}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        record, sig_problems = build_provenance_mod.build_signed_build_provenance(
            payload=bp_payload, signature_bytes=bp_signature_bytes, trusted_public_key_pem=trusted_public_key_pem,
        )
        if sig_problems:
            click.echo("Distributed-build acceptance BLOCKED -- build-provenance signed-export signature verification failed:", err=True)
            for problem in sig_problems:
                click.echo(f"  - {problem}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        return record, record.raw_bytes, f"build-provenance-signed-export:{bp_signed_export_payload_path}"

    if signed_export_payload_path is not None:
        # Priority 3/4: a real detached signature cryptographically verified
        # against a trust root. The trust root MUST be the machine-config-
        # pinned key (tier verified-signed-export -- the only path that can
        # PASS); --trusted-public-key-file is diagnostic-only (handled
        # first, below, and always ends in blocked-unverified).
        if signed_export_signature_path is None:
            click.echo("--signed-export requires --export-signature.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        try:
            payload = json.loads(Path(signed_export_payload_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            click.echo(f"--signed-export {signed_export_payload_path!r} is not valid JSON: {exc}", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        if not isinstance(payload, dict):
            click.echo(f"--signed-export {signed_export_payload_path!r} must contain a JSON object.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        signature_bytes = Path(signed_export_signature_path).read_bytes()

        if trusted_public_key_file:
            # Priority 4: an explicit per-command public-key override is
            # DIAGNOSTIC ONLY -- it can NEVER produce a release-gating PASS,
            # however genuine the signature verification against it turns
            # out to be, and it never consults (or affects) the pinned trust
            # root below.
            click.echo(
                click.style(
                    "WARNING: --trusted-public-key-file is a DIAGNOSTIC override of the trust root for "
                    "this run only. Its result is ALWAYS recorded as blocked-unverified, never a "
                    "release-gating PASS, regardless of whether the signature verifies.",
                    fg="yellow",
                ),
                err=True,
            )
            diagnostic_pem = Path(trusted_public_key_file).read_text(encoding="utf-8")
            evidence, sig_problems = pe_mod.build_signed_export_evidence(
                payload=payload, signature_bytes=signature_bytes, trusted_public_key_pem=diagnostic_pem,
                signer_fingerprint=signer_fingerprint,
            )
            recorded_payload = evidence or dict(payload, generatedBy="signed-export")
            raw_bytes = json.dumps(recorded_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            adopted_by_label = adopted_by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
            record = dbp_mod.build_provenance_record(
                recorded_payload, release_run_id=run_id_opt, adopted_at=timestamp, adopted_by=adopted_by_label,
                source_path=f"signed-export-diagnostic-unpinned-key:{signed_export_payload_path}",
                raw_source_bytes=raw_bytes, evidence_tier=pe_mod.TIER_DIAGNOSTIC_UNPINNED_KEY,
            )
            diagnostic_notice = (
                "--trusted-public-key-file is a diagnostic override -- its result can never be a "
                "release-gating PASS. Configure trusted_signed_export_public_key_sha256 in machine "
                "config and CALEE_SIGNED_EXPORT_PUBLIC_KEY to reach verified-signed-export."
            )
            report = {
                "runId": run_id_opt,
                "component": dbp_mod.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
                "status": "blocked-unverified",
                "evidenceTier": pe_mod.TIER_DIAGNOSTIC_UNPINNED_KEY,
                "provenance": record,
                "problems": [diagnostic_notice] + list(sig_problems),
                "generatedAt": timestamp,
            }
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            component_dir = workspace.component_dir("distributed-build-acceptance")
            dbp_mod.write_evidence_bundle(component_dir, record, source_bytes=raw_bytes)
            click.echo(diagnostic_notice, err=True)
            for problem in sig_problems:
                click.echo(f"  - {problem}", err=True)
            click.echo(f"Recorded (blocked-unverified): {report_path}")
            raise SystemExit(EXIT_BLOCKED)

        # Priority 4: the ONLY path that may ever reach verified-signed-export
        # -- the trust key is resolved EXCLUSIVELY from credentials.py
        # (environment/Keychain), gated on machine config's PINNED SHA-256
        # fingerprint (never a per-command override).
        default_machine_config_path = REPO_ROOT / "config" / "machine.local.yaml"
        machine_config_file = Path(config_path) if config_path else default_machine_config_path
        pinned_fingerprint = None
        if machine_config_file.is_file():
            from . import machine_config as machine_config_mod

            try:
                pinned_fingerprint = machine_config_mod.load_machine_config(
                    machine_config_file
                ).trusted_signed_export_public_key_sha256
            except machine_config_mod.MachineConfigError as exc:
                click.echo(f"Distributed-build acceptance BLOCKED: could not load machine config: {exc}", err=True)
                raise SystemExit(EXIT_BLOCKED)
        try:
            trusted_public_key_pem, _verified_fingerprint = pe_mod.resolve_pinned_trusted_public_key(
                pinned_fingerprint=pinned_fingerprint,
            )
        except pe_mod.ProviderEvidenceError as exc:
            click.echo(f"Distributed-build acceptance BLOCKED: {exc}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        evidence, sig_problems = pe_mod.build_signed_export_evidence(
            payload=payload, signature_bytes=signature_bytes, trusted_public_key_pem=trusted_public_key_pem,
            signer_fingerprint=signer_fingerprint,
        )
        if sig_problems:
            click.echo("Distributed-build acceptance BLOCKED -- signed-export signature verification failed:", err=True)
            for problem in sig_problems:
                click.echo(f"  - {problem}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        raw_bytes = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        _record_authenticated(
            evidence, raw_bytes=raw_bytes, evidence_tier=pe_mod.TIER_VERIFIED_SIGNED_EXPORT,
            source_label=f"signed-export:{signed_export_payload_path}",
        )
        return

    if provider_side_requested:
        # Priority 1: collect/authenticate the provider side FIRST -- flag
        # validation and credential/chain BLOCKED reasons take priority over
        # the "other side is missing" message below, exactly like every
        # other collection path in this command reports ITS OWN problem
        # before anything else.
        provider_evidence, provider_raw_bytes, provider_source_label = _collect_provider_side()
        if not build_side_requested:
            click.echo(
                "Distributed-build acceptance BLOCKED: build provenance unavailable -- a provider "
                "observation alone can never prove source Git identity (Priority 1). Supply "
                "--build-provenance-github-run-id (+ related flags) or --build-provenance-signed-export "
                "(+ --build-provenance-signature) to complete the identity chain.",
                err=True,
            )
            raise SystemExit(EXIT_BLOCKED)
        build_record, build_raw_bytes, build_source_label = _collect_build_provenance_side()
        verdict = build_provenance_mod.join_provider_and_build_provenance(
            provider_evidence, build_record,
            expected_release_config_git_sha=expected_git_sha, expected_release_config_version=expected_version,
            expected_release_id=expected_release_id,
        )
        if not verdict.ok:
            click.echo("Distributed-build acceptance BLOCKED -- identity chain join rejected:", err=True)
            for problem in verdict.problems:
                click.echo(f"  - {problem}", err=True)
            raise SystemExit(EXIT_BLOCKED)

        # Priority 1.9: both source bundles (provider + build provenance),
        # not just the merged/joined view, are retained in the component
        # directory -- and so end up in the release evidence ZIP alongside
        # everything else write_evidence_bundle below writes.
        component_dir = workspace.component_dir("distributed-build-acceptance")
        component_dir.mkdir(parents=True, exist_ok=True)
        (component_dir / "provider-observation-raw.bin").write_bytes(provider_raw_bytes)
        (component_dir / "build-provenance-raw.bin").write_bytes(build_raw_bytes or b"")

        joined_raw_bytes = json.dumps(
            verdict.evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        _record_authenticated(
            verdict.evidence, raw_bytes=joined_raw_bytes, evidence_tier=pe_mod.TIER_PROVIDER_BUILD_PROVENANCE_JOIN,
            source_label=f"joined:{provider_source_label}+{build_source_label}", verify_version=None,
        )
        return

    if build_side_requested:
        # Build-provenance side given, but no provider side at all -- collect/
        # authenticate it (so ITS OWN flag/credential/chain problems are
        # reported first, same principle as above), then BLOCK: build
        # provenance alone can never prove a store actually distributed this
        # build.
        _collect_build_provenance_side()
        click.echo(
            "Distributed-build acceptance BLOCKED: provider observation unavailable -- build provenance "
            "alone can never prove a store actually distributed this build (Priority 1). Supply "
            "--provider (+ related flags) or --github-run-id to complete the identity chain.",
            err=True,
        )
        raise SystemExit(EXIT_BLOCKED)

    if source_path:
        # Priority 3: arbitrary operator-supplied JSON is NEVER independently
        # authenticated by this process -- at best it is recorded as an
        # explicit, clearly-labelled blocked-unverified claim, exactly like
        # the legacy --channel/... path below. This can NEVER reach PASS,
        # regardless of how well-formed/self-consistent the content looks
        # (evidenceTier is hardcoded to manual-unverified here -- never read
        # from the file's own content -- and consolidated_report.py
        # independently re-derives the same block at consolidation, so even
        # a hand-edited report.json claiming a stronger tier is caught).
        raw_bytes = Path(source_path).read_bytes()
        try:
            claimed_evidence = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            click.echo(f"--source {source_path!r} is not valid JSON: {exc}", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        if not isinstance(claimed_evidence, dict):
            click.echo(f"--source {source_path!r} must contain a JSON object.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)

        adopted_by_label = adopted_by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
        record = dbp_mod.build_provenance_record(
            claimed_evidence, release_run_id=run_id_opt, adopted_at=timestamp, adopted_by=adopted_by_label,
            source_path=str(source_path), raw_source_bytes=raw_bytes, evidence_tier=pe_mod.TIER_MANUAL_UNVERIFIED,
        )
        content_problems = dbp_mod.verify_provenance_record(
            record, source_bytes=raw_bytes, expected_release_run_id=run_id_opt,
            expected_git_sha=expected_git_sha, expected_version=expected_version,
            expected_release_id=expected_release_id,
        )
        unverified_notice = (
            "Operator-supplied --source evidence is never independently authenticated by this process -- "
            "at best it is recorded as blocked-unverified, regardless of how well-formed it looks. Use "
            "--provider (live API), --signed-export (verified signature), or --github-run-id (authenticated "
            "CI artifact) to reach a PASS."
        )
        report = {
            "runId": run_id_opt,
            "component": dbp_mod.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
            "status": "blocked-unverified",
            "evidenceTier": pe_mod.TIER_MANUAL_UNVERIFIED,
            "provenance": record,
            "problems": [unverified_notice] + list(content_problems),
            "generatedAt": timestamp,
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        component_dir = workspace.component_dir("distributed-build-acceptance")
        dbp_mod.write_evidence_bundle(component_dir, record, source_bytes=raw_bytes)
        click.echo(unverified_notice, err=True)
        for problem in content_problems:
            click.echo(f"  - {problem}", err=True)
        click.echo(f"Recorded (blocked-unverified): {report_path}")
        raise SystemExit(EXIT_BLOCKED)

    # Legacy manual path (DEPRECATED): can never reach PASS -- see the module
    # docstring and consolidated_report.component_from_distributed_build_
    # acceptance_report. Retained so a technical owner's claim is still
    # recorded (as explicit blocked-unverified evidence), never silently
    # dropped, rather than removed outright.
    missing = [
        name for name, value in (
            ("--channel", channel), ("--distributed-build-id", distributed_build_id),
            ("--tested-git-sha", tested_git_sha), ("--tested-version", tested_version),
            ("--verified-via", verified_via),
        ) if not value
    ]
    if missing:
        click.echo(
            "Either --provider/--signed-export/--github-run-id (a live-authenticated path -- the only "
            "way to reach a PASS), --source (recorded but always blocked-unverified), or ALL of "
            f"{['--channel', '--distributed-build-id', '--tested-git-sha', '--tested-version', '--verified-via']} "
            f"(the deprecated, always-blocked-unverified manual path) must be given. Missing: {missing}.",
            err=True,
        )
        raise SystemExit(EXIT_INVALID_CONFIG)

    result = distributed_build_acceptance_mod.DistributedBuildAcceptanceResult(
        schema_version=distributed_build_acceptance_mod.DISTRIBUTED_BUILD_ACCEPTANCE_SCHEMA_VERSION,
        component=distributed_build_acceptance_mod.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
        channel=channel,
        distributed_build_id=distributed_build_id,
        tested_git_sha=tested_git_sha,
        tested_version=tested_version,
        verified_via=verified_via,
        release_id=release_id_opt,
        timestamp=timestamp,
    )
    # Still run the well-formedness/identity checks so a technical owner sees
    # exactly what's wrong -- but the OUTCOME can never be better than
    # blocked-unverified, regardless of verdict.ok (Priority 3: "deprecate
    # direct PASS creation from identity flags alone").
    verdict = distributed_build_acceptance_mod.verify_distributed_build_acceptance_evidence(
        result, expected_git_sha=expected_git_sha, expected_version=expected_version,
        expected_release_id=expected_release_id,
    )
    deprecation_notice = (
        "Manual/self-declared distributed-build evidence is DEPRECATED and can never PASS -- "
        "provide an authenticated provenance evidence file via --source."
    )
    report = {
        "runId": run_id_opt,
        "component": distributed_build_acceptance_mod.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
        "status": "blocked-unverified",
        "evidence": result.to_dict(),
        "problems": [deprecation_notice] + list(verdict.problems),
        "generatedAt": timestamp,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    click.echo(deprecation_notice, err=True)
    for problem in verdict.problems:
        click.echo(f"  - {problem}", err=True)
    click.echo(f"Recorded (blocked-unverified): {report_path}")
    raise SystemExit(EXIT_BLOCKED)


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
        workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
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


def _load_release_config_dict(workspace: run_context.RunWorkspace) -> "dict | None":
    """This run's own already-composed release-config evidence
    (reports/runs/<run-id>/release-config/results.json), or None when no
    release-config has been composed for this run (a bare/dev invocation) --
    used throughout Priority 2 to prefer a schema-v2 run's own composed
    scope over config/release-platforms.yaml."""
    path = workspace.component_report_path("release-config")
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _v2_platforms_features_expected(release_config_composition: dict):
    """Priority 2: derive consolidate/selector-contract's platforms/features/
    expected-identity inputs from THIS run's own already-composed schema-v2
    release-config evidence, instead of config/release-platforms.yaml. The
    release bundle manifest is self-contained and authoritative for release
    scope once a schema-v2 release-config has been composed -- the legacy
    file is not consulted at all for such a run (Priority 2)."""
    selections = release_config_composition.get("releaseSelections") or {}
    enabled_platforms = set(selections.get("enabledPlatforms") or [])
    enabled_features = set(selections.get("enabledFeatures") or [])
    expected = selections.get("expectedIdentities") or {}
    expected_calee = expected.get("calee") or {}
    expected_shell = expected.get("caleeShell") or {}
    expected_mobile = expected.get("caleeMobile") or {}

    platforms = release_platforms.ReleasePlatforms(
        tablet="tablet" in enabled_platforms,
        mobile_android="android" in enabled_platforms,
        mobile_ios="ios" in enabled_platforms,
        source="release-bundle-manifest (schemaVersion 2)",
    )
    features = release_platforms.ReleaseFeatures(
        synchronization="synchronization" in enabled_features,
        meals="meals" in enabled_features,
        onboarding="onboarding" in enabled_features,
        google_calendar="google_calendar" in enabled_features,
        kiosk_admin="kiosk_admin" in enabled_features,
        source="release-bundle-manifest (schemaVersion 2)",
    )
    expected_identity = release_platforms.ExpectedBuildIdentity(
        calee_build_version=expected_calee.get("buildVersion"),
        calee_git_sha=expected_calee.get("gitSha"),
        calee_application_id=expected_calee.get("applicationId"),
        calee_version_code=expected_calee.get("versionCode"),
        caleemobile_build_version=expected_mobile.get("buildVersion"),
        caleemobile_git_sha=expected_mobile.get("gitSha"),
        caleeshell_version=expected_shell.get("version"),
        # Schema v2's own profile controls production/development policy --
        # a v2 release never falls back to the legacy file's `production:`
        # flag (Priority 2 requirement 5/6).
        production=(selections.get("profile") == "production"),
        source="release-bundle-manifest (schemaVersion 2)",
    )
    return platforms, features, expected_identity


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
@click.option("--installation-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's installation/results.json (bundle verify + APK inspection + install)")
@click.option("--machine-config-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's machine-config/results.json (secrets-excluded snapshot)")
@click.option("--release-config-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's release-config/results.json (machine + release-candidate composition)")
@click.option("--kiosk-report", type=click.Path(exists=True), default=None, help="Override: defaults to this run's kiosk-admin/results.json (kiosk/admin feature evidence)")
@click.option("--manual-checks", "manual_checks_path", type=click.Path(exists=True), default=None, help="Override: defaults to this run's manual-checks/results.json")
@click.option(
    "--environment-report", type=click.Path(exists=True), default=None,
    help="Override: defaults to this run's environment/results.json (prepare's output).",
)
@click.option(
    "--android-mandatory/--android-optional", "android_mandatory", default=None,
    help="Whether the Android UI report is release-gating. Defaults to this run's own schema-v2 "
         "release-config when one was composed, else config/release-platforms.yaml (mobile_android), "
         "or True if neither is available.",
)
@click.option(
    "--ios-mandatory/--ios-optional", "ios_mandatory", default=None,
    help="Whether the iPhone UI report is release-gating. Defaults to this run's own schema-v2 "
         "release-config when one was composed, else config/release-platforms.yaml (mobile_ios), or "
         "True if neither is available.",
)
@click.option(
    "--sync-mandatory/--sync-optional", "sync_mandatory", default=None,
    help="Whether cross-device synchronization is release-gating. Defaults to this run's own "
         "schema-v2 release-config when one was composed, else config/release-platforms.yaml "
         "(release_features.synchronization), or True if neither is available. An optional sync is "
         "still shown in the report, just never blocks a PASS.",
)
@click.option(
    "--selector-contract-mandatory/--selector-contract-optional", "selector_contract_mandatory", default=None,
    help="Whether CaleeMobile selector evidence is release-gating (Priority 1). The full "
         "launcher passes --selector-contract-mandatory. When omitted, the component is still "
         "auto-included as mandatory if a selector-contract report exists for this run; a release "
         "can never PASS without valid selector evidence for the exact CaleeMobile build.",
)
@click.option(
    "--distributed-build-acceptance-mandatory/--distributed-build-acceptance-optional",
    "distributed_build_acceptance_mandatory", default=None,
    help="Whether distributed-build acceptance evidence is release-gating (Priority 3). Defaults "
         "to this run's own schema-v2 release-config (caleeMobile.distributedBuildAcceptanceRequired), "
         "recorded as an explicit not-required component when false. With no release-config "
         "composed for this run at all, the component does not apply and is omitted.",
)
@click.option(
    "--installation-mandatory/--installation-optional", "installation_mandatory", default=None,
    help="Whether tablet release installation is release-gating (Priority 6). The full launcher "
         "passes --installation-mandatory; when omitted it is auto-included as mandatory if an "
         "installation report exists. Installation BLOCKED/FAILED can never read as a release PASS.",
)
@click.option(
    "--machine-config-mandatory/--machine-config-optional", "machine_config_mandatory", default=None,
    help="Whether the machine-config snapshot is release-gating (Priority 4). Auto-included as "
         "mandatory if a machine-config snapshot exists for this run.",
)
@click.option(
    "--release-config-mandatory/--release-config-optional", "release_config_mandatory", default=None,
    help="Whether the release-config composition (machine + release-candidate) is release-gating "
         "(Priority 1/3). The full launcher passes --release-config-mandatory whenever a machine "
         "config is present; when omitted it is auto-included as mandatory if a release-config "
         "report exists. A BLOCKED/missing release-config composition can never read as a release "
         "PASS, and no product test may run once it is BLOCKED.",
)
@click.option(
    "--subscribed-fixture-mandatory/--subscribed-fixture-optional", "subscribed_fixture_mandatory", default=None,
    help="Whether the subscribed-calendar fixture component is release-gating (Priority 7). When "
         "omitted, this is derived automatically from scenarios/promotion/subscribed_calendar.yaml's "
         "releaseSuiteEligible -- optional while that scenario stays draft-unverified, and "
         "automatically mandatory once it is promoted. A component report is still shown/recorded "
         "either way.",
)
# Independent release-feature gating (Workstream 3). Each defaults to this
# run's own schema-v2 release-config's enabled-features scope when one was
# composed, else the legacy release feature profile (config/release-
# platforms.yaml release_features.*), or True if neither is available -- an
# omitted feature is mandatory. An optional feature is still shown as an
# explicit component, just never blocks a PASS.
@click.option("--meals-mandatory/--meals-optional", "meals_mandatory", default=None,
              help="Whether the CaleeMobile Meals feature is release-gating. Defaults to this run's "
                   "schema-v2 release-config when composed, else release_features.meals.")
@click.option("--onboarding-mandatory/--onboarding-optional", "onboarding_mandatory", default=None,
              help="Whether onboarding + display/mobile handoff is release-gating. Defaults to this "
                   "run's schema-v2 release-config when composed, else release_features.onboarding.")
@click.option("--google-calendar-mandatory/--google-calendar-optional", "google_calendar_mandatory", default=None,
              help="Whether Google Calendar connection is release-gating. Defaults to this run's "
                   "schema-v2 release-config when composed, else release_features.google_calendar.")
@click.option("--kiosk-admin-mandatory/--kiosk-admin-optional", "kiosk_admin_mandatory", default=None,
              help="Whether CaleeShell kiosk/admin is release-gating. Defaults to this run's schema-v2 "
                   "release-config when composed, else release_features.kiosk_admin.")
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
@click.option("--production/--development", "production", default=None, help="Production release profile (Workstream 3): the expected identity below becomes REQUIRED, and a dirty tree needs a named waiver. Defaults to this run's schema-v2 release-config when composed, else config/release-platforms.yaml (expected_build_identity.production).")
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
@click.option("--allow-dirty/--no-allow-dirty", "allow_dirty_opt", default=None, help="Explicitly approve testing an uncommitted build. Defaults to this run's schema-v2 release-config when composed, else config/release-platforms.yaml (expected_build_identity.allow_dirty).")
@click.option("--android-device-id", default=None, help="Android device/emulator identifier used for the Android UI run")
@click.option("--ios-device-id", default=None, help="iOS device/simulator identifier used for the iOS UI run")
@click.option("--test-environment", default="", help="Target environment URL")
@click.option("--tester", default="", help="Tester name/identifier")
@click.option("--out-dir", type=click.Path(), default=None, help="Where to write the consolidated bundle (default: this run's workspace)")
def consolidate(
    run_id_opt, tablet_report, mobile_api_report, mobile_android_report, mobile_ios_report,
    sync_report, installation_report, machine_config_report, release_config_report, kiosk_report, manual_checks_path, environment_report,
    android_mandatory, ios_mandatory, sync_mandatory,
    selector_contract_mandatory, installation_mandatory, machine_config_mandatory, release_config_mandatory,
    subscribed_fixture_mandatory, distributed_build_acceptance_mandatory,
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
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id)
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
    installation = resolve("installation", installation_report)
    machine_config_snapshot = resolve("machine-config", machine_config_report)
    release_config_composition = resolve("release-config", release_config_report)
    kiosk = resolve("kiosk-admin", kiosk_report)
    # CaleeMobile selector contract (Priority 1/2). Auto-discovered from this
    # run's workspace and run-ID-validated like every other component. The
    # gating decision (mandatory in any mobile release, unconditionally so in
    # production) is deferred below, once the mobile scope, production profile
    # and any named waiver are resolved.
    selector_contract_report = resolve("selector-contract", None)
    # Subscribed-calendar fixture (Priority 7). Auto-discovered the same way;
    # its mandatory-ness (below) defaults to the scenario's promotion state.
    subscribed_fixture_report = resolve("subscribed-fixture", None)
    # Distributed-build acceptance (Priority 3). Auto-discovered the same way;
    # its mandatory-ness (below) defaults to this run's release-config
    # composition (caleeMobile.distributedBuildAcceptanceRequired).
    distributed_build_acceptance_report = resolve("distributed-build-acceptance", None)

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

    # Priority 2: once a schema-v2 release-config has been composed for this
    # run, config/release-platforms.yaml is no longer consulted at all -- a
    # malformed legacy file must never block a valid schema-v2 bundle, and
    # the bundle's own platform/feature/expected-identity scope is what
    # controls the rest of this command, not the legacy file's.
    release_config_is_v2 = (
        release_config_composition is not None
        and release_config_composition.get("schemaVersion") == 2
    )
    try:
        platforms = release_platforms.load_release_platforms()
        features = release_platforms.load_release_features()
        expected_identity = release_platforms.load_expected_build_identity()
    except release_platforms.ReleasePlatformsError as exc:
        if release_config_is_v2:
            platforms = release_platforms.ReleasePlatforms()
            features = release_platforms.ReleaseFeatures()
            expected_identity = release_platforms.ExpectedBuildIdentity()
        else:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
    if release_config_is_v2:
        platforms, features, expected_identity = _v2_platforms_features_expected(release_config_composition)
    android_gating = android_mandatory if android_mandatory is not None else platforms.mobile_android
    ios_gating = ios_mandatory if ios_mandatory is not None else platforms.mobile_ios
    # Cross-device synchronization gating (Workstream 1): explicit
    # --sync-mandatory/--sync-optional wins, else the release feature profile
    # (release_features.synchronization), which defaults to True.
    sync_gating = sync_mandatory if sync_mandatory is not None else features.synchronization
    # Installation (Priority 6) and machine-config (Priority 4) gating: an
    # explicit flag wins; else the component is auto-included as MANDATORY when a
    # report for it exists in this run's workspace (a real release run always
    # produces both), and left out entirely (None) for ad-hoc/unit consolidation
    # that has neither -- so existing callers are unaffected.
    installation_gating = (
        installation_mandatory if installation_mandatory is not None
        else (True if installation is not None else None)
    )
    machine_config_gating = (
        machine_config_mandatory if machine_config_mandatory is not None
        else (True if machine_config_snapshot is not None else None)
    )
    release_config_gating = (
        release_config_mandatory if release_config_mandatory is not None
        else (True if release_config_composition is not None else None)
    )
    # Subscribed-calendar fixture gating (Priority 7): an explicit flag wins;
    # else derived from the scenario's OWN promotion state -- optional while
    # scenarios/promotion/subscribed_calendar.yaml stays draft (releaseSuite
    # Eligible: false), automatically mandatory once it is promoted. A
    # promotion file that fails to load is treated as still-draft (optional),
    # never silently mandatory from a parsing accident.
    from . import promotion as promotion_mod

    if subscribed_fixture_mandatory is not None:
        subscribed_fixture_gating = subscribed_fixture_mandatory
    else:
        try:
            subscribed_fixture_gating = promotion_mod.load_promotion(
                promotion_mod.PROMOTION_DIR / "subscribed_calendar.yaml"
            ).release_suite_eligible
        except (promotion_mod.PromotionError, OSError):
            subscribed_fixture_gating = False

    # Distributed-build acceptance gating (Priority 3): an explicit flag wins;
    # else this run's own release-config composition's distributedBuildRequired
    # (sourced from a schema-v2 bundle's caleeMobile.distributedBuildAcceptance
    # Required, or the legacy release-platforms.yaml distributed_build_required
    # key for schema v1 -- release_config.py already unifies both into ONE
    # field). No release-config composed for this run at all (ad-hoc/dev
    # consolidation) leaves this None -- the component does not apply and is
    # never added, so ordinary/legacy consolidation is unaffected. When it DOES
    # apply, False is recorded as an explicit not-required component, never
    # silently omitted.
    if distributed_build_acceptance_mandatory is not None:
        distributed_build_gating = distributed_build_acceptance_mandatory
    elif release_config_composition is not None:
        distributed_build_gating = bool(
            (release_config_composition.get("releaseSelections") or {}).get("distributedBuildRequired", False)
        )
    else:
        distributed_build_gating = None

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

    # Selector evidence is unavoidable in a mobile release (Priority 2). It
    # defaults to MANDATORY whenever a mobile platform is in scope, and is
    # UNCONDITIONALLY mandatory in a production mobile release -- there,
    # --selector-contract-optional is rejected outright. In a development /
    # diagnostic release it may be opted out ONLY through a valid named waiver
    # (reason + approver + timestamp); a bare --selector-contract-optional
    # without a waiver is refused and the contract stays mandatory. A missing
    # report then surfaces as a visible NOT_RUN/BLOCKED component (see
    # build_release_report), never omission.
    mobile_in_scope = bool(android_gating or ios_gating)
    # Priority 3: the schema-v2 manifest's own caleeMobile.selectorEvidence
    # Required (via this run's composed release-config) is the default
    # whenever no explicit --selector-contract-mandatory/-optional flag is
    # given -- true makes it mandatory even outside a mobile release; false
    # is recorded as an explicit not-required component, never silently
    # omitted. It never overrides an explicit CLI flag, and production policy
    # (immediately below) still overrides a manifest false -- see
    # docs/RELEASE_POLICY.md's selector-evidence precedence section.
    manifest_selector_required = None
    if release_config_composition is not None:
        manifest_selector_required = (
            (release_config_composition.get("releaseSelections") or {})
            .get("expectedIdentities", {})
            .get("caleeMobile", {})
            .get("selectorEvidenceRequired")
        )
    if eff_production and mobile_in_scope:
        if selector_contract_mandatory is False:
            click.echo(
                "--selector-contract-optional is not permitted in a production mobile release; "
                "selector evidence is unconditionally mandatory for any mobile release.",
                err=True,
            )
            raise SystemExit(EXIT_INVALID_CONFIG)
        selector_contract_gating = True
    elif selector_contract_mandatory is False:
        # Explicit opt-out (development / diagnostic): allowed ONLY with a valid
        # named waiver. Without one, when a mobile platform is in scope, the
        # opt-out is refused and the contract stays mandatory -- selector
        # evidence can never be silently dropped from a mobile release.
        if mobile_in_scope and not waiver_is_valid:
            click.echo(
                "Refusing --selector-contract-optional without a named waiver "
                "(reason + approver + timestamp); selector evidence stays mandatory for "
                "this mobile release.",
                err=True,
            )
            selector_contract_gating = True
        else:
            selector_contract_gating = False
            if mobile_in_scope:
                click.echo(
                    "NOTE: selector evidence opted out by named waiver "
                    f"(approver={waiver.get('approver')!r}).",
                )
    elif selector_contract_mandatory is True:
        selector_contract_gating = True
    elif manifest_selector_required is True:
        selector_contract_gating = True
    elif manifest_selector_required is False:
        selector_contract_gating = False
    elif mobile_in_scope:
        # Default for any mobile release: mandatory.
        selector_contract_gating = True
    elif selector_contract_report is not None:
        # No mobile platform in scope, but a selector gate was recorded for this
        # run -- never silently ignore it.
        selector_contract_gating = True
    else:
        # No mobile in scope, no recorded gate: selector evidence is not
        # applicable to this (non-mobile) release.
        selector_contract_gating = None

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

    # Priority 8: the release ID this run's selector-contract evidence must be
    # bound to -- this run's OWN composed release-config releaseId when one
    # was recorded (the same release the whole run is for). No release-config
    # for this run (ad-hoc/dev consolidation) leaves this None, so ordinary
    # selector-contract validation is unaffected.
    expected_release_id_for_selector = (
        release_config_composition.get("releaseId") if release_config_composition is not None else None
    )

    # Distributed-build acceptance (Priority 3): built only when it applies
    # (distributed_build_gating is not None) so ad-hoc/legacy consolidation
    # that never had a release-config composed for it is unaffected.
    if distributed_build_gating is not None:
        distributed_build_component = component_from_distributed_build_acceptance_report(
            DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT_NAME, distributed_build_acceptance_report,
            mandatory=distributed_build_gating,
            expected_git_sha=eff_expected_caleemobile_sha,
            expected_version=eff_expected_caleemobile_build,
            expected_release_id=expected_release_id_for_selector,
            expected_release_run_id=run_id_opt,
            component_dir=(
                workspace.component_dir("distributed-build-acceptance")
                if distributed_build_acceptance_report is not None else None
            ),
        )
        extra_components.append(distributed_build_component)

    report = build_release_report(
        environment=env_report,
        tablet=tablet,
        mobile_api=mobile_api,
        mobile_android_ui=mobile_android,
        mobile_ios_ui=mobile_ios,
        sync=sync,
        subscribed_fixture=subscribed_fixture_report,
        subscribed_fixture_mandatory=subscribed_fixture_gating,
        kiosk_admin=kiosk,
        installation=installation,
        installation_mandatory=installation_gating,
        machine_config=machine_config_snapshot,
        machine_config_mandatory=machine_config_gating,
        release_config=release_config_composition,
        release_config_mandatory=release_config_gating,
        feature_profile=feature_profile,
        manual_checks=manual_checks_list,
        meta=meta,
        android_mandatory=android_gating,
        ios_mandatory=ios_gating,
        sync_mandatory=sync_gating,
        selector_contract=selector_contract_report,
        selector_contract_mandatory=selector_contract_gating,
        selector_contract_dir=(
            str(workspace.component_dir("selector-contract"))
            if selector_contract_report is not None else None
        ),
        expected_release_id=expected_release_id_for_selector,
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

    # Attach resume provenance (reused vs. executed, source attempt, reuse
    # validation, input digest, previous attempts) onto each component this
    # run resumed at least once -- see resume_release.component_resume_info.
    # A run that was never resumed has no attempts/ directory at all, so this
    # is a no-op and every existing report is unaffected.
    from . import resume_release as resume_release_mod

    resume_info = resume_release_mod.component_resume_info(workspace)
    if resume_info:
        for component_result in report.components:
            slug = _CONSOLIDATED_COMPONENT_SLUGS.get(component_result.name)
            if slug is not None and slug in resume_info:
                component_result.resume = resume_info[slug]

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
        # Priority 3 evidence bundle: the exact source ZIP + JSON bytes, their
        # raw-byte sha256 sidecars, and the envelope-protected provenance.json.
        p3_bundle = [
            selector_dir / name for name in (
                "source-artifact.zip", "source-result.json",
                "source-result.sha256", "source-artifact.sha256", "provenance.json",
            )
        ]
        for candidate in (raw_evidence, provenance_evidence, selector_report_path, *p3_bundle):
            if candidate.is_file():
                evidence_paths.append(candidate)
    # Priority 3 (this session): the distributed-build-acceptance provenance
    # bundle (raw source evidence + its raw-byte sha256 sidecar + the
    # envelope-protected provenance record) travels with the release ZIP too,
    # exactly like the selector-contract bundle above.
    if distributed_build_gating is not None and distributed_build_acceptance_report is not None:
        from . import distributed_build_provenance as _dbp_mod

        dist_dir = workspace.component_dir("distributed-build-acceptance")
        for name in (_dbp_mod.BUNDLE_SOURCE_JSON, _dbp_mod.BUNDLE_SOURCE_SHA, _dbp_mod.BUNDLE_PROVENANCE):
            candidate = dist_dir / name
            if candidate.is_file():
                evidence_paths.append(candidate)
    # Priority 5.9: row-scoped runtime diagnostics (screenshots + page sources)
    # captured by the tablet/mobile UI runs travel into the release ZIP too.
    for ui_report in (tablet, mobile_android, mobile_ios):
        evidence_paths.extend(collect_step_diagnostic_paths(ui_report))
    # Priority 9: every component's own results.json travels into the release
    # ZIP (not just machine-config/installation) -- the qualification bundle
    # must contain the complete evidence set, not a hand-picked subset. Uses
    # the SAME path each component was actually resolved from above (an
    # explicit --foo-report override, not just the default in-workspace path)
    # so the bundle always contains the exact file that backed the decision --
    # `loaded is None` means that component was never executed or was
    # rejected by _resolve_component, and there is nothing to package for it
    # (already correctly reflected as NOT_RUN/BLOCKED in the status above).
    for component_name, explicit_override, loaded in (
        ("machine-config", machine_config_report, machine_config_snapshot),
        ("release-config", release_config_report, release_config_composition),
        ("installation", installation_report, installation),
        ("environment", environment_report, env_report),
        ("tablet", tablet_report, tablet),
        ("mobile-api", mobile_api_report, mobile_api),
        ("mobile-android", mobile_android_report, mobile_android),
        ("mobile-ios", mobile_ios_report, mobile_ios),
        ("sync", sync_report, sync),
        ("kiosk-admin", kiosk_report, kiosk),
        ("manual-checks", manual_checks_path, manual_checks_raw),
        ("subscribed-fixture", None, subscribed_fixture_report),
        ("distributed-build-acceptance", None, distributed_build_acceptance_report),
    ):
        if loaded is None:
            continue
        evidence_paths.append(Path(explicit_override) if explicit_override else workspace.component_report_path(component_name))
    # The today-relative subscribed-fixture ICS sidecar (Priority 7) -- not a
    # results.json, so it isn't covered by the loop above.
    subscribed_ics = workspace.component_dir("subscribed-fixture") / "reg_sub_today_relative.ics"
    if subscribed_ics.is_file():
        evidence_paths.append(subscribed_ics)
    # Priority 4/Phase 3-4: the pre/post build-identity snapshots (not under
    # component_report_path -- see cli.py's build-identity command).
    for phase in ("pre", "post"):
        identity_snapshot = workspace.root / "identity" / f"{phase}.json"
        if identity_snapshot.is_file():
            evidence_paths.append(identity_snapshot)
    # Priority 12: the frozen release-candidate's fingerprint + manifest +
    # checksums (Priority 4's snapshot_release_candidate) travel with the
    # release ZIP too -- the exact proof of what THIS run approved and
    # installed from, not just a claim in a report. The APK bytes themselves
    # are deliberately NOT included here (they are the far larger installer
    # artifacts, already tracked at their own installation-evidence paths) --
    # only the small identity/proof files a reviewer or a later audit needs.
    from . import release_candidate as release_candidate_mod

    release_candidate_dir = workspace.component_dir("release-candidate")
    for name in (
        release_candidate_mod.FINGERPRINT_FILENAME,
        release_candidate_mod.MANIFEST_NAME,
        release_candidate_mod.CHECKSUMS_NAME,
    ):
        candidate = release_candidate_dir / name
        if candidate.is_file():
            evidence_paths.append(candidate)

    # Priority 9: a file this consolidation resolved and intended to package
    # (above) but that has since vanished from disk is a hard evidence-
    # integrity problem -- never silently ship a bundle missing a file it
    # claims to include. Block rather than produce an incomplete ZIP.
    vanished_evidence = [p for p in evidence_paths if not p.is_file()]
    if vanished_evidence:
        report.overall_status = STATUS_BLOCKED
        report.summary["suggestedNextAction"] = (
            "Evidence file(s) resolved for this run vanished before the release ZIP could be written: "
            + ", ".join(str(p) for p in vanished_evidence) + " -- rerun this run's consolidation."
        )

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
        latest_link = _resolved_report_root() / "reports" / "latest-run"
        try:
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()
            latest_link.symlink_to(Path("runs") / run_id, target_is_directory=True)
        except OSError:
            pass  # Best-effort convenience link; never fail the run over it.

    raise SystemExit(exit_code)


@main.command("machine-config")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Path to machine.local.yaml (defaults to config/machine.local.yaml).")
def machine_config_cmd(config_path):
    """Load and validate config/machine.local.yaml and emit its values as
    shell assignments a launcher can `eval`. Prints MACHINE_* variables on
    success; on a malformed config (or an inline secret) exits
    EXIT_INVALID_CONFIG with the problems on stderr so a launcher stops.
    """
    from . import machine_config as machine_mod

    path = Path(config_path) if config_path else (REPO_ROOT / "config" / "machine.local.yaml")
    try:
        cfg = machine_mod.load_machine_config(path)
    except machine_mod.MachineConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    def _emit(name, value):
        import shlex as _shlex

        if value is None:
            value = ""
        click.echo(f"MACHINE_{name}={_shlex.quote(str(value))}")

    _emit("TABLET_SERIAL", cfg.tablet_serial or "")
    _emit("EXPECTED_TABLET_STATE", cfg.expected_tablet_state)
    _emit("CALEE_PACKAGE_ID", cfg.calee_package_id)
    _emit("CALEESHELL_PACKAGE_ID", cfg.caleeshell_package_id)
    _emit("HOME_ACTIVITY", cfg.home_activity)
    _emit("CALEE_LAUNCH_ACTION", cfg.calee_launch_action)
    _emit("RELEASE_BUNDLE_DIR", str(cfg.resolved_bundle_dir()))
    _emit("BACKEND_URL", cfg.backend_url)
    _emit("RELEASE_PROFILE", cfg.release_profile)
    _emit("REPORT_DIR", cfg.report_dir)
    _emit("MOBILE_PLATFORMS", ",".join(cfg.mobile_platforms))
    _emit("IPHONE_DEVICE", cfg.iphone_device or "")
    _emit("ALLOW_CALEESHELL_TECHNICAL", "true" if cfg.allow_caleeshell_technical else "false")
    raise SystemExit(EXIT_SUCCESS)


def _resolved_report_root(config_path: "str | None" = None) -> Path:
    """Resolve the canonical report root for this process (Priority 3): the
    CALEE_REPORT_ROOT environment variable (already exported once by the
    tester launchers before any file is written) if set, else this
    invocation's machine-config report_dir (best-effort -- a missing/invalid
    machine config here is never itself fatal; commands that require a valid
    machine config load and validate it separately), else REPO_ROOT/reports
    -- the original, unchanged default. Every RunWorkspace(...) construction
    in this module uses this instead of the bare REPO_ROOT constant, so one
    component can never silently disagree with another about where evidence
    lives. Exits BLOCKED with a clear reason on an unsafe/unwritable
    configured root -- never silently falls back to the default."""
    machine_report_dir = None
    try:
        from . import machine_config as machine_config_mod

        machine_path = Path(config_path) if config_path else (REPO_ROOT / "config" / "machine.local.yaml")
        if machine_path.is_file():
            machine_report_dir = machine_config_mod.load_machine_config(machine_path).report_dir
    except Exception:
        pass
    try:
        return report_root_mod.resolve_report_root(repo_root=REPO_ROOT, machine_report_dir=machine_report_dir)
    except report_root_mod.ReportRootError as exc:
        click.echo(f"[BLOCKED] Report root problem: {exc}", err=True)
        raise SystemExit(EXIT_BLOCKED)


@main.command("report-root")
@click.option(
    "--config", "config_path", default=None, type=click.Path(),
    help="Path to machine.local.yaml (defaults to config/machine.local.yaml, best-effort).",
)
def report_root_cmd(config_path):
    """Resolve and print the ONE canonical report root for this run (Priority 3).

    Precedence: the CALEE_REPORT_ROOT environment variable, else this
    machine's config/machine.local.yaml report_dir (best-effort), else this
    repo's own reports/ directory. Prints ONLY the resolved absolute path to
    stdout on success (nothing else -- safe to capture with $(...) from a
    shell launcher) and exits BLOCKED with a clear reason on an unsafe or
    unwritable configured root -- never silently falling back to the
    default. The tester launchers call this ONCE, at the very start of a
    run, and export CALEE_REPORT_ROOT so every downstream process (every
    delegated calee_regression subcommand, the mobile test scripts, "07 Open
    Latest Report") inherits and agrees on the same resolved value.
    """
    click.echo(str(_resolved_report_root(config_path)))
    raise SystemExit(EXIT_SUCCESS)


@main.command("machine-config-snapshot")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Path to machine.local.yaml (defaults to config/machine.local.yaml).")
@click.option("--legacy-config", "legacy_config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Legacy tester config to reconcile (defaults to config/tester.local.yaml).")
@click.option("--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True, help="Shared release run ID (run_context.py).")
def machine_config_snapshot_cmd(config_path, legacy_config_path, run_id_opt):
    """Make config/machine.local.yaml AUTHORITATIVE for this run (Priority 4).

    Loads and validates the machine config ONCE, reconciles it with the legacy
    tester config (machine config wins every overlap; a differing legacy value
    is OVERRIDDEN with a recorded explanation), writes an effective tester
    config the runner loads, and records a secrets-excluded snapshot at
    reports/runs/<run-id>/machine-config/results.json (the selected backend,
    devices, package ids and release profile appear in the run evidence).

    Emits eval-able shell assignments on stdout -- including MACHINE_EFFECTIVE_
    CONFIG (the reconciled tester config the launcher points CALEE_TEST_CONFIG
    at, so machine config actually controls execution with no second, conflicting
    source of truth) and derived MACHINE_PLATFORM_ANDROID/IOS. On a malformed or
    secret-bearing machine config, records a BLOCKED snapshot and exits BLOCKED
    so the whole release stops.
    """
    import shlex as _shlex

    import yaml as _yaml

    from . import machine_adapter
    from . import machine_config as machine_mod

    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
    workspace.ensure_created()

    def _record_blocked(detail: "list[str]") -> None:
        payload = {"runId": run_id_opt, "status": STATUS_BLOCKED, "detail": detail}
        path = workspace.component_report_path("machine-config")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        manifest = _load_or_init_manifest(workspace)
        manifest.record_component("machine-config", report_path=str(path), exit_code=EXIT_BLOCKED)
        manifest.write(workspace.manifest_path)

    path = Path(config_path) if config_path else (REPO_ROOT / "config" / "machine.local.yaml")
    try:
        cfg = machine_mod.load_machine_config(path)
    except machine_mod.MachineConfigError as exc:
        _record_blocked([str(exc)])
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_BLOCKED)

    # Legacy tester config (raw) -- best-effort; its non-overlapping keys are
    # preserved. A missing/unreadable one just means machine config supplies the
    # overlaps and the rest come from placeholders the caller must still fill in.
    legacy_path = Path(legacy_config_path) if legacy_config_path else (REPO_ROOT / "config" / "tester.local.yaml")
    legacy_raw = None
    legacy_note = None
    if legacy_path.is_file():
        try:
            loaded = _yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
            legacy_raw = loaded if isinstance(loaded, dict) else None
            if legacy_raw is None:
                legacy_note = f"Legacy tester config at {legacy_path} is not a mapping -- ignored."
        except _yaml.YAMLError as exc:
            legacy_note = f"Legacy tester config at {legacy_path} did not parse ({exc}) -- ignored."
    else:
        legacy_note = f"No legacy tester config at {legacy_path}; machine config supplies the overlapping values."

    effective = machine_adapter.reconcile(cfg, legacy_raw)

    # Write the reconciled effective tester config the runner will load.
    effective_config_path = workspace.component_dir("machine-config") / "effective-tester-config.yaml"
    effective_config_path.parent.mkdir(parents=True, exist_ok=True)
    effective_config_path.write_text(_yaml.safe_dump(effective.tester_config, sort_keys=True), encoding="utf-8")

    snapshot = machine_adapter.snapshot(
        effective, machine_config_path=str(path), effective_tester_config_path=str(effective_config_path)
    )
    snapshot["runId"] = run_id_opt
    if legacy_note:
        snapshot["detail"].append(legacy_note)

    report_path = workspace.component_report_path("machine-config")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    manifest = _load_or_init_manifest(workspace)
    manifest.record_component("machine-config", report_path=str(report_path), exit_code=EXIT_SUCCESS)
    manifest.target_backend = effective.backend_url or manifest.target_backend
    manifest.write(workspace.manifest_path)

    def _emit(name, value):
        if value is None:
            value = ""
        click.echo(f"MACHINE_{name}={_shlex.quote(str(value))}")

    _emit("EFFECTIVE_CONFIG", str(effective_config_path))
    _emit("TABLET_SERIAL", effective.tablet_serial or "")
    _emit("RELEASE_BUNDLE_DIR", effective.release_bundle_dir or "")
    _emit("BACKEND_URL", effective.backend_url)
    _emit("RELEASE_PROFILE", effective.release_profile)
    _emit("REPORT_DIR", effective.report_dir)
    _emit("IPHONE_DEVICE", effective.iphone_device or "")
    _emit("ANDROID_DEVICE", effective.android_device or "")
    _emit("CALEE_PACKAGE_ID", effective.calee_package_id)
    _emit("CALEESHELL_PACKAGE_ID", effective.caleeshell_package_id)
    _emit("HOME_ACTIVITY", effective.home_activity)
    _emit("CALEE_LAUNCH_ACTION", effective.calee_launch_action)
    _emit("PLATFORM_ANDROID", "true" if "android" in effective.mobile_platforms else "false")
    _emit("PLATFORM_IOS", "true" if "ios" in effective.mobile_platforms else "false")
    _emit("ALLOW_CALEESHELL_TECHNICAL", "true" if effective.allow_caleeshell_technical else "false")
    raise SystemExit(EXIT_SUCCESS)


def _load_subscribed_fixture_config(config_path: "Path | None") -> dict:
    import yaml as _yaml

    path = config_path or (REPO_ROOT / "config" / "machine.local.yaml")
    if not path.is_file():
        return {}
    try:
        raw = _yaml.safe_load(path.read_text(encoding="utf-8"))
    except _yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    section = raw.get("subscribed_fixture")
    return section if isinstance(section, dict) else {}


def _load_machine_backend_url(config_path: "Path | None") -> "str | None":
    """The top-level machine.local.yaml backend_url -- the SAME backend the
    rest of this run already talks to -- used as the default Calee API base
    URL for Priority 6's ingestion-verification bridge call. Best-effort:
    an absent/malformed file just means no default is available (the
    ingestion check is then unavailable, not a crash)."""
    import yaml as _yaml

    path = config_path or (REPO_ROOT / "config" / "machine.local.yaml")
    if not path.is_file():
        return None
    try:
        raw = _yaml.safe_load(path.read_text(encoding="utf-8"))
    except _yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("backend_url")
    return value if isinstance(value, str) and value.strip() else None


@main.command("prepare-subscribed-fixture")
@click.option("--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True, help="Shared release run ID (run_context.py).")
@click.option("--release-id", "release_id_opt", envvar="CALEE_RELEASE_ID", default=None, help="Release id recorded in this component's evidence.")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Path to machine.local.yaml (defaults to config/machine.local.yaml); reads its subscribed_fixture: section.")
@click.option(
    "--mode", "mode_opt", type=click.Choice(["published", "fixed-date", "offline-only"]), default=None,
    help="Subscribed-fixture mode (Priority 6). Defaults to config/machine.local.yaml's subscribed_fixture.mode, else 'offline-only'. Never silently falls back between modes.",
)
@click.option("--target-date", "date_opt", default=None, help="Pin the subscribed target date (YYYY-MM-DD); defaults to today. Ignored in fixed-date mode (its own known date is used).")
@click.option("--timezone", "tz_opt", default=None, help="Timezone label recorded in evidence (default Australia/Perth).")
@click.option(
    "--gate/--non-gating", "gate_opt", default=None,
    help="Priority 6 (this session): explicit execution policy for THIS COMMAND's own exit code. "
         "--gate: a publication, exact public-read, or Calee-ingestion failure exits BLOCKED here and "
         "now (not merely recorded). --non-gating: every failure is still recorded in full, but this "
         "command exits success regardless (matches this scenario staying draft-unverified/optional). "
         "Defaults to config/machine.local.yaml's subscribed_fixture.gate when set, else this "
         "scenario's OWN promotion state (scenarios/promotion/subscribed_calendar.yaml's "
         "releaseSuiteEligible) -- the SAME derivation 'consolidate' uses for whether this component "
         "is mandatory, so the two can never disagree.",
)
def prepare_subscribed_fixture_cmd(run_id_opt, release_id_opt, config_path, mode_opt, date_opt, tz_opt, gate_opt):
    """Generate the today-relative subscribed ICS and run it through exactly
    ONE explicit mode -- published / fixed-date / offline-only (Priority 5/6)
    -- recording first-class subscribed-fixture evidence (Priority 7).

    published: publishes the ICS to config/machine.local.yaml's
    subscribed_fixture.public_url via the configured adapter (webdav/
    presigned-put/s3-cli/local) and polls until the run-specific event is
    observable, using bounded polling (never an arbitrary sleep). fixed-date:
    uses the existing static fixture at its own known date, never Today.
    offline-only (the default -- always safe, no setup required): generates
    and validates the ICS locally only, never claims provisioning. Neither
    fixed-date nor offline-only ever claims published verification (their
    publicationStatus/publicReadVerificationStatus/ingestionStatus stay
    "not-attempted").

    Writes reports/runs/<run-id>/subscribed-fixture/results.json and
    reg_sub_today_relative.ics, and records the generated event titles as
    scenario variables -- consumable ONLY via subscribed_publisher.
    safe_scenario_variables_from_report (Priority 6), which refuses anything
    but a same-run, same-release, fully-"ok" published report. Never silently
    falls back from published to fixed-date.
    """
    import datetime as _dt

    from . import subscribed_publisher as sp

    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
    workspace.ensure_created()

    # Priority 7: an explicit --release-id wins; else, when this run already
    # composed its release-config (launcher "00" always does before this step
    # runs), adopt ITS releaseId -- the same release the whole run is for,
    # exactly like selector-contract's Priority 8 adoption. No release-config
    # composed for this run at all (a bare/dev invocation) leaves this None,
    # so ordinary ad-hoc use is unaffected.
    release_config_dict = _load_release_config_dict(workspace)
    if release_id_opt is None and release_config_dict is not None:
        release_id_opt = release_config_dict.get("releaseId")

    target_date = None
    if date_opt:
        try:
            target_date = _dt.date.fromisoformat(date_opt)
        except ValueError:
            click.echo(f"Invalid --target-date {date_opt!r}; expected YYYY-MM-DD.", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)

    section = _load_subscribed_fixture_config(Path(config_path) if config_path else None)
    mode = mode_opt or section.get("mode") or sp.MODE_OFFLINE_ONLY
    if mode not in sp.VALID_MODES:
        click.echo(f"subscribed_fixture.mode {mode!r} is not one of {sorted(sp.VALID_MODES)}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    kwargs = dict(
        run_id=run_id_opt, release_id=release_id_opt, mode=mode,
        target_date=target_date, timezone=tz_opt or section.get("timezone") or sp.DEFAULT_TIMEZONE,
    )
    if mode == sp.MODE_PUBLISHED:
        publisher, publisher_type, public_url = sp.build_publisher_from_config(section, mode=mode)
        poll_check = None
        if public_url:
            # Re-fetches the run's OWN published URL; prepare_subscribed_
            # fixture (Priority 5) wraps this raw fetch with the full
            # verification contract (byte SHA-256, both run-specific titles,
            # expected target date) -- this callable only needs to return the
            # downloaded bytes, never merely a nonempty-response check.
            def poll_check(_url=public_url):
                import urllib.request
                with urllib.request.urlopen(_url, timeout=15) as resp:
                    return resp.read()
        kwargs.update(
            publisher=publisher, publisher_type=publisher_type, public_url=public_url,
            poll_check=poll_check,
            poll_interval_seconds=float(section.get("poll_interval_seconds", 10)),
            poll_timeout_seconds=float(section.get("timeout_seconds", 300)),
        )

        # Priority 6: the SECOND, separate phase -- proving Calee actually
        # INGESTED the published feed, via the EXISTING authenticated
        # GET /client/v1/events operation (CaleeMobile-Regression's
        # sync_smoke_cli.py 'find-event-by-title'), never a new backend
        # endpoint. Only wired when every prerequisite is genuinely
        # available (regression credentials, a resolvable backend, and the
        # sibling CaleeMobile-Regression checkout with the bridge action) --
        # otherwise ingestion_check stays None and prepare_subscribed_fixture
        # itself records the precise BLOCKED reason (never silently passing
        # from public-URL readability alone).
        ingestion_backend = section.get("ingestion_backend") or _load_machine_backend_url(
            Path(config_path) if config_path else None
        )
        ingestion_calendar_id = section.get("ingestion_calendar_id", "regression:regsub")
        ingestion_email = ingestion_password = None
        try:
            ingestion_resolver = credentials_mod.default_resolver()
            ingestion_email = ingestion_resolver.require(credentials_mod.REGRESSION_USERNAME)
            ingestion_password = ingestion_resolver.require(credentials_mod.REGRESSION_PASSWORD)
        except credentials_mod.CredentialError:
            ingestion_email = ingestion_password = None

        from . import sync_smoke_bridge as ssb_mod

        ingestion_check = None
        if (
            ingestion_backend and ingestion_email and ingestion_password
            and ssb_mod.is_ingestion_bridge_available(REPO_ROOT)
        ):
            ingestion_titles = sp.scenario_variables(
                sp.resolve_target_date(target_date), run_token=sp.build_run_token(run_id_opt),
            )
            ingestion_title = ingestion_titles["REG_SUB_TIMED_TITLE"]

            def ingestion_check(
                _repo_root=REPO_ROOT, _base_url=ingestion_backend, _email=ingestion_email,
                _password=ingestion_password, _title=ingestion_title, _calendar_id=ingestion_calendar_id,
            ):
                return ssb_mod.find_event_by_title(
                    repo_root=_repo_root, base_url=_base_url, email=_email, password=_password,
                    title=_title, calendar_id=_calendar_id,
                )

        kwargs.update(
            ingestion_check=ingestion_check,
            ingestion_interval_seconds=float(section.get("ingestion_poll_interval_seconds", 10)),
            ingestion_timeout_seconds=float(section.get("ingestion_timeout_seconds", 300)),
            ingestion_api_label="CaleeMobile-Regression sync_smoke_cli.py find-event-by-title (GET /client/v1/events)",
            ingestion_expected_calendar_id=ingestion_calendar_id,
        )
    elif mode == sp.MODE_FIXED_DATE:
        kwargs.update(fixed_date=section.get("fixed_date"), fixed_date_titles=section.get("fixed_date_titles"))

    result = sp.prepare_subscribed_fixture(**kwargs)

    report_path = workspace.component_report_path("subscribed-fixture")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({"runId": run_id_opt, "releaseRunId": run_id_opt, **result.to_dict()}, indent=2) + "\n", encoding="utf-8")
    # The generated ICS is provisioning INPUT (recorded next to, not inside, the
    # results json). It carries only regression event titles, never a secret.
    if result.ics:
        (report_path.parent / "reg_sub_today_relative.ics").write_text(result.ics, encoding="utf-8")

    manifest = _load_or_init_manifest(workspace)
    manifest.record_component(
        "subscribed-fixture", report_path=str(report_path),
        exit_code=(EXIT_SUCCESS if result.ok else EXIT_BLOCKED),
    )
    manifest.write(workspace.manifest_path)

    click.echo(f"Subscribed-fixture evidence: {report_path}")
    click.echo(f"  mode: {result.mode}  status: {result.status}  date: {result.resolved_date}")
    for d in result.detail:
        click.echo(f"  - {d}")

    # Priority 6 (this session): an explicit --gate/--non-gating always wins;
    # otherwise default to this scenario's OWN promotion state -- the SAME
    # derivation 'consolidate' uses to decide whether this component is
    # mandatory, so a technical owner never sees this step's own exit code
    # disagree with what the final release verdict will do with the same
    # evidence.
    if gate_opt is not None:
        gate = gate_opt
    else:
        from . import promotion as promotion_mod

        try:
            gate = promotion_mod.load_promotion(
                promotion_mod.PROMOTION_DIR / "subscribed_calendar.yaml"
            ).release_suite_eligible
        except (promotion_mod.PromotionError, OSError):
            gate = False

    if gate and not result.ok:
        click.echo(
            click.style(
                "[BLOCKED] Subscribed-fixture gating is ON and this run's evidence did not reach "
                "status \"ok\" -- see the detail above (publication/public-read/ingestion). No scenario "
                "variables from this run are safe to consume (see safe_scenario_variables_from_report).",
                fg="red",
            ),
            err=True,
        )
        raise SystemExit(EXIT_BLOCKED)
    raise SystemExit(EXIT_SUCCESS)


@main.command("run-with-credentials", context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run_with_credentials_cmd(command):
    """Resolve regression credentials ONCE and exec a delegated command with the
    credentials present ONLY in that command's child environment (Priority 5).

        python -m calee_regression run-with-credentials -- <command...>

    The credentials are resolved through the standard chain (injected -> env ->
    macOS Keychain), so a technical owner who stores them in the Keychain never
    has to export CALEE_TEST_EMAIL / CALEE_TEST_PASSWORD -- this single secure
    boundary is how the Bash mobile orchestration (and everything it spawns:
    Prepare, the CaleeMobile Client API, the mobile UI, the sync receivers)
    obtains them. The credentials NEVER appear in argv, in any report, in this
    process's logs, or in a persistent plaintext file; only the specific
    CALEE_TEST_EMAIL / CALEE_TEST_PASSWORD (and, when present, CALEE_API_TOKEN)
    are added to the child environment, which is otherwise inherited unchanged.
    """
    argv = list(command)
    while argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        click.echo("run-with-credentials needs a command: run-with-credentials -- <command...>", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    resolver = credentials_mod.default_resolver()
    try:
        resolved = resolver.resolve_all([
            credentials_mod.REGRESSION_USERNAME,
            credentials_mod.REGRESSION_PASSWORD,
            credentials_mod.API_TOKEN,
        ])
    except credentials_mod.CredentialError as exc:
        # CredentialError names the env var / keychain item, never a value.
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_BLOCKED)

    mapping = {
        credentials_mod.REGRESSION_USERNAME.name: credentials_mod.REGRESSION_USERNAME.env_var,
        credentials_mod.REGRESSION_PASSWORD.name: credentials_mod.REGRESSION_PASSWORD.env_var,
        credentials_mod.API_TOKEN.name: credentials_mod.API_TOKEN.env_var,
    }
    child_env = credentials_mod.build_env(os.environ, resolved, mapping)

    # Replace THIS process with the delegated command: the credentials then live
    # only in the delegated process tree's environment, never touching a report
    # or a persistent file. A failed exec (command not found) BLOCKS.
    try:
        os.execvpe(argv[0], argv, child_env)
    except OSError as exc:
        click.echo(f"run-with-credentials could not exec {argv[0]!r}: {exc}", err=True)
        raise SystemExit(EXIT_BLOCKED)


def _emit_release_config_vars(workspace: run_context.RunWorkspace, effective_dict: dict) -> None:
    """Emits the eval-able RELEASE_* shell assignments launchers "00"/"06"
    source, from an ``EffectiveReleaseConfig.to_dict()`` payload (freshly
    composed OR loaded back from this run's own already-written evidence --
    see release_config_cmd's reuse-not-recompute path).

    Priority 2: this is the ONE choke point both launchers eval, so every
    schema-v2 value that must be authoritative for the rest of the run --
    release id/schema version, the full per-feature scope (not just the
    flattened comma list), the expected Calee/CaleeShell/CaleeMobile
    identities, and the selector-evidence/distributed-build-acceptance
    requirement flags -- is emitted here, not only recorded in the JSON
    evidence."""
    import shlex as _shlex

    def _emit(name, value):
        click.echo(f"RELEASE_{name}={_shlex.quote(str('' if value is None else value))}")

    def _emit_bool(name, value):
        _emit(name, "true" if value else "false")

    release_selections = effective_dict.get("releaseSelections") or {}
    machine_selections = effective_dict.get("machineSelections") or {}
    device_ids = effective_dict.get("deviceIds") or {}
    enabled_platforms = release_selections.get("enabledPlatforms") or []
    enabled_features = release_selections.get("enabledFeatures") or []
    expected_identities = release_selections.get("expectedIdentities") or {}
    expected_calee = expected_identities.get("calee") or {}
    expected_caleeshell = expected_identities.get("caleeShell") or {}
    expected_caleemobile = expected_identities.get("caleeMobile") or {}

    _emit("ID", effective_dict.get("releaseId") or "")
    _emit("SCHEMA_VERSION", effective_dict.get("schemaVersion"))
    _emit("EFFECTIVE_CONFIG", str(workspace.component_report_path("release-config")))
    _emit("PROFILE", release_selections.get("profile"))
    _emit("SELECTED_BACKEND", release_selections.get("selectedBackend") or "")
    _emit("PLATFORM_TABLET", "true" if "tablet" in enabled_platforms else "false")
    _emit("PLATFORM_ANDROID", "true" if "android" in enabled_platforms else "false")
    _emit("PLATFORM_IOS", "true" if "ios" in enabled_platforms else "false")
    _emit("ENABLED_FEATURES", ",".join(enabled_features))
    _emit_bool("FEATURE_SYNCHRONIZATION", "synchronization" in enabled_features)
    _emit_bool("FEATURE_MEALS", "meals" in enabled_features)
    _emit_bool("FEATURE_ONBOARDING", "onboarding" in enabled_features)
    _emit_bool("FEATURE_GOOGLE_CALENDAR", "google_calendar" in enabled_features)
    _emit_bool("FEATURE_KIOSK_ADMIN", "kiosk_admin" in enabled_features)
    _emit_bool("FEATURE_NOTIFICATIONS", "notifications" in enabled_features)
    _emit("TABLET_SERIAL", device_ids.get("tablet") or "")
    _emit("IPHONE_DEVICE", device_ids.get("ios") or "")
    _emit("ANDROID_DEVICE", device_ids.get("android") or "")
    _emit("REPORT_ROOT", machine_selections.get("reportRoot") or "")

    _emit("EXPECTED_CALEE_VERSION", expected_calee.get("buildVersion") or "")
    _emit("EXPECTED_CALEE_VERSION_CODE", expected_calee.get("versionCode"))
    _emit("EXPECTED_CALEE_GIT_SHA", expected_calee.get("gitSha") or "")
    _emit("EXPECTED_CALEE_PACKAGE_ID", expected_calee.get("applicationId") or "")
    _emit("EXPECTED_CALEE_SIGNER_SHA256", expected_calee.get("signerSha256") or "")

    _emit("EXPECTED_CALEESHELL_VERSION", expected_caleeshell.get("version") or "")
    _emit("EXPECTED_CALEESHELL_VERSION_CODE", expected_caleeshell.get("versionCode"))
    _emit("EXPECTED_CALEESHELL_GIT_SHA", expected_caleeshell.get("gitSha") or "")
    _emit("EXPECTED_CALEESHELL_PACKAGE_ID", expected_caleeshell.get("applicationId") or "")
    _emit("EXPECTED_CALEESHELL_SIGNER_SHA256", expected_caleeshell.get("signerSha256") or "")

    _emit("EXPECTED_CALEEMOBILE_VERSION", expected_caleemobile.get("buildVersion") or "")
    _emit("EXPECTED_CALEEMOBILE_GIT_SHA", expected_caleemobile.get("gitSha") or "")
    # Priority 2 (this session): the FULLY RESOLVED selector-evidence policy
    # (production+mobile-in-scope override > schema-v2 manifest opinion >
    # legacy mobile-in-scope default), not merely the bundle manifest's raw,
    # unresolved flag -- this is the ONE value launcher "06" reads to decide
    # its own --mandatory/--optional on the selector-contract command itself
    # (see release_config.resolve_selector_evidence_required's docstring).
    # Emitted as a concrete true/false (never left unset) so a launcher
    # always has an explicit decision to act on; "not applicable" (no mobile
    # platform in scope) is emitted as false -- non-gating, like "optional".
    from . import release_config as _rc_mod

    resolved_selector_required = _rc_mod.resolve_selector_evidence_required(
        profile=release_selections.get("profile"),
        enabled_platforms=enabled_platforms,
        schema_version=effective_dict.get("schemaVersion"),
        manifest_required=expected_caleemobile.get("selectorEvidenceRequired"),
    )
    _emit_bool("SELECTOR_EVIDENCE_REQUIRED", bool(resolved_selector_required))
    _emit_bool("DISTRIBUTED_BUILD_ACCEPTANCE_REQUIRED", expected_caleemobile.get("distributedBuildAcceptanceRequired", True))


_RELEASE_CONFIG_REQUIRED_KEYS = {"status", "machineSelections", "releaseSelections", "deviceIds", "conflicts"}


@main.command("release-config")
@click.option("--config", "config_path", default=None, type=click.Path(), help="Path to machine.local.yaml (defaults to config/machine.local.yaml).")
@click.option("--release-platforms", "platforms_path", envvar="CALEE_RELEASE_PLATFORMS", default=None, type=click.Path(), help="Path to release-platforms.yaml (schema-v1 release candidate manifest).")
@click.option("--release-id", "release_id_opt", envvar="CALEE_RELEASE_ID", default=None, help="Release candidate id override; a schema-v2 bundle manifest's releaseId is authoritative and a mismatch BLOCKS.")
@click.option("--bundle", "bundle_path", default=None, type=click.Path(), help="Path to the release bundle directory. When given, it is verified and folded into this composition (Priority 1/2). Omit for a bundle-less diagnostic/dev run.")
@click.option("--run-id", "run_id_opt", envvar="CALEE_RUN_ID", required=True, help="Shared release run ID (run_context.py).")
def release_config_cmd(config_path, platforms_path, release_id_opt, bundle_path, run_id_opt):
    """Compose the ONE effective RELEASE configuration for this run (Priority 3),
    or -- when this run already recorded one -- CONSUME that same evidence
    instead of recomputing a second, possibly-different composition (Priority 1).

    Combines the MACHINE config (how/where a run executes) with the RELEASE
    CANDIDATE -- the verified release bundle manifest when schema version 2
    (authoritative for scope: platforms, features, profile, backend, expected
    identity; config/release-platforms.yaml is then not consulted), else
    config/release-platforms.yaml (schema version 1) -- under one precedence
    rule: the release candidate is authoritative for scope, and the machine
    must be consistent with and capable of it. Any disagreement or missing
    capability is a CONFLICT that BLOCKS. Writes the composed config, the full
    pre-install identity comparison matrix, and every conflict decision to
    reports/runs/<run-id>/release-config/results.json, and emits eval-able
    RELEASE_* assignments (enabled platforms, device ids, selected backend,
    profile) so the composition actually drives execution.

    Idempotent per run: called a second time for the SAME run ID (e.g. by "06"
    after "00" already composed it), this reuses and re-validates the
    already-written evidence instead of recomposing -- rejecting it if it is
    missing, malformed, stale, or was written for a different run.
    """
    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
    workspace.ensure_created()

    existing_path = workspace.component_report_path("release-config")
    if existing_path.is_file():
        # Priority 1: launcher 06 must CONSUME the same-run release-config
        # evidence launcher 00 already composed, never recompute a second,
        # possibly-different one. Reject missing/malformed/stale/wrong-run
        # evidence rather than silently trusting or silently recomposing it.
        try:
            existing_report = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            click.echo(f"This run's release-config evidence at {existing_path} is unreadable: {exc}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        run_manifest = _load_or_init_manifest(workspace)
        run_started_at_epoch = None
        if run_manifest.started_at:
            try:
                run_started_at_epoch = time.mktime(time.strptime(run_manifest.started_at, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                run_started_at_epoch = None
        try:
            run_context.validate_component_report(
                existing_report, report_path=existing_path, run_id=run_id_opt, workspace=workspace,
                component="release-config", run_started_at_epoch=run_started_at_epoch,
            )
        except run_context.RunIdError as exc:
            click.echo(f"This run's release-config evidence was rejected: {exc}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        if not _RELEASE_CONFIG_REQUIRED_KEYS.issubset(existing_report):
            click.echo(
                f"This run's release-config evidence at {existing_path} is malformed "
                f"(missing one of {sorted(_RELEASE_CONFIG_REQUIRED_KEYS)}).", err=True,
            )
            raise SystemExit(EXIT_BLOCKED)
        _emit_release_config_vars(workspace, existing_report)
        if existing_report.get("status") != "ok":  # matches release_config.STATUS_OK
            click.echo(click.style(
                "[BLOCKED] Reusing this run's already-composed (and already-BLOCKED) release configuration "
                "-- see the detail above/in the report.", fg="red",
            ), err=True)
            raise SystemExit(EXIT_BLOCKED)
        click.echo(click.style(
            f"[OK] Reusing this run's already-composed effective release configuration for {run_id_opt}.",
            fg="green",
        ), err=True)
        raise SystemExit(EXIT_SUCCESS)

    import yaml as _yaml

    from . import machine_config as machine_mod
    from . import release_candidate as release_candidate_mod
    from . import release_config as rc_mod
    from . import release_installer as ri_mod
    from . import release_platforms as rp_mod

    def _record(payload: dict, exit_code: int) -> None:
        payload = {"runId": run_id_opt, **payload}
        path = workspace.component_report_path("release-config")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        manifest = _load_or_init_manifest(workspace)
        manifest.record_component("release-config", report_path=str(path), exit_code=exit_code)
        manifest.write(workspace.manifest_path)

    machine_path = Path(config_path) if config_path else (REPO_ROOT / "config" / "machine.local.yaml")
    try:
        machine = machine_mod.load_machine_config(machine_path)
    except machine_mod.MachineConfigError as exc:
        _record({"status": STATUS_BLOCKED, "detail": [str(exc)]}, EXIT_BLOCKED)
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_BLOCKED)

    # Priority 1: verify and parse the release bundle -- WITHOUT touching any
    # device -- before composing. Its manifest feeds the composition below
    # (schema v2: authoritative for scope; schema v1: cross-checked against
    # release-platforms.yaml). Only when EXPLICITLY given --bundle (the
    # launcher always passes the machine's release_bundle_dir) -- a bare
    # dev/diagnostic `release-config` invocation with no --bundle composes
    # exactly as before Priority 2 existed, even if a machine config happens
    # to declare a release_bundle_dir for unrelated (installation) purposes.
    bundle_manifest = None
    verification = None
    if bundle_path:
        verification = ri_mod.verify_release_bundle(bundle_path)
        if not verification.ok:
            _record({
                "status": STATUS_BLOCKED,
                "detail": ["Release bundle failed verification:"] + list(verification.errors),
                "bundleVerification": verification.to_dict(),
            }, EXIT_BLOCKED)
            click.echo(click.style("[BLOCKED] Release bundle failed verification:", fg="red"), err=True)
            for err in verification.errors:
                click.echo(f"  - {err}", err=True)
            raise SystemExit(EXIT_BLOCKED)
        bundle_manifest = verification.manifest

    # Priority 2 (requirement 7): once a schema-v2 bundle has been verified,
    # config/release-platforms.yaml is not consulted AT ALL -- the bundle
    # manifest is self-contained and authoritative for scope. A malformed
    # legacy file must never block a valid schema-v2 bundle, so it is not
    # even loaded here for a v2 run. Schema v1 (or no bundle) keeps loading
    # and cross-checking it exactly as before.
    is_v2_bundle = bundle_manifest is not None and bundle_manifest.is_schema_v2
    if is_v2_bundle:
        platforms = rp_mod.ReleasePlatforms()
        features = rp_mod.ReleaseFeatures()
        expected = rp_mod.ExpectedBuildIdentity()
        expected_backend = None
        distributed_build_required = False
    else:
        try:
            platforms = rp_mod.load_release_platforms(platforms_path)
            features = rp_mod.load_release_features(platforms_path)
            expected = rp_mod.load_expected_build_identity(platforms_path)
        except rp_mod.ReleasePlatformsError as exc:
            _record({"status": STATUS_BLOCKED, "detail": [f"release-platforms.yaml problem: {exc}"]}, EXIT_BLOCKED)
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_BLOCKED)

        # Optional release-candidate extras (backend/environment pin +
        # distributed build acceptance) read from the same release-platforms.
        # yaml top level. Schema v2 does not consult release-platforms.yaml at
        # all -- the bundle manifest is authoritative -- so these are only
        # meaningful for schema v1.
        expected_backend = None
        distributed_build_required = False
        resolved_platforms_path = Path(platforms_path) if platforms_path else rp_mod.DEFAULT_CONFIG_PATH
        if resolved_platforms_path.is_file():
            try:
                raw = _yaml.safe_load(resolved_platforms_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    expected_backend = (raw.get("backend") or raw.get("expected_backend") or None)
                    distributed_build_required = bool(raw.get("distributed_build_required", False))
                    release_id_opt = release_id_opt or raw.get("release_id")
            except _yaml.YAMLError:
                pass

    effective = rc_mod.compose_effective_release_config(
        machine, platforms, features, expected,
        run_id=run_id_opt, release_id=release_id_opt,
        expected_backend=expected_backend, distributed_build_required=distributed_build_required,
        bundle_manifest=bundle_manifest,
    )
    exit_code = EXIT_SUCCESS if effective.ok else EXIT_BLOCKED
    effective_dict = effective.to_dict()

    if bundle_path and verification is not None:
        # Priority 4/5: freeze the just-verified release candidate into a
        # run-scoped immutable snapshot + content-addressed fingerprint,
        # binding it (Priority 5) to THIS run and to the digest of the
        # release selections just composed above -- so install-tablet-
        # release can later independently recompute that same digest from
        # this same-run report and refuse to install if they disagree.
        # Closes the TOCTOU gap between this approval and install-tablet-
        # release's first mutating ADB command -- see release_candidate.py.
        # A snapshot failure (source vanished/changed mid-copy) is itself a
        # hard BLOCK.
        try:
            candidate_fingerprint = release_candidate_mod.snapshot_release_candidate(
                verification, workspace.component_dir("release-candidate"),
                release_id=bundle_manifest.release_id, schema_version=bundle_manifest.schema_version,
                run_id=run_id_opt, release_config_digest=effective_dict["releaseConfigDigest"],
            )
        except release_candidate_mod.CandidateFingerprintError as exc:
            _record({
                "status": STATUS_BLOCKED,
                "detail": [f"Could not freeze the approved release candidate: {exc}"],
                "bundleVerification": verification.to_dict(),
            }, EXIT_BLOCKED)
            click.echo(click.style(f"[BLOCKED] Could not freeze the approved release candidate: {exc}", fg="red"), err=True)
            raise SystemExit(EXIT_BLOCKED)
        # Priority 4: the same fingerprint install-tablet-release will later
        # re-verify against, so release-config and installation evidence
        # always reference the identical approved candidate.
        effective_dict["releaseCandidateFingerprint"] = candidate_fingerprint.to_dict()

    _record(effective_dict, exit_code)
    _emit_release_config_vars(workspace, effective_dict)

    if not effective.ok:
        click.echo(click.style("[BLOCKED] Machine/release configuration conflict:", fg="red"), err=True)
        for c in effective.conflicts:
            if c.blocking:
                click.echo(f"  - {c.explanation}", err=True)
        raise SystemExit(EXIT_BLOCKED)
    click.echo(click.style(f"[OK] Effective release configuration composed for {run_id_opt}.", fg="green"), err=True)
    raise SystemExit(EXIT_SUCCESS)


@main.command("coverage-report")
@click.option("--manifest", "manifest_path", default=None, type=click.Path(), help="Path to coverage-manifest.yaml (defaults to coverage/coverage-manifest.yaml).")
@click.option("--check", is_flag=True, default=False, help="Validate the manifest and cross-check it against suites.py; exit non-zero on any contradiction.")
def coverage_report_cmd(manifest_path, check):
    """Render the human-readable coverage report from the machine-readable
    coverage manifest, or (with --check) validate the manifest and prove it
    does not contradict the actual suite membership.

    --check is what CI runs: a draft component slipped into a release suite, or
    a release-gating component missing from every composite, exits
    EXIT_INVALID_CONFIG with the exact contradiction.
    """
    from . import coverage_manifest as coverage_mod

    try:
        manifest = coverage_mod.load_manifest(manifest_path)
    except coverage_mod.CoverageManifestError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    if check:
        from . import promotion as promotion_mod

        problems = list(coverage_mod.cross_check_against_suites(manifest))
        # Also validate every scenario-promotion file and its consistency with
        # the scenario YAML + suites.py, so one CI gate covers all
        # release-metadata consistency (coverage + promotion state machine).
        try:
            for record in promotion_mod.load_all():
                for p in promotion_mod.check_consistency(record):
                    problems.append(f"promotion[{record.scenario}]: {p}")
        except promotion_mod.PromotionError as exc:
            problems.append(f"promotion file invalid: {exc}")
        if problems:
            click.echo(click.style("Release-metadata consistency check FAILED:", fg="red"), err=True)
            for p in problems:
                click.echo(f"  - {p}", err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        click.echo(click.style(
            "[OK] Coverage manifest + promotion files are internally consistent and agree with suites.py.",
            fg="green",
        ))
        raise SystemExit(EXIT_SUCCESS)

    click.echo(coverage_mod.render_report(manifest))
    raise SystemExit(EXIT_SUCCESS)


def _write_installer_report(report_path: "Path | None", payload: dict) -> None:
    """Write an installer/inspection report JSON, best-effort. A missing
    --report just means the result is printed, never a hard failure."""
    if report_path is None:
        return
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        click.echo(f"Report: {report_path}")
    except OSError as exc:
        click.echo(f"Could not write report to {report_path}: {exc}", err=True)


@main.command("verify-release-bundle")
@click.option("--bundle", "bundle_path", required=True, type=click.Path(), help="Path to the release bundle directory.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write a JSON result.")
def verify_release_bundle_cmd(bundle_path, report_path):
    """Verify a release bundle (manifest schema, full Git SHAs, package ids,
    version formats, APK existence, SHA-256 match, no unexpected files, no
    duplicate/traversal APK names) WITHOUT touching any device.

    Exits 0 when the bundle is fully trustworthy, EXIT_INVALID_CONFIG when the
    bundle the technical owner supplied is malformed (with every problem
    listed), so a broken bundle can never silently proceed to an install.
    """
    from . import release_installer

    result = release_installer.verify_release_bundle(bundle_path)
    _write_installer_report(Path(report_path) if report_path else None, result.to_dict())
    if result.ok:
        click.echo(click.style(f"[OK] Release bundle verified: {result.manifest.release_id}", fg="green"))
        for app in result.verified_apps:
            click.echo(f"     {app.key}: {app.package_id} {app.version_name} (code {app.version_code}) sha {app.git_sha[:12]}…")
        raise SystemExit(EXIT_SUCCESS)
    click.echo(click.style(f"[INVALID] Release bundle has {len(result.errors)} problem(s):", fg="red"), err=True)
    for err in result.errors:
        click.echo(f"  - {err}", err=True)
    raise SystemExit(EXIT_INVALID_CONFIG)


@main.command("verify-main-ci-evidence")
@click.option(
    "--expected-sha", "expected_sha", required=True,
    help="The full 40-character commit SHA of the ACTUAL merge/main commit. Retrieve this AFTER the "
         "PR merges -- never predict or assume it during the PR session.",
)
@click.option(
    "--summary", "summary_path", required=True, type=click.Path(exists=True, dir_okay=False),
    help="Path to a downloaded CI-evidence summary file -- calee-regression's own "
         "framework-test-summary-<sha>.json, or CaleeMobile-Regression's ci-summary-<sha>.json.",
)
@click.option(
    "--required-gate", "required_gates", multiple=True,
    help="A gate name that must be present and successful (or an explicitly not-applicable skip) in the "
         "summary's 'gates' breakdown. Repeatable. Omit to verify every gate the summary itself lists "
         "(or, for calee-regression's single-job evidence, just its identity/event).",
)
@click.option(
    "--artifact-sha256", "artifact_sha256", default=None,
    help="Optional: the expected raw-byte sha256 of the --summary file itself (e.g. recorded elsewhere "
         "when the artifact was retrieved) -- a mismatch means the file was altered since retrieval.",
)
@click.option(
    "--expected-repository", "expected_repository", default=None,
    help="Optional: the exact 'owner/repo' the evidence's own 'repository' field must match (Priority 5). "
         "Passing CaleeAdmin/CaleeMobile-Regression additionally applies that repository's CANONICAL "
         "required-gate set (owned in code by main_ci_evidence.py) regardless of --required-gate, so a "
         "missing/empty gates breakdown BLOCKS even when no --required-gate was given at all.",
)
@click.option(
    "--expected-workflow-file", "expected_workflow_file", default=None,
    help="Optional: the exact workflow file path (e.g. .github/workflows/ci.yml) the evidence's own "
         "'workflowFile' field must match.",
)
def verify_main_ci_evidence_cmd(
    expected_sha, summary_path, required_gates, artifact_sha256, expected_repository, expected_workflow_file,
):
    """Priority 8: independently verify that a downloaded CI-evidence summary
    describes the EXACT merged-main (or merge-queue) commit, never a pull
    request's HEAD commit.

    Run this AFTER a pull request has merged: download the retained
    framework-test-summary-<merge-sha>.json (or CaleeMobile-Regression's
    ci-summary-<merge-sha>.json) artifact from the Actions run that executed
    for the ACTUAL merge commit on main, and pass its SHA as --expected-sha.
    A PR-head run's evidence -- even one that passed every gate -- is REJECTED
    here; it is proof about the PR HEAD commit only, never about what
    actually landed on main.

    Exits 0 only when the evidence's schemaVersion is supported, its
    commitSha exactly matches --expected-sha, its event is a push to
    refs/heads/main or a merge_group run, its repository/workflowFile agree
    with --expected-repository/--expected-workflow-file (when given), and
    every requested/canonical (or, if neither applies, every evidence-listed)
    gate succeeded or was an explicitly not-applicable skip. Otherwise exits
    BLOCKED with every problem listed.

    NOTE (Priority 6): this command only STRUCTURALLY validates a summary
    file already on disk -- it never contacts GitHub, so it cannot prove the
    file actually came from the workflow run it claims to. See
    verify-main-ci-artifact for authenticated origin verification.
    """
    from . import main_ci_evidence as mce_mod

    try:
        summary, raw_bytes = mce_mod.load_summary(summary_path)
    except mce_mod.MainCiEvidenceError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    canonical_required_gates = None
    if expected_repository == mce_mod.CALEEMOBILE_REGRESSION_REPOSITORY:
        canonical_required_gates = mce_mod.CALEEMOBILE_REGRESSION_REQUIRED_GATES

    problems = mce_mod.verify_main_ci_evidence(
        summary, expected_sha=expected_sha, required_gates=list(required_gates) or None,
        raw_bytes=raw_bytes, expected_artifact_sha256=artifact_sha256,
        expected_repository=expected_repository, expected_workflow_file=expected_workflow_file,
        canonical_required_gates=canonical_required_gates,
    )
    click.echo(click.style(
        "STRUCTURAL VALIDATION ONLY -- ORIGIN NOT AUTHENTICATED: this checks the CONTENTS of the file at "
        "--summary for internal consistency; it does not prove GitHub Actions produced it. Use "
        "verify-main-ci-artifact for authenticated merged-main evidence.",
        fg="yellow",
    ), err=True)
    if not problems:
        click.echo(click.style(
            f"[OK] Merged-main CI evidence verified: commit {summary.get('commitSha')} "
            f"(event {summary.get('event')!r}, ref {summary.get('ref')!r}).",
            fg="green",
        ))
        raise SystemExit(EXIT_SUCCESS)
    click.echo(click.style(f"[BLOCKED] Merged-main CI evidence has {len(problems)} problem(s):", fg="red"), err=True)
    for p in problems:
        click.echo(f"  - {p}", err=True)
    raise SystemExit(EXIT_BLOCKED)


@main.command("verify-main-ci-artifact")
@click.option(
    "--repository", "repository", required=True,
    help="The exact 'owner/repo' the workflow run must belong to, e.g. CaleeAdmin/CaleeMobile-Regression "
         "or CaleeAdmin/calee-regression.",
)
@click.option("--workflow-run-id", "workflow_run_id", required=True, help="GitHub Actions workflow run ID.")
@click.option("--artifact-id", "artifact_id", required=True, help="GitHub Actions artifact ID.")
@click.option(
    "--expected-merge-sha", "expected_merge_sha", required=True,
    help="The full 40-character commit SHA of the ACTUAL merge/main commit -- retrieved AFTER the merge.",
)
@click.option(
    "--workflow-file", "workflow_file", default=None,
    help="Workflow file path (e.g. .github/workflows/ci.yml). Defaults from --repository for a known repo.",
)
@click.option(
    "--artifact-name", "artifact_name", default=None,
    help="Expected exact artifact name (embeds the merge SHA, e.g. ci-summary-<sha>). Defaults from "
         "--repository for a known repo.",
)
@click.option(
    "--result-filename", "result_filename", default=None,
    help="Expected exact filename inside the artifact ZIP (e.g. ci-summary.json). Defaults from "
         "--repository for a known repo.",
)
@click.option(
    "--artifact-zip", "artifact_zip_path", default=None, type=click.Path(exists=True, dir_okay=False),
    help="Optional: an already-downloaded artifact ZIP. Metadata (run/artifact ownership, digest) is still "
         "authenticated via the GitHub API even when this is given.",
)
@click.option(
    "--required-gate", "required_gates", multiple=True,
    help="A gate name that must be present and successful (or an explicitly not-applicable skip). "
         "Repeatable. The repository's canonical required-gate set (when known) is ALWAYS enforced too.",
)
def verify_main_ci_artifact_cmd(
    repository, workflow_run_id, artifact_id, expected_merge_sha, workflow_file, artifact_name,
    result_filename, artifact_zip_path, required_gates,
):
    """Priority 6: AUTHENTICATED verification that a merged-main CI artifact
    was actually produced by GitHub Actions for the expected repository,
    workflow, and commit -- not merely a structurally-consistent JSON file.

    Unlike verify-main-ci-evidence (which only checks the CONTENTS of an
    already-downloaded file for internal consistency), this command
    authenticates the file's ORIGIN via the GitHub API: the workflow run
    belongs to --repository, ran the expected workflow FILE (never just a
    display name), was triggered by an organic push-to-main or merge_group
    event (never workflow_dispatch/pull_request), completed with conclusion
    success, and its head_sha equals --expected-merge-sha; the artifact
    records that exact run as its owner, its name embeds the exact merge
    SHA, and GitHub's own recorded digest matches the downloaded ZIP's
    actual bytes; the ZIP contains exactly the one expected summary file
    (hardened extraction: no path traversal, no duplicate/extra entries);
    and finally the extracted summary itself passes the SAME canonical
    schema/gate verifier verify-main-ci-evidence uses.

    Requires GitHub API credentials (REGRESSION_API_TOKEN, GITHUB_TOKEN, or
    GH_TOKEN in the environment/Keychain) -- without one, this BLOCKS naming
    the exact missing secret rather than falling back to unauthenticated
    (structural-only) verification.
    """
    from . import main_ci_artifact as mca_mod
    from . import main_ci_evidence as mce_evidence_mod

    profile = mca_mod.KNOWN_PROFILES.get(repository, {})
    resolved_workflow_file = workflow_file or profile.get("workflow_path")
    resolved_result_filename = result_filename or profile.get("result_filename")
    resolved_artifact_name = artifact_name or (
        f"{profile['artifact_prefix']}{expected_merge_sha}" if profile.get("artifact_prefix") else None
    )
    missing_flags = [
        flag for flag, value in (
            ("--workflow-file", resolved_workflow_file),
            ("--artifact-name", resolved_artifact_name),
            ("--result-filename", resolved_result_filename),
        ) if not value
    ]
    if missing_flags:
        click.echo(
            f"--repository {repository!r} is not a recognised profile; {', '.join(missing_flags)} "
            f"must be supplied explicitly.",
            err=True,
        )
        raise SystemExit(EXIT_INVALID_CONFIG)

    canonical_required_gates = None
    if repository == mce_evidence_mod.CALEEMOBILE_REGRESSION_REPOSITORY:
        canonical_required_gates = mce_evidence_mod.CALEEMOBILE_REGRESSION_REQUIRED_GATES

    try:
        chain = mca_mod.acquire_main_ci_artifact(
            repository=repository, workflow_path=resolved_workflow_file,
            run_id=workflow_run_id, artifact_id=artifact_id, expected_merge_sha=expected_merge_sha,
            expected_artifact_name=resolved_artifact_name, expected_result_filename=resolved_result_filename,
            local_zip_path=artifact_zip_path, required_gates=list(required_gates) or None,
            canonical_required_gates=canonical_required_gates,
        )
    except mca_mod.MainCiArtifactError as exc:
        click.echo(click.style(f"[BLOCKED] {exc}", fg="red"), err=True)
        raise SystemExit(EXIT_BLOCKED)

    if chain.ok:
        click.echo(click.style(
            f"[OK] Authenticated merged-main CI artifact verified: commit {expected_merge_sha} "
            f"(run {workflow_run_id}, artifact {artifact_id}, repository {repository}).",
            fg="green",
        ))
        raise SystemExit(EXIT_SUCCESS)
    click.echo(click.style(
        f"[BLOCKED] Authenticated merged-main CI artifact has {len(chain.problems)} problem(s):", fg="red",
    ), err=True)
    for p in chain.problems:
        click.echo(f"  - {p}", err=True)
    raise SystemExit(EXIT_BLOCKED)


@main.command("qualification-preflight")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Path to machine.local.yaml (defaults to config/machine.local.yaml).")
@click.option("--bundle", "bundle_path", default=None, type=click.Path(), help="Release bundle directory. Verified FIRST; its effective release configuration is composed exactly like the real launcher and drives every required check below (Priority 7).")
@click.option("--distributed-build-evidence", "distributed_build_evidence_path", default=None, type=click.Path(), help="Path to the run-scoped distributed-build-acceptance report (reports/runs/<run-id>/distributed-build-acceptance/results.json, as written by record-distributed-build-acceptance) -- Priority 7: re-verified through the SAME authenticated-provenance re-verification consolidation uses, never merely a format check over an arbitrary file.")
@click.option("--manual-checks", "manual_checks_path", default=None, type=click.Path(), help="Path to manual-checks.json (defaults to config/manual-checks.json, falling back to the .example.json).")
@click.option("--main-ci-evidence", "main_ci_evidence_path", default=None, type=click.Path(), help="Path to a downloaded main-CI evidence summary (ci-summary-<sha>.json / framework-test-summary-<sha>.json) -- verified via the canonical schema/gate verifier (structural validation only; see --calee-regression-main-* / --caleemobile-regression-main-* for authenticated verification).")
@click.option("--main-ci-repository", "main_ci_repository", default=None, help="Expected 'owner/repo' for --main-ci-evidence's structural-only check, e.g. CaleeAdmin/CaleeMobile-Regression.")
@click.option("--expected-caleemobile-sha", "expected_caleemobile_sha", default=None, help="Expected CaleeMobile sibling-checkout HEAD SHA. Defaults from the release bundle's own expected identity when --bundle is given.")
@click.option("--expected-caleemobile-regression-sha", "expected_caleemobile_regression_sha", default=None, help="Expected CaleeMobile-Regression sibling-checkout HEAD SHA -- also the expected 'regressionSha' a selector-contract artifact must carry (see --selector-workflow-run-id).")
@click.option("--selector-workflow-run-id", "selector_workflow_run_id", default=None, help="Priority 6: GitHub Actions workflow run ID (CaleeMobile-Regression) that produced a selector-contract-result artifact -- with --selector-artifact-id, authenticates it via the GitHub API instead of only checking that a credential is resolvable.")
@click.option("--selector-artifact-id", "selector_artifact_id", default=None, help="GitHub Actions artifact ID of the selector-contract-result artifact, paired with --selector-workflow-run-id.")
@click.option("--selector-artifact-zip", "selector_artifact_zip", envvar="CALEEMOBILE_SELECTOR_GITHUB_ARTIFACT_ZIP", default=None, type=click.Path(exists=True, dir_okay=False), help="Already-downloaded selector-contract-result artifact ZIP. This is the same CALEEMOBILE_SELECTOR_GITHUB_ARTIFACT_ZIP input accepted by launcher 06: it avoids only the redirected ZIP download; GitHub API run/job/artifact/digest authentication still requires --selector-workflow-run-id, --selector-artifact-id, and a token.")
@click.option("--calee-regression-main-sha", "calee_regression_main_sha", default=None, help="Priority 8: expected calee-regression HEAD SHA for its OWN authenticated merged-main CI check -- never the CaleeMobile product SHA.")
@click.option("--calee-regression-main-workflow-run-id", "calee_regression_main_workflow_run_id", default=None, help="GitHub Actions workflow run ID for calee-regression's own framework-tests.yml run, paired with --calee-regression-main-artifact-id.")
@click.option("--calee-regression-main-artifact-id", "calee_regression_main_artifact_id", default=None, help="GitHub Actions artifact ID, paired with --calee-regression-main-workflow-run-id.")
@click.option("--caleemobile-regression-main-sha", "caleemobile_regression_main_sha", default=None, help="Priority 8: expected CaleeMobile-Regression HEAD SHA for its OWN authenticated merged-main CI check -- never the CaleeMobile product SHA.")
@click.option("--caleemobile-regression-main-workflow-run-id", "caleemobile_regression_main_workflow_run_id", default=None, help="GitHub Actions workflow run ID for CaleeMobile-Regression's own ci.yml run, paired with --caleemobile-regression-main-artifact-id.")
@click.option("--caleemobile-regression-main-artifact-id", "caleemobile_regression_main_artifact_id", default=None, help="GitHub Actions artifact ID, paired with --caleemobile-regression-main-workflow-run-id.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write a JSON result.")
@click.option("--run-id", "run_id_opt", envvar="CALEE_RUN_ID", default=None, help="Release run ID. With --bundle, evidence acquisition runs FIRST (acquire-release-evidence) and every discovered run/artifact ID feeds the authenticated checks below -- the normal workflow needs only --bundle and --run-id.")
@click.option("--acquire/--no-acquire", "acquire_evidence", default=True, help="Disable the automatic evidence acquisition step (diagnostics only).")
def qualification_preflight_cmd(
    config_path, bundle_path, distributed_build_evidence_path, manual_checks_path, main_ci_evidence_path,
    main_ci_repository, expected_caleemobile_sha,
    expected_caleemobile_regression_sha, selector_workflow_run_id, selector_artifact_id, selector_artifact_zip,
    calee_regression_main_sha, calee_regression_main_workflow_run_id, calee_regression_main_artifact_id,
    caleemobile_regression_main_sha, caleemobile_regression_main_workflow_run_id, caleemobile_regression_main_artifact_id,
    report_path, run_id_opt, acquire_evidence,
):
    """Priority 9 (prior session); Priority 7-8 (this session) -- a
    read-only, RELEASE-AUTHORITATIVE preflight for a technical owner about
    to run a real physical qualification. When --bundle is given, the SAME
    effective release configuration the real launcher would compose is
    derived from it, and every required check (tablet, Android-mobile vs.
    tablet distinguished, iOS matched to the CONFIGURED device identifier,
    Android SDK/build tools, exact Flutter version, Appium status +
    required drivers, sibling-checkout SHAs, authenticated selector/
    distributed-build evidence, EACH regression repository's own
    authenticated merged-main CI, kiosk/admin authorisation) is derived from
    what the release candidate ACTUALLY requires -- never merely from the
    machine's own declared capability scope. A required capability whose
    state cannot be determined BLOCKS, it is never merely a warning.

    Every check is read-only: no APK is installed, no fixture is published,
    no credential value is printed, no product API is mutated, and neither
    is any GitHub API call anything but a read.

    `overall` is READY, WARNING, or BLOCKED -- any WARNING prevents an
    unqualified READY. The JSON output's `blockedCapabilities`/
    `warnedCapabilities` name exactly which checks are not READY. Exits 0
    only when `overall` is READY; exits non-zero (BLOCKED) otherwise,
    listing every failing check.
    """
    from . import qualification_preflight as qp_mod

    # Automatic exact-identity evidence acquisition (this session): with
    # --bundle and --run-id, acquisition derives every expected identity from
    # the verified bundle, finds/authenticates the exact GitHub evidence, and
    # the discovered run/artifact IDs + cached ZIPs feed the authenticated
    # checks below. Explicit --selector-*/--*-main-* values remain supported
    # as diagnostic overrides and are authenticated identically.
    acquisition_outcome = None
    if bundle_path and run_id_opt and acquire_evidence:
        from . import evidence_acquisition as ea_mod

        report_root, plan = _acquisition_plan_or_exit(bundle_path, run_id_opt, config_path)
        overrides = _acquisition_overrides(
            selector_workflow_run_id, selector_artifact_id, selector_artifact_zip,
            calee_regression_main_workflow_run_id, calee_regression_main_artifact_id,
            caleemobile_regression_main_workflow_run_id, caleemobile_regression_main_artifact_id,
        )
        try:
            acquisition_outcome = ea_mod.acquire_release_evidence(
                plan, report_root=report_root, overrides=overrides or None,
            )
        except ea_mod.AcquisitionError as exc:
            click.echo(click.style(f"[BLOCKED] evidence acquisition: {exc}", fg="red"), err=True)
            raise SystemExit(EXIT_BLOCKED)
        click.echo("Evidence acquisition (automatic vs explicit vs cache):")
        _echo_acquisition_items(acquisition_outcome.items)

        def _feed(evidence_type):
            item = acquisition_outcome.item(evidence_type)
            if item is None or item.status not in (ea_mod.STATUS_ACQUIRED, ea_mod.STATUS_REUSED_CACHE):
                return None, None, None, None
            run = item.run_data or {}
            art = item.artifact_data or {}
            return (str(run.get("id")) if run.get("id") is not None else None,
                    str(art.get("id")) if art.get("id") is not None else None,
                    item.cached_path, item.spec.expected_head_sha)

        if not (selector_workflow_run_id or selector_artifact_id):
            rid, aid, zpath, _sha = _feed(ea_mod.TYPE_SELECTOR_CERTIFICATION)
            selector_workflow_run_id = rid or selector_workflow_run_id
            selector_artifact_id = aid or selector_artifact_id
            selector_artifact_zip = zpath or selector_artifact_zip
        if not (calee_regression_main_workflow_run_id or calee_regression_main_artifact_id):
            rid, aid, _z, sha = _feed(ea_mod.TYPE_CALEE_REGRESSION_MAIN_CI)
            calee_regression_main_workflow_run_id = rid or calee_regression_main_workflow_run_id
            calee_regression_main_artifact_id = aid or calee_regression_main_artifact_id
            calee_regression_main_sha = calee_regression_main_sha or sha
        if not (caleemobile_regression_main_workflow_run_id or caleemobile_regression_main_artifact_id):
            rid, aid, _z, sha = _feed(ea_mod.TYPE_CALEEMOBILE_REGRESSION_MAIN_CI)
            caleemobile_regression_main_workflow_run_id = rid or caleemobile_regression_main_workflow_run_id
            caleemobile_regression_main_artifact_id = aid or caleemobile_regression_main_artifact_id
            caleemobile_regression_main_sha = caleemobile_regression_main_sha or sha
        if distributed_build_evidence_path is None:
            for dist_type in (ea_mod.TYPE_DISTRIBUTED_BUILD_ANDROID, ea_mod.TYPE_DISTRIBUTED_BUILD_IOS):
                item = acquisition_outcome.item(dist_type)
                if item is not None and item.source == ea_mod.SOURCE_RECORDED and item.cached_path:
                    distributed_build_evidence_path = item.cached_path
                    break

    report = qp_mod.run_qualification_preflight(
        config_path=Path(config_path) if config_path else None,
        bundle_path=Path(bundle_path) if bundle_path else None,
        distributed_build_evidence_path=Path(distributed_build_evidence_path) if distributed_build_evidence_path else None,
        manual_checks_path=Path(manual_checks_path) if manual_checks_path else None,
        main_ci_evidence_path=Path(main_ci_evidence_path) if main_ci_evidence_path else None,
        main_ci_repository=main_ci_repository,
        expected_caleemobile_sha=expected_caleemobile_sha,
        expected_caleemobile_regression_sha=expected_caleemobile_regression_sha,
        selector_workflow_run_id=selector_workflow_run_id, selector_artifact_id=selector_artifact_id,
        selector_artifact_zip=Path(selector_artifact_zip) if selector_artifact_zip else None,
        calee_regression_main_sha=calee_regression_main_sha,
        calee_regression_main_workflow_run_id=calee_regression_main_workflow_run_id,
        calee_regression_main_artifact_id=calee_regression_main_artifact_id,
        caleemobile_regression_main_sha=caleemobile_regression_main_sha,
        caleemobile_regression_main_workflow_run_id=caleemobile_regression_main_workflow_run_id,
        caleemobile_regression_main_artifact_id=caleemobile_regression_main_artifact_id,
        repo_root=REPO_ROOT,
    )
    payload = report.to_dict()
    _write_installer_report(Path(report_path) if report_path else None, payload)

    color = {"ready": "green", "warning": "yellow", "blocked": "red"}.get(report.overall, "red")
    click.echo(click.style(f"[{payload['overall']}] qualification preflight -- {len(report.checks)} check(s):", fg=color))
    section_color = {"READY": "green", "WARNING": "yellow", "BLOCKED": "red", "NOT_APPLICABLE": "cyan"}
    for section in payload["sections"]:
        click.echo(click.style(f"-- {section['title']} [{section['status']}]", fg=section_color.get(section["status"], "red")))
        for check in report.checks:
            if check.name not in section["checks"]:
                continue
            marker = {"ready": "OK", "warning": "WARN", "blocked": "BLOCKED"}.get(check.status, check.status.upper())
            click.echo(f"  [{marker}] {check.name}: {check.detail}")
            if check.hint:
                click.echo(f"           hint: {check.hint}")

    if report.overall == qp_mod.STATUS_READY:
        raise SystemExit(EXIT_SUCCESS)
    raise SystemExit(EXIT_BLOCKED)


@main.command("inspect-tablet")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option("--serial", "serial", default=None, help="ADB serial; falls back to the config's udid.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write a JSON result.")
def inspect_tablet_cmd(config_path, serial, report_path):
    """Read-only inspection of the connected tablet: adb availability, device
    presence, installed Calee/CaleeShell versions, and the resolved HOME
    package. Uses only read-only adb commands -- never installs or mutates.

    With no device/adb (as in a CI or a laptop with nothing plugged in), this
    exits EXIT_BLOCKED with an honest "no device" result -- it never fabricates
    an inspection.
    """
    from . import release_installer

    if serial is None and config_path:
        try:
            serial = config_mod.load_config(config_path).udid
        except config_mod.ConfigError:
            serial = None
    inspection = release_installer.inspect_tablet(release_installer.real_adb_runner, serial=serial)
    _write_installer_report(Path(report_path) if report_path else None, inspection.to_dict())
    if inspection.status == release_installer.STATUS_OK:
        click.echo(click.style("[OK] Tablet inspected.", fg="green"))
        for ident in inspection.installed:
            state = f"{ident.version_name} (code {ident.version_code})" if ident.present else "not installed"
            click.echo(f"     {ident.package_id}: {state}")
        click.echo(f"     HOME resolves to: {inspection.home_package}")
        raise SystemExit(EXIT_SUCCESS)
    click.echo(click.style(f"[BLOCKED] {inspection.detail}", fg="yellow"), err=True)
    raise SystemExit(EXIT_BLOCKED)


def _record_installation_component(
    run_id_opt: "str | None", payload: dict, exit_code: int
) -> None:
    """Write the installation evidence into this run's workspace (Priority 6):
    reports/runs/<run-id>/installation/results.json + a manifest record, so the
    install is a first-class consolidated component. A payload always carries
    ``runId`` so consolidation's run-ID validation accepts it. No-op when the
    command is run standalone without a --run-id."""
    if not run_id_opt or not run_context.is_valid_run_id(run_id_opt):
        return
    workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
    workspace.ensure_created()
    payload = {"runId": run_id_opt, **payload}
    report_path = workspace.component_report_path("installation")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    manifest = _load_or_init_manifest(workspace)
    manifest.record_component("installation", report_path=str(report_path), exit_code=exit_code)
    manifest.write(workspace.manifest_path)
    click.echo(f"Installation evidence: {report_path}")


@main.command("install-tablet-release")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option("--bundle", "bundle_path", required=True, type=click.Path(), help="Path to the release bundle directory.")
@click.option("--serial", "serial", default=None, help="ADB serial; falls back to the config's udid.")
@click.option("--allow-downgrade", is_flag=True, default=False, help="Explicitly authorise a version downgrade (normally BLOCKED).")
@click.option("--plan-only", is_flag=True, default=False, help="Print/write the ordered install plan without executing it.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write a JSON result.")
@click.option(
    "--retain-diagnostics", is_flag=True, default=False,
    help="Keep the temporary pulled-APK workspace used to read the installed signer, for diagnosis. "
         "By default it is deleted after inspection.",
)
@click.option(
    "--run-id", "run_id_opt", envvar="CALEE_RUN_ID", default=None,
    help="Shared release run ID (run_context.py). When given, the full installation evidence "
         "(bundle verification + APK content/signer inspection + tablet inspection + plan + "
         "execution) is written into reports/runs/<run-id>/installation/results.json as this run's "
         "mandatory installation component.",
)
@click.option(
    "--production/--development", "production_opt", default=None,
    help="Production release profile (Priority 2): trusted signer identity for BOTH Calee and "
         "CaleeShell becomes REQUIRED (a missing/malformed/unreadable/mismatching signer BLOCKS the "
         "complete-solution verification, instead of recording 'not_compared'). For a schema-v2 "
         "release candidate, the BUNDLE's own composed profile always wins (a disagreeing "
         "--production/--development here only prints a note, never overrides it); otherwise "
         "defaults to config/release-platforms.yaml (expected_build_identity.production).",
)
def install_tablet_release_cmd(config_path, bundle_path, serial, allow_downgrade, plan_only, report_path, retain_diagnostics, run_id_opt, production_opt):
    """Verify a release bundle, INSPECT each APK's actual contents + signer, and
    then install it in the correct, data-preserving order (Calee first,
    CaleeShell second, reassert HOME, reboot, verify identities/HOME/launch).

    Order of gates (each BLOCKS before the next when it can't be trusted):
      1. bundle verification (manifest schema, checksums, absolute APK paths);
      2. actual APK content + signer inspection (Priority 5) -- the real
         application id/version must match the manifest and the canonical Calee/
         CaleeShell package; a missing SDK tool BLOCKS with setup guidance; an
         already-installed app whose signer MISMATCHES the release APK BLOCKS
         (data is never wiped to work around it);
      3. read-only tablet inspection (no device -> BLOCKED, honestly);
      4. the ordered, data-preserving install plan + its execution.

    A malformed bundle exits EXIT_INVALID_CONFIG; a tool/signer/device/version/
    HOME problem exits EXIT_BLOCKED. The installer NEVER auto-uninstalls or
    clears data. ``--plan-only`` records the ordered plan without running it.

    Priority 1 -- schema-v2 authority: when ``--run-id`` identifies a
    same-run ``release-config`` evidence report (``reports/runs/<run-id>/
    release-config/results.json``) whose OWN ``schemaVersion`` is 2, that
    report is the SOLE source of the release profile and every other release
    policy decision below -- ``config/release-platforms.yaml`` is never even
    read for this run, so a malformed/stale legacy file cannot affect a valid
    schema-v2 installation. Missing, stale, malformed, or wrong-run
    release-config evidence BLOCKS outright rather than silently falling
    back to legacy configuration. A same-run report whose OWN schema is 1,
    or the complete absence of one (a bare/diagnostic invocation with no
    run-scoped release-config at all), keeps the original
    ``release_platforms.yaml``-based behaviour unchanged.

    Priority 2 -- trusted signer policy: the post-install complete-solution
    verification requires a trusted ``signerSha256`` for BOTH Calee and
    CaleeShell (a missing/malformed/unreadable/mismatching signer BLOCKS)
    whenever this is a release-gating run -- a production release
    (schema-v2: the same-run release-config's ``releaseSelections.profile``;
    otherwise ``--production``, or config/release-platforms.yaml's
    ``expected_build_identity.production``), or ANY run carrying a ``--run-id``
    (every real release run through the launcher always does; only a bare
    ad-hoc/diagnostic invocation with no run ID is treated as non-release
    development, where an undeclared signer may still record 'not_compared').
    """
    from . import apk_inspect
    from . import release_candidate as release_candidate_mod
    from . import release_config as release_config_mod
    from . import release_installer

    # Priority 1: resolve this run's release-config evidence (if any) FIRST,
    # before any release-policy decision is made, so schema-v2 authority is
    # established up front and release_platforms.py is never even imported
    # for that path below.
    #
    # _reject_release_config is hoisted out of the "report exists" branch
    # below (rather than nested inside it, as before) because it must also
    # handle the case where NO report exists at all but a frozen candidate
    # does -- see the candidate-freeze gate further down, which is exactly
    # the fail-open path this priority closes.
    def _reject_release_config(detail: str) -> None:
        payload = {"status": "blocked", "detail": [detail]}
        _write_installer_report(Path(report_path) if report_path else None, payload)
        _record_installation_component(run_id_opt, payload, EXIT_BLOCKED)
        click.echo(click.style(f"[BLOCKED] {detail}", fg="red"), err=True)
        raise SystemExit(EXIT_BLOCKED)

    release_config_report = None
    parsed_release_config = None
    if run_id_opt:
        probe_workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
        rc_report_path = probe_workspace.component_report_path("release-config")
        if rc_report_path.is_file():
            try:
                raw_release_config = json.loads(rc_report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _reject_release_config(f"This run's release-config evidence at {rc_report_path} is unreadable: {exc}")

            run_manifest_probe = _load_or_init_manifest(probe_workspace)
            run_started_at_epoch = None
            if run_manifest_probe.started_at:
                try:
                    run_started_at_epoch = time.mktime(time.strptime(run_manifest_probe.started_at, "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    run_started_at_epoch = None
            try:
                run_context.validate_component_report(
                    raw_release_config, report_path=rc_report_path, run_id=run_id_opt, workspace=probe_workspace,
                    component="release-config", run_started_at_epoch=run_started_at_epoch,
                )
            except run_context.RunIdError as exc:
                _reject_release_config(f"This run's release-config evidence was rejected: {exc}")

            if not _RELEASE_CONFIG_REQUIRED_KEYS.issubset(raw_release_config):
                _reject_release_config(
                    f"This run's release-config evidence at {rc_report_path} is malformed "
                    f"(missing one of {sorted(_RELEASE_CONFIG_REQUIRED_KEYS)})."
                )

            if raw_release_config.get("status") != "ok":
                detail = ["This run's release-config composition did not pass -- installation cannot proceed:"]
                detail.extend(str(d) for d in raw_release_config.get("detail", []))
                _reject_release_config("\n  - ".join(detail))

            # Priority 2: strictly parse+validate every field policy below
            # actually relies on (rejecting wrong JSON types instead of
            # silently coercing them via bool()/str()), and independently
            # RECOMPUTE releaseConfigDigest from the parsed releaseSelections
            # rather than trusting this report's own stored copy of it.
            try:
                parsed_release_config = release_config_mod.parse_release_config_report(raw_release_config)
            except release_config_mod.ReleaseConfigReportError as exc:
                _reject_release_config(
                    f"This run's release-config evidence at {rc_report_path} is malformed: {exc}"
                )
            if not parsed_release_config.digest_matches:
                _reject_release_config(
                    f"This run's release-config evidence at {rc_report_path} is tampered: the stored "
                    f"releaseConfigDigest {parsed_release_config.stored_digest!r} does not match the digest "
                    f"recomputed from its own releaseSelections ({parsed_release_config.recomputed_digest!r})."
                )

            release_config_report = raw_release_config

    # Candidate freeze enforcement: once release-config has snapshotted +
    # fingerprinted this run's approved release candidate (release_candidate.
    # py), install ONLY from that immutable snapshot -- never the original,
    # still-mutable --bundle path, even if the caller still points at it, and
    # even if the original has since been corrupted or deleted entirely. This
    # redirect happens BEFORE the bundle is verified from any path, so a
    # since-tampered original drop folder can never even be READ again, let
    # alone block a run that already has a valid frozen candidate. Refuses
    # outright if the snapshot's CURRENT bytes disagree with the recorded
    # fingerprint, closing the TOCTOU gap between release-config approval and
    # the first mutating ADB command below. A run with no same-run snapshot
    # (a bare/diagnostic invocation, or release-config composed without
    # --bundle) is unaffected -- it installs from --bundle exactly as before
    # this existed.
    #
    # Priority 1: this is resolved BEFORE the schema-v2-vs-legacy policy
    # decision below (moved up from its previous position after it), so a
    # frozen candidate's mere existence can gate that decision -- a run whose
    # release-config report is missing/wrong-schema must never silently fall
    # back to legacy release-platforms.yaml policy just because a same-run
    # candidate happens not to be readable at policy-decision time.
    fingerprint = None
    candidate_workspace = None
    if run_id_opt:
        candidate_workspace = run_context.RunWorkspace(_resolved_report_root(), run_id_opt)
        snapshot_dir = candidate_workspace.component_dir("release-candidate")
        fingerprint_path = snapshot_dir / release_candidate_mod.FINGERPRINT_FILENAME
        if fingerprint_path.is_file():
            try:
                fingerprint = release_candidate_mod.load_candidate_fingerprint(fingerprint_path)
            except release_candidate_mod.CandidateFingerprintError as exc:
                payload = {
                    "status": "invalid",
                    "detail": [f"This run's release-candidate fingerprint is unreadable: {exc}"],
                }
                _write_installer_report(Path(report_path) if report_path else None, payload)
                _record_installation_component(run_id_opt, payload, EXIT_INVALID_CONFIG)
                click.echo(click.style(f"[INVALID] {payload['detail'][0]}", fg="red"), err=True)
                raise SystemExit(EXIT_INVALID_CONFIG)

            # Priority 5 (prior session): bind this verification to the SAME
            # release-config evidence that governs policy above, when one
            # exists -- a candidate copied wholesale from another run, or one
            # approved under a different release-config composition, is
            # rejected here even though its own internal envelope digest is
            # self-consistent. Priority 2 (this session): the expected digest
            # passed here is the INDEPENDENTLY RECOMPUTED one (not the
            # report's own stored copy), so this comparison never relies on a
            # stored value either.
            fp_kwargs = {}
            if release_config_report is not None:
                fp_kwargs = dict(
                    expected_run_id=run_id_opt,
                    expected_release_id=release_config_report.get("releaseId"),
                    expected_schema_version=release_config_report.get("schemaVersion"),
                    expected_release_config_digest=(
                        parsed_release_config.recomputed_digest if parsed_release_config is not None
                        else release_config_report.get("releaseConfigDigest")
                    ),
                )
            fp_problems = release_candidate_mod.verify_candidate_fingerprint(snapshot_dir, fingerprint, **fp_kwargs)
            if release_config_report is not None:
                recorded_fp = release_config_report.get("releaseCandidateFingerprint") or {}
                if recorded_fp.get("envelopeDigest") != fingerprint.envelope_digest:
                    fp_problems.append(
                        "this run's release-config evidence recorded a DIFFERENT candidate fingerprint "
                        f"(envelopeDigest {recorded_fp.get('envelopeDigest')!r}) than the one on disk in the "
                        f"release-candidate snapshot ({fingerprint.envelope_digest!r}) -- release-config, the "
                        f"candidate fingerprint, and this installation must all reference the identical "
                        f"approved candidate."
                    )
            if fp_problems:
                payload = {
                    "status": "blocked",
                    "detail": (
                        ["The approved release candidate changed after release-config approved it -- "
                         "refusing to install:"] + fp_problems
                    ),
                    "releaseCandidateFingerprint": fingerprint.to_dict(),
                }
                _write_installer_report(Path(report_path) if report_path else None, payload)
                _record_installation_component(run_id_opt, payload, EXIT_BLOCKED)
                click.echo(click.style(
                    "[BLOCKED] Approved release candidate changed since release-config -- refusing to install:",
                    fg="red",
                ), err=True)
                for p in fp_problems:
                    click.echo(f"  - {p}", err=True)
                raise SystemExit(EXIT_BLOCKED)
            # Install ONLY from the frozen snapshot from here on -- the
            # original --bundle path is never read again in this run.
            bundle_path = str(snapshot_dir)

    # Priority 1: a frozen release candidate existing for this run makes a
    # matching same-run release-config MANDATORY -- never silently fall back
    # to legacy release-platforms.yaml policy just because the report happens
    # to be missing right now (a real crash-consistency window: release-
    # config writes the candidate snapshot before it writes its own report,
    # so a killed/OOM'd process can leave exactly this state on disk). This
    # also guarantees a schema-v2 candidate can never fall back to legacy
    # policy: when a release-config report IS present, the schema-version
    # agreement between it and the candidate is already enforced above (the
    # verify_candidate_fingerprint call's expected_schema_version binding) --
    # a same-run report whose schema disagrees with the candidate is already
    # BLOCKED before this point is ever reached.
    if fingerprint is not None and release_config_report is None:
        _reject_release_config(
            f"A frozen release candidate exists for run {run_id_opt!r} (releaseId="
            f"{fingerprint.release_id!r}, schemaVersion={fingerprint.schema_version!r}) but this run has "
            f"no matching release-config evidence at {candidate_workspace.component_report_path('release-config')} "
            f"-- refusing to fall back to legacy release-platforms.yaml policy for a run with a frozen candidate."
        )

    if release_config_report is not None and release_config_report.get("schemaVersion") == 2:
        # Priority 1: schema-v2 authority. Profile and every other release
        # policy decision below come ONLY from this same-run evidence --
        # release_platforms.load_* is never called on this path.
        release_selections = release_config_report.get("releaseSelections") or {}
        parsed_selections = parsed_release_config.release_selections
        eff_production = parsed_selections.profile == "production"
        if production_opt is not None and production_opt != eff_production:
            click.echo(click.style(
                f"[NOTE] --{'production' if production_opt else 'development'} was given, but schema-v2 "
                f"release-config evidence is authoritative for the release profile "
                f"({release_selections.get('profile')!r}) -- the flag is ignored.",
                fg="yellow",
            ), err=True)
        # Priority 2: sourced from the STRICTLY-TYPED parse, not
        # bool(dict.get(...)) over untrusted JSON -- a non-boolean value here
        # already BLOCKED above, during parsing.
        selector_evidence_required = parsed_selections.calee_mobile_selector_evidence_required
        if selector_evidence_required is None:
            selector_evidence_required = True
        distributed_build_acceptance_required = parsed_selections.calee_mobile_distributed_build_acceptance_required
        if distributed_build_acceptance_required is None:
            distributed_build_acceptance_required = True
    else:
        # Schema v1, or a bare diagnostic invocation with no run-scoped
        # release-config evidence at all -- unchanged legacy behaviour.
        try:
            expected_identity = release_platforms.load_expected_build_identity()
        except release_platforms.ReleasePlatformsError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(EXIT_INVALID_CONFIG)
        eff_production = production_opt if production_opt is not None else expected_identity.production
        selector_evidence_required = None
        distributed_build_acceptance_required = None

    # Release-gating: unconditionally true in production; for a staging/
    # development profile, a run carrying a shared run ID is a real
    # launcher-driven release run (Priority 2 policy -- see docstring above).
    signer_trust_required = bool(eff_production) or bool(run_id_opt)

    verification = release_installer.verify_release_bundle(bundle_path)
    if not verification.ok:
        payload = {
            "status": "invalid", "detail": list(verification.errors),
            "bundleVerification": verification.to_dict(),
            "releaseCandidateFingerprint": fingerprint.to_dict() if fingerprint is not None else None,
        }
        _write_installer_report(Path(report_path) if report_path else None, payload)
        _record_installation_component(run_id_opt, payload, EXIT_INVALID_CONFIG)
        click.echo(click.style("[INVALID] Bundle failed verification -- refusing to install:", fg="red"), err=True)
        for err in verification.errors:
            click.echo(f"  - {err}", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    # Priority 4: the effective (machine-authoritative) config controls the
    # install plan -- the HOME activity and the Calee launch/START action come
    # from config, not hardcoded defaults, so they actually reach the installer
    # command arrays and the post-reboot verification.
    home_component = None
    calee_launch_action = None
    if config_path:
        try:
            _cfg = config_mod.load_config(config_path)
            if serial is None:
                serial = _cfg.udid
            if _cfg.shell_package and _cfg.shell_activity:
                home_component = f"{_cfg.shell_package}/{_cfg.shell_activity}"
            calee_launch_action = _cfg.start_action or None
        except config_mod.ConfigError:
            pass

    plan_kwargs = {"serial": serial, "allow_downgrade": allow_downgrade}
    if home_component:
        plan_kwargs["home_component"] = home_component
    if calee_launch_action:
        plan_kwargs["calee_launch_action"] = calee_launch_action
    plan = release_installer.build_install_plan(verification, **plan_kwargs)

    if plan_only:
        payload = {"status": "plan-only", "detail": ["Plan constructed, not executed."],
                   "bundleVerification": verification.to_dict(), "plan": plan.to_dict()}
        _write_installer_report(Path(report_path) if report_path else None, payload)
        click.echo(f"[PLAN] {len(plan.steps)} step(s) for release {plan.release_id} (not executed):")
        for step in plan.steps:
            click.echo(f"  {step.label}: {' '.join(step.argv)}")
        raise SystemExit(EXIT_SUCCESS)

    # Priority 5: inspect ACTUAL APK contents + signer before any install.
    # A signer that cannot be authoritatively read (SIGNER_UNKNOWN) BLOCKS here,
    # before execute_install_plan is ever reached -- no install command runs.
    signer_reader = apk_inspect.device_installed_signer_reader(
        serial=serial, retain_diagnostics=retain_diagnostics
    )
    inspection = apk_inspect.preinstall_inspect_bundle(verification, installed_signer_reader=signer_reader)
    if inspection.status != apk_inspect.STATUS_OK:
        exit_code = EXIT_INVALID_CONFIG if inspection.status == apk_inspect.STATUS_INVALID else EXIT_BLOCKED
        payload = {"status": inspection.status, "detail": list(inspection.detail),
                   "bundleVerification": verification.to_dict(),
                   "apkInspection": inspection.to_dict(), "plan": plan.to_dict()}
        _write_installer_report(Path(report_path) if report_path else None, payload)
        _record_installation_component(run_id_opt, payload, exit_code)
        label = "INVALID" if inspection.status == apk_inspect.STATUS_INVALID else "BLOCKED"
        click.echo(click.style(f"[{label}] APK content/signer inspection did not pass:", fg="yellow"), err=True)
        for d in inspection.detail:
            click.echo(f"  - {d}", err=True)
        raise SystemExit(exit_code)

    # Read-only tablet pre-install inspection (installed identities + HOME).
    tablet_inspection = release_installer.inspect_tablet(release_installer.real_adb_runner, serial=serial)

    execute_kwargs = {}
    if calee_launch_action:
        execute_kwargs["calee_launch_action"] = calee_launch_action
    execution = release_installer.execute_install_plan(plan, verification, release_installer.real_adb_runner, **execute_kwargs)
    # Wireless ADB may receive a new mDNS/tcp transport after reboot.  The
    # installer only sets this after a unique stable-identity match.
    serial = execution.serial or serial
    # Record the tablet's own stable identity (read-only) alongside the
    # installation evidence, so a later `resume-release` can confirm the
    # SAME physical tablet is still connected before reusing this passed
    # installation -- see resume_release.evaluate_installation_reuse.
    tablet_stable_identity, _tablet_identity_detail = release_installer.capture_device_identity(
        release_installer.real_adb_runner, serial
    )
    status = "ok" if execution.status == release_installer.STATUS_OK else "blocked"
    detail = [] if status == "ok" else [execution.detail or "Installation did not complete."]
    if tablet_inspection.status != release_installer.STATUS_OK and status == "ok":
        # The install steps succeeded but the pre-install device read did not --
        # record it, but the execution's own verify steps are authoritative.
        detail.append(f"Tablet pre-install inspection: {tablet_inspection.detail}")

    # Priority 2: after a successful install+reboot, verify the COMPLETE tablet
    # solution -- BOTH Calee and CaleeShell (present/version/signer, plus Calee's
    # START action and CaleeShell as HOME) -- even when this release replaced
    # only one of them. A gap in the unchanged app BLOCKS the release.
    solution = None
    if status == "ok":
        solution_kwargs = {}
        if calee_launch_action:
            solution_kwargs["calee_launch_action"] = calee_launch_action
        solution = release_installer.verify_tablet_solution(
            verification.expected_app("calee"),
            verification.expected_app("caleeShell"),
            release_installer.real_adb_runner,
            serial=serial,
            release_id=plan.release_id,
            installed_signer_reader=signer_reader,
            signer_trust_required=signer_trust_required,
            **solution_kwargs,
        )
        if solution.status != release_installer.STATUS_OK:
            status = "blocked"
            detail.append(f"Complete-solution verification: {solution.detail}")

    payload = {
        "status": status,
        "detail": detail,
        "bundleVerification": verification.to_dict(),
        "apkInspection": inspection.to_dict(),
        "tabletInspection": tablet_inspection.to_dict(),
        "plan": plan.to_dict(),
        "execution": execution.to_dict(),
        "solutionVerification": solution.to_dict() if solution is not None else None,
        "releaseId": plan.release_id,
        "productionProfile": bool(eff_production),
        "signerTrustRequired": signer_trust_required,
        # Priority 1: which source actually governed this installation's
        # release policy -- "release-config-v2" (release_platforms.py never
        # consulted), "release-config-v1"/"legacy" (release-platforms.yaml,
        # or explicit CLI flags with no run-scoped release-config at all).
        "releasePolicySource": (
            "release-config-v2" if (release_config_report is not None and release_config_report.get("schemaVersion") == 2)
            else ("release-config-v1" if release_config_report is not None else "legacy")
        ),
        "selectorEvidenceRequired": selector_evidence_required,
        "distributedBuildAcceptanceRequired": distributed_build_acceptance_required,
        "releaseCandidateFingerprint": fingerprint.to_dict() if fingerprint is not None else None,
        "tabletStableIdentity": tablet_stable_identity.to_dict() if tablet_stable_identity is not None else None,
    }
    _write_installer_report(Path(report_path) if report_path else None, payload)
    exit_code = EXIT_SUCCESS if status == "ok" else EXIT_BLOCKED
    _record_installation_component(run_id_opt, payload, exit_code)
    if status == "ok":
        click.echo(click.style(f"[OK] Installed and verified the complete solution for release {plan.release_id}.", fg="green"))
        raise SystemExit(EXIT_SUCCESS)
    click.echo(click.style(f"[BLOCKED] {'; '.join(detail) or execution.detail}", fg="yellow"), err=True)
    raise SystemExit(EXIT_BLOCKED)


def _resume_serial(config_path: "str | None", serial: "str | None") -> "str | None":
    if serial is not None:
        return serial
    if config_path:
        try:
            return config_mod.load_config(config_path).udid
        except config_mod.ConfigError:
            return None
    return None


def _print_resume_decisions(decisions) -> None:
    from . import resume_release

    for decision in decisions:
        label = {
            resume_release.DECISION_REUSE: "REUSED PASS",
            resume_release.DECISION_EXECUTE: "REQUIRES EXECUTION",
            resume_release.DECISION_REFUSED: "REFUSED (must re-execute)",
        }[decision.decision]
        click.echo(f"  {decision.component}: {label} -- {decision.reason}")


@main.command("inspect-resume")
@click.option("--run-id", "run_id_opt", required=True, help="The run ID to inspect for resumability.")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Path to machine/tester config (used only to resolve --serial and to resolve the report root).")
@click.option("--serial", "serial", default=None, help="ADB serial for the bounded, read-only tablet-identity/installed-package recheck; falls back to the config's udid. Omit when no tablet is attached this invocation.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write a JSON inspection result.")
def inspect_resume_cmd(run_id_opt, config_path, serial, report_path):
    """Read-only: report whether a blocked release run can be resumed.

    Never mutates anything -- no attempt is recorded, Prepare is never
    rerun, and the tablet is only ever touched with the same bounded,
    read-only identity probe a real resume would perform. Reports every
    immutable-input mismatch, which components are reusable, which require
    execution, and which can never be reused (see docs/RELEASE_POLICY.md).

    Exit codes: 0 when the run is resumable (or has never been attempted
    before, and would establish its immutable-input baseline on first
    resume); 2 for a malformed --run-id or a run workspace that doesn't
    exist; 3 when resuming is refused (an immutable input no longer
    matches the original attempt, or a tablet/installed-package identity
    changed) -- a new release run is required.
    """
    from . import release_installer
    from . import resume_release

    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r} (expected letters/digits/._- only).", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    report_root = _resolved_report_root(config_path)
    workspace = run_context.RunWorkspace(report_root, run_id_opt)
    if not workspace.root.is_dir():
        click.echo(f"No run workspace found for run ID {run_id_opt!r} at {workspace.root}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    serial = _resume_serial(config_path, serial)
    outcome = resume_release.inspect_resume(
        run_id_opt, repo_root=REPO_ROOT, report_root=report_root,
        adb_runner=release_installer.real_adb_runner, tablet_serial=serial,
    )
    click.echo(f"Run ID: {run_id_opt}")
    click.echo(f"Would be attempt: {outcome.attempt_number}")
    if outcome.immutable_mismatches:
        click.echo(click.style("Immutable-input mismatches (resume would be REFUSED):", fg="red"), err=True)
        for problem in outcome.immutable_mismatches:
            click.echo(f"  - {problem}", err=True)
    else:
        click.echo(click.style("Immutable inputs: MATCH the original attempt.", fg="green"))
    if outcome.decisions:
        click.echo("Components:")
        _print_resume_decisions(outcome.decisions)
    # This session: state whether missing evidence can now be automatically
    # acquired (read-only assessment -- no GitHub call, no download).
    evidence_note = _resume_evidence_acquirability(workspace)
    click.echo(f"Evidence acquisition: {evidence_note}")
    if report_path:
        _write_installer_report(Path(report_path), {
            "runId": run_id_opt,
            "attemptNumber": outcome.attempt_number,
            "resumable": outcome.resumable,
            "immutableMismatches": outcome.immutable_mismatches,
            "components": [d.to_dict() for d in outcome.decisions],
            "evidenceAcquisition": evidence_note,
        })
    if outcome.resumable:
        click.echo(click.style("[OK] This run can be resumed.", fg="green"))
    else:
        click.echo(click.style("[BLOCKED] This run cannot be resumed -- a new release run is required.", fg="red"), err=True)
    raise SystemExit(outcome.exit_code)


@main.command("resume-release")
@click.option("--run-id", "run_id_opt", required=True, help="The blocked run to resume.")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Path to machine/tester config, passed through to a rerun of Prepare and used to resolve --serial and the report root.")
@click.option("--suite", "suite_name", default=None, help="Suite name to pass through to a rerun of Prepare (see the `prepare` command).")
@click.option("--serial", "serial", default=None, help="ADB serial for the bounded, read-only tablet-identity/installed-package recheck; falls back to the config's udid.")
@click.option("--tester", "tester_opt", envvar="CALEE_TESTER_ID", default=None, help="Operator identity recorded on this attempt, when available.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write a JSON attempt summary.")
def resume_release_cmd(run_id_opt, config_path, suite_name, serial, tester_opt, report_path):
    """Resume a blocked release qualification without repeating already-passed
    destructive or disruptive steps.

    Fail-closed: refuses outright (exit 3) when any immutable input --
    release ID, release-candidate fingerprint, APK digests, expected
    identities, release configuration digest, backend, profile, platform/
    feature scope, calee-regression/CaleeMobile-Regression SHAs, or tablet
    stable identity -- no longer matches the ORIGINAL attempt. There is no
    flag to bypass a mismatch; a refused resume always means a new release
    run is required.

    A previously-passed installation is reused (no reinstall, no reboot)
    only after a bounded, read-only recheck confirms the same tablet and
    the same installed package identity. Prepare (environment readiness +
    the deterministic fixture) is rerun in-process whenever it is not
    reusable, exactly as a fresh run would. Every other component is
    decided (reused / requires execution / refused) but left for the
    normal per-component commands to actually (re-)execute -- see the
    tester launcher integration for how this is wired end-to-end.

    Every call is recorded as a new, immutable attempt under
    reports/runs/<run-id>/attempts/<n>/ -- a later PASS never erases an
    earlier FAIL/BLOCKED attempt's history.

    Exit codes: 0 resume succeeded (remaining components, if any, still
    need execution -- see the printed list); 1 a mandatory component
    already carries a real product regression that a resume cannot fix;
    2 invalid --run-id or no run workspace; 3 resume refused (a new
    release run is required).
    """
    from . import release_installer
    from . import resume_release

    if not run_context.is_valid_run_id(run_id_opt):
        click.echo(f"Invalid --run-id {run_id_opt!r} (expected letters/digits/._- only).", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    report_root = _resolved_report_root(config_path)
    workspace = run_context.RunWorkspace(report_root, run_id_opt)
    if not workspace.root.is_dir():
        click.echo(f"No run workspace found for run ID {run_id_opt!r} at {workspace.root}.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    serial = _resume_serial(config_path, serial)
    outcome = resume_release.perform_resume(
        run_id_opt, repo_root=REPO_ROOT, report_root=report_root,
        adb_runner=release_installer.real_adb_runner, tablet_serial=serial,
        config_path=config_path, suite_name=suite_name, operator=tester_opt,
        evidence_acquirer=_default_resume_evidence_acquirer(report_root),
    )
    if outcome.attempt is not None and outcome.attempt.evidence_acquisition:
        summary = outcome.attempt.evidence_acquisition
        click.echo(f"Evidence acquisition for this attempt: {summary.get('status', 'unknown')}"
                   + (f" -- {summary.get('detail')}" if summary.get("detail") else ""))
    click.echo(f"Run ID: {run_id_opt}")
    click.echo(f"Attempt: {outcome.attempt_number}")
    if outcome.immutable_mismatches:
        click.echo(click.style("[BLOCKED] Immutable-input mismatch -- a new release run is required:", fg="red"), err=True)
        for problem in outcome.immutable_mismatches:
            click.echo(f"  - {problem}", err=True)
    else:
        click.echo(click.style("Immutable inputs: MATCH the original attempt.", fg="green"))
        if outcome.decisions:
            click.echo("Components:")
            _print_resume_decisions(outcome.decisions)
    if report_path and outcome.attempt is not None:
        _write_installer_report(Path(report_path), outcome.attempt.to_dict())
    if outcome.exit_code == EXIT_SUCCESS:
        click.echo(click.style(f"[OK] Attempt {outcome.attempt_number} resumed for run {run_id_opt}.", fg="green"))
    elif outcome.exit_code == EXIT_REGRESSION:
        click.echo(click.style("[REGRESSION] A mandatory component already carries a real product FAIL.", fg="red"), err=True)
    else:
        click.echo(click.style("[BLOCKED] This run cannot be resumed -- a new release run is required.", fg="red"), err=True)
    raise SystemExit(outcome.exit_code)


@main.command("list-resumable-runs")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Path to machine/tester config (used only to resolve the report root).")
def list_resumable_runs_cmd(config_path):
    """Read-only: list every run workspace under reports/runs/, for a tester
    to explicitly choose one to resume (see the resume launcher). Never
    picks a run automatically -- this only ever lists them.
    """
    from . import resume_release

    report_root = _resolved_report_root(config_path)
    runs = resume_release.list_runs(REPO_ROOT, report_root=report_root)
    if not runs:
        click.echo("No runs found under reports/runs/.")
        raise SystemExit(EXIT_SUCCESS)
    click.echo(resume_release.render_run_menu(runs))
    raise SystemExit(EXIT_SUCCESS)


@main.command("select-run-to-resume")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Path to machine/tester config (used only to resolve the report root).")
@click.option("--out-file", "out_file", default=None, type=click.Path(), help="Write the selected run ID here (one line), for a shell launcher to read back. Nothing is written if the tester cancels.")
def select_run_to_resume_cmd(config_path, out_file):
    """Interactive, tester-facing: list every run under reports/runs/ and
    require an EXPLICIT choice -- never auto-picks the newest run. See the
    "08 Resume Blocked Release" launcher, which drives this then hands the
    selected run ID to `resume-release`.
    """
    from . import resume_release

    report_root = _resolved_report_root(config_path)
    runs = resume_release.list_runs(REPO_ROOT, report_root=report_root)
    chosen = resume_release.choose_run(runs)
    if chosen is None:
        raise SystemExit(EXIT_SUCCESS)
    if out_file:
        Path(out_file).write_text(chosen.run_id + "\n", encoding="utf-8")
    click.echo(chosen.run_id)
    raise SystemExit(EXIT_SUCCESS)


def _load_expected_identity_json(path: "str | None", flag_name: str) -> "dict | None":
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"{flag_name} {path!r} could not be read as JSON: {exc}", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    if not isinstance(raw, dict):
        click.echo(f"{flag_name} {path!r} must contain a JSON object.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    return raw


@main.command("assemble-release-bundle")
@click.option("--release-id", required=True, help="Release candidate id, e.g. 2026.07.20-rc3. Ambiguous references (e.g. 'latest') are rejected downstream by the identity checks below.")
@click.option("--profile", type=click.Choice(["staging", "production"]), required=True)
@click.option("--backend", required=True, help="Backend base URL this release targets.")
@click.option("--calee-apk", type=click.Path(exists=True), default=None, help="Path to an already-signed calee.apk. Omit when Calee is unchanged this release (then --calee-expected is required).")
@click.option("--calee-git-sha", default=None, help="Full 40-character Git SHA the Calee APK was built from. Required with --calee-apk.")
@click.option("--calee-expected", type=click.Path(exists=True), default=None, help="JSON file with Calee's expected installed identity (packageId/versionName/versionCode/gitSha/signerSha256), required when --calee-apk is omitted.")
@click.option("--caleeshell-apk", type=click.Path(exists=True), default=None, help="Path to an already-signed caleeshell.apk. Omit when CaleeShell is unchanged this release (then --caleeshell-expected is required).")
@click.option("--caleeshell-git-sha", default=None, help="Full 40-character Git SHA the CaleeShell APK was built from. Required with --caleeshell-apk.")
@click.option("--caleeshell-expected", type=click.Path(exists=True), default=None, help="JSON file with CaleeShell's expected installed identity, required when --caleeshell-apk is omitted.")
@click.option("--caleemobile-sha", required=True, help="Full 40-character CaleeMobile Git SHA this release expects.")
@click.option("--caleemobile-version", required=True, help="CaleeMobile pubspec version+build, e.g. 0.0.24+24.")
@click.option("--selector-evidence-required/--no-selector-evidence-required", default=True, help="Whether release certification requires CaleeMobile selector evidence (Priority 8).")
@click.option("--distributed-build-acceptance-required/--no-distributed-build-acceptance-required", default=True)
@click.option("--tablet/--no-tablet", "platform_tablet", default=True)
@click.option("--mobile-android/--no-mobile-android", "platform_android", default=True)
@click.option("--mobile-ios/--no-mobile-ios", "platform_ios", default=True)
@click.option("--sync/--no-sync", "feature_sync", default=True)
@click.option("--meals/--no-meals", "feature_meals", default=True)
@click.option("--onboarding/--no-onboarding", "feature_onboarding", default=True)
@click.option("--google-calendar/--no-google-calendar", "feature_google_calendar", default=True)
@click.option("--kiosk-admin/--no-kiosk-admin", "feature_kiosk_admin", default=True)
@click.option("--notifications/--no-notifications", "feature_notifications", default=True)
@click.option("--source-repo", default=None, help="Optional provenance: the source repository (e.g. CaleeAdmin/Calee). Metadata only -- never used to fetch anything.")
@click.option("--source-workflow-run-id", default=None, help="Optional provenance: the CI workflow run id these APKs came from.")
@click.option("--source-artifact-name", default=None, help="Optional provenance: the CI artifact name these APKs came from.")
@click.option("--source-commit", default=None, help="Optional provenance: the source commit these APKs were built from.")
@click.option("--source-artifact-digest", default=None, help="Optional provenance: the CI artifact's own digest.")
@click.option("--out", "out_dir", required=True, type=click.Path(), help="Output directory for the assembled bundle (e.g. ~/Calee-Releases/current).")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write a JSON assembly result.")
def assemble_release_bundle_cmd(
    release_id, profile, backend, calee_apk, calee_git_sha, calee_expected,
    caleeshell_apk, caleeshell_git_sha, caleeshell_expected, caleemobile_sha, caleemobile_version,
    selector_evidence_required, distributed_build_acceptance_required,
    platform_tablet, platform_android, platform_ios,
    feature_sync, feature_meals, feature_onboarding, feature_google_calendar, feature_kiosk_admin, feature_notifications,
    source_repo, source_workflow_run_id, source_artifact_name, source_commit, source_artifact_digest,
    out_dir, report_path,
):
    """Deterministically assemble a schema-version-2 release bundle from
    already-signed, locally-available APKs (Priority 4): inspects each APK's
    ACTUAL package id/version/signer (never signs anything), generates SHA-256
    checksums, and writes a release-manifest.json that verify-release-bundle/
    install-tablet-release/release-config can consume directly.

    Supports a Calee-only, CaleeShell-only, or both-app release. An app this
    release does not ship an APK for still needs an EXPLICIT expected
    installed identity (--calee-expected/--caleeshell-expected) -- an
    unchanged application is never silently dropped from the manifest.

    Never downloads anything: every APK is an already-local path, and the
    optional --source-* provenance flags are recorded verbatim as metadata,
    never used to fetch an artifact. No GitHub (or any other) credential is
    ever a parameter here, so none can leak into arguments, this command's
    report, or the generated manifest.
    """
    from . import release_bundle_assembly as rba_mod

    if calee_apk and not calee_git_sha:
        click.echo("--calee-git-sha is required when --calee-apk is given.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    if caleeshell_apk and not caleeshell_git_sha:
        click.echo("--caleeshell-git-sha is required when --caleeshell-apk is given.", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    calee_expected_raw = _load_expected_identity_json(calee_expected, "--calee-expected")
    caleeshell_expected_raw = _load_expected_identity_json(caleeshell_expected, "--caleeshell-expected")

    provenance = {}
    if source_repo:
        provenance["repository"] = source_repo
    if source_workflow_run_id:
        provenance["workflowRunId"] = source_workflow_run_id
    if source_artifact_name:
        provenance["artifactName"] = source_artifact_name
    if source_commit:
        provenance["sourceCommit"] = source_commit
    if source_artifact_digest:
        provenance["artifactDigest"] = source_artifact_digest

    assembly = rba_mod.assemble_release_bundle(
        release_id=release_id, profile=profile, backend=backend,
        calee_apk=calee_apk, calee_git_sha=calee_git_sha, calee_expected=calee_expected_raw,
        caleeshell_apk=caleeshell_apk, caleeshell_git_sha=caleeshell_git_sha, caleeshell_expected=caleeshell_expected_raw,
        caleemobile_sha=caleemobile_sha, caleemobile_version=caleemobile_version,
        selector_evidence_required=selector_evidence_required,
        distributed_build_acceptance_required=distributed_build_acceptance_required,
        platforms={"tablet": platform_tablet, "mobileAndroid": platform_android, "mobileIos": platform_ios},
        features={
            "synchronization": feature_sync, "meals": feature_meals, "onboarding": feature_onboarding,
            "googleCalendar": feature_google_calendar, "kioskAdmin": feature_kiosk_admin,
            "notifications": feature_notifications,
        },
        provenance=provenance or None,
    )

    if not assembly.ok:
        _write_installer_report(Path(report_path) if report_path else None, assembly.to_dict())
        click.echo(click.style(f"[INVALID] Release bundle assembly has {len(assembly.errors)} problem(s):", fg="red"), err=True)
        for err in assembly.errors:
            click.echo(f"  - {err}", err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)

    written = rba_mod.write_release_bundle(assembly, out_dir)
    _write_installer_report(Path(report_path) if report_path else None, assembly.to_dict())
    click.echo(click.style(f"[OK] Assembled release bundle {release_id} at {written}.", fg="green"))
    for key in ("calee", "caleeShell"):
        section = assembly.manifest["tabletSolution"][key]
        if section["installArtifact"]:
            click.echo(f"     {key}: installing {section['apk']} -> {section['expectedInstalled']['versionName']} (code {section['expectedInstalled']['versionCode']})")
        else:
            click.echo(f"     {key}: unchanged, expected {section['expectedInstalled']['versionName']} (code {section['expectedInstalled']['versionCode']})")
    raise SystemExit(EXIT_SUCCESS)


# --- exact-identity release evidence acquisition -----------------------------


def _acquisition_plan_or_exit(bundle_path, run_id_opt, config_path):
    """Shared derivation for acquire/inspect-release-evidence: resolves the
    report root and derives the evidence plan. Invalid usage exits 2 BEFORE
    any GitHub call could happen."""
    from . import evidence_acquisition as ea_mod

    report_root = _resolved_report_root(config_path)
    try:
        plan = ea_mod.derive_evidence_plan(
            bundle_path=Path(bundle_path), run_id=run_id_opt,
            repo_root=REPO_ROOT, report_root=report_root,
        )
    except ea_mod.AcquisitionUsageError as exc:
        click.echo(click.style(f"[INVALID] {exc}", fg="red"), err=True)
        raise SystemExit(EXIT_INVALID_CONFIG)
    return report_root, plan


def _acquisition_overrides(selector_run_id, selector_artifact_id, selector_zip,
                           cr_run_id, cr_artifact_id, cmr_run_id, cmr_artifact_id):
    from . import evidence_acquisition as ea_mod

    overrides = {}
    if selector_run_id or selector_artifact_id or selector_zip:
        overrides[ea_mod.TYPE_SELECTOR_CERTIFICATION] = {
            "run_id": selector_run_id, "artifact_id": selector_artifact_id, "zip_path": selector_zip,
        }
    if cr_run_id or cr_artifact_id:
        overrides[ea_mod.TYPE_CALEE_REGRESSION_MAIN_CI] = {
            "run_id": cr_run_id, "artifact_id": cr_artifact_id,
        }
    if cmr_run_id or cmr_artifact_id:
        overrides[ea_mod.TYPE_CALEEMOBILE_REGRESSION_MAIN_CI] = {
            "run_id": cmr_run_id, "artifact_id": cmr_artifact_id,
        }
    return overrides


_ACQ_STATUS_STYLE = {
    "acquired": ("green", "ACQUIRED"),
    "reused-cache": ("green", "REUSED"),
    "not_applicable": ("cyan", "N/A"),
    "blocked": ("red", "BLOCKED"),
    "contradicted": ("red", "CONTRADICTED"),
}


def _echo_acquisition_items(items):
    for item in items:
        color, label = _ACQ_STATUS_STYLE.get(item.status, ("red", item.status.upper()))
        source = f" ({item.source})" if item.source else ""
        click.echo(click.style(f"  [{label}] {item.spec.evidence_type}{source}", fg=color))
        for problem in item.problems:
            click.echo(f"      - {problem}")
        if item.remediation:
            click.echo(f"      remediation: {item.remediation}")


def _frozen_candidate_bundle_dir(workspace):
    """The run's frozen release candidate directory (manifest + checksums +
    APKs), usable as the --bundle identity source for acquisition. Returns
    None when this run has no (readable) frozen candidate."""
    from . import release_candidate as release_candidate_mod

    root = workspace.component_dir("release-candidate")
    if not root.is_dir():
        return None
    for manifest in sorted(root.rglob(release_candidate_mod.MANIFEST_NAME)):
        if (manifest.parent / release_candidate_mod.CHECKSUMS_NAME).is_file():
            return manifest.parent
    return None


def _resume_evidence_acquirability(workspace):
    """Read-only, network-free assessment for inspect-resume: can missing
    evidence now be automatically acquired?"""
    bundle_dir = _frozen_candidate_bundle_dir(workspace)
    if bundle_dir is None:
        return ("not automatically acquirable -- this run has no frozen release candidate; "
                "run acquire-release-evidence with the original --bundle.")
    if not github_artifact_mod.resolve_token():
        missing = " or ".join(github_artifact_mod.TOKEN_ENV_VARS)
        return (f"missing evidence could be acquired from the frozen candidate at {bundle_dir}, "
                f"but no GitHub credentials are available (set one of {missing}).")
    return (f"missing evidence CAN be automatically acquired (frozen candidate: {bundle_dir}; "
            f"GitHub credentials available). resume-release will acquire it before rerunning "
            f"blocked components.")


def _default_resume_evidence_acquirer(report_root):
    """The evidence acquirer resume-release hands to perform_resume: acquires
    missing evidence from the run's own frozen candidate BEFORE blocked
    components are re-decided. Fail-closed: no candidate or no token yields a
    'skipped'/'blocked' summary, never an unauthenticated fallback."""
    from . import evidence_acquisition as ea_mod

    def _acquire(workspace):
        bundle_dir = _frozen_candidate_bundle_dir(workspace)
        if bundle_dir is None:
            return {"status": "skipped",
                    "detail": "no frozen release candidate in this run workspace."}
        if not github_artifact_mod.resolve_token():
            missing = " or ".join(github_artifact_mod.TOKEN_ENV_VARS)
            return {"status": "blocked",
                    "detail": f"GitHub credentials missing (set one of {missing})."}
        try:
            plan = ea_mod.derive_evidence_plan(
                bundle_path=bundle_dir, run_id=workspace.run_id,
                repo_root=REPO_ROOT, report_root=Path(report_root),
            )
            outcome = ea_mod.acquire_release_evidence(plan, report_root=Path(report_root))
        except (ea_mod.AcquisitionUsageError, ea_mod.AcquisitionError) as exc:
            return {"status": "blocked", "detail": str(exc)}
        return {
            "status": "ok" if outcome.ok else "blocked",
            "manifestPath": outcome.manifest_path,
            "items": {i.spec.evidence_type: i.status for i in outcome.items},
        }

    return _acquire


@main.command("acquire-release-evidence")
@click.option("--bundle", "bundle_path", required=True, type=click.Path(exists=True, file_okay=False), help="Verified release bundle directory -- the ONLY identity source of truth (with the effective release configuration). Never 'the newest GitHub run'.")
@click.option("--run-id", "run_id_opt", required=True, help="Release run ID whose workspace receives the cached evidence + acquisition manifest.")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Path to machine.local.yaml (report-root resolution only).")
@click.option("--selector-run-id", default=None, help="DIAGNOSTIC override: selector workflow run ID. Authenticated exactly like discovered evidence; a mismatch BLOCKS.")
@click.option("--selector-artifact-id", default=None, help="DIAGNOSTIC override: selector artifact ID.")
@click.option("--selector-artifact-zip", default=None, type=click.Path(exists=True, dir_okay=False), help="DIAGNOSTIC override: already-downloaded selector artifact ZIP (still authenticated against GitHub metadata + digest).")
@click.option("--calee-regression-main-run-id", default=None, help="DIAGNOSTIC override: calee-regression merged-main workflow run ID.")
@click.option("--calee-regression-main-artifact-id", default=None, help="DIAGNOSTIC override: calee-regression merged-main artifact ID.")
@click.option("--caleemobile-regression-main-run-id", default=None, help="DIAGNOSTIC override: CaleeMobile-Regression merged-main workflow run ID.")
@click.option("--caleemobile-regression-main-artifact-id", default=None, help="DIAGNOSTIC override: CaleeMobile-Regression merged-main artifact ID.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional extra copy of the acquisition manifest.")
def acquire_release_evidence_cmd(bundle_path, run_id_opt, config_path,
                                 selector_run_id, selector_artifact_id, selector_artifact_zip,
                                 calee_regression_main_run_id, calee_regression_main_artifact_id,
                                 caleemobile_regression_main_run_id, caleemobile_regression_main_artifact_id,
                                 report_path):
    """Fail-closed exact-identity evidence acquisition: derives every expected
    identity from the verified release bundle + effective release
    configuration, locates the EXACT matching GitHub Actions evidence
    (exact repository, approved workflow path, approved event, exact SHA /
    release tuple, exactly one artifact), authenticates run ownership and the
    GitHub-recorded digest, caches the bytes under
    reports/runs/<run-id>/evidence/acquired/ and writes a secret-free
    acquisition manifest. Zero or ambiguous matches BLOCK; 'latest successful
    run' is never used. Exit codes: 0 acquired, 1 authenticated evidence
    contradiction (a genuine regression), 2 invalid usage/bundle, 3 missing/
    ambiguous/unauthenticated evidence."""
    from . import evidence_acquisition as ea_mod

    report_root, plan = _acquisition_plan_or_exit(bundle_path, run_id_opt, config_path)
    overrides = _acquisition_overrides(
        selector_run_id, selector_artifact_id, selector_artifact_zip,
        calee_regression_main_run_id, calee_regression_main_artifact_id,
        caleemobile_regression_main_run_id, caleemobile_regression_main_artifact_id,
    )
    try:
        outcome = ea_mod.acquire_release_evidence(
            plan, report_root=report_root, overrides=overrides or None,
        )
    except ea_mod.AcquisitionError as exc:
        click.echo(click.style(f"[BLOCKED] {exc}", fg="red"), err=True)
        raise SystemExit(EXIT_BLOCKED)
    _echo_acquisition_items(outcome.items)
    if outcome.manifest_path:
        click.echo(f"Acquisition manifest: {outcome.manifest_path}")
    if report_path:
        _write_installer_report(Path(report_path), outcome.to_manifest_dict())
    raise SystemExit(outcome.exit_code)


@main.command("inspect-release-evidence")
@click.option("--bundle", "bundle_path", required=True, type=click.Path(exists=True, file_okay=False), help="Verified release bundle directory.")
@click.option("--run-id", "run_id_opt", required=True, help="Release run ID whose workspace/cache is inspected.")
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path(), help="Path to machine.local.yaml (report-root resolution only).")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional path to write the JSON planning report.")
def inspect_release_evidence_cmd(bundle_path, run_id_opt, config_path, report_path):
    """Read-only planning twin of acquire-release-evidence: reports the
    expected evidence, what is already cached, what needs a GitHub lookup,
    missing credentials, matching workflow runs / ambiguity, distributed-
    provider prerequisites, and whether acquisition can proceed. Downloads
    nothing and writes nothing into the run workspace."""
    from . import evidence_acquisition as ea_mod

    report_root, plan = _acquisition_plan_or_exit(bundle_path, run_id_opt, config_path)
    try:
        result = ea_mod.inspect_release_evidence(plan, report_root=report_root)
    except ea_mod.AcquisitionError as exc:
        click.echo(click.style(f"[BLOCKED] {exc}", fg="red"), err=True)
        raise SystemExit(EXIT_BLOCKED)
    _write_installer_report(Path(report_path) if report_path else None, result)
    click.echo(f"Credentials available: {'yes' if result['credentialsAvailable'] else 'NO'}")
    for entry in result["items"]:
        spec = entry["spec"]
        cached = " [cached]" if entry.get("cached") else ""
        click.echo(f"  {spec['evidenceType']}{cached}: {entry.get('assessment', '')}")
    for problem in result["planProblems"]:
        click.echo(click.style(f"  plan problem: {problem}", fg="red"))
    if result["canProceed"]:
        click.echo(click.style("[READY] acquisition can proceed.", fg="green"))
        raise SystemExit(EXIT_SUCCESS)
    click.echo(click.style("[BLOCKED] acquisition cannot proceed yet (see above).", fg="red"))
    raise SystemExit(EXIT_BLOCKED)


if __name__ == "__main__":
    main()
