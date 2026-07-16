"""Tests for the consolidated cross-repo release report.

Exercises the release-approval policy entirely with synthetic/simulated
per-framework results -- no real device, backend, or run is needed. This is
the "consolidated reporting works using simulated or framework-level test
results" requirement made concrete and checked in CI.
"""

from __future__ import annotations

import json

from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_NOT_RUN,
    STATUS_PASS,
    ManualCheck,
    build_release_report,
    component_from_build_version_match,
    component_from_environment_report,
    decide_status,
    write_html,
    write_json,
    write_junit,
    write_release_bundle,
)

PASSING_ENVIRONMENT_REPORT = {
    "runId": "release-20260716-000000-abc123",
    "status": "pass",
    "detail": ["Environment and fixture ready."],
}
BLOCKED_ENVIRONMENT_REPORT = {
    "runId": "release-20260716-000000-abc123",
    "status": "blocked",
    "detail": ["Fixture verification failed: HTTP 500"],
}

PASSING_TABLET_REPORT = {
    "passed_count": 10, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
    "scenarios": [{"name": f"s{i}", "status": "passed"} for i in range(10)],
}

FAILING_TABLET_REPORT = {
    "passed_count": 8, "failed_count": 2, "blocked_count": 0, "skipped_count": 0,
    "scenarios": (
        [{"name": f"s{i}", "status": "passed"} for i in range(8)]
        + [{"name": "bad-1", "status": "failed"}, {"name": "bad-2", "status": "failed"}]
    ),
}

BLOCKED_TABLET_REPORT = {
    "passed_count": 0, "failed_count": 0, "blocked_count": 3, "skipped_count": 0,
    "scenarios": [
        {"name": f"s{i}", "status": "blocked", "blocked_reason": "Appium unreachable"} for i in range(3)
    ],
}

PASSING_API_REPORT = {"runId": "r1", "counts": {"PASS": 40, "INFO": 2}, "steps": [{"name": "x", "status": "PASS"}] * 40}
FAILING_API_REPORT = {
    "runId": "r2",
    "counts": {"PASS": 39, "FAIL": 1},
    "steps": [{"name": "x", "status": "PASS"}] * 39 + [{"name": "bad step", "status": "FAIL", "detail": "wrong value"}],
}
BLOCKED_API_REPORT = {
    "runId": "r3",
    "counts": {"BLOCKED": 1},
    "steps": [{"name": "auth login", "status": "BLOCKED", "detail": "bad credentials"}],
}

ALL_PASSED_MANUAL_CHECKS = [
    ManualCheck(title="Kiosk escape check", instruction="Try swiping down", expected_result="No shade opens", status="pass"),
]


def test_decide_status_basic_cases():
    assert decide_status(passed=5, failed=0, blocked=0) == STATUS_PASS
    assert decide_status(passed=4, failed=1, blocked=0) == STATUS_FAIL
    assert decide_status(passed=4, failed=0, blocked=1) == STATUS_BLOCKED
    assert decide_status(passed=0, failed=1, blocked=1) == STATUS_FAIL  # fail always wins
    assert decide_status(passed=0, failed=0, blocked=0, total=3) == STATUS_BLOCKED  # nothing passed


def test_all_components_pass_and_manual_checks_pass_yields_overall_pass():
    report = build_release_report(
        environment=PASSING_ENVIRONMENT_REPORT,
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
        meta={"buildVersion": "1.2.3"},
        android_mandatory=False,
        ios_mandatory=False,
    )
    assert report.overall_status == STATUS_PASS


def test_environment_component_unit_pass_and_blocked():
    passed = component_from_environment_report("env", PASSING_ENVIRONMENT_REPORT)
    assert passed.status == STATUS_PASS
    blocked = component_from_environment_report("env", BLOCKED_ENVIRONMENT_REPORT)
    assert blocked.status == STATUS_BLOCKED
    not_run = component_from_environment_report("env", None)
    assert not_run.status == STATUS_NOT_RUN
    # An environment report with no recognizable status must never be
    # silently trusted as ready -- degrade to blocked, not pass.
    garbled = component_from_environment_report("env", {"status": "who-knows"})
    assert garbled.status == STATUS_BLOCKED


def test_missing_environment_blocks_overall_even_when_everything_else_passes():
    # This is the core Workstream 4 requirement: Prepare is mandatory. A
    # release run where the tablet/mobile/manual checks all passed but
    # Prepare never reported ready must never read as an overall PASS.
    report = build_release_report(
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
        android_mandatory=False,
        ios_mandatory=False,
    )
    assert report.overall_status == STATUS_BLOCKED
    env_component = next(c for c in report.components if c.name == "Test environment and regression fixture")
    assert env_component.status == STATUS_NOT_RUN
    assert env_component.mandatory is True


def test_blocked_environment_blocks_overall_even_when_everything_else_passes():
    report = build_release_report(
        environment=BLOCKED_ENVIRONMENT_REPORT,
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
        android_mandatory=False,
        ios_mandatory=False,
    )
    assert report.overall_status == STATUS_BLOCKED
    env_component = next(c for c in report.components if c.name == "Test environment and regression fixture")
    assert env_component.status == STATUS_BLOCKED


def test_mobile_ui_platforms_default_to_mandatory_and_block_when_missing():
    # An omitted required platform must block the release -- the default
    # (no explicit android_mandatory/ios_mandatory override) is mandatory,
    # not the old hard-coded mandatory=False.
    report = build_release_report(
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
    )
    assert report.overall_status == STATUS_BLOCKED
    android_component = next(c for c in report.components if c.name == "CaleeMobile Android UI")
    assert android_component.mandatory is True
    assert android_component.status == STATUS_NOT_RUN


def test_a_single_tablet_failure_makes_overall_fail_even_if_everything_else_passes():
    report = build_release_report(
        tablet=FAILING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
    )
    assert report.overall_status == STATUS_FAIL


def test_a_blocked_mandatory_component_yields_overall_blocked_not_pass():
    report = build_release_report(
        tablet=BLOCKED_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
    )
    assert report.overall_status == STATUS_BLOCKED


def test_blocked_is_never_reported_as_pass_even_alongside_a_failure():
    report = build_release_report(tablet=BLOCKED_TABLET_REPORT, mobile_api=FAILING_API_REPORT)
    # A real regression exists (API), so FAIL must win over BLOCKED -- but
    # neither may ever be reported as PASS.
    assert report.overall_status == STATUS_FAIL
    assert report.overall_status != STATUS_PASS


def test_missing_mandatory_component_blocks_overall_pass():
    report = build_release_report(tablet=PASSING_TABLET_REPORT, mobile_api=None)
    assert report.overall_status == STATUS_BLOCKED
    api_component = next(c for c in report.components if c.name == "CaleeMobile Client API")
    assert api_component.status == STATUS_NOT_RUN


def test_missing_manual_checks_blocks_overall_pass():
    report = build_release_report(tablet=PASSING_TABLET_REPORT, mobile_api=PASSING_API_REPORT, manual_checks=None)
    assert report.overall_status == STATUS_BLOCKED


def test_optional_component_being_not_run_does_not_block_overall_pass():
    report = build_release_report(
        environment=PASSING_ENVIRONMENT_REPORT,
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        mobile_android_ui=None,
        mobile_ios_ui=None,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
        android_mandatory=False,
        ios_mandatory=False,
    )
    assert report.overall_status == STATUS_PASS
    android_component = next(c for c in report.components if c.name == "CaleeMobile Android UI")
    assert android_component.status == STATUS_NOT_RUN
    assert android_component.mandatory is False


def test_a_failed_mandatory_manual_check_makes_overall_fail():
    checks = [
        ManualCheck(title="Kiosk escape check", instruction="...", expected_result="No shade opens", status="fail", note="Shade opened"),
    ]
    report = build_release_report(tablet=PASSING_TABLET_REPORT, mobile_api=PASSING_API_REPORT, manual_checks=checks)
    assert report.overall_status == STATUS_FAIL


def test_no_expected_build_version_configured_means_no_check_performed():
    assert component_from_build_version_match(name="x", expected=None, detected="1.2.3") is None


def test_matching_build_version_passes():
    component = component_from_build_version_match(name="Calee build version", expected="1.2.3", detected="1.2.3")
    assert component.status == STATUS_PASS


def test_mismatched_build_version_blocks():
    component = component_from_build_version_match(name="Calee build version", expected="1.2.3", detected="1.2.4")
    assert component.status == STATUS_BLOCKED


def test_missing_detected_build_version_blocks_when_one_was_expected():
    component = component_from_build_version_match(name="Calee build version", expected="1.2.3", detected=None)
    assert component.status == STATUS_BLOCKED


def test_build_version_mismatch_blocks_the_whole_release():
    report = build_release_report(
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
        android_mandatory=False,
        ios_mandatory=False,
        calee_build_version="0.3.21",
        expected_calee_build_version="0.3.22",
    )
    assert report.overall_status == STATUS_BLOCKED
    version_component = next(c for c in report.components if c.name == "Calee build version")
    assert version_component.status == STATUS_BLOCKED


def test_suggested_next_action_present_for_each_overall_status():
    fail_report = build_release_report(tablet=FAILING_TABLET_REPORT, mobile_api=PASSING_API_REPORT, android_mandatory=False, ios_mandatory=False)
    blocked_report = build_release_report(tablet=PASSING_TABLET_REPORT, mobile_api=None, android_mandatory=False, ios_mandatory=False)
    pass_report = build_release_report(
        environment=PASSING_ENVIRONMENT_REPORT,
        tablet=PASSING_TABLET_REPORT, mobile_api=PASSING_API_REPORT, manual_checks=ALL_PASSED_MANUAL_CHECKS,
        android_mandatory=False, ios_mandatory=False,
    )
    assert "do not release" in fail_report.summary["suggestedNextAction"].lower()
    assert "not yet releasable" in blocked_report.summary["suggestedNextAction"].lower()
    assert "approved to release" in pass_report.summary["suggestedNextAction"].lower()


def test_write_json_html_junit_and_bundle(tmp_path):
    report = build_release_report(
        environment=PASSING_ENVIRONMENT_REPORT,
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
        meta={"buildVersion": "1.2.3", "testEnvironment": "https://hub-dev.calee.com.au"},
        android_mandatory=False,
        ios_mandatory=False,
    )

    json_path = tmp_path / "report.json"
    html_path = tmp_path / "report.html"
    junit_path = tmp_path / "report.junit.xml"
    write_json(report, json_path)
    write_html(report, html_path)
    write_junit(report, junit_path)

    parsed = json.loads(json_path.read_text())
    assert parsed["overallStatus"] == STATUS_PASS
    assert "1.2.3" in html_path.read_text()
    assert "<testsuite" in junit_path.read_text()

    bundle_path = write_release_bundle(report, tmp_path / "bundle", build_label="1.2.3")
    assert bundle_path.exists()
    assert bundle_path.name.startswith("Calee-Regression-")
    assert bundle_path.name.endswith("-PASS.zip")


def test_synthetic_fail_bundle_can_be_generated_and_inspected(tmp_path):
    report = build_release_report(
        tablet=FAILING_TABLET_REPORT, mobile_api=PASSING_API_REPORT, manual_checks=ALL_PASSED_MANUAL_CHECKS,
        android_mandatory=False, ios_mandatory=False,
    )
    assert report.overall_status == STATUS_FAIL
    bundle_path = write_release_bundle(report, tmp_path / "bundle", build_label="1.2.3")
    assert bundle_path.exists()
    assert bundle_path.name.endswith("-FAIL.zip")


def test_synthetic_blocked_bundle_can_be_generated_and_inspected(tmp_path):
    report = build_release_report(
        tablet=PASSING_TABLET_REPORT, mobile_api=None, manual_checks=ALL_PASSED_MANUAL_CHECKS,
        android_mandatory=False, ios_mandatory=False,
    )
    assert report.overall_status == STATUS_BLOCKED
    bundle_path = write_release_bundle(report, tmp_path / "bundle", build_label="1.2.3")
    assert bundle_path.exists()
    assert bundle_path.name.endswith("-BLOCKED.zip")


def test_write_release_bundle_sanitizes_unsafe_build_labels(tmp_path):
    # A combined tablet+mobile build label like "0.3.22 / 0.0.22" must never
    # break the bundle file path -- "/" is a path separator, not a version
    # delimiter, to the filesystem.
    report = build_release_report(tablet=PASSING_TABLET_REPORT, mobile_api=PASSING_API_REPORT, manual_checks=ALL_PASSED_MANUAL_CHECKS)
    bundle_path = write_release_bundle(report, tmp_path / "bundle", build_label="0.3.22 / 0.0.22")
    assert bundle_path.exists()
    assert bundle_path.parent == tmp_path / "bundle"
    assert "/" not in bundle_path.name
