"""Reject CaleeMobile selector-contract evidence for the wrong build (Workstream 1).

A release that ships CaleeMobile commit Y while its selector-contract proof was
gathered against commit X has no proof at all. These tests lock in that the
verifier BLOCKS on a contract that didn't pass, on a missing/malformed tested
identity, on the wrong Flutter toolchain, on absent/inconsistent selector
counts, on a missing/invalid/future/stale timestamp, on an unknown schema
version, and -- most importantly -- on a tested SHA or version that differs
from the expected release identity (Priority 3 hardens each of these).
"""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner

from calee_regression import selector_evidence as se
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS

SHA_RELEASE = "a" * 40
SHA_OTHER = "b" * 40
VERSION_RELEASE = "0.0.23+23"

UTC = datetime.timezone.utc
# A fixed reference instant so freshness/future checks are deterministic
# regardless of the wall clock the tests run on.
NOW = datetime.datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
FRESH_TS = "2026-07-18T11:00:00Z"  # 1h before NOW -- fresh, not future


def _result(**overrides) -> se.SelectorContractResult:
    kwargs = dict(
        caleemobile_ref="dev",
        tested_sha=SHA_RELEASE,
        pubspec_version=VERSION_RELEASE,
        flutter_version="3.44.1",
        contract="PASS",
        selectors_checked=62,
        selectors_present=62,
        missing=[],
        timestamp=FRESH_TS,
        schema_version=1,
    )
    kwargs.update(overrides)
    return se.SelectorContractResult(**kwargs)


def _verify(result, **kwargs):
    """verify with the deterministic clock unless the test overrides it."""
    kwargs.setdefault("now", NOW)
    return se.verify_selector_contract_evidence(result, **kwargs)


# --- happy path --------------------------------------------------------------


def test_matching_evidence_is_accepted():
    verdict = _verify(_result(), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE)
    assert verdict.ok, verdict.problems


# --- release-identity mismatch ----------------------------------------------


def test_mismatched_sha_is_rejected():
    verdict = _verify(_result(tested_sha=SHA_OTHER), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE)
    assert not verdict.ok
    assert any("different CaleeMobile commit" in p for p in verdict.problems)


def test_mismatched_version_is_rejected():
    verdict = _verify(
        _result(pubspec_version="0.0.22+22"), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE
    )
    assert not verdict.ok
    assert any("different CaleeMobile version" in p for p in verdict.problems)


def test_abbreviated_expected_sha_is_rejected():
    # A misconfigured expectation is itself a block -- you cannot match against
    # an ambiguous SHA.
    verdict = _verify(_result(), expected_git_sha="abc1234")
    assert not verdict.ok
    assert any("expected CaleeMobile SHA" in p for p in verdict.problems)


# --- Priority 8: release-ID binding (release certification only) -----------


RELEASE_A = "2026.07.20-rc3"
RELEASE_B = "2026.07.21-rc1"


def test_no_expected_release_id_is_ordinary_pr_checking_unaffected():
    # expected_release_id omitted entirely (the default): this is ordinary PR
    # selector checking, never touched by release-ID binding, even when the
    # evidence itself carries no releaseId at all.
    verdict = _verify(_result(), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE)
    assert verdict.ok, verdict.problems


def test_matching_release_id_is_accepted():
    verdict = _verify(
        _result(release_id=RELEASE_A), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE,
        expected_release_id=RELEASE_A,
    )
    assert verdict.ok, verdict.problems


def test_missing_release_identity_in_evidence_fails_certification():
    # requirement 2: a release-certification request (expected_release_id set)
    # fails when the evidence carries no releaseId at all.
    verdict = _verify(
        _result(release_id=None), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE,
        expected_release_id=RELEASE_A,
    )
    assert not verdict.ok
    assert any("no releaseId recorded" in p for p in verdict.problems)


def test_empty_expected_release_id_fails_certification():
    verdict = _verify(_result(release_id=RELEASE_A), expected_release_id="   ")
    assert not verdict.ok
    assert any("expected release ID is empty" in p for p in verdict.problems)


def test_release_id_mismatch_fails_even_with_matching_sha_and_version():
    # requirement 6 + 8: calee-regression must reject selector evidence for
    # another release ID even if SHA and version match.
    verdict = _verify(
        _result(release_id=RELEASE_B), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE,
        expected_release_id=RELEASE_A,
    )
    assert not verdict.ok
    assert any(RELEASE_B in p and RELEASE_A in p for p in verdict.problems)


def test_release_id_round_trips_through_to_dict_and_parse():
    result = _result(release_id=RELEASE_A, correlation_id="corr-123", expected_sha=SHA_RELEASE, expected_version=VERSION_RELEASE)
    data = result.to_dict()
    assert data["releaseId"] == RELEASE_A
    assert data["correlationId"] == "corr-123"
    assert data["expectedSha"] == SHA_RELEASE
    assert data["expectedVersion"] == VERSION_RELEASE
    reparsed = se.parse_selector_contract_result(data)
    assert reparsed.release_id == RELEASE_A
    assert reparsed.correlation_id == "corr-123"


def test_release_id_absent_from_to_dict_when_unset():
    data = _result().to_dict()
    assert "releaseId" not in data and "correlationId" not in data


def test_ref_mismatch_is_a_nonblocking_note():
    verdict = _verify(
        _result(caleemobile_ref="main"),
        expected_git_sha=SHA_RELEASE,
        expected_version=VERSION_RELEASE,
        expected_ref="dev",
    )
    assert verdict.ok  # SHA/version match -> still accepted
    assert any(p.startswith("NOTE:") for p in verdict.problems)


# --- contract / missing selectors -------------------------------------------


def test_failed_contract_is_rejected():
    verdict = _verify(
        _result(contract="FAIL", selectors_present=60, missing=["calendar_add_event_button", "meal_save_button"]),
        expected_git_sha=SHA_RELEASE,
        expected_version=VERSION_RELEASE,
    )
    assert not verdict.ok
    assert any("did not PASS" in p for p in verdict.problems)


def test_missing_selector_is_rejected():
    # Even a PASS-labelled contract that lists a missing selector is refused.
    verdict = _verify(_result(selectors_present=61, missing=["meal_save_button"]))
    assert not verdict.ok
    assert any("missing selector" in p for p in verdict.problems)


# --- tested identity fields --------------------------------------------------


def test_abbreviated_tested_sha_is_rejected():
    verdict = _verify(_result(tested_sha="abc1234"))
    assert not verdict.ok
    assert any("abbreviated" in p for p in verdict.problems)


def test_missing_tested_sha_is_rejected():
    verdict = _verify(_result(tested_sha=None))
    assert not verdict.ok
    assert any("no tested CaleeMobile SHA" in p for p in verdict.problems)


def test_malformed_tested_version_is_rejected():
    verdict = _verify(_result(pubspec_version="latest"))
    assert not verdict.ok
    assert any("well-formed" in p for p in verdict.problems)


def test_missing_pubspec_version_is_rejected():
    verdict = _verify(_result(pubspec_version=None))
    assert not verdict.ok
    assert any("no CaleeMobile pubspec version" in p for p in verdict.problems)


# --- schema version / component ---------------------------------------------


def test_missing_schema_version_is_rejected():
    verdict = _verify(_result(schema_version=None))
    assert not verdict.ok
    assert any("no schemaVersion" in p for p in verdict.problems)


def test_missing_component_is_rejected():
    verdict = _verify(_result(component=None))
    assert not verdict.ok
    assert any("no component marker" in p for p in verdict.problems)


# --- Flutter toolchain -------------------------------------------------------


def test_wrong_flutter_version_is_rejected():
    verdict = _verify(_result(flutter_version="3.43.0"))
    assert not verdict.ok
    assert any("different toolchain" in p for p in verdict.problems)


def test_missing_flutter_version_is_rejected():
    verdict = _verify(_result(flutter_version=None))
    assert not verdict.ok
    assert any("no Flutter version" in p for p in verdict.problems)


# --- selector counts ---------------------------------------------------------


def test_missing_selectors_checked_is_rejected():
    verdict = _verify(_result(selectors_checked=None))
    assert not verdict.ok
    assert any("no selectorsChecked" in p for p in verdict.problems)


def test_zero_selectors_checked_is_rejected():
    verdict = _verify(_result(selectors_checked=0, selectors_present=0))
    assert not verdict.ok
    assert any("positive integer" in p for p in verdict.problems)


def test_missing_selectors_present_is_rejected():
    verdict = _verify(_result(selectors_present=None))
    assert not verdict.ok
    assert any("no selectorsPresent" in p for p in verdict.problems)


def test_present_not_equal_checked_is_rejected():
    # present < checked but missing empty -> both "not every selector present"
    # and "internally inconsistent" fire.
    verdict = _verify(_result(selectors_present=60, missing=[]))
    assert not verdict.ok
    assert any("not every required selector" in p for p in verdict.problems)
    assert any("internally inconsistent" in p for p in verdict.problems)


def test_missing_length_inconsistent_with_counts_is_rejected():
    # counts say 2 missing, but the list names only 1 -> inconsistent evidence.
    verdict = _verify(_result(contract="FAIL", selectors_present=60, missing=["only_one"]))
    assert not verdict.ok
    assert any("internally inconsistent" in p for p in verdict.problems)


# --- timestamp ---------------------------------------------------------------


def test_missing_timestamp_is_rejected():
    verdict = _verify(_result(timestamp=None))
    assert not verdict.ok
    assert any("no timestamp" in p for p in verdict.problems)


def test_invalid_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="not-a-date"))
    assert not verdict.ok
    assert any("not a valid UTC" in p for p in verdict.problems)


def test_naive_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="2026-07-18T11:00:00"))  # no timezone
    assert not verdict.ok
    assert any("not a valid UTC" in p for p in verdict.problems)


def test_non_utc_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="2026-07-18T11:00:00+08:00"))
    assert not verdict.ok
    assert any("not a valid UTC" in p for p in verdict.problems)


def test_future_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="2026-07-19T00:00:00Z"))  # after NOW
    assert not verdict.ok
    assert any("in the future" in p for p in verdict.problems)


def test_stale_timestamp_is_rejected():
    verdict = _verify(_result(timestamp="2026-06-01T00:00:00Z"))  # >14d before NOW
    assert not verdict.ok
    assert any("stale" in p for p in verdict.problems)


# --- release-run provenance --------------------------------------------------


def test_provenance_required_but_absent_is_rejected():
    verdict = _verify(_result(), require_release_provenance=True, expected_release_run_id="release-1")
    assert not verdict.ok
    assert any("no releaseRunId" in p for p in verdict.problems)


def test_provenance_run_id_mismatch_is_rejected():
    verdict = _verify(
        _result(release_run_id="release-OTHER", workflow_run_id="123"),
        require_release_provenance=True,
        expected_release_run_id="release-1",
    )
    assert not verdict.ok
    assert any("different release run" in p for p in verdict.problems)


def test_provenance_without_source_is_rejected():
    verdict = _verify(
        _result(release_run_id="release-1"),  # no workflowRunId and no generatedBy
        require_release_provenance=True,
        expected_release_run_id="release-1",
    )
    assert not verdict.ok
    assert any("no provenance recorded" in p for p in verdict.problems)


def test_valid_release_provenance_is_accepted():
    verdict = _verify(
        _result(release_run_id="release-1", generated_by="local", regression_sha="c" * 40),
        expected_git_sha=SHA_RELEASE,
        expected_version=VERSION_RELEASE,
        require_release_provenance=True,
        expected_release_run_id="release-1",
    )
    assert verdict.ok, verdict.problems


# --- timestamp helper --------------------------------------------------------


def test_parse_utc_iso8601_variants():
    assert se.parse_utc_iso8601("2026-07-18T11:00:00Z") == datetime.datetime(2026, 7, 18, 11, 0, 0, tzinfo=UTC)
    assert se.parse_utc_iso8601("2026-07-18T11:00:00+00:00") == datetime.datetime(2026, 7, 18, 11, 0, 0, tzinfo=UTC)
    assert se.parse_utc_iso8601("2026-07-18T11:00:00") is None  # naive
    assert se.parse_utc_iso8601("2026-07-18T11:00:00+08:00") is None  # non-UTC
    assert se.parse_utc_iso8601("garbage") is None
    assert se.parse_utc_iso8601(None) is None


# --- parsing -----------------------------------------------------------------


def test_parse_roundtrips_through_to_dict():
    original = _result(
        release_run_id="release-1", regression_sha="c" * 40, workflow_run_id="99", generated_by="ci"
    )
    parsed = se.parse_selector_contract_result(original.to_dict())
    assert parsed.tested_sha == original.tested_sha
    assert parsed.pubspec_version == original.pubspec_version
    assert parsed.release_run_id == "release-1"
    assert parsed.regression_sha == "c" * 40
    assert parsed.workflow_run_id == "99"
    assert parsed.generated_by == "ci"
    assert parsed.passed


def test_parse_rejects_non_object():
    with pytest.raises(se.SelectorEvidenceError):
        se.parse_selector_contract_result(["not", "an", "object"])


def test_parse_rejects_wrong_component():
    with pytest.raises(se.SelectorEvidenceError):
        se.parse_selector_contract_result({"component": "something-else", "testedSha": SHA_RELEASE})


def test_parse_rejects_unsupported_schema_version():
    with pytest.raises(se.SelectorEvidenceError):
        se.parse_selector_contract_result(_result(schema_version=2).to_dict())


def test_parse_rejects_non_integer_count():
    data = _result().to_dict()
    data["selectorsChecked"] = "sixty-two"
    with pytest.raises(se.SelectorEvidenceError):
        se.parse_selector_contract_result(data)


def test_parse_rejects_boolean_count():
    data = _result().to_dict()
    data["selectorsPresent"] = True
    with pytest.raises(se.SelectorEvidenceError):
        se.parse_selector_contract_result(data)


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(se.SelectorEvidenceError):
        se.load_selector_contract_result(tmp_path / "nope.json")


# --- CLI end to end ----------------------------------------------------------
# The CLI uses the real clock, so build a timestamp relative to "now" to stay
# fresh whatever the wall clock is when the suite runs.


def _fresh_timestamp() -> str:
    return (datetime.datetime.now(UTC) - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_evidence(tmp_path, **overrides):
    overrides.setdefault("timestamp", _fresh_timestamp())
    path = tmp_path / "selector-contract-result.json"
    path.write_text(json.dumps(_result(**overrides).to_dict()))
    return path


def test_cli_accepts_matching_evidence(tmp_path):
    path = _write_evidence(tmp_path)
    result = CliRunner().invoke(
        main,
        ["verify-selector-evidence", "--evidence", str(path),
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "accepted" in result.output


def test_cli_blocks_on_mismatched_sha(tmp_path):
    path = _write_evidence(tmp_path, tested_sha=SHA_OTHER)
    result = CliRunner().invoke(
        main,
        ["verify-selector-evidence", "--evidence", str(path),
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "REJECTED" in result.output


def test_cli_blocks_on_wrong_flutter(tmp_path):
    path = _write_evidence(tmp_path, flutter_version="3.43.0")
    result = CliRunner().invoke(
        main,
        ["verify-selector-evidence", "--evidence", str(path),
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "REJECTED" in result.output


def test_cli_blocks_on_unreadable_evidence(tmp_path):
    result = CliRunner().invoke(
        main,
        ["verify-selector-evidence", "--evidence", str(tmp_path / "missing.json"),
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert result.exit_code == EXIT_BLOCKED
