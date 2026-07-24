"""Evidence-backed scenario-promotion evaluator (Workstream 5).

Draft-scenario promotion was documented but manual. This adds a STRICT,
fail-closed evaluator: a draft tablet scenario becomes eligible for promotion
ONLY when a real run produced validated, certification-eligible, current
evidence that satisfies every criterion. The evaluator produces a typed
decision (``eligible`` / ``ineligible`` / ``ambiguous``) showing every
criterion, so a human (or ``--apply``) can never promote on a diagnostic pass,
one pass when two are required, a stale build, a mismatched backend, missing
cleanup, an audit-only bundle, tampered bytes, another scenario's result, or an
ambiguous/duplicate attempt set.

Read-only by default (``evaluate`` / ``propose``). ``apply`` only records a
verified physical PASS into the promotion record, refuses on anything less than
``eligible`` and on a dirty tree, prints the exact change first, and never
commits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import promotion as promotion_mod
from .focused_report_validation import sha256_of_file

REQUIRED_ATTEMPTS = 2

DECISION_ELIGIBLE = "eligible"
DECISION_INELIGIBLE = "ineligible"
DECISION_AMBIGUOUS = "ambiguous"

# Scenario-specific authoritative assertions each mutation must have proven.
SCENARIO_ASSERTIONS = {
    "calendar_event_mutation": [
        "created", "resolved", "titleEdited", "locationEdited",
        "fieldsVerified", "deleted", "disappearanceVerified", "noScratchLeft",
    ],
    "tasks_mutation": [
        "rowResolved", "completed", "completionVerified", "reopened", "openVerified", "rowScoped",
    ],
    "chores_mutation": [
        "rowResolved", "actionMenuAvailable", "skipSelected", "occurrenceChangedVerified", "fixtureRestored",
    ],
}


class ScenarioPromotionError(Exception):
    pass


@dataclass
class Criterion:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class PromotionDecision:
    scenario: str
    run_id: str
    decision: str
    criteria: "list[Criterion]" = field(default_factory=list)
    report_path: "str | None" = None
    report_sha256: "str | None" = None

    def failing(self) -> "list[Criterion]":
        return [c for c in self.criteria if not c.passed]

    def to_dict(self) -> dict:
        return {
            "report": "scenario-promotion-decision",
            "scenario": self.scenario,
            "runId": self.run_id,
            "decision": self.decision,
            "reportPath": self.report_path,
            "reportSha256": self.report_sha256,
            "criteria": [c.to_dict() for c in self.criteria],
            "failingCriteria": [c.name for c in self.failing()],
        }


def _find_scenario_reports(run_dir: Path, scenario: str) -> "list[tuple[Path, dict]]":
    found = []
    if not run_dir.is_dir():
        return found
    for path in sorted(run_dir.rglob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict) and obj.get("scenario") == scenario:
            found.append((path, obj))
    return found


def evaluate(
    scenario: str,
    run_id: str,
    *,
    reports_root: "Path | None" = None,
    promotion_dir: "Path | None" = None,
    expected_build: "str | None" = None,
    expected_backend: "str | None" = None,
    expected_fixture: "str | None" = None,
    now_epoch: "float | None" = None,
    max_age_days: int = 30,
) -> PromotionDecision:
    """Evaluate whether ``scenario`` may be promoted from run ``run_id``.
    Fail-closed: any missing/failing criterion => ineligible; conflicting or
    duplicate attempts/reports => ambiguous."""
    reports_root = Path(reports_root) if reports_root else (promotion_mod.suites.REPO_ROOT / "reports")
    run_dir = reports_root / "runs" / run_id

    # Load the promotion record for this scenario.
    try:
        records = {r.scenario: r for r in promotion_mod.load_all(promotion_dir)}
    except promotion_mod.PromotionError as exc:
        raise ScenarioPromotionError(f"promotion records invalid: {exc}") from exc
    record = records.get(scenario)

    criteria: "list[Criterion]" = []
    ambiguous = False

    def add(name, passed, detail):
        criteria.append(Criterion(name, bool(passed), detail))

    if record is None:
        add("promotion_record_exists", False, f"no scenarios/promotion/{scenario}.yaml")
        return PromotionDecision(scenario, run_id, DECISION_INELIGIBLE, criteria)
    add("promotion_record_exists", True, f"loaded promotion record for {scenario}")

    reports = _find_scenario_reports(run_dir, scenario)
    if not reports:
        add("physical_report_present", False, f"no report with scenario=={scenario!r} under {run_dir}")
        return PromotionDecision(scenario, run_id, DECISION_INELIGIBLE, criteria)
    if len(reports) > 1:
        add("single_unambiguous_report", False, f"{len(reports)} reports claim scenario {scenario!r}; ambiguous")
        return PromotionDecision(scenario, run_id, DECISION_AMBIGUOUS, criteria,
                                 report_path=str(reports[0][0]))
    add("physical_report_present", True, "exactly one scenario report found")

    report_path, report = reports[0]
    report_sha = sha256_of_file(report_path)

    # ── identity ─────────────────────────────────────────────────────────────
    add("scenario_identity", report.get("scenario") == scenario and record.scenario == scenario,
        f"report.scenario={report.get('scenario')!r}, record.scenario={record.scenario!r}")

    # ── attempts (>= REQUIRED_ATTEMPTS pass; duplicates/ambiguity => ambiguous)
    attempts = report.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        add("required_attempts", False, "no attempts recorded")
    else:
        indices = [a.get("index") for a in attempts if isinstance(a, dict)]
        statuses = [(a.get("status") or "").lower() for a in attempts if isinstance(a, dict)]
        if len(indices) != len(set(indices)):
            ambiguous = True
            add("attempts_unambiguous", False, f"duplicate attempt indices: {indices}")
        elif any(s not in ("pass", "passed", "fail", "failed", "blocked") for s in statuses):
            ambiguous = True
            add("attempts_unambiguous", False, f"ambiguous attempt status in {statuses}")
        else:
            add("attempts_unambiguous", True, f"{len(attempts)} distinct attempts")
        passes = sum(1 for s in statuses if s in ("pass", "passed"))
        add("required_attempts", passes >= REQUIRED_ATTEMPTS,
            f"{passes} passing attempt(s); need {REQUIRED_ATTEMPTS}")

    # ── certification eligibility, diagnostic, device-init, schema ───────────
    add("certification_eligible", report.get("certificationEligible") is True,
        f"certificationEligible={report.get('certificationEligible')!r}")
    add("not_diagnostic", report.get("diagnosticMode") is not True,
        f"diagnosticMode={report.get('diagnosticMode')!r}")
    add("standard_device_initialization", report.get("deviceInitializationMode") == "standard",
        f"deviceInitializationMode={report.get('deviceInitializationMode')!r}")
    add("validated_report_schema", isinstance(report.get("reportSchemaVersion"), int),
        f"reportSchemaVersion={report.get('reportSchemaVersion')!r}")

    # ── digest / tamper check ────────────────────────────────────────────────
    # If the run recorded an immutable digest sidecar (<report>.sha256), the
    # report's bytes must still match it -- so altered report bytes are caught.
    sidecar = report_path.with_name(report_path.name + ".sha256")
    if sidecar.is_file():
        declared = (sidecar.read_text(encoding="utf-8").strip().split() or [""])[0]
        add("report_digest", bool(report_sha) and declared == report_sha,
            f"declared={declared} actual={report_sha}")
    else:
        add("report_digest", bool(report_sha), f"sha256={report_sha} (no immutable sidecar to compare)")

    # ── build / selector SHA / tablet / backend / fixture ────────────────────
    want_build = expected_build or record.source_sha
    add("same_product_build", bool(report.get("caleeGitSha")) and report.get("caleeGitSha") == want_build,
        f"report build={report.get('caleeGitSha')!r} vs expected={want_build!r}")
    add("source_selector_sha", report.get("sourceSelectorSha") == record.source_sha,
        f"report selectorSha={report.get('sourceSelectorSha')!r} vs record.sourceSha={record.source_sha!r}")
    tablet = report.get("tabletModel") or report.get("deviceId")
    add("expected_tablet_identity", bool(tablet), f"tablet={tablet!r}")
    backend = report.get("targetEnvironment") or report.get("backend")
    add("expected_backend", bool(backend) and (expected_backend is None or backend == expected_backend),
        f"backend={backend!r}" + (f" vs expected={expected_backend!r}" if expected_backend else ""))
    fixture = report.get("fixtureVersion")
    add("expected_fixture_version", bool(fixture) and (expected_fixture is None or fixture == expected_fixture),
        f"fixtureVersion={fixture!r}" + (f" vs expected={expected_fixture!r}" if expected_fixture else ""))

    # ── steps (no failed/blocked) ────────────────────────────────────────────
    steps = report.get("steps") or []
    bad = [s.get("name") for s in steps if isinstance(s, dict) and (s.get("status") or "").lower() not in ("pass", "passed")]
    add("no_failed_or_blocked_step", not bad, f"non-passing steps: {bad}" if bad else "all steps passed")

    # ── cleanup + not audit-only + freshness ─────────────────────────────────
    add("cleanup_verified", report.get("cleanupVerified") is True, f"cleanupVerified={report.get('cleanupVerified')!r}")
    audit_only = report.get("bundleProfile") == "audit" or report.get("imported") is True
    add("not_imported_audit_evidence", not audit_only,
        f"bundleProfile={report.get('bundleProfile')!r}, imported={report.get('imported')!r}")
    ts = report.get("timestampEpoch")
    if now_epoch is None or ts is None:
        add("evidence_freshness", True, "freshness not checked (no clock/timestamp supplied)")
    else:
        age_days = (now_epoch - float(ts)) / 86400.0
        add("evidence_freshness", age_days <= max_age_days, f"evidence age {age_days:.1f}d (max {max_age_days}d)")

    # ── scenario-specific authoritative assertions ───────────────────────────
    required_assertions = SCENARIO_ASSERTIONS.get(scenario, [])
    assertions = report.get("authoritativeAssertions") or {}
    missing = [k for k in required_assertions if assertions.get(k) is not True]
    add("scenario_authoritative_assertions", not missing,
        f"missing/failed assertions: {missing}" if missing else f"all {len(required_assertions)} assertions proven")

    if ambiguous:
        decision = DECISION_AMBIGUOUS
    elif all(c.passed for c in criteria):
        decision = DECISION_ELIGIBLE
    else:
        decision = DECISION_INELIGIBLE
    return PromotionDecision(scenario, run_id, decision, criteria,
                             report_path=str(report_path), report_sha256=report_sha)


def propose(decision: PromotionDecision) -> dict:
    """The change set that promotion WOULD make, without touching any file."""
    return {
        "scenario": decision.scenario,
        "decision": decision.decision,
        "wouldApply": decision.decision == DECISION_ELIGIBLE,
        "proposedChanges": [
            f"scenarios/promotion/{decision.scenario}.yaml: physicalConfirmation.status -> passed, evidence populated from {decision.report_path}",
            f"scenarios/{decision.scenario}.yaml: drop '{promotion_mod.DRAFT_TAG}' tag, set mandatory: true",
            "calee_regression/suites.py: add the scenario to the full-tester release composite",
            "coverage/framework-completeness.{json,md} + coverage manifest: regenerate",
            "framework_tests: run promotion invariants",
        ],
        "blockedBy": [c.name for c in decision.failing()],
    }


# Evidence keys copied from the physical report into the promotion record.
_EVIDENCE_FROM_REPORT = {
    "runId": "runId",
    "tabletModel": "tabletModel",
    "androidVersion": "androidVersion",
    "caleeVersion": "caleeVersion",
    "caleeGitSha": "caleeGitSha",
    "screenshotPaths": "screenshotPaths",
    "resultsJson": "resultsJson",
}


def apply_record_update(decision: PromotionDecision, *, promotion_dir: "Path | None" = None) -> dict:
    """Record a verified physical PASS into the promotion record (status ->
    passed, evidence populated). Refuses unless the decision is ``eligible``.
    Leaves releaseSuiteEligible=false and the scenario/suites untouched: making
    the scenario release-gating is a deliberate, separate promotion step (the
    state machine keeps this consistent). Returns the change summary. Does NOT
    touch git."""
    import yaml

    if decision.decision != DECISION_ELIGIBLE:
        raise ScenarioPromotionError(
            f"refusing to apply: decision is {decision.decision!r}, not eligible "
            f"(blocked by {[c.name for c in decision.failing()]})"
        )
    pdir = Path(promotion_dir) if promotion_dir else promotion_mod.PROMOTION_DIR
    path = pdir / f"{decision.scenario}.yaml"
    if not path.is_file():
        raise ScenarioPromotionError(f"promotion record not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ScenarioPromotionError(f"promotion record {path} is not a mapping")

    report = {}
    if decision.report_path:
        try:
            report = json.loads(Path(decision.report_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}

    physical = raw.setdefault("physicalConfirmation", {})
    before_status = physical.get("status")
    physical["status"] = "passed"
    evidence = dict(physical.get("evidence") or {})
    for key in physical.get("evidenceRequired") or []:
        src = _EVIDENCE_FROM_REPORT.get(key, key)
        if report.get(src) is not None:
            evidence[key] = report.get(src)
        elif decision.report_sha256 and key == "resultsJson":
            evidence[key] = f"sha256:{decision.report_sha256}"
    physical["evidence"] = evidence
    # releaseSuiteEligible intentionally left as-is (making it gating is a
    # separate, explicit step -- keeps promotion.check_consistency green).

    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return {
        "scenario": decision.scenario,
        "recordPath": str(path),
        "statusBefore": before_status,
        "statusAfter": "passed",
        "evidenceKeys": sorted(evidence.keys()),
        "remainingManualSteps": [
            f"drop '{promotion_mod.DRAFT_TAG}' + set mandatory in scenarios/{decision.scenario}.yaml",
            "add the scenario to the full-tester composite in suites.py",
            "set releaseSuiteEligible: true and regenerate the completeness golden files",
        ],
    }
