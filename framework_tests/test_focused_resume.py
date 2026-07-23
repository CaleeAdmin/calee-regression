"""Unit tests for safe focused resume (Phase 7, focused_resume.py): pure
`evaluate_resume` with an injected file-digest reader -- full-match
eligibility, per-criterion whole-resume refusal, digest tamper, retained
FAIL vs --retry-failed, blocked-step re-execution, and reused-vs-executed
evidence marking.
"""

from __future__ import annotations

import pytest

from calee_regression import focused_resume as fr, focused_workflow

RUN_ID = "run-20260723-101500-abc123"
BACKEND = "https://staging.calee.invalid"
SHAS = {"calee-regression": "aa" * 20, "caleemobile-regression": "bb" * 20}
PRODUCT_SHA = "c0ffee00" * 5
DEVICES = {"tablet": "TABLET-1", "ios": "IPHONE-1"}
ARTIFACT = {"status": "verified", "installed": {"versionName": "2.5.0", "versionCode": "25"}}
PURPOSE = "focused-post-fix-verification"

DIGESTS = {
    "/prior/environment/results.json": "fix" * 10,
    "/prior/tablet/standard/results.json": "ts" * 10,
    "/prior/tablet/diagnostic/results.json": "td" * 10,
    "/prior/api/attempt-1/results.json": "a1" * 10,
    "/prior/api/attempt-2/results.json": "a2" * 10,
    "/prior/ios/results.json": "io" * 10,
}


def fake_digest(path):
    return DIGESTS.get(str(path))


def step(step_id, status, path=None, mode=None):
    return {
        "id": step_id, "title": f"Step {step_id}", "status": status, "mode": mode,
        "reportPath": path, "reportSha256": DIGESTS.get(path) if path else None,
    }


def make_summary(**overrides):
    summary = {
        "reportType": "focused-verify-summary",
        "reportSchemaVersion": 2,
        "runId": RUN_ID,
        "status": "pass",
        "verifiedBackend": BACKEND,
        "fixtureVersion": "REG-9",
        "regressionShas": dict(SHAS),
        "productBuild": {"caleeMobileSha": PRODUCT_SHA},
        "deviceIds": dict(DEVICES),
        "installedArtifactIdentity": dict(ARTIFACT),
        "executionPurpose": PURPOSE,
        "fixtureOwnership": {
            "acquisition": {"state": "acquired"},
            "release": {"state": "released"},
        },
        "steps": [
            step("fixture", "pass", "/prior/environment/results.json"),
            step("tablet-standard", "pass", "/prior/tablet/standard/results.json", "standard"),
            step("tablet-diagnostic", "pass", "/prior/tablet/diagnostic/results.json", "diagnostic"),
            step("api-1", "pass", "/prior/api/attempt-1/results.json"),
            step("api-2", "pass", "/prior/api/attempt-2/results.json"),
            step("ios", "pass", "/prior/ios/results.json"),
        ],
    }
    summary.update(overrides)
    return summary


def make_context(**overrides):
    kwargs = dict(
        run_id=RUN_ID, backend=BACKEND, fixture_version="REG-9",
        regression_shas=dict(SHAS), product_sha=PRODUCT_SHA,
        device_ids=dict(DEVICES), installed_artifact=dict(ARTIFACT),
        execution_purpose=PURPOSE,
        step_ids=["tablet-standard", "tablet-diagnostic", "api-1", "api-2", "ios"],
        lock_history_clean=True, fixture_needs_reset=False,
    )
    kwargs.update(overrides)
    return fr.ResumeContext(**kwargs)


def evaluate(summary=None, context=None, **kwargs):
    return fr.evaluate_resume(
        summary if summary is not None else make_summary(),
        context if context is not None else make_context(),
        file_digest=fake_digest, **kwargs,
    )


# ── eligibility ────────────────────────────────────────────────────────────
def test_full_match_reuses_every_prior_pass():
    decision = evaluate()
    assert decision.eligible
    assert decision.failed_criteria == []
    assert sorted(r.id for r in decision.reused) == [
        "api-1", "api-2", "ios", "tablet-diagnostic", "tablet-standard"]
    assert decision.retained_failures == []
    assert decision.execute_step_ids == []
    for reused in decision.reused:
        assert reused.report_path.startswith("/prior/")
        assert reused.report_sha256 == DIGESTS[reused.report_path]
        result = reused.to_result()
        assert result.evidence == fr.EVIDENCE_REUSED
        assert result.status == "pass"


@pytest.mark.parametrize("summary_overrides, context_overrides, criterion", [
    ({"reportSchemaVersion": 1}, {}, fr.CRITERION_SCHEMA),
    ({"reportType": "something-else"}, {}, fr.CRITERION_SCHEMA),
    ({"runId": "a-different-run"}, {}, fr.CRITERION_RUN_ID),
    ({"regressionShas": {"calee-regression": "ff" * 20}}, {}, fr.CRITERION_FRAMEWORK_SHAS),
    ({}, {"regression_shas": {}}, fr.CRITERION_FRAMEWORK_SHAS),
    ({"productBuild": {"caleeMobileSha": "deadbeef"}}, {}, fr.CRITERION_PRODUCT_SHAS),
    ({}, {"product_sha": None}, fr.CRITERION_PRODUCT_SHAS),
    ({"verifiedBackend": "https://other.invalid"}, {}, fr.CRITERION_BACKEND),
    ({"fixtureVersion": "REG-8"}, {}, fr.CRITERION_FIXTURE_VERSION),
    ({"fixtureOwnership": {"acquisition": {"state": "acquired"}, "release": None}}, {},
     fr.CRITERION_FIXTURE_OWNERSHIP),
    ({}, {"lock_history_clean": None}, fr.CRITERION_FIXTURE_OWNERSHIP),
    ({}, {"lock_history_clean": False}, fr.CRITERION_FIXTURE_OWNERSHIP),
    ({}, {"fixture_needs_reset": True}, fr.CRITERION_FIXTURE_IDENTITY),
    ({}, {"device_ids": {"tablet": "OTHER", "ios": "IPHONE-1"}}, fr.CRITERION_DEVICE_ID),
    ({"installedArtifactIdentity": {"status": "unproven"}}, {}, fr.CRITERION_INSTALLED_BUILD),
    ({}, {"installed_artifact": {"status": "verified",
                                 "installed": {"versionName": "2.6.0", "versionCode": "26"}}},
     fr.CRITERION_INSTALLED_BUILD),
    ({"executionPurpose": "focused-environment-check"}, {}, fr.CRITERION_EXECUTION_PURPOSE),
    ({}, {"step_ids": ["tablet-standard", "tablet-diagnostic"]}, fr.CRITERION_FEATURE_SCOPE),
])
def test_any_single_criterion_mismatch_refuses_the_whole_resume(
        summary_overrides, context_overrides, criterion):
    decision = evaluate(make_summary(**summary_overrides), make_context(**context_overrides))
    assert not decision.eligible
    assert criterion in decision.failed_criteria
    # nothing is reused when refused -- there is no partial trust
    assert decision.reused == []
    assert decision.retained_failures == []
    # the refusal names the criterion and the exact fresh-run command
    message = decision.refusal_message()
    assert criterion in message
    assert fr.FRESH_RUN_COMMAND in message
    assert "<" not in fr.FRESH_RUN_COMMAND and ">" not in fr.FRESH_RUN_COMMAND


def test_refusal_names_every_failed_criterion():
    decision = evaluate(
        make_summary(verifiedBackend="https://other.invalid", fixtureVersion="REG-8"))
    assert not decision.eligible
    assert fr.CRITERION_BACKEND in decision.failed_criteria
    assert fr.CRITERION_FIXTURE_VERSION in decision.failed_criteria
    message = decision.refusal_message()
    assert fr.CRITERION_BACKEND in message and fr.CRITERION_FIXTURE_VERSION in message


# ── digest integrity ───────────────────────────────────────────────────────
def test_fixture_report_digest_tamper_refuses():
    tampered = dict(DIGESTS, **{"/prior/environment/results.json": "tampered"})
    decision = fr.evaluate_resume(
        make_summary(), make_context(), file_digest=lambda p: tampered.get(str(p)))
    assert not decision.eligible
    assert fr.CRITERION_FIXTURE_IDENTITY in decision.failed_criteria


def test_child_report_digest_tamper_refuses_whole_resume():
    tampered = dict(DIGESTS, **{"/prior/api/attempt-2/results.json": "tampered"})
    decision = fr.evaluate_resume(
        make_summary(), make_context(), file_digest=lambda p: tampered.get(str(p)))
    assert not decision.eligible
    assert fr.CRITERION_CHILD_DIGEST in decision.failed_criteria
    assert any("api-2" in r for r in decision.reasons)
    assert decision.reused == []


def test_missing_child_report_refuses():
    missing = {k: v for k, v in DIGESTS.items() if not k.endswith("ios/results.json")}
    decision = fr.evaluate_resume(
        make_summary(), make_context(), file_digest=lambda p: missing.get(str(p)))
    assert not decision.eligible
    assert fr.CRITERION_CHILD_DIGEST in decision.failed_criteria


def test_missing_prior_fixture_pass_refuses():
    summary = make_summary()
    summary["steps"][0]["status"] = "blocked"
    decision = evaluate(summary)
    assert not decision.eligible
    assert fr.CRITERION_FIXTURE_IDENTITY in decision.failed_criteria


# ── reuse semantics ────────────────────────────────────────────────────────
def test_prior_fail_is_retained_not_rerun_by_default():
    summary = make_summary()
    summary["steps"][3] = step("api-1", "fail", "/prior/api/attempt-1/results.json")
    decision = evaluate(summary)
    assert decision.eligible
    assert [r.id for r in decision.retained_failures] == ["api-1"]
    assert "api-1" not in decision.execute_step_ids
    retained = decision.retained_failures[0].to_result()
    assert retained.status == "fail"
    assert retained.evidence == fr.EVIDENCE_REUSED
    assert "never automatically rerun" in retained.detail


def test_retry_failed_reruns_fail_as_new_attempt():
    summary = make_summary()
    summary["steps"][3] = step("api-1", "fail", "/prior/api/attempt-1/results.json")
    decision = evaluate(summary, retry_failed=True)
    assert decision.eligible
    assert decision.retained_failures == []
    assert decision.execute_step_ids == ["api-1"]
    assert sorted(r.id for r in decision.reused) == [
        "api-2", "ios", "tablet-diagnostic", "tablet-standard"]


def test_blocked_and_not_run_and_invalid_config_steps_are_reexecuted():
    summary = make_summary()
    summary["steps"][1] = step("tablet-standard", "blocked", None, "standard")
    summary["steps"][2] = step("tablet-diagnostic", "blocked_not_run", None, "diagnostic")
    summary["steps"][4] = step("api-2", "invalid_config", None)
    decision = evaluate(summary)
    assert decision.eligible
    assert sorted(decision.execute_step_ids) == ["api-2", "tablet-diagnostic", "tablet-standard"]
    assert sorted(r.id for r in decision.reused) == ["api-1", "ios"]


def test_pass_is_never_copied_to_a_different_run_id():
    decision = evaluate(context=make_context(run_id="another-run-id"))
    assert not decision.eligible
    assert fr.CRITERION_RUN_ID in decision.failed_criteria
    assert decision.reused == []
    assert "never copied" in decision.refusal_message()


def test_reused_result_carries_original_path_and_digest_and_mode():
    decision = evaluate()
    by_id = {r.id: r for r in decision.reused}
    result = by_id["tablet-standard"].to_result()
    assert result.report_path == "/prior/tablet/standard/results.json"
    assert result.report_sha256 == DIGESTS["/prior/tablet/standard/results.json"]
    assert result.mode == "standard"
    assert result.to_dict()["evidence"] == "reused"
