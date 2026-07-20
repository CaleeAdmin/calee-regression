"""Verify CaleeMobile selector-contract evidence against the expected release
identity (Workstream 1).

CaleeMobile-Regression's selector contract greps the real CaleeMobile source
for every stable selector the tablet/mobile robots depend on and emits a
machine-readable result (see ``ui/selector_contract.py``'s
``build_contract_result`` and ``config/release-platforms.example.yaml``). That
result names the exact CaleeMobile ref, the full tested Git SHA, the pubspec
version, the Flutter version, PASS/FAIL, and a timestamp.

This module is the *consuming* side: a release run must reject selector
evidence that was produced against a **different** CaleeMobile build than the
one being released. Passing selectors for commit X are not evidence about
commit Y -- and a release that ships Y while its selector proof is for X has no
proof at all. So evidence is rejected (BLOCKS) when:

  * the schema version is unknown/unsupported (a newer emitter this consumer
    does not understand yet), or is not the supported version;
  * the component marker is missing or wrong;
  * the contract did not PASS, or reports any missing selector;
  * the tested SHA is missing/abbreviated, or the pubspec version is malformed;
  * the Flutter toolchain the evidence was produced with is not the pinned
    ``3.44.1`` (selectors verified on a different toolchain are not evidence for
    the shipped build);
  * the selector counts are absent, non-positive, or internally inconsistent
    (``selectorsPresent != selectorsChecked``, or ``missing`` disagreeing with
    ``selectorsChecked - selectorsPresent``);
  * the timestamp is missing, not a valid UTC ISO-8601 instant, dated in the
    future, or older than the freshness window;
  * the tested SHA differs from the expected CaleeMobile release SHA; or
  * the pubspec version differs from the expected CaleeMobile release version.

The canonical result schema is defined here so both the emitter (in
CaleeMobile-Regression) and this verifier agree on one shape. A malformed
evidence file is a framework/pipeline fault -- it is raised as
``SelectorEvidenceError`` (BLOCKED), never silently treated as a passing (or
failing) product result. A genuine identity mismatch is a *verdict*, not an
error.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity_format import is_full_git_sha, is_wellformed_version

SELECTOR_EVIDENCE_SCHEMA_VERSION = 1
# Schema versions this consumer knows how to read. An evidence file that
# declares a version outside this set was produced by a newer (or older,
# incompatible) emitter -- the consumer must refuse it rather than guess, so a
# future field-semantics change can never be silently misread as a pass. See
# Priority 3's "Reject unknown future schema versions until the consumer
# explicitly supports them."
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

SELECTOR_EVIDENCE_COMPONENT = "caleemobile-selector-contract"

# The Flutter toolchain the selector contract MUST have been produced with,
# pinned to match CaleeMobile product CI (.github/workflows/flutter-ci.yml) and
# CaleeMobile-Regression CI (.github/workflows/ci.yml env.FLUTTER_VERSION).
# Selectors verified against a different toolchain are not evidence for the
# build actually being released.
EXPECTED_FLUTTER_VERSION = "3.44.1"

CONTRACT_PASS = "PASS"
CONTRACT_FAIL = "FAIL"

# How recent selector evidence must be to be trusted for a release. The
# authoritative staleness gate for a *release run* is the run-scoped
# mtime/run-ID validation in run_context.py (a report reused from an earlier run
# is rejected there); this schema-level window is an independent second guard
# against ancient evidence whose embedded timestamp is long past. A small future
# skew absorbs clock differences between the emitter host and this consumer.
DEFAULT_FRESHNESS = datetime.timedelta(days=14)
FUTURE_SKEW = datetime.timedelta(minutes=5)


class SelectorEvidenceError(Exception):
    """The selector-contract result file is missing, unreadable, or malformed
    (a framework/pipeline problem), as opposed to a genuine identity mismatch
    (which is reported as a verdict, not raised)."""


@dataclass
class SelectorContractResult:
    """One CaleeMobile selector-contract run's machine-readable result."""

    caleemobile_ref: "str | None" = None
    tested_sha: "str | None" = None
    pubspec_version: "str | None" = None
    flutter_version: "str | None" = None
    contract: "str | None" = None
    selectors_checked: "int | None" = None
    selectors_present: "int | None" = None
    missing: "list[str]" = field(default_factory=list)
    timestamp: "str | None" = None
    schema_version: "int | None" = None
    component: "str | None" = SELECTOR_EVIDENCE_COMPONENT
    # Release-run provenance (Priority 3). Present when the evidence was
    # generated for a specific release run; carried verbatim into the
    # consolidated report so a passing release can be traced back to the exact
    # regression run / CI run / artifact that produced its selector proof.
    release_run_id: "str | None" = None
    regression_sha: "str | None" = None  # CaleeMobile-Regression commit SHA
    workflow_run_id: "str | None" = None  # CI workflow run ID (when CI-produced)
    generated_by: "str | None" = None  # "ci" | "local" -- how it was produced
    artifact_digest: "str | None" = None  # sha256 of the evidence, where available
    # Release-certification binding (Priority 8): the PRODUCT release this
    # evidence is bound to, distinct from release_run_id (the calee-regression
    # TEST run it was adopted into). See schemas/selector_release_certification.
    # schema.json (duplicated verbatim in CaleeMobile-Regression).
    release_id: "str | None" = None
    correlation_id: "str | None" = None
    expected_sha: "str | None" = None
    expected_version: "str | None" = None

    @property
    def passed(self) -> bool:
        return (self.contract or "").strip().upper() == CONTRACT_PASS

    def to_dict(self) -> dict:
        data = {
            "schemaVersion": self.schema_version if self.schema_version is not None else SELECTOR_EVIDENCE_SCHEMA_VERSION,
            "component": self.component or SELECTOR_EVIDENCE_COMPONENT,
            "caleemobileRef": self.caleemobile_ref,
            "testedSha": self.tested_sha,
            "pubspecVersion": self.pubspec_version,
            "flutterVersion": self.flutter_version,
            "contract": self.contract,
            "selectorsChecked": self.selectors_checked,
            "selectorsPresent": self.selectors_present,
            "missing": list(self.missing),
            "timestamp": self.timestamp,
        }
        # Provenance keys are emitted only when set, so a plain CI/local result
        # (no release context) keeps the minimal shared shape.
        for key, value in (
            ("releaseRunId", self.release_run_id),
            ("regressionSha", self.regression_sha),
            ("workflowRunId", self.workflow_run_id),
            ("generatedBy", self.generated_by),
            ("artifactDigest", self.artifact_digest),
            ("releaseId", self.release_id),
            ("correlationId", self.correlation_id),
            ("expectedSha", self.expected_sha),
            ("expectedVersion", self.expected_version),
        ):
            if value is not None:
                data[key] = value
        return data


def parse_selector_contract_result(data: "Any") -> SelectorContractResult:
    """Build a SelectorContractResult from a decoded JSON mapping.

    Raises SelectorEvidenceError for anything that isn't the expected shape --
    a malformed evidence file is a framework/pipeline fault, never silently
    treated as a passing (or failing) product result. In particular, an
    unknown/unsupported ``schemaVersion`` is refused up front (see
    SUPPORTED_SCHEMA_VERSIONS): this consumer must not guess at the meaning of a
    result a newer emitter produced.
    """
    if not isinstance(data, dict):
        raise SelectorEvidenceError("selector-contract result must be a JSON object.")

    schema_version = _opt_int(data.get("schemaVersion"))
    if schema_version is not None and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SelectorEvidenceError(
            f"unsupported selector-contract schemaVersion {schema_version!r}; this consumer "
            f"supports only {sorted(SUPPORTED_SCHEMA_VERSIONS)} -- refusing to read evidence "
            f"produced by an unknown emitter version."
        )

    component = _opt_str(data.get("component"))
    if component is not None and component != SELECTOR_EVIDENCE_COMPONENT:
        raise SelectorEvidenceError(
            f"unexpected component {component!r} (expected {SELECTOR_EVIDENCE_COMPONENT!r})."
        )

    missing = data.get("missing", [])
    if missing is None:
        missing = []
    if not isinstance(missing, list):
        raise SelectorEvidenceError("selector-contract result 'missing' must be a list.")

    return SelectorContractResult(
        caleemobile_ref=_opt_str(data.get("caleemobileRef")),
        tested_sha=_opt_str(data.get("testedSha")),
        pubspec_version=_opt_str(data.get("pubspecVersion")),
        flutter_version=_opt_str(data.get("flutterVersion")),
        contract=_opt_str(data.get("contract")),
        selectors_checked=_opt_int(data.get("selectorsChecked")),
        selectors_present=_opt_int(data.get("selectorsPresent")),
        missing=[str(m) for m in missing],
        timestamp=_opt_str(data.get("timestamp")),
        schema_version=schema_version,
        component=component,
        release_run_id=_opt_str(data.get("releaseRunId")),
        regression_sha=_opt_str(data.get("regressionSha")),
        workflow_run_id=_opt_str(data.get("workflowRunId")),
        generated_by=_opt_str(data.get("generatedBy")),
        artifact_digest=_opt_str(data.get("artifactDigest")),
        release_id=_opt_str(data.get("releaseId")),
        correlation_id=_opt_str(data.get("correlationId")),
        expected_sha=_opt_str(data.get("expectedSha")),
        expected_version=_opt_str(data.get("expectedVersion")),
    )


def load_selector_contract_result(path: "Path | str") -> SelectorContractResult:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SelectorEvidenceError(f"could not read selector-contract result at {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SelectorEvidenceError(f"{path} is not valid JSON: {exc}") from exc
    return parse_selector_contract_result(data)


@dataclass
class SelectorEvidenceVerdict:
    ok: bool
    problems: "list[str]" = field(default_factory=list)
    result: "SelectorContractResult | None" = None

    def summary(self) -> str:
        if self.ok:
            r = self.result
            where = f"{r.pubspec_version} @ {r.tested_sha}" if r else "?"
            return f"Selector-contract evidence accepted for CaleeMobile {where}."
        return "Selector-contract evidence REJECTED: " + " ".join(self.problems)


def parse_utc_iso8601(value: "str | None") -> "datetime.datetime | None":
    """Parse a UTC ISO-8601 instant (``2026-07-18T00:00:00Z`` or
    ``...+00:00``). Returns an aware UTC datetime, or None when the value is
    absent, unparseable, naive (no timezone), or not UTC. The selector-contract
    emitter always writes ``...Z``; anything else is treated as malformed."""
    if not value:
        return None
    text = str(value).strip()
    # datetime.fromisoformat accepts 'Z' from Python 3.11, but normalise
    # explicitly so behaviour does not depend on the minor version.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # naive timestamp -- we cannot know it is UTC
    if parsed.utcoffset() != datetime.timedelta(0):
        return None  # a non-UTC offset is not the UTC instant we require
    return parsed.astimezone(datetime.timezone.utc)


def verify_selector_contract_evidence(
    result: SelectorContractResult,
    *,
    expected_git_sha: "str | None" = None,
    expected_version: "str | None" = None,
    expected_ref: "str | None" = None,
    expected_flutter_version: "str | None" = EXPECTED_FLUTTER_VERSION,
    now: "datetime.datetime | None" = None,
    max_age: "datetime.timedelta | None" = DEFAULT_FRESHNESS,
    future_skew: datetime.timedelta = FUTURE_SKEW,
    expected_release_run_id: "str | None" = None,
    require_release_provenance: bool = False,
    expected_release_id: "str | None" = None,
) -> SelectorEvidenceVerdict:
    """Reject selector evidence that doesn't prove the *expected* CaleeMobile
    release build. Returns a verdict listing every problem (never raises for a
    genuine mismatch -- that's an expected outcome, not an error).

    Every schema requirement in Priority 3 is enforced here: schema version,
    component marker, full tested SHA, well-formed version, pinned Flutter
    toolchain, PASS contract with no missing selectors, positive and internally
    consistent selector counts, and a valid, non-future, in-window UTC
    timestamp. ``expected_git_sha``/``expected_version`` add the release-identity
    match. ``require_release_provenance`` additionally demands the release-run
    provenance fields when the caller is a release gate (not the standalone
    verify command).
    """
    problems: "list[str]" = []

    # --- schema / component -------------------------------------------------
    if result.schema_version is None:
        problems.append("no schemaVersion recorded in the evidence.")
    elif result.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        problems.append(
            f"schemaVersion {result.schema_version!r} is not supported "
            f"(this consumer supports {sorted(SUPPORTED_SCHEMA_VERSIONS)})."
        )
    if not result.component:
        problems.append("no component marker recorded in the evidence.")
    elif result.component != SELECTOR_EVIDENCE_COMPONENT:
        problems.append(
            f"component {result.component!r} != expected {SELECTOR_EVIDENCE_COMPONENT!r}."
        )

    # --- contract / missing selectors --------------------------------------
    if not result.passed:
        problems.append(f"selector contract did not PASS (contract={result.contract!r}).")
    if result.missing:
        problems.append(
            f"selector contract reported {len(result.missing)} missing selector(s): {result.missing}."
        )

    # --- tested identity ----------------------------------------------------
    if not result.tested_sha:
        problems.append("no tested CaleeMobile SHA recorded in the evidence.")
    elif not is_full_git_sha(result.tested_sha):
        problems.append(f"tested SHA {result.tested_sha!r} is abbreviated/ambiguous (need the full 40-character SHA).")

    if not result.pubspec_version:
        problems.append("no CaleeMobile pubspec version recorded in the evidence.")
    elif not is_wellformed_version(result.pubspec_version):
        problems.append(f"tested pubspec version {result.pubspec_version!r} is not a well-formed version identity.")

    # --- Flutter toolchain --------------------------------------------------
    if expected_flutter_version is not None:
        if not result.flutter_version:
            problems.append("no Flutter version recorded in the evidence.")
        elif result.flutter_version.strip() != expected_flutter_version.strip():
            problems.append(
                f"evidence Flutter version {result.flutter_version!r} != pinned "
                f"{expected_flutter_version!r} -- selectors were verified on a different toolchain."
            )

    # --- selector counts (present, positive, internally consistent) --------
    checked = result.selectors_checked
    present = result.selectors_present
    if checked is None:
        problems.append("no selectorsChecked count recorded in the evidence.")
    elif checked <= 0:
        problems.append(f"selectorsChecked {checked!r} must be a positive integer.")
    if present is None:
        problems.append("no selectorsPresent count recorded in the evidence.")
    if checked is not None and present is not None:
        if present != checked:
            problems.append(
                f"selectorsPresent ({present}) != selectorsChecked ({checked}) -- "
                f"not every required selector is present."
            )
        # The missing list must reconcile with the counts: an internally
        # inconsistent result (e.g. missing=[] but present<checked, or a
        # missing list whose length disagrees with checked-present) is
        # untrustworthy evidence, not a pass.
        expected_missing = checked - present
        if expected_missing < 0 or len(result.missing) != expected_missing:
            problems.append(
                f"internally inconsistent selector counts: selectorsChecked={checked}, "
                f"selectorsPresent={present}, missing has {len(result.missing)} entr"
                f"{'y' if len(result.missing) == 1 else 'ies'} (expected {max(expected_missing, 0)})."
            )

    # --- timestamp: valid UTC, not future, in freshness window -------------
    if not result.timestamp:
        problems.append("no timestamp recorded in the evidence.")
    else:
        parsed_ts = parse_utc_iso8601(result.timestamp)
        if parsed_ts is None:
            problems.append(f"timestamp {result.timestamp!r} is not a valid UTC ISO-8601 instant.")
        else:
            reference = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=datetime.timezone.utc)
            if parsed_ts > reference + future_skew:
                problems.append(
                    f"timestamp {result.timestamp!r} is in the future relative to now "
                    f"({reference.isoformat()}) -- evidence cannot postdate its verification."
                )
            elif max_age is not None and (reference - parsed_ts) > max_age:
                age = reference - parsed_ts
                problems.append(
                    f"timestamp {result.timestamp!r} is stale: {age.days}d old, older than the "
                    f"{max_age.days}d freshness window."
                )

    # --- expected release identity -----------------------------------------
    if expected_git_sha is not None:
        if not is_full_git_sha(expected_git_sha):
            problems.append(
                f"expected CaleeMobile SHA {expected_git_sha!r} is abbreviated/ambiguous; configure the full SHA."
            )
        elif result.tested_sha and result.tested_sha.strip().lower() != expected_git_sha.strip().lower():
            problems.append(
                f"tested SHA {result.tested_sha!r} != expected release SHA {expected_git_sha!r} -- "
                f"this evidence is for a different CaleeMobile commit than the one being released."
            )

    if expected_version is not None and result.pubspec_version:
        if result.pubspec_version.strip() != expected_version.strip():
            problems.append(
                f"tested version {result.pubspec_version!r} != expected release version {expected_version!r} -- "
                f"this evidence is for a different CaleeMobile version than the one being released."
            )

    # --- release-ID binding (Priority 8) ------------------------------------
    # expected_release_id is set ONLY for a release-certification request
    # (ordinary PR selector checking never passes it and stays unaffected).
    # Missing release identity, on EITHER side, fails certification -- a
    # release-certification request must state up front which release it's
    # for, and evidence must be bound to a release. calee-regression rejects
    # evidence for another release ID even when SHA/version match (Priority
    # 8, requirement 8) -- two releases must never share one selector proof.
    if expected_release_id is not None:
        if not (expected_release_id or "").strip():
            problems.append("expected release ID is empty -- a release-certification request must state its release ID.")
        elif not result.release_id:
            problems.append(
                "no releaseId recorded in the evidence -- a release-certification request requires evidence "
                "bound to a release ID; missing release identity fails certification."
            )
        elif result.release_id.strip() != expected_release_id.strip():
            problems.append(
                f"evidence releaseId {result.release_id!r} != expected release {expected_release_id!r} -- "
                f"refusing to accept selector evidence for another release, even if SHA/version match."
            )

    # --- release-run provenance (release gate only) ------------------------
    if require_release_provenance:
        if not result.release_run_id:
            problems.append("no releaseRunId recorded -- evidence was not generated for this release run.")
        elif expected_release_run_id is not None and result.release_run_id.strip() != expected_release_run_id.strip():
            problems.append(
                f"releaseRunId {result.release_run_id!r} != current release run {expected_release_run_id!r} -- "
                f"this evidence belongs to a different release run."
            )
        if not (result.workflow_run_id or (result.generated_by or "").strip()):
            problems.append(
                "no provenance recorded (neither a CI workflowRunId nor a local-generation "
                "generatedBy marker) -- cannot trace how this evidence was produced."
            )

    if expected_ref is not None and result.caleemobile_ref:
        # A ref mismatch is a warning-level note, not a hard block: refs move,
        # but the SHA/version identity is the authoritative gate above. It is
        # still surfaced so an operator can see the evidence was gathered from
        # an unexpected ref.
        if result.caleemobile_ref.strip() != expected_ref.strip():
            problems.append(
                f"NOTE: evidence ref {result.caleemobile_ref!r} != expected ref {expected_ref!r} "
                f"(non-blocking; SHA/version identity is authoritative)."
            )

    hard_problems = [p for p in problems if not p.startswith("NOTE:")]
    return SelectorEvidenceVerdict(ok=not hard_problems, problems=problems, result=result)


def _opt_str(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: "Any") -> "int | None":
    if value is None:
        return None
    # Reject bools masquerading as ints (True == 1) and non-integral values.
    if isinstance(value, bool):
        raise SelectorEvidenceError(f"expected an integer, got boolean {value!r}.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SelectorEvidenceError(f"expected an integer, got {value!r}.") from exc
