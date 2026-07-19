"""Immutable source provenance vs. release adoption (Priority 1, Problem B)."""

from __future__ import annotations

import json

import pytest

from calee_regression import selector_provenance as sp

SHA40 = "a" * 40
REG40 = "c" * 40


def _ci_source(**overrides):
    data = {
        "schemaVersion": 1,
        "component": "caleemobile-selector-contract",
        "testedSha": SHA40,
        "pubspecVersion": "0.0.23+23",
        "flutterVersion": "3.44.1",
        "contract": "PASS",
        "selectorsChecked": 62,
        "selectorsPresent": 62,
        "missing": [],
        "timestamp": "2026-07-18T00:00:00Z",
        "generatedBy": "ci",
        "workflowRunId": "1234567890",
        "regressionSha": REG40,
    }
    data.update(overrides)
    return data


def test_content_digest_is_order_independent():
    a = {"b": 2, "a": 1, "missing": []}
    b = {"a": 1, "missing": [], "b": 2}
    assert sp.content_digest(a) == sp.content_digest(b)


def test_content_digest_excludes_self_referential_field():
    ev = _ci_source()
    d1 = sp.content_digest(ev)
    ev2 = dict(ev, artifactDigest=d1)
    # Embedding the digest does not change the digest.
    assert sp.content_digest(ev2) == d1


def test_valid_ci_source_has_no_problems():
    assert sp.validate_source_provenance(_ci_source()) == []


def test_missing_generated_by_flagged():
    problems = sp.validate_source_provenance(_ci_source(generatedBy=None))
    assert any("generatedBy" in p for p in problems)


def test_invalid_generated_by_flagged():
    problems = sp.validate_source_provenance(_ci_source(generatedBy="buildkite"))
    assert any("not exactly 'ci' or 'local'" in p for p in problems)


def test_ci_without_workflow_run_id_flagged():
    problems = sp.validate_source_provenance(_ci_source(workflowRunId=None))
    assert any("workflowRunId" in p for p in problems)


def test_abbreviated_regression_sha_flagged():
    problems = sp.validate_source_provenance(_ci_source(regressionSha="abc1234"))
    assert any("40-character" in p for p in problems)


def test_local_requires_verified_toolchain():
    src = _ci_source(generatedBy="local", workflowRunId=None, regressionSha=REG40)
    # No local verification block -> flagged.
    problems = sp.validate_source_provenance(src, local_verification=None)
    assert any("verified local command evidence" in p for p in problems)
    # A failing verification -> flagged.
    problems = sp.validate_source_provenance(src, local_verification={"ok": False, "problems": ["no flutter"]})
    assert any("local toolchain verification failed" in p for p in problems)
    # A passing verification -> accepted.
    assert sp.validate_source_provenance(src, local_verification={"ok": True, "problems": []}) == []


def test_ci_with_local_verification_is_contradictory():
    problems = sp.validate_source_provenance(_ci_source(), local_verification={"ok": True})
    assert any("contradictory provenance" in p for p in problems)


def test_self_declared_digest_mismatch_flagged():
    problems = sp.validate_source_provenance(_ci_source(artifactDigest="sha256:" + "0" * 64))
    assert any("does not match its actual content digest" in p for p in problems)


def test_build_and_verify_round_trip():
    src = _ci_source()
    record = sp.build_provenance_record(
        src, release_run_id="run-1", adopted_at="2026-07-18T00:00:00Z",
        adopted_by="gate", source_path="/tmp/artifact.json",
        source_artifact_id="42", source_artifact_digest="sha256:" + "d" * 64,
    )
    assert record["sourceEvidence"] == src  # byte-for-byte content
    assert record["adoption"]["releaseRunId"] == "run-1"
    assert record["sourceArtifactId"] == "42"
    assert sp.verify_provenance_record(record) == []


def test_verify_detects_tampering_after_digest():
    record = sp.build_provenance_record(
        _ci_source(), release_run_id="run-1", adopted_at="t", adopted_by="gate",
        source_path="/tmp/a.json",
    )
    # Mutate a preserved field after the digest was computed.
    record["sourceEvidence"]["testedSha"] = "b" * 40
    problems = sp.verify_provenance_record(record)
    assert any("digest mismatch" in p or "modified after adoption" in p for p in problems)


def test_anchored_digest_accepts_matching_record():
    # P7.8: a trusted digest anchored outside the bundle that matches the
    # record's envelopeDigest adds no problems.
    record = sp.build_provenance_record(
        _ci_source(), release_run_id="run-1", adopted_at="t", adopted_by="gate",
        source_path="/tmp/a.json",
    )
    anchored = record["envelopeDigest"]
    assert sp.verify_provenance_record(record, trusted_envelope_digest=anchored) == []


def test_anchored_digest_catches_coordinated_rehash():
    # P7.8: the KEY case. An attacker edits a preserved field AND recomputes the
    # envelopeDigest, producing a self-consistent record. The plain re-verify
    # cannot detect this (the bundle is internally consistent); a trusted digest
    # anchored OUTSIDE the bundle does.
    record = sp.build_provenance_record(
        _ci_source(), release_run_id="run-1", adopted_at="t", adopted_by="gate",
        source_path="/tmp/a.json",
    )
    trusted = record["envelopeDigest"]  # captured before tampering, kept outside
    # Coordinated re-hash: change a field, refresh both digests.
    record["sourceEvidence"]["testedSha"] = "b" * 40
    record["sourceContentDigest"] = sp.content_digest(record["sourceEvidence"])
    record["envelopeDigest"] = sp.envelope_digest(record)
    # Without the anchor, the re-hashed record looks internally consistent.
    assert sp.verify_provenance_record(record) == []
    # With the outside anchor, the swap is caught.
    problems = sp.verify_provenance_record(record, trusted_envelope_digest=trusted)
    assert any("trusted anchored digest" in p for p in problems)


def test_anchored_digest_mismatch_on_untouched_record_flags():
    record = sp.build_provenance_record(
        _ci_source(), release_run_id="run-1", adopted_at="t", adopted_by="gate",
        source_path="/tmp/a.json",
    )
    problems = sp.verify_provenance_record(record, trusted_envelope_digest="sha256:" + "0" * 64)
    assert any("trusted anchored digest" in p for p in problems)


def test_build_does_not_mutate_source():
    src = _ci_source()
    sp.build_provenance_record(src, release_run_id="r", adopted_at="t", adopted_by="g", source_path="p")
    # The caller's dict is untouched (no releaseRunId/adoption stamped in).
    assert "adoption" not in src
    assert src == _ci_source()


def test_verify_raises_without_source_evidence():
    with pytest.raises(sp.ProvenanceError):
        sp.verify_provenance_record({"adoption": {"releaseRunId": "r"}})


def test_record_is_json_serializable():
    record = sp.build_provenance_record(
        _ci_source(), release_run_id="r", adopted_at="t", adopted_by="g", source_path="p",
        local_verification={"ok": True, "commands": []},
    )
    json.dumps(record)
