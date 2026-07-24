"""Three-measure completeness model (Workstream 2).

Independently proves the report separates *implementation* (built + offline-
tested + wired) from *qualification* (validated, current physical/backend
evidence) from *release readiness* (a pass/fail/blocked verdict, never a
percentage) -- and that the legacy conflated measure is preserved verbatim.
"""

from __future__ import annotations

import json

from calee_regression import framework_completeness as fc


def _report():
    return fc.build_report()


# ── axis vocabularies + presence ────────────────────────────────────────────
def test_every_dimension_has_valid_axis_statuses():
    for d in _report().dimensions:
        assert d.implementation_status in fc.IMPL_STATUSES
        assert d.qualification_status in fc.QUAL_STATUSES


def test_legacy_measure_is_preserved_unchanged():
    d = _report().to_dict()
    # The legacy conflated status + weighted percentage must NOT change.
    assert d["summary"]["weightedCompletionPercent"] == 52.7
    assert d["statusVocabulary"] == list(fc.VALID_STATUSES)
    # And the three new measures are present alongside it.
    assert "implementationCompleteness" in d
    assert "qualificationCompleteness" in d
    assert "releaseReadiness" in d


# ── implementation NEVER counts a physical blocker as missing code ──────────
def test_physical_only_gates_are_implementation_complete_but_qualification_blocked():
    r = _report()
    for key in ("androidPhysicalQualification", "iosPhysicalQualification", "tabletStandardQualification"):
        dim = r.dimension(key)
        assert dim.implementation_status == fc.IMPL_COMPLETE, key   # harness is built
        assert dim.qualification_status == fc.QUAL_BLOCKED, key       # no device evidence


def test_draft_mutation_is_implemented_but_unqualified_not_missing():
    dim = _report().dimension("tabletMutationCoverage")
    # Legacy status conflates this to 'blocked'...
    assert dim.status == fc.STATUS_BLOCKED
    # ...but the automation IS built + offline-tested (draft = pending physical
    # promotion), so implementation is complete, not missing.
    assert dim.implementation_status == fc.IMPL_COMPLETE
    assert dim.qualification_status == fc.QUAL_IMPLEMENTED_UNQUALIFIED


def test_internal_and_fixture_dimensions_need_no_qualification():
    r = _report()
    for key in ("frameworkArchitecture", "releaseEvidenceIntegrity", "fixtureExclusivity"):
        assert r.dimension(key).qualification_status == fc.QUAL_NOT_APPLICABLE, key


# ── offline currency of the whole report ────────────────────────────────────
def test_offline_report_has_no_qualified_dimension_and_is_release_blocked():
    r = _report()
    assert not fc.scan_physical_evidence()  # empty offline checkout
    assert all(d.qualification_status != fc.QUAL_QUALIFIED for d in r.dimensions)
    assert r.release_readiness()["status"] == fc.READINESS_BLOCKED


def test_implementation_exceeds_qualification_offline():
    d = _report().to_dict()
    impl = d["implementationCompleteness"]["implementationCompletionPercent"]
    qual = d["qualificationCompleteness"]["qualificationCompletionPercent"]
    # The framework is substantially BUILT even though it is not yet qualified.
    assert impl > qual
    assert impl >= 80.0
    # not-applicable dimensions are excluded from the qualification denominator.
    assert d["qualificationCompleteness"]["statusCounts"]["not-applicable"] == 3


def test_percentages_recompute_from_axis_scores():
    r = _report()
    total = sum(dim.weight for dim in r.dimensions)
    impl_earned = sum(dim.weight * fc.IMPL_SCORE[dim.implementation_status] for dim in r.dimensions)
    assert r.implementation_summary()["implementationCompletionPercent"] == round(100.0 * impl_earned / total, 1)
    scored = [dim for dim in r.dimensions if dim.qualification_score is not None]
    qtot = sum(dim.weight for dim in scored)
    qearned = sum(dim.weight * dim.qualification_score for dim in scored)
    assert r.qualification_summary()["qualificationCompletionPercent"] == round(100.0 * qearned / qtot, 1)


# ── stale evidence is DISTINCT from missing evidence ────────────────────────
def _write_evidence(tmp_path, key, *, build="BUILD-1"):
    run = tmp_path / "runs" / "r1" / "focused-verify"
    run.mkdir(parents=True)
    (run / "results.json").write_text(json.dumps({
        "completenessKey": key, "certificationEligible": True, "status": "pass",
        "deviceId": "emulator-5554", "runId": "r1", "qualificationBuild": build,
    }))
    return tmp_path


def test_fresh_evidence_qualifies_stale_evidence_does_not(tmp_path):
    _write_evidence(tmp_path, "mobile-android", build="BUILD-1")
    # Same build -> current -> qualifies.
    fresh = fc.scan_physical_evidence(tmp_path, current_build="BUILD-1")["mobile-android"]
    assert fresh.stale is False
    from calee_regression.framework_completeness import DIMENSION_SPECS
    spec = next(s for s in DIMENSION_SPECS if s.key == "androidPhysicalQualification")
    assert fc._qualification_status(spec, fc.IMPL_COMPLETE, fresh) == fc.QUAL_QUALIFIED
    # Newer build under test -> the evidence is STALE, not missing.
    stale = fc.scan_physical_evidence(tmp_path, current_build="BUILD-2")["mobile-android"]
    assert stale.stale is True
    assert fc._qualification_status(spec, fc.IMPL_COMPLETE, stale) == fc.QUAL_IMPLEMENTED_UNQUALIFIED


# ── release readiness verdict (unit) ────────────────────────────────────────
def _dim(key, *, gating, qual, impl=fc.IMPL_COMPLETE):
    return fc.Dimension(key=key, title=key, status=fc.STATUS_COMPLETE, release_gating=gating,
                        implementation_status=impl, qualification_status=qual, weight=1.0)


def test_release_readiness_pass_blocked_fail_and_na():
    # All gating dims qualified (or built internal) -> pass.
    passing = fc.CompletenessReport(
        dimensions=[_dim("a", gating=True, qual=fc.QUAL_QUALIFIED),
                    _dim("internal", gating=True, qual=fc.QUAL_NOT_APPLICABLE)],
        physical_session=True, feature_scope_source="x", platform_scope_source="y")
    assert passing.release_readiness()["status"] == fc.READINESS_PASS

    # One gating dim unqualified -> blocked.
    blocked = fc.CompletenessReport(
        dimensions=[_dim("a", gating=True, qual=fc.QUAL_QUALIFIED),
                    _dim("b", gating=True, qual=fc.QUAL_BLOCKED)],
        physical_session=True, feature_scope_source="x", platform_scope_source="y")
    assert blocked.release_readiness()["status"] == fc.READINESS_BLOCKED
    assert "b" in blocked.release_readiness()["blocking"]

    # A gating dim with FAILED evidence -> fail (a product regression, not just missing).
    failed = fc.CompletenessReport(
        dimensions=[_dim("a", gating=True, qual=fc.QUAL_QUALIFIED)],
        physical_session=True, feature_scope_source="x", platform_scope_source="y",
        failed_evidence_keys={"a"})
    assert failed.release_readiness()["status"] == fc.READINESS_FAIL

    # No gating dims -> not-applicable.
    na = fc.CompletenessReport(
        dimensions=[_dim("a", gating=False, qual=fc.QUAL_QUALIFIED)],
        physical_session=True, feature_scope_source="x", platform_scope_source="y")
    assert na.release_readiness()["status"] == fc.READINESS_NOT_APPLICABLE


def test_readiness_is_never_a_percentage():
    verdict = _report().release_readiness()
    assert verdict["status"] in (fc.READINESS_PASS, fc.READINESS_FAIL, fc.READINESS_BLOCKED, fc.READINESS_NOT_APPLICABLE)
    # The verdict is a categorical status, never a numeric percentage field.
    assert not any("percent" in k.lower() for k in verdict)
    assert not isinstance(verdict["status"], (int, float))
