"""Priority 3 adversarial tests: raw-byte preservation + envelope integrity.

Covers that the provenance record (a) records raw-byte digests distinct from the
semantic content digest, (b) is protected by an envelope digest, and (c) BLOCKS
on tampering with EVERY mutable field -- source-evidence, adoption,
local-verification, artifact-id, workflow-run-id, regression-SHA, and the raw
JSON/ZIP bytes themselves. Also checks the on-disk bundle layout writes the
exact bytes back.
"""

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
        "pubspecVersion": "0.0.24+24",
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


def _record(result_bytes=None, zip_bytes=None):
    src = _ci_source()
    if result_bytes is None:
        result_bytes = json.dumps(src).encode("utf-8")
    if zip_bytes is None:
        zip_bytes = b"PK\x03\x04 pretend zip bytes"
    return sp.build_provenance_record(
        src, release_run_id="run-42", adopted_at="2026-07-18T00:00:00Z",
        adopted_by="gate", source_path="/tmp/artifact.json",
        source_artifact_id="8428705832", source_artifact_digest="sha256:" + "d" * 64,
        raw_result_bytes=result_bytes, raw_zip_bytes=zip_bytes,
    ), result_bytes, zip_bytes


def test_records_raw_and_semantic_digests_distinctly():
    src = _ci_source()
    rb = json.dumps(src, indent=2).encode("utf-8")  # different bytes, same semantics
    zb = b"PK\x03\x04zzz"
    rec = sp.build_provenance_record(
        src, release_run_id="r", adopted_at="t", adopted_by="g", source_path="p",
        raw_result_bytes=rb, raw_zip_bytes=zb,
    )
    assert rec["sourceResultSha256"] == sp.raw_sha256(rb)
    assert rec["sourceArtifactZipSha256"] == sp.raw_sha256(zb)
    # The semantic content digest is NOT the raw-byte digest (different inputs).
    assert rec["sourceContentDigest"] != rec["sourceResultSha256"]
    assert "envelopeDigest" in rec


def test_clean_round_trip_verifies_with_raw_bytes():
    rec, rb, zb = _record()
    assert sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb) == []


def test_tamper_source_evidence_field_blocks():
    rec, rb, zb = _record()
    rec["sourceEvidence"]["testedSha"] = "b" * 40
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb)
    assert problems  # both the content digest and envelope digest catch it
    assert any("digest mismatch" in p for p in problems)


def test_tamper_adoption_field_blocks():
    rec, rb, zb = _record()
    rec["adoption"]["adoptedBy"] = "attacker"
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb)
    assert any("envelope digest mismatch" in p for p in problems)


def test_tamper_artifact_id_blocks():
    rec, rb, zb = _record()
    rec["sourceArtifactId"] = "999"
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb)
    assert any("envelope digest mismatch" in p for p in problems)


def test_tamper_workflow_run_id_blocks():
    rec, rb, zb = _record()
    rec["sourceEvidence"]["workflowRunId"] = "0"
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb)
    assert any("envelope digest mismatch" in p for p in problems) or any("digest mismatch" in p for p in problems)


def test_tamper_regression_sha_blocks():
    rec, rb, zb = _record()
    rec["sourceEvidence"]["regressionSha"] = "d" * 40
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb)
    assert problems


def test_tamper_local_verification_blocks():
    src = _ci_source(generatedBy="local", workflowRunId=None)
    rec = sp.build_provenance_record(
        src, release_run_id="r", adopted_at="t", adopted_by="g", source_path="p",
        local_verification={"ok": True, "problems": [], "flutterVersion": "3.44.1"},
        raw_result_bytes=b"{}",
    )
    rec["localVerification"]["ok"] = False  # pretend it passed
    problems = sp.verify_provenance_record(rec, result_bytes=b"{}")
    assert any("envelope digest mismatch" in p for p in problems)


def test_tamper_recorded_result_digest_blocks():
    rec, rb, zb = _record()
    rec["sourceResultSha256"] = "sha256:" + "0" * 64
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb)
    # envelope catches the field change; and the raw digest check also fires.
    assert any("envelope digest mismatch" in p for p in problems)


def test_altered_raw_result_bytes_block():
    rec, rb, zb = _record()
    tampered = rb + b" "  # one extra byte -> different raw digest
    problems = sp.verify_provenance_record(rec, result_bytes=tampered, zip_bytes=zb)
    assert any("source-result.json raw-byte digest mismatch" in p for p in problems)


def test_altered_raw_zip_bytes_block():
    rec, rb, zb = _record()
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb + b"x")
    assert any("source-artifact.zip raw-byte digest mismatch" in p for p in problems)


def test_missing_envelope_digest_blocks():
    rec, rb, zb = _record()
    del rec["envelopeDigest"]
    problems = sp.verify_provenance_record(rec, result_bytes=rb, zip_bytes=zb)
    assert any("no envelopeDigest" in p for p in problems)


def test_consolidation_rehashes_raw_bundle_files_and_blocks_on_tamper(tmp_path):
    """Priority 3.6: consolidation recomputes raw-byte digests against the
    on-disk bundle files, so altering a preserved byte after adoption BLOCKS."""
    import datetime

    from calee_regression import consolidated_report as cr

    src = _ci_source(testedSha="a" * 40, pubspecVersion="0.0.24+24")
    result_bytes = json.dumps(src).encode("utf-8")
    zip_bytes = b"PK\x03\x04 exact zip bytes"
    record = sp.build_provenance_record(
        src, release_run_id="run-9", adopted_at="2026-07-18T00:00:00Z",
        adopted_by="gate", source_path="github-artifact:1@run:2",
        source_artifact_id="1", raw_result_bytes=result_bytes, raw_zip_bytes=zip_bytes,
    )
    sp.write_evidence_bundle(tmp_path, record, result_bytes=result_bytes, zip_bytes=zip_bytes)

    report_dict = {"status": "passed", "provenance": record, "evidence": src}
    now = datetime.datetime(2026, 7, 18, 1, 0, 0, tzinfo=datetime.timezone.utc)

    ok = cr.component_from_selector_contract(
        "sel", report_dict, mandatory=True, component_dir=str(tmp_path),
        expected_git_sha="a" * 40, expected_version="0.0.24+24",
        expected_release_run_id="run-9", now=now,
    )
    assert ok.status == cr.STATUS_PASS, ok.detail

    # Tamper the preserved raw JSON file on disk (a byte the semantic digest
    # might not notice, but the raw-byte digest will).
    (tmp_path / sp.BUNDLE_RESULT_JSON).write_bytes(result_bytes + b" ")
    tampered = cr.component_from_selector_contract(
        "sel", report_dict, mandatory=True, component_dir=str(tmp_path),
        expected_git_sha="a" * 40, expected_version="0.0.24+24",
        expected_release_run_id="run-9", now=now,
    )
    assert tampered.status == cr.STATUS_BLOCKED
    assert any("raw-byte digest mismatch" in d for d in tampered.detail)


def test_write_evidence_bundle_writes_exact_bytes(tmp_path):
    rec, rb, zb = _record()
    written = sp.write_evidence_bundle(tmp_path, rec, result_bytes=rb, zip_bytes=zb)
    assert set(written) == {
        sp.BUNDLE_ARTIFACT_ZIP, sp.BUNDLE_ARTIFACT_SHA,
        sp.BUNDLE_RESULT_JSON, sp.BUNDLE_RESULT_SHA, sp.BUNDLE_PROVENANCE,
    }
    # Raw bytes are written back byte-for-byte.
    assert (tmp_path / sp.BUNDLE_RESULT_JSON).read_bytes() == rb
    assert (tmp_path / sp.BUNDLE_ARTIFACT_ZIP).read_bytes() == zb
    # Sidecar digests match the raw bytes.
    assert sp.raw_sha256(rb) in (tmp_path / sp.BUNDLE_RESULT_SHA).read_text()
    assert sp.raw_sha256(zb) in (tmp_path / sp.BUNDLE_ARTIFACT_SHA).read_text()
    # The written provenance re-verifies against the written raw files.
    reloaded = json.loads((tmp_path / sp.BUNDLE_PROVENANCE).read_text())
    assert sp.verify_provenance_record(
        reloaded,
        result_bytes=(tmp_path / sp.BUNDLE_RESULT_JSON).read_bytes(),
        zip_bytes=(tmp_path / sp.BUNDLE_ARTIFACT_ZIP).read_bytes(),
    ) == []
