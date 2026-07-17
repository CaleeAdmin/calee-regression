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
    backend_evidence_component,
    backend_match_status,
    build_release_report,
    component_from_api_report,
    component_from_build_identity,
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


def _mobile_report(steps):
    counts = {}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    return {"runId": "r1", "counts": counts, "steps": steps}


def _passed(name):
    return {"name": name, "status": "PASS", "mandatory": True, "skipCategory": None, "detail": ""}


def _optional_skip(name):
    return {"name": name, "status": "SKIP", "mandatory": False, "skipCategory": "optional_feature", "detail": "OPTIONAL: no chore service"}


def _mandatory_skip(name):
    return {"name": name, "status": "SKIP", "mandatory": True, "skipCategory": "unspecified", "detail": ""}


def _fixture_missing_skip(name):
    return {"name": name, "status": "SKIP", "mandatory": True, "skipCategory": "missing_fixture", "detail": "FIXTURE_MISSING: REG-EVENT-RECURRING-001"}


# Workstream 5: component_from_api_report must read the mandatory/
# skipCategory fields CaleeMobile-Regression's UI reports now carry (see
# ui/run_ui_suite.py in that repo) and fold a mandatory skip into BLOCKED
# -- the same "not_run == blocked for a mandatory component" rule applied
# everywhere else in this framework.


def test_passed_tests_plus_one_optional_skip_still_passes():
    report = _mobile_report([_passed("a"), _passed("b"), _optional_skip("chores: complete a chore")])
    component = component_from_api_report("CaleeMobile Android UI", report)
    assert component.status == STATUS_PASS


def test_passed_tests_plus_one_mandatory_skip_blocks():
    # The core Workstream 5 requirement: a suite containing passed tests
    # plus one mandatory skipped test must never return success.
    report = _mobile_report([_passed("a"), _passed("b"), _mandatory_skip("tasks: reopen a task")])
    component = component_from_api_report("CaleeMobile Android UI", report)
    assert component.status == STATUS_BLOCKED
    assert any("tasks: reopen a task" in d for d in component.detail)


def test_step_with_no_mandatory_key_defaults_to_mandatory_and_blocks():
    # Absence of "mandatory" (e.g. an older report, or the Client API
    # report shape which has no such concept) must default to
    # mandatory=True, never be read as an accepted optional skip.
    report = _mobile_report([_passed("a"), {"name": "b", "status": "SKIP", "detail": "no reason given"}])
    component = component_from_api_report("CaleeMobile Client API", report)
    assert component.status == STATUS_BLOCKED


def test_missing_fixture_skip_blocks_not_fails():
    # "Do not classify 'fixture record missing' as a product FAIL."
    report = _mobile_report([_passed("a"), _fixture_missing_skip("calendar: recurring event edit")])
    component = component_from_api_report("CaleeMobile iPhone UI", report)
    assert component.status == STATUS_BLOCKED
    assert component.status != STATUS_FAIL


def test_multiple_optional_skips_alongside_passes_still_passes():
    report = _mobile_report([
        _passed("a"), _passed("b"),
        _optional_skip("chores: complete a chore"),
        _optional_skip("meals: edit a meal"),
    ])
    component = component_from_api_report("CaleeMobile Android UI", report)
    assert component.status == STATUS_PASS


def test_real_failure_alongside_mandatory_skip_is_still_fail_not_blocked():
    # FAIL always wins over BLOCKED, even when the block comes from a
    # mandatory skip in the same report.
    report = _mobile_report([
        {"name": "bad", "status": "FAIL", "mandatory": True, "skipCategory": None, "detail": "assertion failed"},
        _mandatory_skip("other test"),
    ])
    component = component_from_api_report("CaleeMobile Android UI", report)
    assert component.status == STATUS_FAIL


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
    version_component = next(c for c in report.components if c.name == "Calee tablet build identity")
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


# --- Phase 4: per-platform backend evidence ------------------------------

DEV_BACKEND = "https://hub-dev.calee.com.au"
PROD_BACKEND = "https://hub.calee.com.au"


def _mobile_ui_report_with_backend(
    steps, *, requested, resolved, fixture, device_id="emulator-5554", platform="android"
):
    """A CaleeMobile-Regression run_ui_suite.py-shaped report that carries the
    per-platform backend triple this run recorded (requested/resolved/fixture)
    plus a device id -- exactly what the consolidator independently verifies."""
    counts = {}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    return {
        "runId": "ui-local",
        "releaseRunId": "r1",
        "platform": platform,
        "deviceId": device_id,
        "backend": {"requested": requested, "resolved": resolved, "fixture": fixture},
        "counts": counts,
        "steps": steps,
    }


def test_backend_match_status_classifies_the_triple():
    assert backend_match_status(DEV_BACKEND, DEV_BACKEND, DEV_BACKEND) == "match"
    # Only the non-empty values need to agree; a resolved backend must exist.
    assert backend_match_status(None, DEV_BACKEND, None) == "match"
    # Trailing slash / case are normalized before comparison.
    assert backend_match_status(DEV_BACKEND, DEV_BACKEND + "/", DEV_BACKEND.upper()) == "match"
    # A resolved backend that disagrees with what was requested/fixtured.
    assert backend_match_status(DEV_BACKEND, PROD_BACKEND, DEV_BACKEND) == "mismatch"
    # Something was requested but the app never reported a resolved backend.
    assert backend_match_status(DEV_BACKEND, None, DEV_BACKEND) == "missing_resolved"
    # Nothing recorded at all.
    assert backend_match_status(None, None, None) == "no_evidence"


def _passing_release_kwargs():
    return dict(
        environment=PASSING_ENVIRONMENT_REPORT,
        tablet=PASSING_TABLET_REPORT,
        mobile_api=PASSING_API_REPORT,
        manual_checks=ALL_PASSED_MANUAL_CHECKS,
    )


def test_matching_backend_triple_passes_and_surfaces_evidence():
    android = _mobile_ui_report_with_backend(
        [_passed("boot"), _passed("login")],
        requested=DEV_BACKEND, resolved=DEV_BACKEND, fixture=DEV_BACKEND,
        device_id="emulator-5554",
    )
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=android,
        android_mandatory=True, ios_mandatory=False,
        caleemobile_build_version="0.0.22+22",
        caleemobile_git_sha="abc1234",
    )
    assert report.overall_status == STATUS_PASS
    backend = next(c for c in report.components if c.name == "CaleeMobile Android UI backend")
    assert backend.status == STATUS_PASS
    assert backend.mandatory is True
    assert backend.evidence["matchStatus"] == "match"
    assert backend.evidence["resolved"] == DEV_BACKEND
    assert backend.evidence["deviceId"] == "emulator-5554"
    assert backend.evidence["buildVersion"] == "0.0.22+22"
    assert backend.evidence["gitSha"] == "abc1234"
    # No iOS platform this release -> no iOS backend component.
    assert not any(c.name == "CaleeMobile iPhone UI backend" for c in report.components)


def test_backend_mismatch_blocks_even_when_the_ui_suite_itself_passed():
    # The mobile UI counts are all PASS (the runner would exit 0), but the app
    # resolved a DIFFERENT backend than the prepared fixture. The consolidator
    # must catch this independently of the exit code and BLOCK.
    android = _mobile_ui_report_with_backend(
        [_passed("boot"), _passed("login")],
        requested=DEV_BACKEND, resolved=PROD_BACKEND, fixture=DEV_BACKEND,
    )
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=android,
        android_mandatory=True, ios_mandatory=False,
    )
    assert report.overall_status == STATUS_BLOCKED
    ui = next(c for c in report.components if c.name == "CaleeMobile Android UI")
    assert ui.status == STATUS_PASS  # the UI suite itself passed...
    backend = next(c for c in report.components if c.name == "CaleeMobile Android UI backend")
    assert backend.status == STATUS_BLOCKED  # ...but the backend evidence blocks
    assert backend.evidence["matchStatus"] == "mismatch"


def test_missing_resolved_backend_blocks_a_mandatory_platform():
    android = _mobile_ui_report_with_backend(
        [_passed("boot")],
        requested=DEV_BACKEND, resolved=None, fixture=DEV_BACKEND,
    )
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=android,
        android_mandatory=True, ios_mandatory=False,
    )
    assert report.overall_status == STATUS_BLOCKED
    backend = next(c for c in report.components if c.name == "CaleeMobile Android UI backend")
    assert backend.status == STATUS_BLOCKED
    assert backend.evidence["matchStatus"] == "missing_resolved"


def test_backend_mismatch_on_an_optional_platform_does_not_block():
    android = _mobile_ui_report_with_backend(
        [_passed("boot")], requested=DEV_BACKEND, resolved=DEV_BACKEND, fixture=DEV_BACKEND,
    )
    ios = _mobile_ui_report_with_backend(
        [_passed("boot")], requested=DEV_BACKEND, resolved="https://somewhere-else.example.com",
        fixture=DEV_BACKEND, platform="ios", device_id="ABCD-SIM",
    )
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=android, mobile_ios_ui=ios,
        android_mandatory=True, ios_mandatory=False,
    )
    ios_backend = next(c for c in report.components if c.name == "CaleeMobile iPhone UI backend")
    assert ios_backend.status == STATUS_BLOCKED
    assert ios_backend.mandatory is False
    # An optional platform's backend problem is recorded but does not gate.
    assert report.overall_status == STATUS_PASS


def test_report_without_a_backend_block_gets_no_backend_component():
    # A legacy/synthetic mobile report with no "backend" key is not gated --
    # only a real run (which always records the backend triple) is verified.
    android = _mobile_report([_passed("boot")])
    assert "backend" not in android
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=android,
        android_mandatory=True, ios_mandatory=False,
    )
    assert not any("backend" in c.name for c in report.components)
    assert report.overall_status == STATUS_PASS


def test_backend_evidence_component_returns_none_when_not_run():
    assert backend_evidence_component("x", None, mandatory=True) is None


def test_backend_evidence_surfaced_in_json_and_html(tmp_path):
    android = _mobile_ui_report_with_backend(
        [_passed("boot")], requested=DEV_BACKEND, resolved=DEV_BACKEND, fixture=DEV_BACKEND,
        device_id="emulator-5554",
    )
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=android,
        android_mandatory=True, ios_mandatory=False,
        caleemobile_build_version="0.0.22+22", caleemobile_git_sha="abc1234",
    )
    json_path = tmp_path / "r.json"
    write_json(report, json_path)
    data = json.loads(json_path.read_text())
    backend = next(c for c in data["components"] if c["name"] == "CaleeMobile Android UI backend")
    ev = backend["evidence"]
    assert ev["requested"] == DEV_BACKEND
    assert ev["resolved"] == DEV_BACKEND
    assert ev["fixture"] == DEV_BACKEND
    assert ev["matchStatus"] == "match"
    assert ev["deviceId"] == "emulator-5554"
    assert ev["buildVersion"] == "0.0.22+22"
    assert ev["gitSha"] == "abc1234"

    html_path = tmp_path / "r.html"
    write_html(report, html_path)
    html = html_path.read_text()
    for expected in ("Requested backend", "Resolved backend", "Fixture backend", "Match status",
                     "Device ID", "Build version", "Git SHA", DEV_BACKEND, "emulator-5554", "abc1234"):
        assert expected in html, f"{expected!r} missing from consolidated HTML"


# --- Phase 3: mandatory automatic build identity -------------------------


def test_build_identity_not_required_and_unconfigured_is_no_component():
    assert component_from_build_identity(
        "CaleeMobile build identity", detected_version=None, required=False,
    ) is None


def test_build_identity_required_but_unknown_blocks():
    component = component_from_build_identity(
        "CaleeMobile build identity", detected_version=None, required=True,
    )
    assert component.status == STATUS_BLOCKED
    assert component.mandatory is True
    assert "unknown build" in " ".join(component.detail).lower()


def test_build_identity_unavailable_blocks_even_with_a_version_string():
    # available=False means "we could not determine this" -- never trust a
    # stale/echoed version string in that case.
    component = component_from_build_identity(
        "Calee tablet build identity", detected_version="0.3.22", available=False, required=True,
    )
    assert component.status == STATUS_BLOCKED


def test_build_identity_dirty_blocks_unless_approved():
    dirty = component_from_build_identity(
        "CaleeMobile build identity", detected_version="0.0.22+22", dirty=True, required=True,
    )
    assert dirty.status == STATUS_BLOCKED
    assert "uncommitted" in " ".join(dirty.detail).lower()
    approved = component_from_build_identity(
        "CaleeMobile build identity", detected_version="0.0.22+22", dirty=True,
        required=True, allow_dirty=True,
    )
    assert approved.status == STATUS_PASS
    assert approved.evidence["dirty"] is True


def test_build_identity_version_mismatch_blocks():
    component = component_from_build_identity(
        "Calee tablet build identity", detected_version="0.3.21", expected_version="0.3.22",
    )
    assert component.status == STATUS_BLOCKED
    assert "0.3.22" in " ".join(component.detail)


def test_build_identity_git_sha_mismatch_blocks():
    component = component_from_build_identity(
        "CaleeMobile build identity", detected_version="0.0.22+22",
        detected_git_sha="aaaaaaa", expected_git_sha="bbbbbbb",
    )
    assert component.status == STATUS_BLOCKED
    assert "commit" in " ".join(component.detail).lower()


def test_build_identity_expected_git_sha_but_none_detected_blocks():
    component = component_from_build_identity(
        "CaleeMobile build identity", detected_version="0.0.22+22",
        detected_git_sha=None, expected_git_sha="bbbbbbb",
    )
    assert component.status == STATUS_BLOCKED


def test_build_identity_match_passes_and_records_evidence():
    component = component_from_build_identity(
        "CaleeMobile build identity", detected_version="0.0.22+22", expected_version="0.0.22+22",
        detected_git_sha="abc1234", expected_git_sha="abc1234", version_code="22",
        application_id="com.calee.mobile",
    )
    assert component.status == STATUS_PASS
    assert component.evidence["buildVersion"] == "0.0.22+22"
    assert component.evidence["gitSha"] == "abc1234"
    assert component.evidence["versionCode"] == "22"
    assert component.evidence["applicationId"] == "com.calee.mobile"


def test_release_blocks_when_required_caleemobile_identity_is_unknown():
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=_mobile_ui_report_with_backend(
            [_passed("boot")], requested=DEV_BACKEND, resolved=DEV_BACKEND, fixture=DEV_BACKEND),
        android_mandatory=True, ios_mandatory=False,
        require_caleemobile_identity=True,  # in scope, but no caleemobile_build_version provided
    )
    assert report.overall_status == STATUS_BLOCKED
    identity = next(c for c in report.components if c.name == "CaleeMobile build identity")
    assert identity.status == STATUS_BLOCKED


def test_release_passes_with_known_clean_matching_identity():
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=_mobile_ui_report_with_backend(
            [_passed("boot")], requested=DEV_BACKEND, resolved=DEV_BACKEND, fixture=DEV_BACKEND),
        android_mandatory=True, ios_mandatory=False,
        require_calee_identity=True, require_caleemobile_identity=True,
        calee_build_version="0.3.22", caleemobile_build_version="0.0.22+22",
        caleemobile_git_sha="abc1234", expected_caleemobile_git_sha="abc1234",
    )
    assert report.overall_status == STATUS_PASS
    identity = next(c for c in report.components if c.name == "CaleeMobile build identity")
    assert identity.status == STATUS_PASS


def test_release_blocks_on_dirty_caleemobile_build_without_approval():
    report = build_release_report(
        **_passing_release_kwargs(),
        mobile_android_ui=_mobile_ui_report_with_backend(
            [_passed("boot")], requested=DEV_BACKEND, resolved=DEV_BACKEND, fixture=DEV_BACKEND),
        android_mandatory=True, ios_mandatory=False,
        require_calee_identity=True, require_caleemobile_identity=True,
        calee_build_version="0.3.22", caleemobile_build_version="0.0.22+22",
        caleemobile_dirty=True,
    )
    assert report.overall_status == STATUS_BLOCKED
    identity = next(c for c in report.components if c.name == "CaleeMobile build identity")
    assert identity.status == STATUS_BLOCKED


def test_build_identity_evidence_surfaced_in_html(tmp_path):
    report = build_release_report(
        **_passing_release_kwargs(),
        android_mandatory=False, ios_mandatory=False,
        require_calee_identity=True,
        calee_build_version="0.3.22", calee_git_sha="tab123",
        calee_version_code="322", calee_application_id="com.calee.app",
        caleeshell_version="1.4.0",
    )
    html_path = tmp_path / "r.html"
    write_html(report, html_path)
    html = html_path.read_text()
    for expected in ("Calee tablet build identity", "0.3.22", "tab123", "322",
                     "com.calee.app", "CaleeShell version", "1.4.0"):
        assert expected in html, f"{expected!r} missing from build-identity HTML"
