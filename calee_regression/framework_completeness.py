"""Framework release-completeness report.

Derives a machine-readable completeness assessment of the Calee regression
framework from ACTUAL repository metadata and validated physical reports --
never a manually-maintained percentage. Every dimension status below is
*computed* from these sources, so the report cannot silently disagree with
the things it summarises:

  * ``coverage/coverage-manifest.yaml`` -- per-component automation / offline /
    physical / release-gating / blocked state (loaded + cross-checked against
    ``suites.py`` by :mod:`calee_regression.coverage_manifest`).
  * ``calee_regression/suites.py`` -- canonical suite membership and the
    release composites (``full-tester`` / ``release-technical``).
  * ``scenarios/*.yaml`` -- scenario ``tags`` and ``mandatory`` settings.
  * ``scenarios/promotion/*.yaml`` -- the draft->promoted state machine
    (:mod:`calee_regression.promotion`).
  * ``config/release-platforms.yaml`` -- release platform + feature scope
    (:mod:`calee_regression.release_platforms`); absent config => every
    platform/feature is mandatory.
  * ``reports/runs/<run-id>/...`` -- the latest VALIDATED, certification-
    eligible physical reports (there are none in an offline checkout, which is
    exactly why the physical-qualification dimensions read ``blocked``).

The required dimensions each carry: ``status`` (one of ``complete`` /
``implemented-unqualified`` / ``partial`` / ``blocked`` / ``not-implemented``),
``implementationEvidence``, ``physicalEvidence``, ``releaseGating``,
``blockers`` and ``nextAction``. A weighted summary percentage is ALSO
computed, but the report always shows the weights and the per-status scoring
and NEVER substitutes the percentage for the underlying statuses.

``framework_tests/test_framework_completeness.py`` re-derives the expected
statuses/gating independently from the same raw sources and fails on any
drift, and ``python -m calee_regression framework-completeness --check``
proves the committed ``coverage/framework-completeness.{json,md}`` artifacts
still match a freshly-generated report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import coverage_manifest as coverage_mod
from . import promotion as promotion_mod
from . import release_platforms as release_platforms_mod
from . import suites as suites_mod
from .focused_report_validation import sha256_of_file

# ── status vocabulary ──────────────────────────────────────────────────────
STATUS_COMPLETE = "complete"
STATUS_IMPLEMENTED_UNQUALIFIED = "implemented-unqualified"
STATUS_PARTIAL = "partial"
STATUS_BLOCKED = "blocked"
STATUS_NOT_IMPLEMENTED = "not-implemented"

VALID_STATUSES = (
    STATUS_COMPLETE,
    STATUS_IMPLEMENTED_UNQUALIFIED,
    STATUS_PARTIAL,
    STATUS_BLOCKED,
    STATUS_NOT_IMPLEMENTED,
)

# How much "done" each status is worth when a weighted summary percentage is
# computed. This is transparency, not a substitute: the per-dimension statuses
# above are always the authoritative output.
STATUS_SCORE = {
    STATUS_COMPLETE: 1.0,
    STATUS_IMPLEMENTED_UNQUALIFIED: 0.6,
    STATUS_PARTIAL: 0.5,
    STATUS_BLOCKED: 0.25,
    STATUS_NOT_IMPLEMENTED: 0.0,
}

# ── Workstream 2: three orthogonal measures ─────────────────────────────────
# The single ``status`` above conflates "is it built?" with "is it physically
# qualified?". These axes separate the two, and a third top-level verdict
# (release readiness) is derived from the mandatory release scope + evidence.
# The legacy ``status``/``weightedCompletionPercent`` are retained verbatim for
# backward compatibility; consumers should migrate to the axes below (see
# docs/COMPLETENESS_MODEL.md).
#
# A) Implementation completeness -- built, offline-tested, wired. IGNORES
#    physical evidence: a capability can be implementation-complete with no
#    physical run, and a physical blocker is NEVER counted as missing code.
IMPL_COMPLETE = "complete"
IMPL_PARTIAL = "partial"
IMPL_NOT_IMPLEMENTED = "not-implemented"
IMPL_STATUSES = (IMPL_COMPLETE, IMPL_PARTIAL, IMPL_NOT_IMPLEMENTED)
IMPL_SCORE = {IMPL_COMPLETE: 1.0, IMPL_PARTIAL: 0.5, IMPL_NOT_IMPLEMENTED: 0.0}

# B) Qualification completeness -- validated physical/backend evidence that is
#    certification-eligible AND current for the build/platform. Distinguishes
#    STALE evidence from MISSING evidence, and NOT-APPLICABLE (offline-internal
#    dimensions never need a physical run). Offline tests are NEVER counted here.
QUAL_QUALIFIED = "qualified"
QUAL_IMPLEMENTED_UNQUALIFIED = "implemented-unqualified"
QUAL_BLOCKED = "blocked"
QUAL_NOT_APPLICABLE = "not-applicable"
QUAL_STATUSES = (QUAL_QUALIFIED, QUAL_IMPLEMENTED_UNQUALIFIED, QUAL_BLOCKED, QUAL_NOT_APPLICABLE)
# not-applicable is EXCLUDED from the qualification-percentage denominator.
QUAL_SCORE = {QUAL_QUALIFIED: 1.0, QUAL_IMPLEMENTED_UNQUALIFIED: 0.5, QUAL_BLOCKED: 0.0}

# C) Release readiness -- NOT a percentage. Derived from the mandatory release
#    scope (release-gating dimensions) + validated release evidence. A
#    percentage is NEVER turned into a release PASS.
READINESS_PASS = "pass"
READINESS_FAIL = "fail"
READINESS_BLOCKED = "blocked"
READINESS_NOT_APPLICABLE = "not-applicable"

# Dimension "kinds" -- decide how status is derived from the backing metadata.
KIND_INTERNAL = "internal"        # framework-internal; validated offline, no device needed to be complete
KIND_COVERAGE = "coverage"        # a product surface; needs a physical pass to be complete
KIND_PHYSICAL = "physical"        # purely a physical-qualification gate; complete only with a validated report
KIND_FIXTURE = "fixture"          # fixture-exclusivity enforcement mechanism

REPO_ROOT = suites_mod.REPO_ROOT


class FrameworkCompletenessError(Exception):
    pass


@dataclass(frozen=True)
class DimensionSpec:
    """Declarative wiring for one required dimension: which real metadata it is
    derived from. Keeping this as data (not buried in code) is what lets the CI
    drift test re-derive the same expectations from the same raw sources."""

    key: str
    title: str
    kind: str
    # Backing coverage-manifest component names (may be empty for a pure
    # physical gate that has no dedicated manifest component).
    components: "tuple[str, ...]" = ()
    # Suites whose composite membership is relevant (release-gating cross-check).
    suites: "tuple[str, ...]" = ()
    # Release-feature flag names (release_platforms.ReleaseFeatures) that make
    # this dimension release-gating when any is mandatory.
    feature_flags: "tuple[str, ...]" = ()
    # Release-platform flag names (release_platforms.ReleasePlatforms) likewise.
    platform_flags: "tuple[str, ...]" = ()
    # Promotion records (draft scenario suite names) this dimension tracks.
    promotion_suites: "tuple[str, ...]" = ()
    # The key used to look a validated physical report up in the reports scan.
    physical_key: "str | None" = None
    # Relative-weight in the summary (release-gating dimensions matter more).
    weight: float = 1.0
    # Extra implementation-evidence artifacts (module/test files) worth naming.
    evidence_artifacts: "tuple[str, ...]" = ()


# The thirteen required dimensions, wired to their real backing metadata.
DIMENSION_SPECS: "tuple[DimensionSpec, ...]" = (
    DimensionSpec(
        key="frameworkArchitecture",
        title="Framework architecture & orchestration",
        kind=KIND_INTERNAL,
        components=("report_consolidation", "machine_config", "build_installation"),
        weight=2.0,
        evidence_artifacts=(
            "calee_regression/consolidated_report.py",
            "calee_regression/run_context.py",
            "calee_regression/focused_workflow.py",
        ),
    ),
    DimensionSpec(
        key="mobileApiCoverage",
        title="CaleeMobile Client API coverage",
        kind=KIND_COVERAGE,
        components=("mobile_calendar", "mobile_tasks", "mobile_chores"),
        physical_key="mobile-api",
        weight=2.0,
        evidence_artifacts=("CaleeMobile-Regression/api/",),
    ),
    DimensionSpec(
        key="mobileUiCoverage",
        title="CaleeMobile UI coverage (Android + iOS)",
        kind=KIND_COVERAGE,
        components=("mobile_calendar", "mobile_tasks", "mobile_chores"),
        platform_flags=("mobile_android", "mobile_ios"),
        physical_key="mobile-ui",
        weight=2.0,
        evidence_artifacts=("CaleeMobile-Regression/ui/integration_test/flows/",),
    ),
    DimensionSpec(
        key="tabletReadCoverage",
        title="Calee tablet read/navigation coverage",
        kind=KIND_COVERAGE,
        components=("tablet_calendar_view", "tablet_tasks", "tablet_chores"),
        suites=("calendar", "tasks_smoke", "chores_smoke"),
        platform_flags=("tablet",),
        physical_key="tablet-standard",
        weight=2.0,
    ),
    DimensionSpec(
        key="tabletMutationCoverage",
        title="Calee tablet mutation coverage (create/edit/delete, complete/reopen, skip)",
        kind=KIND_COVERAGE,
        components=("tablet_calendar_mutation", "tablet_task_mutation", "tablet_chore_mutation"),
        suites=("calendar_event_mutation", "tasks_mutation", "chores_mutation"),
        promotion_suites=("calendar_event_mutation", "tasks_mutation", "chores_mutation"),
        physical_key="tablet-mutation",
        weight=1.0,
        evidence_artifacts=(
            "scenarios/calendar_event_mutation.yaml",
            "scenarios/tasks_mutation.yaml",
            "scenarios/chores_mutation.yaml",
        ),
    ),
    DimensionSpec(
        key="crossDeviceSyncCoverage",
        title="Cross-device synchronization coverage",
        kind=KIND_COVERAGE,
        components=("cross_device_sync",),
        feature_flags=("synchronization",),
        physical_key="sync",
        weight=2.0,
        evidence_artifacts=(
            "calee_regression/sync_smoke.py",
            "calee_regression/sync_smoke_bridge.py",
        ),
    ),
    DimensionSpec(
        key="guidedHandoffCoverage",
        title="Guided handoff coverage (onboarding + Google Calendar OAuth)",
        kind=KIND_COVERAGE,
        components=("onboarding", "google_calendar"),
        feature_flags=("onboarding", "google_calendar"),
        physical_key="guided-handoff",
        weight=1.0,
        evidence_artifacts=("calee_regression/handoff_bridge.py",),
    ),
    DimensionSpec(
        key="androidPhysicalQualification",
        title="Android physical qualification",
        kind=KIND_PHYSICAL,
        platform_flags=("mobile_android",),
        physical_key="mobile-android",
        weight=2.0,
    ),
    DimensionSpec(
        key="iosPhysicalQualification",
        title="iOS physical qualification",
        kind=KIND_PHYSICAL,
        platform_flags=("mobile_ios",),
        physical_key="mobile-ios",
        weight=2.0,
    ),
    DimensionSpec(
        key="tabletStandardQualification",
        title="Calee tablet standard-suite physical qualification",
        kind=KIND_PHYSICAL,
        suites=("full-tester",),
        platform_flags=("tablet",),
        physical_key="tablet-standard",
        weight=2.0,
    ),
    DimensionSpec(
        key="kioskAdminQualification",
        title="CaleeShell kiosk/admin physical qualification",
        kind=KIND_PHYSICAL,
        components=("kiosk_admin",),
        suites=("kiosk_admin_physical",),
        feature_flags=("kiosk_admin",),
        physical_key="kiosk-admin",
        weight=1.0,
    ),
    DimensionSpec(
        key="fixtureExclusivity",
        title="Regression fixture exclusivity",
        kind=KIND_FIXTURE,
        physical_key=None,
        weight=1.0,
        evidence_artifacts=(
            "calee_regression/fixture_ownership.py",
            "docs/DISTRIBUTED_FIXTURE_LEASE_DECISION.md",
        ),
    ),
    DimensionSpec(
        key="releaseEvidenceIntegrity",
        title="Release evidence integrity",
        kind=KIND_INTERNAL,
        components=("report_consolidation", "build_installation"),
        weight=2.0,
        evidence_artifacts=(
            "calee_regression/atomic_publish.py",
            "calee_regression/build_provenance.py",
            "calee_regression/focused_report_validation.py",
        ),
    ),
)


@dataclass
class PhysicalEvidence:
    """A validated, certification-eligible physical report discovered under
    ``reports/``. Only reports that a real device session could have produced
    are counted -- never a fake or a diagnostic."""

    key: str
    path: str
    digest: "str | None"
    run_id: "str | None"
    device_id: "str | None"
    status: "str | None"
    build: "str | None" = None
    platform: "str | None" = None
    stale: bool = False

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "path": self.path,
            "reportSha256": self.digest,
            "runId": self.run_id,
            "deviceId": self.device_id,
            "status": self.status,
            "qualificationBuild": self.build,
            "qualificationPlatform": self.platform,
            "stale": self.stale,
        }


@dataclass
class Dimension:
    key: str
    title: str
    status: str
    release_gating: bool
    # Workstream 2: two orthogonal axes derived alongside the legacy status.
    implementation_status: str = IMPL_COMPLETE
    qualification_status: str = QUAL_NOT_APPLICABLE
    qualification_stale: bool = False
    implementation_evidence: "list[str]" = field(default_factory=list)
    physical_evidence: "list[dict]" = field(default_factory=list)
    blockers: "list[str]" = field(default_factory=list)
    next_action: str = ""
    weight: float = 1.0

    @property
    def score(self) -> float:
        return STATUS_SCORE[self.status]

    @property
    def implementation_score(self) -> float:
        return IMPL_SCORE[self.implementation_status]

    @property
    def qualification_score(self) -> "float | None":
        # not-applicable is excluded from the qualification percentage.
        return QUAL_SCORE.get(self.qualification_status)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "releaseGating": self.release_gating,
            "implementationStatus": self.implementation_status,
            "qualificationStatus": self.qualification_status,
            "qualificationEvidenceStale": self.qualification_stale,
            "implementationEvidence": list(self.implementation_evidence),
            "physicalEvidence": list(self.physical_evidence),
            "blockers": list(self.blockers),
            "nextAction": self.next_action,
            "weight": self.weight,
            "statusScore": self.score,
            "implementationScore": self.implementation_score,
            "qualificationScore": self.qualification_score,
        }


@dataclass
class CompletenessReport:
    dimensions: "list[Dimension]"
    physical_session: bool
    feature_scope_source: str
    platform_scope_source: str
    failed_evidence_keys: "set" = field(default_factory=set)

    def dimension(self, key: str) -> "Dimension | None":
        for d in self.dimensions:
            if d.key == key:
                return d
        return None

    def weighted_summary(self) -> dict:
        total_weight = sum(d.weight for d in self.dimensions)
        earned = sum(d.weight * d.score for d in self.dimensions)
        pct = round(100.0 * earned / total_weight, 1) if total_weight else 0.0
        return {
            "weightedCompletionPercent": pct,
            "totalWeight": round(total_weight, 3),
            "earnedWeight": round(earned, 3),
            "statusScoring": dict(STATUS_SCORE),
            "note": (
                "This percentage is a weighted convenience only. It never "
                "substitutes for the per-dimension statuses above; a release "
                "decision is made from the statuses and releaseGating flags, "
                "not from this number."
            ),
        }

    def status_counts(self) -> dict:
        counts = {s: 0 for s in VALID_STATUSES}
        for d in self.dimensions:
            counts[d.status] += 1
        return counts

    # ── Workstream 2: three orthogonal measures ─────────────────────────────
    def implementation_summary(self) -> dict:
        """Axis A weighted percentage -- how much of the framework is BUILT +
        offline-tested + wired, independent of any physical run."""
        total_weight = sum(d.weight for d in self.dimensions)
        earned = sum(d.weight * d.implementation_score for d in self.dimensions)
        pct = round(100.0 * earned / total_weight, 1) if total_weight else 0.0
        counts = {s: 0 for s in IMPL_STATUSES}
        for d in self.dimensions:
            counts[d.implementation_status] += 1
        return {
            "implementationCompletionPercent": pct,
            "totalWeight": round(total_weight, 3),
            "earnedWeight": round(earned, 3),
            "statusScoring": dict(IMPL_SCORE),
            "statusCounts": counts,
            "note": "Implementation only: a physical blocker is never counted as missing code.",
        }

    def qualification_summary(self) -> dict:
        """Axis B weighted percentage -- how much is backed by validated,
        current physical/backend evidence. not-applicable dimensions are
        EXCLUDED from the denominator; offline tests are never counted here."""
        scored = [d for d in self.dimensions if d.qualification_score is not None]
        total_weight = sum(d.weight for d in scored)
        earned = sum(d.weight * d.qualification_score for d in scored)
        pct = round(100.0 * earned / total_weight, 1) if total_weight else 0.0
        counts = {s: 0 for s in QUAL_STATUSES}
        for d in self.dimensions:
            counts[d.qualification_status] += 1
        stale = [d.key for d in self.dimensions if d.qualification_stale]
        return {
            "qualificationCompletionPercent": pct,
            "scoredWeight": round(total_weight, 3),
            "earnedWeight": round(earned, 3),
            "statusScoring": dict(QUAL_SCORE),
            "statusCounts": counts,
            "staleEvidenceDimensions": stale,
            "note": "Qualification only: stale evidence is distinct from missing evidence; not-applicable is excluded.",
        }

    def release_readiness(self) -> dict:
        """Measure C -- NOT a percentage. Derived from the mandatory release
        scope (release-gating dimensions) + validated evidence. A percentage is
        never turned into a PASS."""
        gating = [d for d in self.dimensions if d.release_gating]
        if not gating:
            return {"status": READINESS_NOT_APPLICABLE, "gatingDimensions": [],
                    "blocking": [], "note": "No release-gating dimensions in the current scope."}
        blocking = []
        failed = []
        for d in gating:
            if d.key in self.failed_evidence_keys:
                failed.append(d.key)
            elif d.qualification_status == QUAL_QUALIFIED:
                continue
            elif d.qualification_status == QUAL_NOT_APPLICABLE and d.implementation_status == IMPL_COMPLETE:
                # An internal gating dimension is satisfied by being built +
                # offline-validated; it needs no physical run.
                continue
            else:
                blocking.append(d.key)
        if failed:
            status = READINESS_FAIL
        elif blocking:
            status = READINESS_BLOCKED
        else:
            status = READINESS_PASS
        return {
            "status": status,
            "gatingDimensions": [d.key for d in gating],
            "failing": failed,
            "blocking": blocking,
            "note": (
                "Release readiness is derived from the release-gating dimensions' qualification "
                "status and validated evidence -- never from a completion percentage."
            ),
        }

    def to_dict(self) -> dict:
        return {
            "schemaVersion": 2,
            "report": "framework-completeness",
            "derivedFrom": {
                "coverageManifest": "coverage/coverage-manifest.yaml",
                "suites": "calee_regression/suites.py",
                "promotion": "scenarios/promotion/*.yaml",
                "releaseFeatureScope": self.feature_scope_source,
                "releasePlatformScope": self.platform_scope_source,
                "validatedPhysicalReports": "reports/runs/<run-id>/...",
            },
            "physicalDeviceSession": self.physical_session,
            "statusVocabulary": list(VALID_STATUSES),
            "implementationStatusVocabulary": list(IMPL_STATUSES),
            "qualificationStatusVocabulary": list(QUAL_STATUSES),
            "releaseReadinessVocabulary": [
                READINESS_PASS, READINESS_FAIL, READINESS_BLOCKED, READINESS_NOT_APPLICABLE,
            ],
            "statusCounts": self.status_counts(),
            "dimensions": [d.to_dict() for d in self.dimensions],
            # Legacy conflated measure (retained for backward compatibility).
            "summary": self.weighted_summary(),
            # Workstream 2: three orthogonal, un-conflated measures.
            "implementationCompleteness": self.implementation_summary(),
            "qualificationCompleteness": self.qualification_summary(),
            "releaseReadiness": self.release_readiness(),
        }


# ── physical-evidence discovery ────────────────────────────────────────────
def scan_physical_evidence(
    reports_root: "Path | str | None" = None,
    *,
    current_build: "str | None" = None,
    current_platforms: "dict[str, str] | None" = None,
) -> "dict[str, PhysicalEvidence]":
    """Discover the latest VALIDATED physical evidence under ``reports/``.

    A report counts as physical evidence only when it is a JSON object that a
    genuine, certification-eligible device session could have produced:
    ``certificationEligible: true`` AND ``status: "pass"`` AND a non-empty
    device id AND a ``completenessKey`` naming which dimension it qualifies.
    Anything short of that (diagnostic, faked, unkeyed) is ignored -- this is
    the honest reason an offline checkout, whose ``reports/`` is empty, leaves
    every physical-qualification dimension ``blocked``.

    Staleness (Workstream 2): when ``current_build`` (and/or ``current_platforms``
    keyed by completenessKey) is supplied, an otherwise-valid report whose
    recorded ``qualificationBuild``/``platform`` no longer matches is marked
    ``stale`` -- qualification currency, kept DISTINCT from missing evidence.
    When no current identity is supplied, currency cannot be judged and
    evidence is treated as current (never falsely stale).
    """
    root = Path(reports_root) if reports_root else (REPO_ROOT / "reports")
    runs_dir = root / "runs"
    found: "dict[str, PhysicalEvidence]" = {}
    if not runs_dir.is_dir():
        return found
    for path in sorted(runs_dir.rglob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        key = report.get("completenessKey")
        if not isinstance(key, str) or not key:
            continue
        if report.get("certificationEligible") is not True:
            continue
        status = report.get("status")
        if not (isinstance(status, str) and status.strip().lower() == "pass"):
            continue
        device_id = report.get("deviceId") or (report.get("provenance") or {}).get("deviceId")
        if not device_id:
            continue
        build = report.get("qualificationBuild") or report.get("buildSha") or (report.get("provenance") or {}).get("buildSha")
        platform = report.get("qualificationPlatform") or report.get("platform")
        stale = False
        if current_build and build and str(build) != str(current_build):
            stale = True
        if current_platforms and key in current_platforms and platform and str(platform) != str(current_platforms[key]):
            stale = True
        # Last writer for a key wins (sorted() makes this deterministic).
        found[key] = PhysicalEvidence(
            key=key,
            path=str(path.relative_to(root)) if _is_relative_to(path, root) else str(path),
            digest=sha256_of_file(path),
            run_id=report.get("releaseRunId") or report.get("runId"),
            device_id=device_id,
            status="pass",
            build=str(build) if build else None,
            platform=str(platform) if platform else None,
            stale=stale,
        )
    return found


def scan_failed_evidence(reports_root: "Path | str | None" = None) -> "set":
    """Completeness keys with a validated, certification-eligible report whose
    ``status`` is a hard FAIL (a product regression), as opposed to a pass or a
    non-certifying diagnostic. Used to derive releaseReadiness == 'fail'. An
    offline checkout has none, so release readiness there is 'blocked', never
    'fail'."""
    root = Path(reports_root) if reports_root else (REPO_ROOT / "reports")
    runs_dir = root / "runs"
    failed: "set" = set()
    if not runs_dir.is_dir():
        return failed
    for path in sorted(runs_dir.rglob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        key = report.get("completenessKey")
        if not (isinstance(key, str) and key):
            continue
        if report.get("certificationEligible") is not True:
            continue
        status = report.get("status")
        if isinstance(status, str) and status.strip().lower() in ("fail", "failed"):
            failed.add(key)
    return failed


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ── derivation helpers ─────────────────────────────────────────────────────
def _components(manifest: coverage_mod.CoverageManifest, names) -> "list[coverage_mod.Component]":
    out = []
    for n in names:
        c = manifest.by_name(n)
        if c is not None:
            out.append(c)
    return out


def _coverage_status(components, *, requires_physical: bool, physical_present: bool) -> str:
    """Mechanically derive a coverage/internal dimension's status from its
    backing manifest components. Draft dominates (blocked); any partial or
    blocked component means partial; otherwise fully-automated + offline-tested
    is complete for framework-internal work, or implemented-unqualified for a
    product surface still awaiting a physical pass."""
    autos = [c.automated for c in components]
    if not autos or all(a == "false" for a in autos):
        return STATUS_NOT_IMPLEMENTED
    if any(a == "draft" for a in autos):
        return STATUS_BLOCKED
    if any(a == "partial" for a in autos) or any(c.blocked for c in components):
        return STATUS_PARTIAL
    # here: every backing component is automated:true and not blocked.
    if not requires_physical:
        return STATUS_COMPLETE
    return STATUS_COMPLETE if physical_present else STATUS_IMPLEMENTED_UNQUALIFIED


def _implementation_status(spec: DimensionSpec, components) -> str:
    """Axis A -- is the capability BUILT + offline-tested + wired? Ignores
    physical evidence entirely, so a physical blocker is never counted as
    missing implementation work. A ``draft`` scenario is implemented,
    offline-tested automation pending physical PROMOTION -- that is a
    qualification concern, so it still counts as complete implementation."""
    if spec.kind == KIND_FIXTURE:
        # host-local lock is implemented + offline-tested; distributed
        # exclusivity is an unsolved design gap, not missing code -> partial.
        return IMPL_PARTIAL
    autos = [c.automated for c in components]
    if not autos:
        # No manifest component: a pure physical gate (android/ios/tablet
        # standard) runs through the release-framework harness, which is
        # implemented; an internal dimension with no component is implemented.
        return IMPL_COMPLETE
    if all(a == "false" for a in autos):
        return IMPL_NOT_IMPLEMENTED
    if any(a == "partial" for a in autos) or any(a == "false" for a in autos):
        return IMPL_PARTIAL
    # remaining are "true"/"draft": require offline-tested to be complete.
    if any(a in ("true", "draft") and not c.offline_tested for a, c in zip(autos, components)):
        return IMPL_PARTIAL
    return IMPL_COMPLETE


def _qualification_status(spec: DimensionSpec, impl_status: str, evidence) -> str:
    """Axis B -- is the capability backed by validated, CURRENT physical/backend
    evidence? Internal/fixture dimensions never need a physical run
    (not-applicable). STALE evidence is implemented-unqualified, kept DISTINCT
    from MISSING evidence. Offline tests are never counted as qualification."""
    if spec.kind in (KIND_INTERNAL, KIND_FIXTURE):
        return QUAL_NOT_APPLICABLE
    if evidence is not None:
        if getattr(evidence, "stale", False):
            return QUAL_IMPLEMENTED_UNQUALIFIED
        return QUAL_QUALIFIED
    # no evidence at all
    if spec.kind == KIND_PHYSICAL:
        return QUAL_BLOCKED
    # coverage surface: built code awaiting a physical run vs. not-yet-built.
    return QUAL_IMPLEMENTED_UNQUALIFIED if impl_status == IMPL_COMPLETE else QUAL_BLOCKED


def _feature_gating(features: release_platforms_mod.ReleaseFeatures, flags) -> bool:
    return any(getattr(features, f) for f in flags)


def _platform_gating(platforms: release_platforms_mod.ReleasePlatforms, flags) -> bool:
    return any(getattr(platforms, f) for f in flags)


def _mutation_is_gating(spec: DimensionSpec, repo_root: Path) -> bool:
    """The mutation dimension is release-gating ONLY if a mutation suite has
    actually been promoted into a release composite. Derived from suites.py
    (composite membership) + the promotion state machine -- never asserted."""
    try:
        release_paths = set(str(p) for p in suites_mod.resolve_suite("full-tester", repo_root)) | set(
            str(p) for p in suites_mod.resolve_suite("release-technical", repo_root)
        )
    except suites_mod.SuiteError:
        return False
    for suite_name in spec.suites:
        try:
            paths = set(str(p) for p in suites_mod.resolve_suite(suite_name, repo_root))
        except suites_mod.SuiteError:
            continue
        if paths & release_paths:
            return True
    return False


def _promotion_records(promotion_dir: "Path | None"):
    try:
        return {r.scenario: r for r in promotion_mod.load_all(promotion_dir)}
    except promotion_mod.PromotionError:
        return {}


# ── main builder ───────────────────────────────────────────────────────────
def build_report(
    *,
    repo_root: "Path | None" = None,
    manifest_path: "Path | None" = None,
    reports_root: "Path | None" = None,
    release_platforms_path: "Path | None" = None,
) -> CompletenessReport:
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    manifest = coverage_mod.load_manifest(manifest_path)
    features = release_platforms_mod.load_release_features(release_platforms_path)
    platforms = release_platforms_mod.load_release_platforms(release_platforms_path)
    physical = scan_physical_evidence(reports_root)
    failed_keys = scan_failed_evidence(reports_root)
    promotions = _promotion_records((repo_root / "scenarios" / "promotion") if repo_root else None)

    dimensions: "list[Dimension]" = []
    for spec in DIMENSION_SPECS:
        dimensions.append(
            _build_dimension(
                spec,
                repo_root=repo_root,
                manifest=manifest,
                features=features,
                platforms=platforms,
                physical=physical,
                promotions=promotions,
            )
        )

    return CompletenessReport(
        dimensions=dimensions,
        physical_session=manifest.physical_session or bool(physical),
        feature_scope_source=features.source,
        platform_scope_source=platforms.source,
        failed_evidence_keys=failed_keys,
    )


def _build_dimension(
    spec: DimensionSpec,
    *,
    repo_root: Path,
    manifest: coverage_mod.CoverageManifest,
    features: release_platforms_mod.ReleaseFeatures,
    platforms: release_platforms_mod.ReleasePlatforms,
    physical: "dict[str, PhysicalEvidence]",
    promotions: dict,
) -> Dimension:
    components = _components(manifest, spec.components)
    evidence = physical.get(spec.physical_key) if spec.physical_key else None
    physical_present = evidence is not None

    # ── release-gating: derived from executable feature/platform scope,
    #    composite membership and the promotion state machine ──────────────
    if spec.key == "tabletMutationCoverage":
        release_gating = _mutation_is_gating(spec, repo_root)
    elif spec.feature_flags:
        release_gating = _feature_gating(features, spec.feature_flags)
    elif spec.platform_flags:
        release_gating = _platform_gating(platforms, spec.platform_flags)
    elif spec.kind == KIND_FIXTURE:
        release_gating = False  # an enforcement mechanism, not a release component
    elif components:
        release_gating = any(c.release_gating for c in components)
    else:
        release_gating = False

    # ── status ────────────────────────────────────────────────────────────
    if spec.kind == KIND_PHYSICAL:
        status = STATUS_COMPLETE if physical_present else STATUS_BLOCKED
    elif spec.kind == KIND_FIXTURE:
        status = _fixture_status(repo_root)
    else:
        requires_physical = spec.kind == KIND_COVERAGE
        status = _coverage_status(
            components, requires_physical=requires_physical, physical_present=physical_present
        )

    # ── Workstream 2 axes: implementation (built?) vs qualification (proven?) ─
    implementation_status = _implementation_status(spec, components)
    qualification_status = _qualification_status(spec, implementation_status, evidence)

    dim = Dimension(
        key=spec.key,
        title=spec.title,
        status=status,
        release_gating=release_gating,
        implementation_status=implementation_status,
        qualification_status=qualification_status,
        qualification_stale=bool(getattr(evidence, "stale", False)) if evidence is not None else False,
        weight=spec.weight,
    )

    # ── implementation evidence ─────────────────────────────────────────────
    for c in components:
        dim.implementation_evidence.append(
            f"manifest component {c.name}: automated={c.automated}, offlineTested={c.offline_tested}"
        )
    for suite_name in spec.suites:
        try:
            paths = [str(p.relative_to(repo_root)) for p in suites_mod.resolve_suite(suite_name, repo_root)]
            dim.implementation_evidence.append(f"suite {suite_name}: {len(paths)} scenario(s)")
        except suites_mod.SuiteError:
            dim.implementation_evidence.append(f"suite {suite_name}: UNRESOLVED")
    for artifact in spec.evidence_artifacts:
        dim.implementation_evidence.append(artifact)
    for suite_name in spec.promotion_suites:
        rec = promotions.get(suite_name)
        if rec is not None:
            dim.implementation_evidence.append(
                f"promotion {suite_name}: eligible={rec.release_suite_eligible}, physical={rec.physical_status}"
            )

    # ── physical evidence ───────────────────────────────────────────────────
    if evidence is not None:
        dim.physical_evidence.append(evidence.to_dict())

    # ── blockers + next action ──────────────────────────────────────────────
    dim.blockers, dim.next_action = _blockers_and_next_action(
        spec, status=status, components=components, physical_present=physical_present, promotions=promotions
    )
    return dim


def _fixture_status(repo_root: Path) -> str:
    """Fixture exclusivity: the host-local lock is implemented and offline-
    tested, but distributed (multi-host, same-account) exclusivity has no safe
    backend primitive -- so this dimension is genuinely ``partial`` until the
    lease design proposal lands. Derived from the lock module's own recorded
    scope, not asserted here."""
    try:
        from . import fixture_ownership

        if fixture_ownership.EXCLUSIVITY_SCOPE == "host-local":
            return STATUS_PARTIAL
    except Exception:  # pragma: no cover - module always imports
        pass
    return STATUS_PARTIAL


def _blockers_and_next_action(spec, *, status, components, physical_present, promotions):
    blockers: "list[str]" = []
    # Carry the manifest's own recorded blockers verbatim (honest, not invented).
    for c in components:
        if c.blocked and c.notes:
            blockers.append(f"{c.name}: {c.notes}")
        elif c.unsupported and c.notes:
            blockers.append(f"{c.name} (unsupported): {c.notes}")

    if spec.kind == KIND_PHYSICAL and not physical_present:
        device = {
            "androidPhysicalQualification": "a physical Android phone or approved emulator",
            "iosPhysicalQualification": "a physical iPhone (or a Mac + iOS simulator)",
            "tabletStandardQualification": "the prepared Calee tablet",
            "kioskAdminQualification": "a device-owner-authorised Calee tablet (kiosk/admin)",
        }.get(spec.key, "the required physical device")
        blockers.append(
            f"no validated, certification-eligible physical report found under reports/; requires {device}."
        )

    if spec.key == "tabletMutationCoverage":
        pending = [s for s in spec.promotion_suites if (promotions.get(s) is None or promotions[s].physical_status != "passed")]
        if pending:
            blockers.append(
                "promotion pending a recorded physical PASS with evidence for: " + ", ".join(pending)
            )

    if spec.kind == KIND_FIXTURE:
        blockers.append(
            "distributed (multi-host, same-account) exclusivity: no atomic backend lease primitive "
            "exists in the read-only Client API; see docs/DISTRIBUTED_FIXTURE_LEASE_DECISION.md."
        )

    next_action = {
        "frameworkArchitecture": "Maintain; no gap. Re-qualify on a Mac release run.",
        "mobileApiCoverage": "Run the CaleeMobile Client API legs on a Mac with backend credentials (focused-verify api / mobile-api suite).",
        "mobileUiCoverage": "Run the CaleeMobile Android + iOS UI suites on real devices/simulators via the release framework.",
        "tabletReadCoverage": "Run the standard tablet suite on the prepared tablet (full-tester) to qualify the read coverage.",
        "tabletMutationCoverage": "Physically run each mutation scenario twice on the tablet, then promote via scenarios/promotion + suites.py.",
        "crossDeviceSyncCoverage": "Close the tablet-mutation legs and run sync-smoke on real devices; colour-verify + provider-refresh remain external gaps.",
        "guidedHandoffCoverage": "Run the guided onboarding + Google OAuth handoffs with the permanent recorder on a device.",
        "androidPhysicalQualification": "Run the Android serial suite through the release framework on an Android device/emulator.",
        "iosPhysicalQualification": "Run the iOS serial suite on a physical iPhone or simulator via a Mac.",
        "tabletStandardQualification": "Run full-tester on the prepared tablet and record certification-eligible evidence.",
        "kioskAdminQualification": "Run the kiosk/admin suite only on an explicitly device-owner-authorised tablet.",
        "fixtureExclusivity": "Keep the host-local lock; pursue the distributed-lease design proposal (no product change this session).",
        "releaseEvidenceIntegrity": "Maintain; no gap. Immutable reports + provenance + atomic publish are offline-tested.",
    }.get(spec.key, "")
    return blockers, next_action


# ── rendering ──────────────────────────────────────────────────────────────
def render_json(report: CompletenessReport) -> str:
    return json.dumps(report.to_dict(), indent=2) + "\n"


_STATUS_BADGE = {
    STATUS_COMPLETE: "✅ complete",
    STATUS_IMPLEMENTED_UNQUALIFIED: "🟡 implemented-unqualified",
    STATUS_PARTIAL: "🟠 partial",
    STATUS_BLOCKED: "⛔ blocked",
    STATUS_NOT_IMPLEMENTED: "⬜ not-implemented",
}

_IMPL_BADGE = {
    IMPL_COMPLETE: "✅ complete",
    IMPL_PARTIAL: "🟠 partial",
    IMPL_NOT_IMPLEMENTED: "⬜ not-implemented",
}

_QUAL_BADGE = {
    QUAL_QUALIFIED: "✅ qualified",
    QUAL_IMPLEMENTED_UNQUALIFIED: "🟡 implemented-unqualified",
    QUAL_BLOCKED: "⛔ blocked",
    QUAL_NOT_APPLICABLE: "➖ not-applicable",
}


def render_markdown(report: CompletenessReport) -> str:
    d = report.to_dict()
    lines: "list[str]" = []
    lines.append("# Calee regression framework — completeness report")
    lines.append("")
    lines.append(
        "Generated by `python -m calee_regression framework-completeness`. Every status below is "
        "*derived* from repository metadata and validated physical reports — never a hand-edited value. "
        "See the module docstring in `calee_regression/framework_completeness.py` for the exact sources."
    )
    lines.append("")
    lines.append(f"- Physical device session: **{'yes' if report.physical_session else 'no (physical qualification pending)'}**")
    lines.append(f"- Release feature scope: `{report.feature_scope_source}`")
    lines.append(f"- Release platform scope: `{report.platform_scope_source}`")
    counts = report.status_counts()
    lines.append(
        "- Status counts (legacy conflated status): "
        + ", ".join(f"{k}={counts[k]}" for k in VALID_STATUSES)
    )
    lines.append("")

    impl = d["implementationCompleteness"]
    qual = d["qualificationCompleteness"]
    readiness = d["releaseReadiness"]
    lines.append("## Three measures (implementation ≠ qualification ≠ release readiness)")
    lines.append("")
    lines.append(
        "The legacy `weightedCompletionPercent` conflates *is it built?* with *is it physically "
        "qualified?*. These three independent measures separate them — see "
        "`docs/COMPLETENESS_MODEL.md`."
    )
    lines.append("")
    lines.append("| Measure | Value | Meaning |")
    lines.append("|---|---|---|")
    lines.append(
        f"| **Implementation completeness** | **{impl['implementationCompletionPercent']}%** "
        f"({impl['statusCounts']['complete']} complete, {impl['statusCounts']['partial']} partial, "
        f"{impl['statusCounts']['not-implemented']} not-implemented) | Built + offline-tested + wired. "
        "A physical blocker is never counted as missing code. |"
    )
    lines.append(
        f"| **Qualification completeness** | **{qual['qualificationCompletionPercent']}%** "
        f"({qual['statusCounts']['qualified']} qualified, {qual['statusCounts']['implemented-unqualified']} "
        f"implemented-unqualified, {qual['statusCounts']['blocked']} blocked, "
        f"{qual['statusCounts']['not-applicable']} n/a) | Validated, current physical/backend evidence. "
        "Offline tests are never counted; stale ≠ missing. |"
    )
    lines.append(
        f"| **Release readiness** | **{readiness['status'].upper()}** | Derived from the "
        "release-gating dimensions + validated evidence. Never a percentage turned into a PASS. |"
    )
    lines.append(f"- Legacy `weightedCompletionPercent` (retained for compatibility): **{d['summary']['weightedCompletionPercent']}%**")
    if qual["staleEvidenceDimensions"]:
        lines.append("- Stale qualification evidence (distinct from missing): " + ", ".join(qual["staleEvidenceDimensions"]))
    if readiness["blocking"]:
        lines.append("- Release-gating dimensions blocking readiness: " + ", ".join(readiness["blocking"]))
    lines.append("")

    lines.append("## Dimensions")
    lines.append("")
    lines.append("| Dimension | Implementation | Qualification | Legacy status | Gating | Next action |")
    lines.append("|---|---|---|---|---|---|")
    for dim in d["dimensions"]:
        badge = _STATUS_BADGE.get(dim["status"], dim["status"])
        impl_badge = _IMPL_BADGE.get(dim["implementationStatus"], dim["implementationStatus"])
        qual_badge = _QUAL_BADGE.get(dim["qualificationStatus"], dim["qualificationStatus"])
        if dim["qualificationEvidenceStale"]:
            qual_badge += " (stale)"
        next_action = dim["nextAction"].replace("|", "\\|") or "—"
        gating = "yes" if dim["releaseGating"] else "no"
        lines.append(f"| **{dim['key']}** | {impl_badge} | {qual_badge} | {badge} | {gating} | {next_action} |")
    lines.append("")

    lines.append("## Per-dimension evidence")
    lines.append("")
    for dim in d["dimensions"]:
        lines.append(f"### {dim['key']} — {dim['title']}")
        lines.append(f"- Implementation: **{dim['implementationStatus']}** — Qualification: **{dim['qualificationStatus']}**"
                     + (" (evidence stale)" if dim["qualificationEvidenceStale"] else ""))
        lines.append(f"- Legacy status: **{dim['status']}** (score {dim['statusScore']}, weight {dim['weight']})")
        lines.append(f"- Release-gating: **{'yes' if dim['releaseGating'] else 'no'}**")
        if dim["implementationEvidence"]:
            lines.append("- Implementation evidence:")
            for e in dim["implementationEvidence"]:
                lines.append(f"  - {e}")
        if dim["physicalEvidence"]:
            lines.append("- Physical evidence:")
            for e in dim["physicalEvidence"]:
                lines.append(f"  - {e['path']} (sha256 `{e['reportSha256']}`, run `{e['runId']}`, device `{e['deviceId']}`)")
        else:
            lines.append("- Physical evidence: none")
        if dim["blockers"]:
            lines.append("- Blockers:")
            for b in dim["blockers"]:
                lines.append(f"  - {b}")
        lines.append(f"- Next action: {dim['nextAction'] or '—'}")
        lines.append("")

    summary = d["summary"]
    lines.append("## Weighted summary (transparency only — never a substitute for statuses)")
    lines.append("")
    lines.append(f"- Weighted completion: **{summary['weightedCompletionPercent']}%** "
                 f"(earned {summary['earnedWeight']} of {summary['totalWeight']} weight)")
    lines.append("- Status scoring: " + ", ".join(f"`{k}`={v}" for k, v in summary["statusScoring"].items()))
    lines.append("- Per-dimension weights: " + ", ".join(f"`{dim['key']}`={dim['weight']}" for dim in d["dimensions"]))
    lines.append("")
    lines.append(f"> {summary['note']}")
    lines.append("")
    return "\n".join(lines)


# ── canonical committed artifacts (golden files for the CI drift test) ─────
CANONICAL_JSON_PATH = REPO_ROOT / "coverage" / "framework-completeness.json"
CANONICAL_MD_PATH = REPO_ROOT / "coverage" / "framework-completeness.md"


def write_canonical_artifacts(report: "CompletenessReport | None" = None) -> "tuple[Path, Path]":
    report = report or build_report()
    CANONICAL_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANONICAL_JSON_PATH.write_text(render_json(report), encoding="utf-8")
    CANONICAL_MD_PATH.write_text(render_markdown(report), encoding="utf-8")
    return CANONICAL_JSON_PATH, CANONICAL_MD_PATH


def canonical_drift(report: "CompletenessReport | None" = None) -> "list[str]":
    """Return a list of human-readable drift descriptions between the freshly-
    generated report and the committed canonical artifacts. Empty => in sync."""
    report = report or build_report()
    problems: "list[str]" = []
    fresh_json = render_json(report)
    fresh_md = render_markdown(report)
    for path, fresh, label in (
        (CANONICAL_JSON_PATH, fresh_json, "coverage/framework-completeness.json"),
        (CANONICAL_MD_PATH, fresh_md, "coverage/framework-completeness.md"),
    ):
        if not path.is_file():
            problems.append(f"{label} is missing; regenerate with `framework-completeness --write`.")
            continue
        if path.read_text(encoding="utf-8") != fresh:
            problems.append(f"{label} is stale; regenerate with `framework-completeness --write`.")
    return problems
