"""Tests for the PASS/FAIL/SKIPPED/BLOCKED result model.

These specifically cover the core design requirement that environment/
tooling problems (Appium unreachable, a malformed scenario file) are never
reported as a product regression — see docs/TEST_DATA_RESET_CONTRACT.md and
the top-level project brief's "Separate PASS, FAIL and BLOCKED" requirement.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
import yaml

from calee_regression import cli, reporting, runner
from calee_regression.config import Config
from calee_regression.models import (
    EXIT_BLOCKED,
    EXIT_REGRESSION,
    EXIT_SUCCESS,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    ScenarioResult,
    StepResult,
    SuiteResult,
)


def _make_config(**overrides):
    kwargs = dict(
        appium_url="http://127.0.0.1:4723/wd/hub",
        device_name="Calee Test Tablet",
        udid="emulator-5554",
        apk_path="/tmp/calee.apk",
        app_package="com.viso.calee",
        app_activity=".ui.HomeActivity",
        shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity",
        launch_strategy="direct_activity",
        start_action="com.viso.calee.action.START",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _write_scenario(tmp_path, data, filename="scenario.yaml"):
    path = tmp_path / filename
    with path.open("w") as f:
        yaml.safe_dump(data, f)
    return path


class _FailingDriver:
    """Stands in for CaleeDriver when simulating an unreachable Appium/device."""

    def __init__(self, config):
        self.config = config

    def start_session(self):
        raise RuntimeError("Connection refused: could not reach Appium")


def test_corrupt_scenario_file_is_blocked_not_failed(tmp_path):
    bad_path = tmp_path / "broken.yaml"
    bad_path.write_text("name: broken\n# no steps key at all\n")

    result = runner.ScenarioRunner(_make_config()).run_scenarios([bad_path])

    assert len(result.scenarios) == 1
    scenario_result = result.scenarios[0]
    assert scenario_result.status == STATUS_BLOCKED
    assert scenario_result.blocked_reason is not None
    assert "framework/configuration" in scenario_result.blocked_reason
    assert result.failed_count == 0
    assert result.blocked_count == 1


def test_appium_session_start_failure_blocks_all_runnable_scenarios(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CaleeDriver", _FailingDriver)

    scenario_a = _write_scenario(
        tmp_path,
        {"name": "a", "requires_state": "any", "steps": [{"name": "Launch", "action": "launch"}]},
        filename="a.yaml",
    )
    scenario_b = _write_scenario(
        tmp_path,
        {"name": "b", "requires_state": "any", "steps": [{"name": "Launch", "action": "launch"}]},
        filename="b.yaml",
    )

    result = runner.ScenarioRunner(_make_config()).run_scenarios([scenario_a, scenario_b])

    assert result.failed_count == 0
    assert result.blocked_count == 2
    for scenario_result in result.scenarios:
        assert scenario_result.status == STATUS_BLOCKED
        assert "environment/tooling problem" in scenario_result.blocked_reason
        assert scenario_result.steps[0].status == STATUS_BLOCKED


def test_suite_result_blocked_count():
    suite_result = SuiteResult(
        name="mixed",
        scenarios=[
            ScenarioResult(name="p", file="p.yaml", status=STATUS_PASSED),
            ScenarioResult(name="f", file="f.yaml", status=STATUS_FAILED),
            ScenarioResult(name="s", file="s.yaml", status=STATUS_SKIPPED),
            ScenarioResult(name="b1", file="b1.yaml", status=STATUS_BLOCKED),
            ScenarioResult(name="b2", file="b2.yaml", status=STATUS_BLOCKED),
        ],
    )
    assert suite_result.passed_count == 1
    assert suite_result.failed_count == 1
    assert suite_result.skipped_count == 1
    assert suite_result.blocked_count == 2


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([STATUS_PASSED], EXIT_SUCCESS),
        ([STATUS_PASSED, STATUS_FAILED], EXIT_REGRESSION),
        ([STATUS_PASSED, STATUS_BLOCKED], EXIT_BLOCKED),
        # A real failure must never be masked by a simultaneous block.
        ([STATUS_FAILED, STATUS_BLOCKED], EXIT_REGRESSION),
        # Nothing meaningful ran (all skipped) — must not read as success.
        ([STATUS_SKIPPED, STATUS_SKIPPED], EXIT_BLOCKED),
        ([STATUS_BLOCKED], EXIT_BLOCKED),
    ],
)
def test_exit_code_for_result(statuses, expected):
    suite_result = SuiteResult(
        name="s",
        scenarios=[ScenarioResult(name=f"n{i}", file="f.yaml", status=status) for i, status in enumerate(statuses)],
    )
    assert cli._exit_code_for(suite_result) == expected


def test_exit_code_for_empty_result_is_success():
    # An empty scenario list (e.g. an empty suite) isn't itself a blocked
    # condition — resolve_suite()/load_scenario() already guard against that
    # upstream. Exit-code decision just needs to not crash on it.
    assert cli._exit_code_for(SuiteResult(name="empty", scenarios=[])) == EXIT_SUCCESS


def test_junit_xml_reports_blocked_scenarios_as_errors(tmp_path):
    config = _make_config(report_dir=str(tmp_path))
    rb = reporting.ReportBuilder(config, run_name="blocked-run")
    suite_result = SuiteResult(
        name="blocked-run",
        scenarios=[
            ScenarioResult(
                name="blocked-one",
                file="blocked.yaml",
                status=STATUS_BLOCKED,
                blocked_reason="Could not start an Appium session: connection refused",
                steps=[StepResult(name="start_session", action="launch", status=STATUS_BLOCKED)],
            ),
        ],
    )
    report_dir = rb.write(suite_result)

    tree = ET.parse(report_dir / "junit.xml")
    testsuite = tree.getroot()
    assert testsuite.attrib["errors"] == "1"
    assert testsuite.attrib["failures"] == "0"
    testcase = testsuite.find("testcase")
    error_el = testcase.find("error")
    assert error_el is not None
    assert "connection refused" in error_el.attrib["message"]


def test_summary_txt_reports_blocked_count_and_reason(tmp_path):
    config = _make_config(report_dir=str(tmp_path))
    rb = reporting.ReportBuilder(config, run_name="blocked-run")
    suite_result = SuiteResult(
        name="blocked-run",
        scenarios=[
            ScenarioResult(
                name="blocked-one",
                file="blocked.yaml",
                status=STATUS_BLOCKED,
                blocked_reason="Appium is unreachable",
            ),
        ],
    )
    report_dir = rb.write(suite_result)

    summary = (report_dir / "summary.txt").read_text()
    assert "Blocked: 1" in summary
    assert "blocked reason: Appium is unreachable" in summary
