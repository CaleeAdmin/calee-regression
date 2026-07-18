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

  * the contract did not PASS;
  * the tested SHA is missing/abbreviated, or the pubspec version is malformed;
  * the tested SHA differs from the expected CaleeMobile release SHA; or
  * the pubspec version differs from the expected CaleeMobile release version.

The canonical result schema is defined here so both the emitter (in
CaleeMobile-Regression) and this verifier agree on one shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity_format import is_full_git_sha, is_wellformed_version

SELECTOR_EVIDENCE_SCHEMA_VERSION = 1
SELECTOR_EVIDENCE_COMPONENT = "caleemobile-selector-contract"

CONTRACT_PASS = "PASS"
CONTRACT_FAIL = "FAIL"


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

    @property
    def passed(self) -> bool:
        return (self.contract or "").strip().upper() == CONTRACT_PASS

    def to_dict(self) -> dict:
        return {
            "schemaVersion": self.schema_version or SELECTOR_EVIDENCE_SCHEMA_VERSION,
            "component": SELECTOR_EVIDENCE_COMPONENT,
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


def parse_selector_contract_result(data: "Any") -> SelectorContractResult:
    """Build a SelectorContractResult from a decoded JSON mapping.

    Raises SelectorEvidenceError for anything that isn't the expected shape --
    a malformed evidence file is a framework/pipeline fault, never silently
    treated as a passing (or failing) product result.
    """
    if not isinstance(data, dict):
        raise SelectorEvidenceError("selector-contract result must be a JSON object.")
    component = data.get("component")
    if component is not None and component != SELECTOR_EVIDENCE_COMPONENT:
        raise SelectorEvidenceError(
            f"unexpected component {component!r} (expected {SELECTOR_EVIDENCE_COMPONENT!r})."
        )
    missing = data.get("missing", [])
    if missing is None:
        missing = []
    if not isinstance(missing, list):
        raise SelectorEvidenceError("selector-contract result 'missing' must be a list.")

    def _opt_int(value: "Any") -> "int | None":
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise SelectorEvidenceError(f"expected an integer, got {value!r}.") from exc

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
        schema_version=_opt_int(data.get("schemaVersion")),
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


def verify_selector_contract_evidence(
    result: SelectorContractResult,
    *,
    expected_git_sha: "str | None" = None,
    expected_version: "str | None" = None,
    expected_ref: "str | None" = None,
) -> SelectorEvidenceVerdict:
    """Reject selector evidence that doesn't prove the *expected* CaleeMobile
    release build. Returns a verdict listing every problem (never raises for a
    genuine mismatch -- that's an expected outcome, not an error)."""
    problems: "list[str]" = []

    if not result.passed:
        problems.append(f"selector contract did not PASS (contract={result.contract!r}).")
    if result.missing:
        problems.append(f"selector contract reported {len(result.missing)} missing selector(s): {result.missing}.")

    if not result.tested_sha:
        problems.append("no tested CaleeMobile SHA recorded in the evidence.")
    elif not is_full_git_sha(result.tested_sha):
        problems.append(f"tested SHA {result.tested_sha!r} is abbreviated/ambiguous (need the full 40-character SHA).")

    if not result.pubspec_version:
        problems.append("no CaleeMobile pubspec version recorded in the evidence.")
    elif not is_wellformed_version(result.pubspec_version):
        problems.append(f"tested pubspec version {result.pubspec_version!r} is not a well-formed version identity.")

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
