"""Priority 3 (this session) -- authenticated distributed-build acceptance
provenance. Pure unit tests of distributed_build_provenance.py, mirroring
test_selector_provenance.py / test_selector_provenance_raw.py's conventions:
build a valid record, prove every mutation of a mutable field is caught by
recompute-and-compare, prove raw-byte tamper detection, and prove every
generatedBy-specific proof requirement is enforced.
"""

from __future__ import annotations

import datetime
import json

import pytest

from calee_regression import distributed_build_provenance as dbp

NOW = datetime.datetime(2026, 7, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)
SHA = "a" * 40


def _evidence(**overrides) -> dict:
    data = {
        "schemaVersion": 2,
        "component": "caleemobile-distributed-build-acceptance",
        "provider": "app_store_connect",
        "channel": "testflight",
        "distributedBuildId": "TF-4821",
        "releaseId": "2026.07.21-rc1",
        "testedGitSha": SHA,
        "testedVersion": "0.0.24+24",
        "providerAccountOrProject": "acct-12345",
        "providerRecordId": "asc-build-98765",
        "providerObservedAt": "2026-07-21T10:00:00Z",
        "generatedBy": "provider-api",
        "sourceDigest": "sha256:" + "1" * 64,
        "timestamp": "2026-07-21T11:00:00Z",
    }
    data.update(overrides)
    return data


def _verify(evidence, **kwargs):
    kwargs.setdefault("now", NOW)
    return dbp.validate_distributed_evidence(evidence, **kwargs)


# ── validate_distributed_evidence: acceptance ───────────────────────────


def test_valid_evidence_is_accepted():
    assert _verify(_evidence()) == []


def test_valid_signed_export_evidence_is_accepted():
    ev = _evidence(
        provider="custom_signed_export", generatedBy="signed-export",
        signatureOrArtifactProvenance={"signerFingerprint": "AA:BB:CC", "signature": "base64=="},
    )
    assert _verify(ev) == []


def test_valid_ci_artifact_evidence_is_accepted():
    ev = _evidence(generatedBy="ci-artifact", provider="play_console", providerRecordId="run-123456")
    assert _verify(ev) == []


# ── rejections: missing / malformed fields ──────────────────────────────


def test_missing_schema_version_rejected():
    ev = _evidence()
    del ev["schemaVersion"]
    assert any("schemaVersion" in p for p in _verify(ev))


def test_unknown_schema_version_rejected():
    assert any("schemaVersion" in p for p in _verify(_evidence(schemaVersion=99)))


def test_wrong_component_rejected():
    assert any("component" in p for p in _verify(_evidence(component="something-else")))


@pytest.mark.parametrize("field", [
    "provider", "channel", "distributedBuildId", "providerAccountOrProject",
    "providerRecordId", "providerObservedAt", "generatedBy", "testedGitSha",
    "testedVersion", "sourceDigest", "timestamp",
])
def test_missing_required_field_rejected(field):
    ev = _evidence()
    del ev[field]
    problems = _verify(ev)
    assert problems, f"expected a problem when {field!r} is missing"


def test_unrecognised_provider_rejected():
    assert any("provider" in p for p in _verify(_evidence(provider="carrier_pigeon")))


def test_unrecognised_channel_rejected():
    assert any("channel" in p for p in _verify(_evidence(channel="carrier_pigeon")))


def test_abbreviated_sha_rejected():
    assert any("abbreviated" in p for p in _verify(_evidence(testedGitSha="abc1234")))


def test_malformed_version_rejected():
    assert any("well-formed" in p for p in _verify(_evidence(testedVersion="latest")))


def test_invalid_provider_observed_at_rejected():
    assert any("providerObservedAt" in p for p in _verify(_evidence(providerObservedAt="not-a-date")))


# ── rejections: fabrication / self-declaration (Priority 3.9) ──────────


@pytest.mark.parametrize("rejected", sorted(dbp.REJECTED_GENERATED_BY))
def test_rejected_generated_by_is_explicitly_refused(rejected):
    problems = _verify(_evidence(generatedBy=rejected))
    assert any("explicitly rejected" in p for p in problems)


def test_unrecognised_generated_by_rejected():
    assert any("not a recognised authentic source" in p for p in _verify(_evidence(generatedBy="vibes")))


def test_missing_generated_by_rejected():
    ev = _evidence()
    del ev["generatedBy"]
    assert any("never fabricated" in p for p in _verify(ev))


def test_provider_api_with_wrong_provider_rejected():
    ev = _evidence(generatedBy="provider-api", provider="custom_signed_export")
    assert any("provider-api" in p for p in _verify(ev))


def test_signed_export_without_signature_is_rejected():
    ev = _evidence(provider="custom_signed_export", generatedBy="signed-export")
    ev.pop("signatureOrArtifactProvenance", None)
    problems = _verify(ev)
    assert any("signed-export" in p and "proves nothing" in p for p in problems)


def test_signed_export_with_empty_signature_dict_is_rejected():
    ev = _evidence(
        provider="custom_signed_export", generatedBy="signed-export", signatureOrArtifactProvenance={},
    )
    assert any("proves nothing" in p for p in _verify(ev))


def test_ci_artifact_without_provider_record_id_rejected():
    ev = _evidence(generatedBy="ci-artifact")
    ev["providerRecordId"] = ""
    problems = _verify(ev)
    assert any("ci-artifact" in p for p in problems)


# ── freshness / expected-identity cross-checks ──────────────────────────


def test_future_timestamp_rejected():
    future = (NOW + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert any("future" in p for p in _verify(_evidence(timestamp=future)))


def test_stale_timestamp_rejected():
    stale = (NOW - datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert any("stale" in p for p in _verify(_evidence(timestamp=stale)))


def test_wrong_expected_sha_rejected():
    problems = _verify(_evidence(), expected_git_sha="b" * 40)
    assert any("different CaleeMobile commit" in p for p in problems)


def test_wrong_expected_version_rejected():
    problems = _verify(_evidence(), expected_version="9.9.9+9")
    assert any("different CaleeMobile version" in p for p in problems)


def test_wrong_expected_release_id_rejected():
    problems = _verify(_evidence(), expected_release_id="2099.01.01-other")
    assert any("another release" in p for p in problems)


def test_missing_release_id_with_expected_release_id_rejected():
    ev = _evidence()
    del ev["releaseId"]
    problems = _verify(ev, expected_release_id="2026.07.21-rc1")
    assert any("not bound to this release" in p for p in problems)


def test_matching_expected_identity_accepted():
    problems = _verify(
        _evidence(), expected_git_sha=SHA, expected_version="0.0.24+24", expected_release_id="2026.07.21-rc1",
    )
    assert problems == []


# ── digests ──────────────────────────────────────────────────────────────


def test_content_digest_excludes_source_digest_field_and_is_key_order_independent():
    ev1 = _evidence()
    ev2 = {k: ev1[k] for k in reversed(list(ev1.keys()))}
    assert dbp.content_digest(ev1) == dbp.content_digest(ev2)
    ev3 = dict(ev1, sourceDigest="sha256:" + "9" * 64)
    assert dbp.content_digest(ev1) == dbp.content_digest(ev3)


def test_raw_sha256_is_sha256_prefixed_and_exact():
    import hashlib
    data = b"exact bytes"
    assert dbp.raw_sha256(data) == "sha256:" + hashlib.sha256(data).hexdigest()


# ── build_provenance_record / verify_provenance_record round trip ──────


def test_build_and_verify_round_trip():
    evidence = _evidence()
    raw_bytes = json.dumps(evidence).encode("utf-8")
    record = dbp.build_provenance_record(
        evidence, release_run_id="release-test-001", adopted_at="2026-07-21T12:00:00Z",
        adopted_by="technical-owner", source_path="/tmp/evidence.json", raw_source_bytes=raw_bytes,
    )
    assert dbp.verify_provenance_record(
        record, source_bytes=raw_bytes, expected_release_run_id="release-test-001", now=NOW,
    ) == []


def test_build_does_not_mutate_source():
    evidence = _evidence()
    original = json.loads(json.dumps(evidence))
    dbp.build_provenance_record(
        evidence, release_run_id="r1", adopted_at="t", adopted_by="a", source_path="p",
    )
    assert evidence == original


def test_verify_raises_without_source_evidence():
    with pytest.raises(dbp.DistributedProvenanceError):
        dbp.verify_provenance_record({"envelopeDigest": "sha256:" + "0" * 64})


@pytest.mark.parametrize("mutate", [
    lambda r: r["sourceEvidence"].__setitem__("testedGitSha", "b" * 40),
    lambda r: r["adoption"].__setitem__("adoptedBy", "someone-else"),
    lambda r: r.__setitem__("sourceContentDigest", "sha256:" + "0" * 64),
    lambda r: r["sourceEvidence"].__setitem__("providerRecordId", "tampered"),
])
def test_tamper_any_mutable_field_is_caught_by_envelope_digest(mutate):
    evidence = _evidence()
    record = dbp.build_provenance_record(
        evidence, release_run_id="r1", adopted_at="t", adopted_by="a", source_path="p",
    )
    mutate(record)
    problems = dbp.verify_provenance_record(record, now=NOW)
    assert any("envelope digest mismatch" in p for p in problems)


def test_tamper_source_content_digest_alone_without_envelope_recompute_is_caught():
    evidence = _evidence()
    record = dbp.build_provenance_record(
        evidence, release_run_id="r1", adopted_at="t", adopted_by="a", source_path="p",
    )
    # Simulate an attacker who only edits sourceEvidence but forgets to
    # recompute sourceContentDigest/envelopeDigest -- both checks fire.
    record["sourceEvidence"]["testedVersion"] = "9.9.9+9"
    problems = dbp.verify_provenance_record(record, now=NOW)
    assert any("envelope digest mismatch" in p for p in problems)
    assert any("source evidence digest mismatch" in p for p in problems)


def test_altered_raw_source_bytes_block():
    evidence = _evidence()
    raw_bytes = json.dumps(evidence).encode("utf-8")
    record = dbp.build_provenance_record(
        evidence, release_run_id="r1", adopted_at="t", adopted_by="a", source_path="p",
        raw_source_bytes=raw_bytes,
    )
    altered = raw_bytes + b"\ntrailing-byte"
    problems = dbp.verify_provenance_record(record, source_bytes=altered, now=NOW)
    assert any("raw-byte digest mismatch" in p for p in problems)


def test_wrong_run_adoption_blocks():
    evidence = _evidence()
    record = dbp.build_provenance_record(
        evidence, release_run_id="release-run-a", adopted_at="t", adopted_by="a", source_path="p",
    )
    problems = dbp.verify_provenance_record(record, expected_release_run_id="release-run-b", now=NOW)
    assert any("adopted by a different run" in p for p in problems)


def test_anchored_trusted_envelope_digest_catches_coordinated_rehash():
    evidence = _evidence()
    record = dbp.build_provenance_record(
        evidence, release_run_id="r1", adopted_at="t", adopted_by="a", source_path="p",
    )
    trusted = record["envelopeDigest"]
    # Attacker edits a field AND recomputes envelopeDigest -- a plain recompute
    # alone would NOT catch this.
    record["sourceEvidence"]["testedVersion"] = "9.9.9+9"
    record["sourceContentDigest"] = dbp.content_digest(record["sourceEvidence"])
    record["envelopeDigest"] = dbp.envelope_digest(record)
    assert dbp.verify_provenance_record(record, now=NOW) == []  # self-consistent, passes alone
    problems = dbp.verify_provenance_record(record, trusted_envelope_digest=trusted, now=NOW)
    assert any("trusted anchored digest" in p for p in problems)


# ── evidence bundle write/read ──────────────────────────────────────────


def test_write_evidence_bundle_writes_exact_bytes(tmp_path):
    evidence = _evidence()
    raw_bytes = json.dumps(evidence).encode("utf-8")
    record = dbp.build_provenance_record(
        evidence, release_run_id="r1", adopted_at="t", adopted_by="a", source_path="p",
        raw_source_bytes=raw_bytes,
    )
    written = dbp.write_evidence_bundle(tmp_path, record, source_bytes=raw_bytes)
    assert set(written) == {dbp.BUNDLE_SOURCE_JSON, dbp.BUNDLE_SOURCE_SHA, dbp.BUNDLE_PROVENANCE}
    assert (tmp_path / dbp.BUNDLE_SOURCE_JSON).read_bytes() == raw_bytes
    reloaded = json.loads((tmp_path / dbp.BUNDLE_PROVENANCE).read_text())
    assert dbp.verify_provenance_record(reloaded, source_bytes=raw_bytes, now=NOW) == []


def test_source_evidence_of_extracts_from_record():
    evidence = _evidence()
    record = dbp.build_provenance_record(
        evidence, release_run_id="r1", adopted_at="t", adopted_by="a", source_path="p",
    )
    assert dbp.source_evidence_of(record)["testedGitSha"] == SHA
    assert dbp.source_evidence_of({"nothing": "here"}) is None
    assert dbp.source_evidence_of(None) is None
