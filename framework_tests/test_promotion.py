"""Tests for the scenario-promotion state machine (Phase 6).

Proves the promotion files' own schema/state-machine rules AND their
consistency with the real scenario YAML + suites.py, so a scenario can never
be quietly slipped into a release suite while its promotion record still calls
it a draft (or vice versa).
"""

from __future__ import annotations

import copy

import pytest

from calee_regression import promotion
from calee_regression.promotion import (
    PromotionError,
    check_consistency,
    load_all,
    load_promotion,
    validate_raw,
)

FULL_SHA = "d5b99712158c27f435681946326f0c7b8df54a3e"

_DRAFT = {
    "scenario": "calendar_event_mutation",
    "scenarioFile": "scenarios/calendar_event_mutation.yaml",
    "sourceConfirmed": True,
    "sourceSha": FULL_SHA,
    "offlineTestsPassed": True,
    "physicalConfirmation": {
        "status": "pending",
        "requiredDevice": "physical_tablet",
        "evidenceRequired": ["runId", "tabletModel", "androidVersion", "caleeVersion",
                             "caleeGitSha", "screenshotPaths", "resultsJson"],
        "evidence": {},
    },
    "releaseSuiteEligible": False,
}


def _promoted_variant():
    data = copy.deepcopy(_DRAFT)
    data["releaseSuiteEligible"] = True
    data["physicalConfirmation"]["status"] = "passed"
    data["physicalConfirmation"]["evidence"] = {
        "runId": "release-20260720-1", "tabletModel": "Lenovo Tab", "androidVersion": "13",
        "caleeVersion": "founder-v0.3.25", "caleeGitSha": FULL_SHA,
        "screenshotPaths": ["a.png"], "resultsJson": "results.json",
    }
    return data


# ── schema / state-machine (independent of scenario YAML) ─────────────────


def test_valid_draft_record():
    assert validate_raw(_DRAFT) == []


def test_abbreviated_source_sha_rejected():
    data = copy.deepcopy(_DRAFT)
    data["sourceSha"] = "d5b9971"
    assert any("full 40-character Git SHA" in e for e in validate_raw(data))


def test_release_eligible_requires_passed_status():
    data = copy.deepcopy(_DRAFT)
    data["releaseSuiteEligible"] = True  # but status still pending, evidence empty
    errors = validate_raw(data)
    assert any("status is not 'passed'" in e for e in errors)
    assert any("evidence is missing" in e for e in errors)


def test_release_eligible_requires_full_evidence():
    data = _promoted_variant()
    data["physicalConfirmation"]["evidence"].pop("screenshotPaths")
    errors = validate_raw(data)
    assert any("evidence is missing" in e and "screenshotPaths" in e for e in errors)


def test_passed_status_requires_evidence_even_if_not_eligible():
    # A 'passed' physical status with an empty evidence block is a lie, even if
    # releaseSuiteEligible is still false.
    data = copy.deepcopy(_DRAFT)
    data["physicalConfirmation"]["status"] = "passed"
    errors = validate_raw(data)
    assert any("evidence is missing" in e for e in errors)


def test_fully_promoted_record_is_schema_valid():
    assert validate_raw(_promoted_variant()) == []


def test_invalid_status_rejected():
    data = copy.deepcopy(_DRAFT)
    data["physicalConfirmation"]["status"] = "maybe"
    assert any("status must be one of" in e for e in validate_raw(data))


# ── consistency against the real scenario YAML + suites.py ────────────────


def test_shipped_promotion_files_are_all_valid_and_consistent():
    records = load_all()
    assert records, "expected promotion files under scenarios/promotion/"
    for record in records:
        # Every shipped record is a DRAFT (nothing physically verified here).
        assert record.release_suite_eligible is False
        assert record.physical_status == "pending"
        problems = check_consistency(record)
        assert problems == [], f"{record.scenario}: {problems}"


def test_consistency_flags_eligible_but_still_draft(tmp_path):
    # Build a record that claims release-eligibility while pointing at a real
    # draft scenario -- the cross-check must catch the contradiction.
    import yaml

    data = _promoted_variant()  # scenario file is the real draft calendar_event_mutation
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data))
    record = load_promotion(path)
    problems = check_consistency(record)
    # The scenario is still tagged draft-unverified / mandatory:false / not in
    # full-tester, so an eligible record contradicts it on all three.
    assert any("draft-unverified" in p for p in problems)
    assert any("mandatory: false" in p for p in problems)
    assert any("not in any release composite" in p for p in problems)


def test_consistency_flags_source_sha_mismatch(tmp_path):
    import yaml

    data = copy.deepcopy(_DRAFT)
    data["sourceSha"] = "b" * 40  # valid format, wrong commit
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data))
    record = load_promotion(path)
    problems = check_consistency(record)
    assert any("does not match the scenario's" in p for p in problems)


def test_missing_promotion_file_raises(tmp_path):
    with pytest.raises(PromotionError):
        load_promotion(tmp_path / "nope.yaml")


def test_every_draft_suite_has_a_promotion_file():
    # Guard: each draft/mutation suite in suites.py must have a promotion file,
    # so a new draft can't be added without declaring its promotion state.
    from calee_regression import suites

    draft_suites = {
        "calendar_event_mutation", "tasks_mutation", "chores_mutation",
        "subscribed_calendar", "calendar_appearance",
    }
    have = {r.scenario for r in load_all()}
    missing = draft_suites - have
    assert not missing, f"missing promotion files for: {missing}"
