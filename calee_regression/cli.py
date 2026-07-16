from __future__ import annotations

import json
import time
from pathlib import Path

import click

from . import config as config_mod
from . import preflight, reporting, suites
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
    means.
    """
    status = decide_status(
        passed=result.passed_count,
        failed=result.failed_count,
        blocked=result.blocked_count,
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


@main.command()
@click.option("--config", "config_path", envvar="CALEE_TEST_CONFIG", default=None, type=click.Path())
@click.option("--fixture-base-url", envvar="CALEE_API_BASE", default=None)
@click.option("--fixture-email", envvar="CALEE_TEST_EMAIL", default=None)
@click.option("--fixture-password", envvar="CALEE_TEST_PASSWORD", default=None)
@click.option(
    "--skip-fixture", is_flag=True, default=False,
    help="Skip resetting the deterministic REG-* fixture (advanced; scenarios that depend on it may block).",
)
def prepare(config_path, fixture_base_url, fixture_email, fixture_password, skip_fixture):
    """Check the local environment and reset the deterministic REG-* fixture.

    This is what "01 Prepare Test Environment" runs. It never claims success
    it can't back up: any preflight error, or any fixture-reset failure when
    fixture credentials were given, exits BLOCKED.
    """
    cfg = _load_config_or_exit(config_path)
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

    if skip_fixture:
        click.echo("\nFixture reset skipped (--skip-fixture).")
        raise SystemExit(EXIT_SUCCESS)

    if not (fixture_base_url and fixture_email and fixture_password):
        click.echo(
            "\nFixture reset skipped: no target environment/test-account configured "
            "(set CALEE_API_BASE, CALEE_TEST_EMAIL, CALEE_TEST_PASSWORD, or pass "
            "--fixture-base-url/--fixture-email/--fixture-password). Scenarios that require "
            "the deterministic fixture (e.g. the calendar suite) will report BLOCKED until this is set up."
        )
        raise SystemExit(EXIT_SUCCESS)

    click.echo(f"\nResetting the regression fixture at {fixture_base_url} ...")
    try:
        output = run_fixture_action(
            "reset", repo_root=REPO_ROOT, base_url=fixture_base_url, email=fixture_email, password=fixture_password,
        )
    except FixtureBridgeError as exc:
        click.echo(f"\n=== Blocked: {exc} ===", err=True)
        raise SystemExit(EXIT_BLOCKED)
    click.echo(output)
    click.echo("Environment ready.")
    raise SystemExit(EXIT_SUCCESS)


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
@click.option("--build-version", default="unknown", help="Application build/version under test")
@click.option("--test-environment", default="", help="Target environment URL")
@click.option("--tester", default="", help="Tester name/identifier")
@click.option("--out-dir", type=click.Path(), default=None, help="Where to write the consolidated bundle (default: reports/)")
def consolidate(
    tablet_report, mobile_api_report, mobile_android_report, mobile_ios_report,
    manual_checks_path, build_version, test_environment, tester, out_dir,
):
    """Combine per-framework JSON reports into one consolidated release report.

    Reads already-produced report files -- it does not run anything itself.
    Any report not given is treated as "not executed", which blocks an
    overall PASS (see docs/RELEASE_POLICY.md).
    """
    report = build_release_report(
        tablet=_load_json_report(tablet_report),
        mobile_api=_load_json_report(mobile_api_report),
        mobile_android_ui=_load_json_report(mobile_android_report),
        mobile_ios_ui=_load_json_report(mobile_ios_report),
        manual_checks=_load_manual_checks(manual_checks_path),
        meta={
            "buildVersion": build_version,
            "testEnvironment": test_environment,
            "tester": tester,
        },
    )

    out = Path(out_dir) if out_dir else REPO_ROOT / "reports"
    bundle_dir = out / f"consolidated-{time.strftime('%Y%m%d-%H%M%S')}"
    bundle_path = write_release_bundle(report, bundle_dir, build_label=build_version)

    click.echo(f"Overall: {report.overall_status.upper()}")
    for component in report.components:
        marker = "" if component.mandatory else " (optional)"
        click.echo(f"  {component.name}{marker}: {component.status.upper()}")
    click.echo(f"Bundle: {bundle_path}")

    raise SystemExit(_STATUS_TO_EXIT_CODE[report.overall_status])


if __name__ == "__main__":
    main()
