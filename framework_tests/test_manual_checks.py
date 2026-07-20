"""Tests for the guided manual-check recorder (Workstream 6).

The interactive loop is driven with scripted input_fn/print_fn so this
never touches a real terminal -- see run_recorder's docstring.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import cli, manual_checks
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_REGRESSION, EXIT_SUCCESS
from calee_regression.suites import REPO_ROOT

SAMPLE_DEFINITIONS = [
    {"title": "Kiosk escape check", "instruction": "Swipe down...", "expectedResult": "Nothing opens", "mandatory": True},
    {"title": "Weather rendering", "instruction": "Look at weather widget", "expectedResult": "Renders", "mandatory": False},
]


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    # record-manual-checks only writes outside --out when --run-id is
    # given (into REPO_ROOT/reports/runs/<run-id>/) -- redirect REPO_ROOT
    # under tmp_path so a test that does pass --run-id never writes into
    # this checkout's working tree.
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)


def _scripted_input(answers):
    it = iter(answers)

    def _input_fn(_prompt=""):
        return next(it)

    return _input_fn


def test_recorder_advances_through_checks_on_pass():
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["1", "1"]), print_fn=lambda *_: None,
    )
    assert [r["status"] for r in results] == ["pass", "pass"]


def test_recorder_records_fail_and_blocked():
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["2", "3"]), print_fn=lambda *_: None,
    )
    assert [r["status"] for r in results] == ["fail", "blocked"]


def test_add_note_does_not_advance_and_is_recorded():
    # note, then pass -- note must persist and only the second answer advances.
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["4", "Shade opened briefly", "1", "1"]), print_fn=lambda *_: None,
    )
    assert results[0]["note"] == "Shade opened briefly"
    assert results[0]["status"] == "pass"


def test_add_screenshot_path_does_not_advance_and_is_recorded():
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["5", "/tmp/shot.png", "1", "1"]), print_fn=lambda *_: None,
    )
    assert results[0]["screenshotRef"] == "/tmp/shot.png"


def test_go_back_returns_to_previous_check():
    # pass check 1 (advances to check 2), go back from check 2 to check 1,
    # re-record check 1 as fail (advances to check 2 again), pass check 2.
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["1", "6", "2", "1"]), print_fn=lambda *_: None,
    )
    assert results[0]["status"] == "fail"
    assert results[1]["status"] == "pass"


def test_unknown_choice_reprompts_same_check():
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["9", "1", "1"]), print_fn=lambda *_: None,
    )
    assert results[0]["status"] == "pass"


def test_unanswered_mandatory_check_stays_none_if_recorder_exits_early():
    # Only answer the first (mandatory) check, leave the loop mid-way is
    # not directly simulated (run_recorder always completes), but the
    # write-then-check-status flow is what matters: an unanswered mandatory
    # check must serialize with status=null so consolidate blocks on it.
    result = manual_checks._new_result(SAMPLE_DEFINITIONS[0])
    assert result["status"] is None
    assert result["mandatory"] is True


def test_load_check_definitions_rejects_empty_or_malformed(tmp_path):
    bad = tmp_path / "checks.json"
    bad.write_text("[]")
    with pytest.raises(manual_checks.ManualChecksDefinitionError):
        manual_checks.load_check_definitions(bad)

    bad.write_text(json.dumps([{"title": "no instruction"}]))
    with pytest.raises(manual_checks.ManualChecksDefinitionError):
        manual_checks.load_check_definitions(bad)


def test_write_results_shape_matches_consolidate_expectations(tmp_path):
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["1", "1"]), print_fn=lambda *_: None,
    )
    out_path = manual_checks.write_results(results, tmp_path / "out" / "manual-checks-20240101-120000.json")
    on_disk = json.loads(out_path.read_text())
    assert on_disk[0]["title"] == "Kiosk escape check"
    assert set(on_disk[0].keys()) == {"title", "instruction", "expectedResult", "status", "note", "screenshotRef", "mandatory"}


def _write_definitions(tmp_path, data):
    path = tmp_path / "checks.json"
    path.write_text(json.dumps(data))
    return str(path)


def test_cli_record_manual_checks_exits_success_when_all_mandatory_pass(tmp_path, monkeypatch):
    checks_path = _write_definitions(tmp_path, SAMPLE_DEFINITIONS)
    monkeypatch.setattr("builtins.input", _scripted_input(["1", "1"]))
    runner = CliRunner()
    result = runner.invoke(
        main, ["record-manual-checks", "--checks", checks_path, "--out", str(tmp_path / "out.json")],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert (tmp_path / "out.json").exists()


def test_cli_record_manual_checks_exits_blocked_when_mandatory_recorded_blocked(tmp_path, monkeypatch):
    checks_path = _write_definitions(tmp_path, SAMPLE_DEFINITIONS)
    monkeypatch.setattr("builtins.input", _scripted_input(["3", "1"]))
    runner = CliRunner()
    result = runner.invoke(
        main, ["record-manual-checks", "--checks", checks_path, "--out", str(tmp_path / "out.json")],
    )
    assert result.exit_code == EXIT_BLOCKED


def test_cli_record_manual_checks_exits_regression_when_mandatory_fails(tmp_path, monkeypatch):
    checks_path = _write_definitions(tmp_path, SAMPLE_DEFINITIONS)
    monkeypatch.setattr("builtins.input", _scripted_input(["2", "1"]))
    runner = CliRunner()
    result = runner.invoke(
        main, ["record-manual-checks", "--checks", checks_path, "--out", str(tmp_path / "out.json")],
    )
    assert result.exit_code == EXIT_REGRESSION


def test_cli_record_manual_checks_with_run_id_writes_into_workspace(tmp_path, monkeypatch):
    checks_path = _write_definitions(tmp_path, SAMPLE_DEFINITIONS)
    monkeypatch.setattr("builtins.input", _scripted_input(["1", "1"]))
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "record-manual-checks", "--checks", checks_path, "--out", str(tmp_path / "out.json"),
            "--run-id", "release-test-manual-id",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    workspace_report = tmp_path / "reports" / "runs" / "release-test-manual-id" / "manual-checks" / "results.json"
    assert workspace_report.is_file()
    payload = json.loads(workspace_report.read_text())
    assert payload["runId"] == "release-test-manual-id"
    assert payload["checks"][0]["title"] == "Kiosk escape check"

    manifest_path = tmp_path / "reports" / "runs" / "release-test-manual-id" / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert "manual-checks" in manifest["reportPaths"]


def test_write_results_wraps_with_run_id_when_given(tmp_path):
    results = manual_checks.run_recorder(
        SAMPLE_DEFINITIONS, input_fn=_scripted_input(["1", "1"]), print_fn=lambda *_: None,
    )
    out_path = manual_checks.write_results(results, tmp_path / "results.json", run_id="release-abc")
    on_disk = json.loads(out_path.read_text())
    assert on_disk["runId"] == "release-abc"
    assert on_disk["checks"][0]["title"] == "Kiosk escape check"


def test_cli_record_manual_checks_rejects_missing_checks_file(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        main, ["record-manual-checks", "--checks", str(tmp_path / "nope.json"), "--out", str(tmp_path / "out.json")],
    )
    assert result.exit_code == EXIT_INVALID_CONFIG


def test_numbered_launchers_include_a_manual_check_recorder_before_full_solution():
    tester_dir = REPO_ROOT / "tester"
    names = sorted(p.name for p in tester_dir.glob("*.command"))
    # The canonical numbered launchers must all be present and in order. "00
    # Run Calee Release Regression" (Phase 3) is the nontechnical one-button
    # front door that sits before them; assert the full expected set including
    # it, so both a missing canonical launcher AND an unexpected extra one are
    # caught.
    assert names == [
        "00 Run Calee Release Regression.command",
        "01 Prepare Test Environment.command",
        "02 Test Calee Tablet.command",
        "03 Test CaleeMobile Android.command",
        "04 Test CaleeMobile iPhone.command",
        "05 Record Manual Checks.command",
        "06 Test Full Calee Solution.command",
        "07 Open Latest Report.command",
    ]
    # The record-manual-checks recorder must come before the full-solution run.
    assert names.index("05 Record Manual Checks.command") < names.index("06 Test Full Calee Solution.command")


def test_manual_checks_launcher_calls_the_record_manual_checks_command():
    text = (REPO_ROOT / "tester" / "05 Record Manual Checks.command").read_text()
    assert "record-manual-checks" in text
    # Never instruct the tester to edit JSON/YAML directly.
    assert ".json" not in text
    assert ".yaml" not in text


def test_one_button_launcher_uses_plain_language_and_installer_and_delegates():
    text = (REPO_ROOT / "tester" / "00 Run Calee Release Regression.command").read_text()
    # Plain-language states the tester sees (Phase 3).
    for state in ("READY", "INSTALLING", "TESTING", "PASSED", "FAILED", "BLOCKED", "NEEDS TECHNICAL OWNER"):
        assert state in text, state
    # It drives the installer subsystem and delegates the regression to "06".
    assert "verify-release-bundle" in text
    assert "install-tablet-release" in text
    assert "machine-config" in text
    assert "06 Test Full Calee Solution.command" in text
    # Every blocker path tells the tester whether it is a product failure and
    # what safe action to take (the needs_owner helper carries all three).
    assert "Is this a product failure?" in text
    assert "What you can do now" in text or "What could not run" in text
    # It never asks the tester to hand-edit a config file.
    assert "edit" not in text.lower() or "double-click" in text.lower()
