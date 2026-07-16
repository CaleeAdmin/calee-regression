from __future__ import annotations

from pathlib import Path

import click

from . import config as config_mod
from . import preflight, reporting, suites
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
        raise SystemExit(2)
    try:
        return config_mod.load_config(config_path)
    except config_mod.ConfigError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(2)


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
    raise SystemExit(1 if preflight.has_errors(checks) else 0)


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
    click.echo(f"Passed: {result.passed_count}  Failed: {result.failed_count}  Skipped: {result.skipped_count}")
    click.echo(f"Report: {report_dir}")
    raise SystemExit(1 if result.failed_count else 0)


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
        raise SystemExit(2)

    if suites.suite_includes_physical(suite_name) and not (confirm_technical or cfg.allow_release_technical):
        click.echo(
            f"Suite '{suite_name}' includes physical-tablet-only scenarios (kiosk/admin/system-receiver "
            f"tests). Re-run with --confirm-technical, or set allow_release_technical: true in your "
            f"config, once you have a real tablet ready.",
            err=True,
        )
        raise SystemExit(2)

    rb = reporting.ReportBuilder(cfg, run_name=suite_name)
    result = ScenarioRunner(cfg, report_builder=rb).run_scenarios(scenario_paths, suite_name=suite_name)
    report_dir = rb.write(result)
    click.echo(f"Passed: {result.passed_count}  Failed: {result.failed_count}  Skipped: {result.skipped_count}")
    click.echo(f"Report: {report_dir}")
    raise SystemExit(1 if result.failed_count else 0)


if __name__ == "__main__":
    main()
