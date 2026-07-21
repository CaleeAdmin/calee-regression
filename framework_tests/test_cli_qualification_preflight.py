"""CLI-level contract tests for `qualification-preflight` (Priority 9)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from calee_regression import cli
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS


def _invoke(*args):
    return CliRunner().invoke(cli.main, ["qualification-preflight", *args])


def test_runs_read_only_and_never_crashes(tmp_path):
    report_path = tmp_path / "preflight.json"
    result = _invoke("--report", str(report_path))
    assert result.exit_code in (EXIT_SUCCESS, EXIT_BLOCKED), result.output
    assert "check(s):" in result.output

    payload = json.loads(report_path.read_text())
    assert payload["overall"] in ("READY", "BLOCKED")
    assert isinstance(payload["checks"], list) and payload["checks"]
    for check in payload["checks"]:
        assert check["status"] in ("ready", "warning", "blocked")


def test_explicit_missing_manual_checks_path_blocks_overall(tmp_path):
    missing = tmp_path / "no-such-manual-checks.json"
    result = _invoke("--manual-checks", str(missing))
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "manual_check_definitions" in result.output
    assert str(missing) in result.output


def test_report_json_check_count_matches_printed_output(tmp_path):
    report_path = tmp_path / "preflight.json"
    result = _invoke("--report", str(report_path))
    payload = json.loads(report_path.read_text())
    for check in payload["checks"]:
        assert check["name"] in result.output
