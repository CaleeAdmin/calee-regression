"""Tests for the coverage manifest (Phase 12): schema/consistency validation,
the cross-check against the real suite registry, and the shipped manifest's
own validity. This is the CI gate that documentation/suite membership cannot
silently contradict the manifest.
"""

from __future__ import annotations

import copy

import pytest

from calee_regression import coverage_manifest as cov
from calee_regression.coverage_manifest import (
    CoverageManifestError,
    cross_check_against_suites,
    load_manifest,
    render_report,
    validate_manifest,
)

_MINIMAL = {
    "meta": {"physicalSession": False},
    "components": {
        "gating_ok": {
            "automated": "true", "offlineTested": True, "physicalVerified": False,
            "releaseGating": True, "scenarioSuite": "calendar", "owningRepo": "calee-regression",
        },
        "draft_ok": {
            "automated": "draft", "offlineTested": True, "physicalVerified": False,
            "releaseGating": False, "scenarioSuite": "calendar_event_mutation", "owningRepo": "calee-regression",
        },
    },
}


def test_shipped_manifest_loads_and_is_consistent():
    manifest = load_manifest()  # default path
    assert manifest.components
    problems = cross_check_against_suites(manifest)
    assert problems == [], problems


def test_shipped_manifest_has_no_physically_verified_components():
    # Honesty guard: this session had no device, so nothing may claim physical
    # verification.
    manifest = load_manifest()
    assert not manifest.physical_session
    assert [c.name for c in manifest.components if c.physical_verified] == []


def test_valid_minimal_manifest():
    manifest, errors = validate_manifest(_MINIMAL)
    assert errors == []
    assert cross_check_against_suites(manifest) == []


def test_draft_cannot_be_release_gating():
    data = copy.deepcopy(_MINIMAL)
    data["components"]["draft_ok"]["releaseGating"] = True
    _, errors = validate_manifest(data)
    assert any("draft component must not be releaseGating" in e for e in errors)


def test_unsupported_cannot_be_release_gating():
    data = copy.deepcopy(_MINIMAL)
    data["components"]["gating_ok"]["unsupported"] = True
    _, errors = validate_manifest(data)
    assert any("unsupported component must not be releaseGating" in e for e in errors)


def test_unsupported_cannot_be_automated_true():
    data = copy.deepcopy(_MINIMAL)
    data["components"]["x"] = {"automated": "true", "unsupported": True, "releaseGating": False}
    _, errors = validate_manifest(data)
    assert any("cannot be automated: true" in e for e in errors)


def test_physical_verified_requires_physical_session():
    data = copy.deepcopy(_MINIMAL)
    data["components"]["gating_ok"]["physicalVerified"] = True  # but meta.physicalSession is False
    _, errors = validate_manifest(data)
    assert any("physicalVerified is true but meta.physicalSession is false" in e for e in errors)


def test_invalid_automated_value_rejected():
    data = copy.deepcopy(_MINIMAL)
    data["components"]["gating_ok"]["automated"] = "sortof"
    _, errors = validate_manifest(data)
    assert any("automated must be one of" in e for e in errors)


def test_cross_check_flags_draft_in_release_suite():
    # A draft component whose suite IS in full-tester is a contradiction.
    data = copy.deepcopy(_MINIMAL)
    data["components"]["draft_ok"]["scenarioSuite"] = "calendar"  # calendar IS in full-tester
    manifest, errors = validate_manifest(data)
    assert errors == []  # schema-valid (draft + not gating)...
    problems = cross_check_against_suites(manifest)
    assert any("inside a release composite" in p for p in problems)


def test_cross_check_flags_gating_component_not_in_any_composite():
    data = copy.deepcopy(_MINIMAL)
    # tasks_mutation is a real suite but NOT in any composite; mark a gating
    # component pointing at it -> contradiction.
    data["components"]["gating_ok"]["scenarioSuite"] = "tasks_mutation"
    manifest, _ = validate_manifest(data)
    problems = cross_check_against_suites(manifest)
    assert any("not inside any release composite" in p for p in problems)


def test_cross_check_flags_unknown_suite():
    data = copy.deepcopy(_MINIMAL)
    data["components"]["gating_ok"]["scenarioSuite"] = "no_such_suite"
    manifest, _ = validate_manifest(data)
    problems = cross_check_against_suites(manifest)
    assert any("is not a known suite" in p for p in problems)


def test_render_report_mentions_pending_physical_verification():
    manifest = load_manifest()
    report = render_report(manifest)
    assert "physical verification pending" in report.lower() or "pending a MacBook" in report
    assert "Release-gating components" in report


def test_missing_manifest_raises():
    with pytest.raises(CoverageManifestError):
        load_manifest("/nonexistent/coverage.yaml")
