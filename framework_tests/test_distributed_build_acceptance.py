"""Distributed-build acceptance evidence (Priority 3).

When ``caleeMobile.distributedBuildAcceptanceRequired`` is true, a release
must carry explicit, externally-verifiable proof that a distributed/
TestFlight/store build's identity matches the release candidate. These tests
lock in that the verifier BLOCKS on a missing/unrecognised channel, on a
missing distributed build identifier, on a ``verifiedVia`` that is absent,
explicitly rejected (local checkout / unsigned build), or unrecognised, on a
mismatched tested SHA/version/release ID, and on a missing/invalid/future/
stale timestamp -- and that it never fabricates a PASS from absent evidence.
"""

from __future__ import annotations

import datetime

import pytest

from calee_regression import distributed_build_acceptance as dba

SHA_RELEASE = "a" * 40
SHA_OTHER = "b" * 40
VERSION_RELEASE = "0.0.23+23"
RELEASE_A = "2026.07.20-rc1"
RELEASE_B = "2026.07.20-rc2"

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
FRESH_TS = "2026-07-18T11:00:00Z"  # 1h before NOW


def _result(**overrides) -> dba.DistributedBuildAcceptanceResult:
    kwargs = dict(
        schema_version=1,
        component=dba.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
        channel="testflight",
        distributed_build_id="TF-4821",
        tested_git_sha=SHA_RELEASE,
        tested_version=VERSION_RELEASE,
        verified_via="testflight_api",
        release_id=RELEASE_A,
        timestamp=FRESH_TS,
    )
    kwargs.update(overrides)
    return dba.DistributedBuildAcceptanceResult(**kwargs)


def _verify(result, **kwargs):
    kwargs.setdefault("now", NOW)
    return dba.verify_distributed_build_acceptance_evidence(result, **kwargs)


# --- happy path --------------------------------------------------------------


def test_matching_evidence_is_accepted():
    verdict = _verify(_result(), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE)
    assert verdict.ok, verdict.problems


@pytest.mark.parametrize("channel", sorted(dba.VALID_CHANNELS))
def test_every_recognised_channel_is_accepted(channel):
    verdict = _verify(_result(channel=channel))
    assert verdict.ok, verdict.problems


@pytest.mark.parametrize("verified_via", sorted(dba.VALID_VERIFIED_VIA))
def test_every_recognised_verification_source_is_accepted(verified_via):
    verdict = _verify(_result(verified_via=verified_via))
    assert verdict.ok, verdict.problems


# --- anti-fabrication: verifiedVia --------------------------------------------


def test_missing_verified_via_is_rejected():
    verdict = _verify(_result(verified_via=None))
    assert not verdict.ok
    assert any("no verifiedVia recorded" in p for p in verdict.problems)


@pytest.mark.parametrize("rejected", sorted(dba.REJECTED_VERIFIED_VIA))
def test_local_or_unsigned_verified_via_is_explicitly_rejected(rejected):
    verdict = _verify(_result(verified_via=rejected))
    assert not verdict.ok
    assert any("explicitly rejected" in p and "never be fabricated" in p for p in verdict.problems)


def test_unrecognised_verified_via_is_rejected():
    verdict = _verify(_result(verified_via="my-custom-check"))
    assert not verdict.ok
    assert any("not a recognised" in p for p in verdict.problems)


# --- channel / build id --------------------------------------------------------


def test_missing_channel_is_rejected():
    verdict = _verify(_result(channel=None))
    assert not verdict.ok
    assert any("no distribution channel" in p for p in verdict.problems)


def test_unrecognised_channel_is_rejected():
    verdict = _verify(_result(channel="carrier_pigeon"))
    assert not verdict.ok
    assert any("not a recognised distribution channel" in p for p in verdict.problems)


def test_missing_distributed_build_id_is_rejected():
    verdict = _verify(_result(distributed_build_id=None))
    assert not verdict.ok
    assert any("no distributed build identifier" in p for p in verdict.problems)


# --- release-identity mismatch ------------------------------------------------


def test_mismatched_sha_is_rejected():
    verdict = _verify(_result(tested_git_sha=SHA_OTHER), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE)
    assert not verdict.ok
    assert any("!= expected release SHA" in p for p in verdict.problems)


def test_mismatched_version_is_rejected():
    verdict = _verify(_result(tested_version="0.0.99+99"), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE)
    assert not verdict.ok
    assert any("!= expected release version" in p for p in verdict.problems)


def test_abbreviated_sha_is_rejected():
    verdict = _verify(_result(tested_git_sha="abc1234"))
    assert not verdict.ok
    assert any("abbreviated/ambiguous" in p for p in verdict.problems)


def test_malformed_version_is_rejected():
    verdict = _verify(_result(tested_version="latest"))
    assert not verdict.ok
    assert any("not a well-formed version" in p for p in verdict.problems)


def test_missing_release_id_when_expected_is_rejected():
    verdict = _verify(_result(release_id=None), expected_release_id=RELEASE_A)
    assert not verdict.ok
    assert any("no releaseId recorded" in p for p in verdict.problems)


def test_wrong_release_id_is_rejected_even_with_matching_sha_version():
    verdict = _verify(
        _result(release_id=RELEASE_B), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE,
        expected_release_id=RELEASE_A,
    )
    assert not verdict.ok
    assert any(RELEASE_B in p and RELEASE_A in p for p in verdict.problems)


# --- schema / component --------------------------------------------------------


def test_missing_schema_version_is_rejected():
    verdict = _verify(_result(schema_version=None))
    assert not verdict.ok
    assert any("no schemaVersion" in p for p in verdict.problems)


def test_unsupported_schema_version_is_rejected_at_parse_time():
    with pytest.raises(dba.DistributedBuildAcceptanceError):
        dba.parse_distributed_build_acceptance_result({"schemaVersion": 99, "component": dba.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT})


def test_wrong_component_marker_is_rejected_at_parse_time():
    with pytest.raises(dba.DistributedBuildAcceptanceError):
        dba.parse_distributed_build_acceptance_result({"schemaVersion": 1, "component": "something-else"})


def test_non_object_payload_is_rejected():
    with pytest.raises(dba.DistributedBuildAcceptanceError):
        dba.parse_distributed_build_acceptance_result(["not", "an", "object"])


# --- timestamp -----------------------------------------------------------------


def test_missing_timestamp_is_rejected():
    verdict = _verify(_result(timestamp=None))
    assert not verdict.ok
    assert any("no timestamp" in p for p in verdict.problems)


def test_future_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="2026-07-19T00:00:00Z"))  # after NOW
    assert not verdict.ok
    assert any("in the future" in p for p in verdict.problems)


def test_stale_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="2026-01-01T00:00:00Z"), max_age=datetime.timedelta(days=30))
    assert not verdict.ok
    assert any("stale" in p for p in verdict.problems)


def test_naive_or_non_utc_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="2026-07-18T11:00:00"))  # no timezone
    assert not verdict.ok
    assert any("not a valid UTC ISO-8601" in p for p in verdict.problems)


# --- round-trip ------------------------------------------------------------


def test_to_dict_round_trips_through_parse():
    original = _result()
    parsed = dba.parse_distributed_build_acceptance_result(original.to_dict())
    verdict = _verify(parsed, expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE, expected_release_id=RELEASE_A)
    assert verdict.ok, verdict.problems
