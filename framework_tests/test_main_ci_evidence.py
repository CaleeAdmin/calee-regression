"""Priority 8 (this session) -- independent verification of merged-main CI
evidence. Pure unit tests of main_ci_evidence.py.
"""

from __future__ import annotations

import json

import pytest

from calee_regression import main_ci_evidence as mce

SHA = "a" * 40
OTHER_SHA = "b" * 40


def _simple_summary(**overrides) -> dict:
    """calee-regression's own actual evidence shape (framework-test-
    summary.json): one unconditional job, no gates breakdown."""
    data = dict(
        workflow="framework-tests", event="push", ref="refs/heads/main",
        commitSha=SHA, runId="123456", runAttempt="1", isMainPush=True, isMergeGroup=False,
    )
    data.update(overrides)
    return data


def _rich_summary(**overrides) -> dict:
    """CaleeMobile-Regression's ci-summary.json shape: multiple named gates
    + skip classification."""
    data = dict(
        commitSha=SHA, runId="999", event="push", ref="refs/heads/main",
        isMainPush=True, isMergeGroup=False, hasCaleemobileToken=True,
        gates={
            "apiFrameworkTests": "success", "uiReportWrapperTests": "success",
            "fixtureCliSmoke": "success", "selectorContract": "success",
            "uiSuiteAnalyze": "success", "releaseCertificationGuard": "success",
        },
        skipClassification={},
    )
    data.update(overrides)
    return data


def _verify(summary, **kwargs):
    kwargs.setdefault("expected_sha", SHA)
    return mce.verify_main_ci_evidence(summary, **kwargs)


# ── simple (calee-regression) shape ─────────────────────────────────────


def test_simple_shape_exact_match_accepted():
    assert _verify(_simple_summary()) == []


def test_simple_shape_merge_group_accepted():
    summary = _simple_summary(event="merge_group", ref="refs/heads/main", isMainPush=False, isMergeGroup=True)
    assert _verify(summary) == []


def test_simple_shape_wrong_sha_rejected():
    problems = _verify(_simple_summary(commitSha=OTHER_SHA))
    assert any("NOT for the commit being verified" in p for p in problems)


def test_simple_shape_missing_commit_sha_rejected():
    summary = _simple_summary()
    del summary["commitSha"]
    problems = _verify(summary)
    assert any("no commitSha" in p for p in problems)


def test_simple_shape_pull_request_event_rejected():
    summary = _simple_summary(event="pull_request", ref="refs/pull/42/merge", isMainPush=False)
    problems = _verify(summary)
    assert any("pull_request" in p and "never about the merged" in p for p in problems)


def test_simple_shape_push_to_non_main_branch_rejected():
    summary = _simple_summary(event="push", ref="refs/heads/feature-x", isMainPush=False)
    problems = _verify(summary)
    assert any("neither a push to" in p for p in problems)


def test_simple_shape_schedule_event_rejected():
    summary = _simple_summary(event="schedule", ref="refs/heads/main", isMainPush=False)
    problems = _verify(summary)
    assert any("neither a push to" in p for p in problems)


def test_simple_shape_tampered_is_main_push_flag_caught():
    # Evidence claims isMainPush=True but the ref says otherwise.
    summary = _simple_summary(ref="refs/heads/dev", isMainPush=True)
    problems = _verify(summary)
    assert any("disagrees with its own event/ref" in p for p in problems)


def test_simple_shape_no_gates_requested_and_none_present_is_fine():
    assert _verify(_simple_summary(), required_gates=None) == []


def test_simple_shape_required_gate_requested_but_absent_is_rejected():
    problems = _verify(_simple_summary(), required_gates=["pytest"])
    assert any("no 'gates' breakdown at all" in p for p in problems)


def test_malformed_expected_sha_rejected():
    problems = mce.verify_main_ci_evidence(_simple_summary(), expected_sha="abc123")
    assert any("full 40-character commit SHA" in p for p in problems)


# ── rich (CaleeMobile-Regression-style) shape ───────────────────────────


def test_rich_shape_all_gates_success_accepted():
    assert _verify(_rich_summary()) == []


def test_rich_shape_missing_required_gate_rejected():
    summary = _rich_summary()
    del summary["gates"]["selectorContract"]
    problems = _verify(summary, required_gates=["selectorContract"])
    assert any("selectorContract" in p and "not present" in p for p in problems)


def test_rich_shape_failed_gate_rejected():
    summary = _rich_summary(gates=dict(_rich_summary()["gates"], apiFrameworkTests="failure"))
    problems = _verify(summary)
    assert any("apiFrameworkTests" in p and "did not succeed" in p for p in problems)


def test_rich_shape_not_applicable_skip_accepted():
    summary = _rich_summary(
        gates=dict(_rich_summary()["gates"], selectorContract="skipped", uiSuiteAnalyze="skipped"),
        skipClassification={"selectorContract": "not-applicable", "uiSuiteAnalyze": "not-applicable"},
    )
    assert _verify(summary) == []


def test_rich_shape_unexpected_skip_rejected():
    summary = _rich_summary(
        gates=dict(_rich_summary()["gates"], apiFrameworkTests="skipped"),
        skipClassification={"apiFrameworkTests": "unexpected"},
    )
    problems = _verify(summary)
    assert any("apiFrameworkTests" in p and "unexpected skip" in p for p in problems)


def test_rich_shape_skip_with_no_classification_at_all_rejected():
    summary = _rich_summary(gates=dict(_rich_summary()["gates"], apiFrameworkTests="skipped"))
    problems = _verify(summary)
    assert any("apiFrameworkTests" in p for p in problems)


def test_rich_shape_specific_required_gates_only_checks_those():
    summary = _rich_summary(gates=dict(_rich_summary()["gates"], uiSuiteAnalyze="failure"))
    # Explicitly asking to check only a DIFFERENT gate should still surface
    # every listed gate's problems when required_gates is a strict subset --
    # but per contract, explicit required_gates narrows to just those named.
    problems = _verify(summary, required_gates=["apiFrameworkTests"])
    assert problems == []
    problems_all = _verify(summary, required_gates=["apiFrameworkTests", "uiSuiteAnalyze"])
    assert any("uiSuiteAnalyze" in p for p in problems_all)


# ── artifact digest ──────────────────────────────────────────────────────


def test_artifact_digest_match_accepted(tmp_path):
    summary = _simple_summary()
    raw = json.dumps(summary).encode("utf-8")
    import hashlib
    digest = hashlib.sha256(raw).hexdigest()
    problems = _verify(summary, raw_bytes=raw, expected_artifact_sha256=digest)
    assert problems == []


def test_artifact_digest_mismatch_rejected():
    summary = _simple_summary()
    raw = json.dumps(summary).encode("utf-8")
    problems = _verify(summary, raw_bytes=raw, expected_artifact_sha256="0" * 64)
    assert any("digest mismatch" in p for p in problems)


def test_artifact_digest_requested_without_raw_bytes_rejected():
    problems = _verify(_simple_summary(), expected_artifact_sha256="0" * 64)
    assert any("no raw evidence bytes" in p for p in problems)


# ── load_summary ─────────────────────────────────────────────────────────


def test_load_summary_reads_json_and_raw_bytes(tmp_path):
    summary = _simple_summary()
    path = tmp_path / "framework-test-summary.json"
    path.write_text(json.dumps(summary))
    parsed, raw = mce.load_summary(path)
    assert parsed["commitSha"] == SHA
    assert raw == path.read_bytes()


def test_load_summary_missing_file_raises():
    with pytest.raises(mce.MainCiEvidenceError):
        mce.load_summary("/no/such/file.json")


def test_load_summary_malformed_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    with pytest.raises(mce.MainCiEvidenceError):
        mce.load_summary(path)


def test_load_summary_non_object_json_raises(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(mce.MainCiEvidenceError):
        mce.load_summary(path)
