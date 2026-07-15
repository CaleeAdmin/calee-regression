import json
import xml.etree.ElementTree as ET

from calee_regression.config import Config
from calee_regression.models import ScenarioResult, StepResult, SuiteResult
from calee_regression.reporting import ReportBuilder


def _make_config(tmp_path):
    return Config(
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
        report_dir=str(tmp_path / "reports"),
    )


def _make_suite_result():
    passed_scenario = ScenarioResult(
        name="passing-scenario",
        file="scenarios/passing.yaml",
        status="passed",
        steps=[StepResult(name="Launch", action="launch", status="passed", message="ok")],
        duration_seconds=1.2,
    )
    failed_scenario = ScenarioResult(
        name="failing-scenario",
        file="scenarios/failing.yaml",
        status="failed",
        steps=[
            StepResult(name="Launch", action="launch", status="passed", message="ok"),
            StepResult(
                name="Assert text", action="assert_text", status="failed",
                message="Expected text not found: 'Foo'", hint="some hint",
            ),
        ],
        duration_seconds=2.5,
    )
    return SuiteResult(
        name="test-suite",
        scenarios=[passed_scenario, failed_scenario],
        started_at="2026-01-01 00:00:00",
        finished_at="2026-01-01 00:01:00",
    )


def test_report_write_creates_all_files(tmp_path):
    cfg = _make_config(tmp_path)
    suite_result = _make_suite_result()

    rb = ReportBuilder(cfg, run_name="test-suite")
    report_dir = rb.write(suite_result)

    summary_txt = report_dir / "summary.txt"
    summary_html = report_dir / "summary.html"
    results_json = report_dir / "results.json"
    junit_xml = report_dir / "junit.xml"

    assert summary_txt.exists() and summary_txt.stat().st_size > 0
    assert summary_html.exists() and summary_html.stat().st_size > 0
    assert results_json.exists() and results_json.stat().st_size > 0
    assert junit_xml.exists() and junit_xml.stat().st_size > 0
    assert (report_dir / "screenshots").is_dir()


def test_results_json_round_trips_counts(tmp_path):
    cfg = _make_config(tmp_path)
    suite_result = _make_suite_result()

    rb = ReportBuilder(cfg, run_name="test-suite")
    report_dir = rb.write(suite_result)

    with (report_dir / "results.json").open() as f:
        data = json.load(f)

    assert data["passed_count"] == 1
    assert data["failed_count"] == 1
    assert len(data["scenarios"]) == 2


def test_junit_xml_is_valid_and_has_expected_testcases(tmp_path):
    cfg = _make_config(tmp_path)
    suite_result = _make_suite_result()

    rb = ReportBuilder(cfg, run_name="test-suite")
    report_dir = rb.write(suite_result)

    tree = ET.parse(report_dir / "junit.xml")
    root = tree.getroot()

    testcases = root.findall("testcase")
    assert len(testcases) == 2
    failures = root.findall("testcase/failure")
    assert len(failures) == 1


def test_screenshot_path_dedupes(tmp_path):
    cfg = _make_config(tmp_path)
    rb = ReportBuilder(cfg, run_name="dedupe-test")

    first = rb.screenshot_path("shot")
    first.write_bytes(b"fake")
    second = rb.screenshot_path("shot")

    assert first != second
