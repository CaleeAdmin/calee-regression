"""CI drift test for the framework-completeness report.

Re-derives the expected per-dimension status / release-gating INDEPENDENTLY
from the same raw sources the report claims to read -- canonical suite
membership (suites.py), scenario tags + `mandatory` settings, the promotion
state machine, release feature/platform scope, the latest validated physical
reports, and the shipped documentation -- and fails on any disagreement. Also
proves the committed coverage/framework-completeness.{json,md} artifacts still
match a freshly-generated report, so a metadata change that isn't reflected in
the committed report is a red build. No device / network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from calee_regression import framework_completeness as fc
from calee_regression import promotion as promotion_mod
from calee_regression import release_platforms as rp
from calee_regression import suites as suites_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
MUTATION_SUITES = ("calendar_event_mutation", "tasks_mutation", "chores_mutation")


@pytest.fixture(scope="module")
def report():
    return fc.build_report()


# ── shape ──────────────────────────────────────────────────────────────────
def test_all_required_dimensions_present_exactly_once(report):
    required = {
        "frameworkArchitecture", "mobileApiCoverage", "mobileUiCoverage",
        "tabletReadCoverage", "tabletMutationCoverage", "crossDeviceSyncCoverage",
        "guidedHandoffCoverage", "androidPhysicalQualification", "iosPhysicalQualification",
        "tabletStandardQualification", "kioskAdminQualification", "fixtureExclusivity",
        "releaseEvidenceIntegrity",
    }
    keys = [d.key for d in report.dimensions]
    assert set(keys) == required
    assert len(keys) == len(set(keys)), "a dimension appears more than once"


def test_every_status_is_in_the_vocabulary(report):
    for d in report.dimensions:
        assert d.status in fc.VALID_STATUSES, f"{d.key} has invalid status {d.status!r}"


def test_every_dimension_records_a_next_action(report):
    for d in report.dimensions:
        assert d.next_action.strip(), f"{d.key} has no nextAction"


# ── agreement with release feature / platform scope (executable policy) ─────
def test_gating_agrees_with_release_feature_and_platform_scope(report):
    features = rp.load_release_features()
    platforms = rp.load_release_platforms()
    expected = {
        "crossDeviceSyncCoverage": features.synchronization,
        "guidedHandoffCoverage": features.onboarding or features.google_calendar,
        "kioskAdminQualification": features.kiosk_admin,
        "androidPhysicalQualification": platforms.mobile_android,
        "iosPhysicalQualification": platforms.mobile_ios,
        "tabletStandardQualification": platforms.tablet,
    }
    for key, want in expected.items():
        assert report.dimension(key).release_gating is bool(want), (
            f"{key}.releaseGating must equal the executable feature/platform scope ({want})"
        )


# ── agreement with canonical suite membership + scenario tags + mandatory ───
def _scenario_raw(path_rel: str) -> dict:
    return yaml.safe_load((REPO_ROOT / path_rel).read_text(encoding="utf-8"))


def test_tablet_mutation_gating_agrees_with_suite_membership_and_tags(report):
    dim = report.dimension("tabletMutationCoverage")

    # Independently: is any mutation suite inside a release composite?
    release_paths = set(str(p) for p in suites_mod.resolve_suite("full-tester")) | set(
        str(p) for p in suites_mod.resolve_suite("release-technical")
    )
    in_release = any(
        set(str(p) for p in suites_mod.resolve_suite(s)) & release_paths for s in MUTATION_SUITES
    )
    assert dim.release_gating is in_release
    # In the shipped repo the drafts are OUTSIDE every composite -> non-gating.
    assert in_release is False
    assert dim.release_gating is False

    # Scenario tags + mandatory must be the draft-gated shape, and because they
    # are, the dimension must read `blocked` (draft), never complete/gating.
    for suite in MUTATION_SUITES:
        scen_path = suites_mod.SCENARIO_GROUPS[suite][0]
        raw = _scenario_raw(scen_path)
        assert "draft-unverified" in (raw.get("tags") or []), f"{suite} lost its draft-unverified tag"
        assert raw.get("mandatory", True) is False, f"{suite} is no longer mandatory: false"
    assert dim.status == fc.STATUS_BLOCKED


def test_promotion_state_matches_mutation_dimension(report):
    # Every mutation promotion record is still pending -> dimension blocked+non-gating.
    records = {r.scenario: r for r in promotion_mod.load_all()}
    for suite in MUTATION_SUITES:
        assert records[suite].physical_status == "pending"
        assert records[suite].release_suite_eligible is False
    dim = report.dimension("tabletMutationCoverage")
    assert dim.status == fc.STATUS_BLOCKED and dim.release_gating is False


# ── agreement with the coverage manifest (automated-true coverage dims) ─────
def test_implemented_but_unqualified_coverage_reads_true(report):
    # mobile + tablet-read are automated:true, offline-tested, not physically
    # verified in an offline checkout -> implemented-unqualified.
    for key in ("mobileApiCoverage", "mobileUiCoverage", "tabletReadCoverage"):
        assert report.dimension(key).status == fc.STATUS_IMPLEMENTED_UNQUALIFIED


def test_internal_dimensions_are_complete(report):
    for key in ("frameworkArchitecture", "releaseEvidenceIntegrity"):
        assert report.dimension(key).status == fc.STATUS_COMPLETE


def test_partial_dimensions_read_partial(report):
    for key in ("crossDeviceSyncCoverage", "guidedHandoffCoverage"):
        assert report.dimension(key).status == fc.STATUS_PARTIAL


# ── agreement with latest validated physical reports ────────────────────────
def test_physical_qualification_blocked_without_validated_reports(report):
    # reports/ has no certification-eligible physical report in this checkout.
    assert not fc.scan_physical_evidence()
    for key in (
        "androidPhysicalQualification", "iosPhysicalQualification",
        "tabletStandardQualification", "kioskAdminQualification",
    ):
        dim = report.dimension(key)
        assert dim.status == fc.STATUS_BLOCKED
        assert dim.physical_evidence == []


def test_a_validated_physical_report_flips_the_gate_to_complete(tmp_path):
    # A certification-eligible, passing, device-bound report keyed to a
    # dimension flips exactly that physical gate to complete.
    runs = tmp_path / "runs" / "release-xyz"
    runs.mkdir(parents=True)
    (runs / "android.json").write_text(json.dumps({
        "completenessKey": "mobile-android",
        "certificationEligible": True,
        "status": "pass",
        "deviceId": "emulator-5554",
        "runId": "release-xyz",
    }), encoding="utf-8")

    found = fc.scan_physical_evidence(tmp_path)
    assert "mobile-android" in found and found["mobile-android"].digest

    report = fc.build_report(reports_root=tmp_path)
    assert report.dimension("androidPhysicalQualification").status == fc.STATUS_COMPLETE
    ev = report.dimension("androidPhysicalQualification").physical_evidence
    assert ev and ev[0]["deviceId"] == "emulator-5554"
    # A non-eligible report must NOT count.
    (runs / "ios.json").write_text(json.dumps({
        "completenessKey": "mobile-ios", "certificationEligible": False,
        "status": "pass", "deviceId": "iphone",
    }), encoding="utf-8")
    assert "mobile-ios" not in fc.scan_physical_evidence(tmp_path)


# ── fixture exclusivity: host-local implemented, distributed blocked ────────
def test_fixture_exclusivity_is_partial_and_names_the_distributed_gap(report):
    dim = report.dimension("fixtureExclusivity")
    assert dim.status == fc.STATUS_PARTIAL
    assert dim.release_gating is False
    assert any("distributed" in b.lower() for b in dim.blockers)


# ── weighted summary is transparent, never a substitute ─────────────────────
def test_weighted_summary_is_consistent_and_shows_its_working(report):
    summary = report.weighted_summary()
    total = sum(d.weight for d in report.dimensions)
    earned = sum(d.weight * fc.STATUS_SCORE[d.status] for d in report.dimensions)
    assert summary["totalWeight"] == pytest.approx(round(total, 3))
    assert summary["earnedWeight"] == pytest.approx(round(earned, 3))
    assert summary["weightedCompletionPercent"] == pytest.approx(round(100.0 * earned / total, 1))
    # The scoring table and the "not a substitute" caveat must both be present.
    assert set(summary["statusScoring"]) == set(fc.VALID_STATUSES)
    assert "substitute" in summary["note"].lower()


# ── documentation claims agree with the report ──────────────────────────────
def test_suite_reference_sync_gating_agrees_with_report(report):
    """The SUITE_REFERENCE.md sync-smoke row must not contradict the executable
    gating the report derives: sync is release-gating, so the doc row may not
    still say it is 'Not yet' release-gating."""
    sync_gating = report.dimension("crossDeviceSyncCoverage").release_gating
    doc = (REPO_ROOT / "docs" / "SUITE_REFERENCE.md").read_text(encoding="utf-8")
    sync_rows = [ln for ln in doc.splitlines() if ln.strip().startswith("| `sync-smoke`")]
    assert len(sync_rows) == 1, "expected exactly one sync-smoke suite-table row"
    last_cell = sync_rows[0].rstrip().rstrip("|").rsplit("|", 1)[-1].strip().lower()
    if sync_gating:
        assert "not yet" not in last_cell, (
            "sync is release-gating per release_features.synchronization, but the SUITE_REFERENCE.md "
            "table still says its release-gating status is 'Not yet' -- documentation drift."
        )


# ── the committed golden artifacts must be in sync ──────────────────────────
def test_committed_canonical_artifacts_are_up_to_date(report):
    problems = fc.canonical_drift(report)
    assert problems == [], (
        "coverage/framework-completeness.{json,md} are stale. Regenerate with "
        "`python -m calee_regression framework-completeness --write`:\n" + "\n".join(problems)
    )


def test_canonical_json_is_valid_and_derives_from_metadata(report):
    data = json.loads(fc.CANONICAL_JSON_PATH.read_text(encoding="utf-8"))
    assert data["report"] == "framework-completeness"
    assert data["schemaVersion"] == 1
    # It advertises the sources it derives from -- not a hand-kept number.
    assert "coverageManifest" in data["derivedFrom"]
    assert data["summary"]["weightedCompletionPercent"] == report.weighted_summary()["weightedCompletionPercent"]
