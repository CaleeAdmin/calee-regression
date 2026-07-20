"""Machine-readable scenario-promotion state machine (Phase 6).

A draft tablet scenario becomes release-gating only through one honest path: a
recorded PASS on a real device, with evidence. Each draft scenario has a
``scenarios/promotion/<name>.yaml`` file stating exactly where it is in that
two-state machine:

  * DRAFT      -- ``releaseSuiteEligible: false``, ``physicalConfirmation.status:
                  pending``. The scenario is tagged ``draft-unverified``,
                  ``mandatory: false``, and absent from every release composite.
  * PROMOTED   -- ``releaseSuiteEligible: true``, ``physicalConfirmation.status:
                  passed`` with a fully-populated evidence block. The scenario
                  has dropped ``draft-unverified``, is mandatory, and is in the
                  correct composite suite.

This module validates a promotion file's own schema/state machine, and
``check_promotion_consistency`` cross-checks it against the real scenario YAML
and ``suites.py`` -- so a promotion file can never claim a scenario is
release-eligible while the scenario itself is still a draft (or vice versa).
``framework_tests/test_promotion.py`` runs both against the shipped files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import suites
from .identity_format import is_full_git_sha

_VALID_STATUS = {"pending", "passed"}
DRAFT_TAG = "draft-unverified"
PROMOTION_DIR = suites.REPO_ROOT / "scenarios" / "promotion"


class PromotionError(Exception):
    pass


@dataclass
class PromotionRecord:
    scenario: str
    scenario_file: str
    source_confirmed: bool
    source_sha: "str | None"
    offline_tests_passed: bool
    physical_status: str
    required_device: str
    evidence_required: "list[str]"
    evidence: dict
    release_suite_eligible: bool
    path: "Path | None" = None

    def missing_evidence(self) -> "list[str]":
        return [k for k in self.evidence_required if not self.evidence.get(k)]


def _parse(raw, *, path: "Path | None" = None) -> "tuple[PromotionRecord | None, list[str]]":
    errors: "list[str]" = []
    if not isinstance(raw, dict):
        return None, ["promotion file must be a YAML mapping at the top level."]

    scenario = raw.get("scenario")
    if not isinstance(scenario, str) or not scenario.strip():
        errors.append("promotion 'scenario' (the suite name) is required.")
    scenario_file = raw.get("scenarioFile", "")
    physical = raw.get("physicalConfirmation") or {}
    if not isinstance(physical, dict):
        errors.append("physicalConfirmation must be a mapping.")
        physical = {}

    status = physical.get("status")
    if status not in _VALID_STATUS:
        errors.append(f"physicalConfirmation.status must be one of {sorted(_VALID_STATUS)} (got {status!r}).")
    evidence_required = physical.get("evidenceRequired") or []
    if not isinstance(evidence_required, list) or not evidence_required:
        errors.append("physicalConfirmation.evidenceRequired must be a non-empty list.")
        evidence_required = []
    required_device = physical.get("requiredDevice") or ""
    if not required_device:
        errors.append("physicalConfirmation.requiredDevice is required.")

    source_sha = raw.get("sourceSha")
    if not is_full_git_sha(source_sha):
        errors.append(f"sourceSha must be a full 40-character Git SHA (got {source_sha!r}).")

    record = PromotionRecord(
        scenario=scenario if isinstance(scenario, str) else "",
        scenario_file=scenario_file,
        source_confirmed=bool(raw.get("sourceConfirmed", False)),
        source_sha=source_sha,
        offline_tests_passed=bool(raw.get("offlineTestsPassed", False)),
        physical_status=status if status in _VALID_STATUS else "pending",
        required_device=required_device,
        evidence_required=list(evidence_required),
        evidence=dict(physical.get("evidence") or {}),
        release_suite_eligible=bool(raw.get("releaseSuiteEligible", False)),
        path=path,
    )

    # State-machine rules (independent of the scenario YAML).
    if record.release_suite_eligible:
        if not record.source_confirmed:
            errors.append("releaseSuiteEligible is true but sourceConfirmed is false.")
        if not record.offline_tests_passed:
            errors.append("releaseSuiteEligible is true but offlineTestsPassed is false.")
        if record.physical_status != "passed":
            errors.append("releaseSuiteEligible is true but physicalConfirmation.status is not 'passed'.")
        missing = record.missing_evidence()
        if missing:
            errors.append(f"releaseSuiteEligible is true but evidence is missing: {missing}.")
    if record.physical_status == "passed":
        missing = record.missing_evidence()
        if missing:
            errors.append(f"physicalConfirmation.status is 'passed' but evidence is missing: {missing}.")

    return (record if not errors else None), errors


def load_promotion(path) -> PromotionRecord:
    path = Path(path)
    if not path.is_file():
        raise PromotionError(f"Promotion file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PromotionError(f"Promotion file {path} is not valid YAML: {exc}") from exc
    record, errors = _parse(raw, path=path.resolve())
    if errors:
        raise PromotionError(
            f"Promotion file {path} has {len(errors)} problem(s):\n" + "\n".join(f"  - {e}" for e in errors)
        )
    return record


def validate_raw(raw) -> "list[str]":
    """Schema + state-machine validation of a raw mapping (no filesystem)."""
    _, errors = _parse(raw)
    return errors


def _scenario_yaml(record: PromotionRecord) -> "tuple[dict | None, str | None]":
    """Load the scenario YAML referenced by a promotion record. Returns
    (raw_dict, error_message)."""
    rel = record.scenario_file
    if not rel:
        return None, f"promotion for {record.scenario!r} has no scenarioFile."
    path = suites.REPO_ROOT / rel
    if not path.is_file():
        return None, f"scenarioFile {rel!r} does not exist."
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), None
    except yaml.YAMLError as exc:
        return None, f"scenarioFile {rel!r} is not valid YAML: {exc}"


def check_consistency(record: PromotionRecord) -> "list[str]":
    """Cross-check a promotion record against the scenario YAML and suites.py.

    DRAFT state (releaseSuiteEligible false) requires the scenario to be tagged
    draft-unverified, mandatory: false, and outside every release composite.
    PROMOTED state (true) requires the opposite on all three, plus the source
    SHA to match. This is the invariant that keeps the promotion file and the
    scenario/suite membership from ever disagreeing."""
    problems: "list[str]" = []
    raw, err = _scenario_yaml(record)
    if err:
        return [err]

    tags = raw.get("tags") or []
    mandatory = raw.get("mandatory", True)
    sv = raw.get("source_verification") or {}
    scenario_sha = sv.get("calee_source_sha")

    try:
        suite_paths = set(str(p) for p in suites.resolve_suite(record.scenario))
        full_tester = set(str(p) for p in suites.resolve_suite("full-tester"))
        release_technical = set(str(p) for p in suites.resolve_suite("release-technical"))
    except suites.SuiteError as exc:
        return [f"scenario suite {record.scenario!r} does not resolve: {exc}"]
    in_release = bool(suite_paths & (full_tester | release_technical))

    if record.source_sha and scenario_sha and record.source_sha != scenario_sha:
        problems.append(
            f"promotion sourceSha {record.source_sha} does not match the scenario's "
            f"source_verification.calee_source_sha {scenario_sha}."
        )

    if record.release_suite_eligible:
        if DRAFT_TAG in tags:
            problems.append(f"scenario is release-eligible but still tagged {DRAFT_TAG!r}.")
        if mandatory is False:
            problems.append("scenario is release-eligible but still 'mandatory: false'.")
        if not in_release:
            problems.append("scenario is release-eligible but not in any release composite (full-tester/release-technical).")
    else:
        if DRAFT_TAG not in tags:
            problems.append(f"draft scenario must be tagged {DRAFT_TAG!r} (it is not).")
        if mandatory is not False:
            problems.append("draft scenario must be 'mandatory: false' (it is not).")
        if in_release:
            problems.append("draft scenario must NOT be in a release composite, but it is.")
    return problems


def load_all(promotion_dir: "Path | None" = None) -> "list[PromotionRecord]":
    directory = Path(promotion_dir) if promotion_dir else PROMOTION_DIR
    if not directory.is_dir():
        return []
    return [load_promotion(p) for p in sorted(directory.glob("*.yaml"))]
