"""Reject CaleeMobile selector-contract evidence for the wrong build (Workstream 1).

A release that ships CaleeMobile commit Y while its selector-contract proof was
gathered against commit X has no proof at all. These tests lock in that the
verifier BLOCKS on a contract that didn't pass, on a missing/malformed tested
identity, and -- most importantly -- on a tested SHA or version that differs
from the expected release identity.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import selector_evidence as se
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

SHA_RELEASE = "a" * 40
SHA_OTHER = "b" * 40
VERSION_RELEASE = "0.0.23+23"


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
        timestamp="2026-07-18T00:00:00Z",
        schema_version=1,
    )
    kwargs.update(overrides)
    return se.SelectorContractResult(**kwargs)


# --- verify_selector_contract_evidence ---------------------------------------


def test_matching_evidence_is_accepted():
    verdict = se.verify_selector_contract_evidence(
        _result(), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE
    )
    assert verdict.ok, verdict.problems


def test_mismatched_sha_is_rejected():
    verdict = se.verify_selector_contract_evidence(
        _result(tested_sha=SHA_OTHER), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE
    )
    assert not verdict.ok
    assert any("different CaleeMobile commit" in p for p in verdict.problems)


def test_mismatched_version_is_rejected():
    verdict = se.verify_selector_contract_evidence(
        _result(pubspec_version="0.0.22+22"), expected_git_sha=SHA_RELEASE, expected_version=VERSION_RELEASE
    )
    assert not verdict.ok
    assert any("different CaleeMobile version" in p for p in verdict.problems)


def test_failed_contract_is_rejected():
    verdict = se.verify_selector_contract_evidence(
        _result(contract="FAIL", selectors_present=60, missing=["calendar_add_event_button", "meal_save_button"]),
        expected_git_sha=SHA_RELEASE,
        expected_version=VERSION_RELEASE,
    )
    assert not verdict.ok
    assert any("did not PASS" in p for p in verdict.problems)


def test_abbreviated_tested_sha_is_rejected():
    verdict = se.verify_selector_contract_evidence(_result(tested_sha="abc1234"))
    assert not verdict.ok
    assert any("abbreviated" in p for p in verdict.problems)


def test_malformed_tested_version_is_rejected():
    verdict = se.verify_selector_contract_evidence(_result(pubspec_version="latest"))
    assert not verdict.ok
    assert any("well-formed" in p for p in verdict.problems)


def test_abbreviated_expected_sha_is_rejected():
    # A misconfigured expectation is itself a block -- you cannot match against
    # an ambiguous SHA.
    verdict = se.verify_selector_contract_evidence(_result(), expected_git_sha="abc1234")
    assert not verdict.ok
    assert any("expected CaleeMobile SHA" in p for p in verdict.problems)


def test_ref_mismatch_is_a_nonblocking_note():
    verdict = se.verify_selector_contract_evidence(
        _result(caleemobile_ref="main"),
        expected_git_sha=SHA_RELEASE,
        expected_version=VERSION_RELEASE,
        expected_ref="dev",
    )
    assert verdict.ok  # SHA/version match -> still accepted
    assert any(p.startswith("NOTE:") for p in verdict.problems)


# --- parsing -----------------------------------------------------------------


def test_parse_roundtrips_through_to_dict():
    original = _result()
    parsed = se.parse_selector_contract_result(original.to_dict())
    assert parsed.tested_sha == original.tested_sha
    assert parsed.pubspec_version == original.pubspec_version
    assert parsed.passed


def test_parse_rejects_non_object():
    with pytest.raises(se.SelectorEvidenceError):
        se.parse_selector_contract_result(["not", "an", "object"])


def test_parse_rejects_wrong_component():
    with pytest.raises(se.SelectorEvidenceError):
        se.parse_selector_contract_result({"component": "something-else", "testedSha": SHA_RELEASE})


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(se.SelectorEvidenceError):
        se.load_selector_contract_result(tmp_path / "nope.json")


# --- CLI end to end ----------------------------------------------------------


def _write_evidence(tmp_path, **overrides):
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


def test_cli_blocks_on_unreadable_evidence(tmp_path):
    result = CliRunner().invoke(
        main,
        ["verify-selector-evidence", "--evidence", str(tmp_path / "missing.json"),
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert result.exit_code == EXIT_BLOCKED
