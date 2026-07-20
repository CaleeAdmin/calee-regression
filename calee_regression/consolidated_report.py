"""Consolidated cross-repo release report.

Combines this repo's own tablet SuiteResult with CaleeMobile-Regression's
API/UI JSON reports and a list of manual guided checks into one release
decision, per the project's release-approval policy:

- Overall FAIL: at least one product assertion failed anywhere.
- Overall BLOCKED: no product failure was proven, but something mandatory
  could not run (blocked, missing/not executed, or skipped when it
  shouldn't have been).
- Overall PASS: every mandatory component passed and nothing was blocked,
  skipped, or left unexecuted, and no mandatory manual check is
  missing/failed.

A missing/not-run mandatory component is treated the same as a blocked one
-- an absent result must never read as a pass by omission. BLOCKED is never
silently converted to PASS.

This module only consumes already-produced JSON reports (or in-memory
dicts shaped like them) -- it does not itself run anything, so it can be
exercised entirely with synthetic/framework-level data (see
framework_tests/test_consolidated_report.py), independent of whether any
real device or environment is available.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


def _sanitize_for_filename(value: str) -> str:
    """Matches reporting.py's ReportBuilder sanitization so build labels
    (which may contain "/", spaces, etc. -- e.g. "0.3.22 / 0.0.22" for a
    combined tablet+mobile version) can never break a bundle file path."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value)

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_BLOCKED = "blocked"
STATUS_NOT_RUN = "not_run"

# The literal per-step status string CaleeMobile-Regression's api/ui
# report JSON uses (uppercase, "PASS"/"FAIL"/"BLOCKED"/"SKIP") -- a
# different namespace from this module's own lowercase ComponentResult
# statuses above. Named separately so a step-status comparison in
# component_from_api_report is never confused for a ComponentResult
# status comparison.
STATUS_SKIP_RAW = "SKIP"


def decide_status(*, passed: int, failed: int, blocked: int, total: "int | None" = None) -> str:
    """The one place the PASS/FAIL/BLOCKED decision rule lives.

    ``total`` defaults to ``passed + failed + blocked`` when omitted; pass it
    explicitly when the caller also has skipped/info counts that inflate the
    total, so "nothing passed" is judged against everything that was
    attempted, not just these three buckets.
    """
    if total is None:
        total = passed + failed + blocked
    if failed:
        return STATUS_FAIL
    if blocked:
        return STATUS_BLOCKED
    if total and not passed:
        # Something ran (or was supposed to) but nothing actually passed --
        # e.g. every scenario was skipped. That must never read as success.
        return STATUS_BLOCKED
    return STATUS_PASS


def status_from_exit_code(code: "int | None") -> "str | None":
    """A recorded process exit code as a ComponentResult status, or None when
    no code was recorded. 0 -> pass, 1 -> fail, any other non-zero -> blocked
    (an environment/tooling problem, never a product FAIL)."""
    if code is None:
        return None
    if code == 0:
        return STATUS_PASS
    if code == 1:
        return STATUS_FAIL
    return STATUS_BLOCKED


# Severity ordering used when reconciling a component's report-derived status
# with a recorded exit-code floor: FAIL is worse than BLOCKED/NOT_RUN, which
# are worse than PASS. A floor can only make a component *worse*, never better.
_STATUS_SEVERITY = {
    STATUS_PASS: 0,
    STATUS_NOT_RUN: 1,
    STATUS_BLOCKED: 1,
    STATUS_FAIL: 2,
}


def _apply_exit_floor(component: "ComponentResult", floor_code: "int | None") -> "ComponentResult":
    """Downgrade `component` to at least the severity of a recorded exit-code
    floor from the run manifest.

    A report file that reads *better* than the worst result the manifest
    recorded for this component -- e.g. a later platform run overwrote an
    earlier FAIL's results.json with a PASS -- must not be trusted: the
    consolidated result is the worse of (report-derived status, recorded
    floor). This closes the file-overwrite hole end-to-end, so an initial API
    (or platform) FAIL can never be laundered into a PASS by a later run. See
    run_context.worst_exit_code and Phase 3.
    """
    floor_status = status_from_exit_code(floor_code)
    if floor_status is None:
        return component
    if _STATUS_SEVERITY.get(floor_status, 0) <= _STATUS_SEVERITY.get(component.status, 0):
        return component
    detail = list(component.detail) + [
        f"Run manifest recorded exit code {floor_code} ({floor_status.upper()}) for "
        f"this component, worse than its report — using the recorded result "
        f"(a later run may not overwrite an earlier failure)."
    ]
    return replace(component, status=floor_status, detail=detail)


@dataclass
class ComponentResult:
    name: str
    status: str
    mandatory: bool = True
    passed: int = 0
    failed: int = 0
    blocked: int = 0
    skipped: int = 0
    detail: "list[str]" = field(default_factory=list)
    # Optional structured evidence for this component (e.g. per-platform
    # backend triple + device/build identity; see backend_evidence_component).
    # Rendered as a labelled table in the HTML report and surfaced verbatim in
    # the JSON report. None (the default) means "no structured evidence", and
    # is omitted from to_dict so existing components are unchanged.
    evidence: "dict | None" = None

    def to_dict(self) -> dict:
        data = {
            "name": self.name,
            "status": self.status,
            "mandatory": self.mandatory,
            "passed": self.passed,
            "failed": self.failed,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "detail": list(self.detail),
        }
        if self.evidence is not None:
            data["evidence"] = dict(self.evidence)
        return data


@dataclass
class ManualCheck:
    title: str
    instruction: str
    expected_result: str
    status: "str | None" = None  # None means not yet recorded
    note: str = ""
    screenshot_ref: "str | None" = None
    mandatory: bool = True

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "instruction": self.instruction,
            "expectedResult": self.expected_result,
            "status": self.status,
            "note": self.note,
            "screenshotRef": self.screenshot_ref,
            "mandatory": self.mandatory,
        }


@dataclass
class ReleaseReport:
    overall_status: str
    components: "list[ComponentResult]"
    manual_checks: "list[ManualCheck]"
    meta: dict
    generated_at: str
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "overallStatus": self.overall_status,
            "meta": self.meta,
            "generatedAt": self.generated_at,
            "components": [c.to_dict() for c in self.components],
            "manualChecks": [m.to_dict() for m in self.manual_checks],
            "summary": self.summary,
        }


def _build_summary(components: "list[ComponentResult]", manual_checks: "list[ManualCheck]", overall: str) -> dict:
    """A human-scannable roll-up for the tester/technical owner: which
    components are blocked/failed, which mandatory/optional tests were
    skipped, and a one-line suggested next action. See Workstream 9."""
    blocked_components = [c.name for c in components if c.status in (STATUS_BLOCKED, STATUS_NOT_RUN)]
    failed_components = [c.name for c in components if c.status == STATUS_FAIL]
    skipped_mandatory_checks = [c.title for c in manual_checks if c.mandatory and c.status not in (STATUS_PASS,)]
    skipped_optional_checks = [
        c.title for c in manual_checks if not c.mandatory and c.status not in (STATUS_PASS, STATUS_FAIL)
    ]

    if overall == STATUS_FAIL:
        next_action = (
            f"Do not release. A real product problem was found in: {', '.join(failed_components) or 'see components below'}."
        )
    elif overall == STATUS_BLOCKED:
        mandatory_blocked = [c.name for c in components if c.mandatory and c.status in (STATUS_BLOCKED, STATUS_NOT_RUN)]
        next_action = (
            f"Not yet releasable. Resolve and re-run the blocked/not-executed mandatory component(s): "
            f"{', '.join(mandatory_blocked) or ', '.join(blocked_components) or 'see components below'}."
        )
    else:
        next_action = "All mandatory components passed. Review any optional/skipped items below, then this build is approved to release."

    return {
        "blockedComponents": blocked_components,
        "failedComponents": failed_components,
        "skippedMandatoryManualChecks": skipped_mandatory_checks,
        "skippedOptionalManualChecks": skipped_optional_checks,
        "suggestedNextAction": next_action,
    }


def component_from_build_version_match(
    *, name: str, expected: "str | None", detected: "str | None"
) -> "ComponentResult | None":
    """Compares a technical-owner-configured expected build/version
    against the detected one for a single app (Calee or CaleeMobile).
    Returns None when no expectation was configured -- there is nothing to
    check, so this must not manufacture a component out of nothing. A
    version mismatch BLOCKS (it means the wrong build was tested, which is
    a process problem, not evidence the tested build itself regressed)."""
    if not expected:
        return None
    if not detected or detected == "unknown":
        return ComponentResult(
            name=name, status=STATUS_BLOCKED, mandatory=True,
            detail=[f"Expected build {expected!r} but no build version was detected/provided."],
        )
    if str(detected) != str(expected):
        return ComponentResult(
            name=name, status=STATUS_BLOCKED, mandatory=True,
            detail=[f"Expected build {expected!r} but detected {detected!r} -- the wrong build may have been tested."],
        )
    return ComponentResult(name=name, status=STATUS_PASS, mandatory=True, detail=[f"Build matches expected {expected!r}."])


def _versions_match(a: "Any | None", b: "Any | None") -> bool:
    return str(a).strip() == str(b).strip()


# Re-exported from the shared identity_format module so existing importers
# (`from calee_regression.consolidated_report import is_full_git_sha`) keep
# working while the predicate lives in one place shared with the config loader.
from .identity_format import is_full_git_sha, is_wellformed_version  # noqa: E402


def component_from_build_identity(
    name: str,
    *,
    detected_version: "str | None" = None,
    expected_version: "str | None" = None,
    detected_git_sha: "str | None" = None,
    expected_git_sha: "str | None" = None,
    dirty: bool = False,
    available: bool = True,
    required: bool = False,
    require_git_sha: bool = False,
    require_package_identity: bool = False,
    allow_dirty: bool = False,
    version_code: "str | None" = None,
    application_id: "str | None" = None,
    caleeshell_version: "str | None" = None,
    source: "str | None" = None,
) -> "ComponentResult | None":
    """Gate one app's build identity for the release (Phase 3).

    ``required`` means this app's identity is in scope for this release and
    must be known -- an unknown/undetected identity then BLOCKS, because a
    release PASS must prove *which* build was tested. Returns None only when
    the identity is neither required nor has any expectation to check
    (nothing to say).

    Result rules (see docs/RELEASE_POLICY.md and the Phase 3 spec):
      * identity unavailable/unknown while required (or expected) -> BLOCKED
        ("do not allow a release PASS with unknown build identity");
      * dirty/uncommitted build without ``allow_dirty`` -> BLOCKED;
      * expected version or Git SHA configured but the detected one differs
        -> BLOCKED ("the wrong build may have been tested");
      * otherwise -> PASS.

    The full detected identity (version, Git SHA, dirty flag, versionCode,
    application id, CaleeShell version) is attached as structured evidence so
    the consolidated report can show exactly what was tested.
    """
    if not required and expected_version is None and expected_git_sha is None:
        return None

    known_version = available and bool(detected_version) and str(detected_version) != "unknown"
    evidence = {
        "buildVersion": detected_version,
        "gitSha": detected_git_sha,
        "dirty": dirty,
        "versionCode": version_code,
        "applicationId": application_id,
        "caleeShellVersion": caleeshell_version,
        "expectedBuildVersion": expected_version,
        "expectedGitSha": expected_git_sha,
        "available": available,
        "source": source,
    }
    evidence = {k: v for k, v in evidence.items() if v is not None}

    def blocked(detail: str) -> ComponentResult:
        return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=True, detail=[detail], evidence=evidence)

    if not known_version:
        return blocked(
            "Build identity could not be determined -- refusing to certify a release "
            "against an unknown build (which build was actually tested?)."
        )
    if require_git_sha:
        # A version/build alone is not a unique identity (e.g. 0.0.22+22 spans
        # many commits), so an in-scope build must carry a full, unambiguous
        # Git SHA. A missing or abbreviated SHA BLOCKS -- see Phase 5.
        if not detected_git_sha or str(detected_git_sha) == "unknown":
            return blocked(
                f"Build {detected_version} has no Git SHA; a unique build identity requires "
                f"the exact commit (a version alone spans multiple commits). Refusing to certify."
            )
        if not is_full_git_sha(detected_git_sha):
            return blocked(
                f"Git SHA {detected_git_sha!r} is abbreviated/ambiguous; a release requires the "
                f"full 40-character commit SHA. Refusing to certify an ambiguous build identity."
            )
        if expected_git_sha is not None and not is_full_git_sha(expected_git_sha):
            return blocked(
                f"Expected Git SHA {expected_git_sha!r} is abbreviated/ambiguous; configure the "
                f"full 40-character commit SHA for the release candidate."
            )
    if require_package_identity:
        # A release-gating tablet run must identify the installed package it
        # drove: its application id and installed versionCode, not just a
        # versionName. See Phase 6.
        missing = []
        if not application_id:
            missing.append("application id")
        if not version_code:
            missing.append("installed versionCode")
        if missing:
            return blocked(
                f"Installed package identity is incomplete (missing {', '.join(missing)}); a "
                f"release-gating tablet run must record the application id and installed "
                f"versionCode of the package it drove. Refusing to certify."
            )
    if dirty and not allow_dirty:
        return blocked(
            f"The build under test ({detected_version}) has uncommitted local changes; a "
            f"dirty/uncommitted build is not approved for release (set allow_dirty to override)."
        )
    if expected_version is not None and not _versions_match(detected_version, expected_version):
        return blocked(
            f"Expected build {expected_version!r} but detected {detected_version!r} -- the wrong build may have been tested."
        )
    if expected_git_sha is not None:
        if not detected_git_sha or str(detected_git_sha) == "unknown":
            return blocked(
                f"Expected commit {expected_git_sha!r} but no Git SHA was detected -- cannot confirm the intended commit was tested."
            )
        if not _versions_match(detected_git_sha, expected_git_sha):
            return blocked(
                f"Expected commit {expected_git_sha!r} but detected {detected_git_sha!r} -- the wrong commit may have been tested."
            )

    note = f"Build {detected_version}"
    if detected_git_sha:
        note += f" @ {detected_git_sha}"
    matched = []
    if expected_version is not None:
        matched.append("version")
    if expected_git_sha is not None:
        matched.append("commit")
    if matched:
        note += f" matches expected {' and '.join(matched)}"
    if dirty and allow_dirty:
        note += " (dirty working tree, explicitly approved)"
    return ComponentResult(name=name, status=STATUS_PASS, mandatory=True, detail=[note + "."], evidence=evidence)


def component_from_tablet_report(name: str, suite_dict: "dict[str, Any] | None", *, mandatory: bool = True) -> ComponentResult:
    """Build a ComponentResult from calee-regression's SuiteResult.to_dict() shape."""
    if suite_dict is None:
        return ComponentResult(name=name, status=STATUS_NOT_RUN, mandatory=mandatory, detail=["Not executed."])
    passed = suite_dict.get("passed_count", 0)
    failed = suite_dict.get("failed_count", 0)
    blocked = suite_dict.get("blocked_count", 0)
    skipped = suite_dict.get("skipped_count", 0)
    total = len(suite_dict.get("scenarios", [])) or (passed + failed + blocked + skipped)
    # A mandatory (release-critical) scenario that ended up SKIPPED must
    # block the same as an outright-blocked one -- see
    # SuiteResult.mandatory_skipped_count and cli.py::_exit_code_for, which
    # this mirrors so the tablet CLI's own exit code and this consolidated
    # component can never disagree about the same run.
    mandatory_skipped = sum(
        1 for s in suite_dict.get("scenarios", [])
        if s.get("status") == "skipped" and s.get("mandatory", True)
    )
    status = decide_status(passed=passed, failed=failed, blocked=blocked + mandatory_skipped, total=total)
    detail = [
        f"{s['name']}: {s.get('blocked_reason') or s.get('skip_reason')}"
        for s in suite_dict.get("scenarios", [])
        if s.get("status") in ("failed", "blocked") or s.get("blocked_reason") or (s.get("status") == "skipped" and s.get("skip_reason"))
    ]
    return ComponentResult(
        name=name, status=status, mandatory=mandatory,
        passed=passed, failed=failed, blocked=blocked, skipped=skipped, detail=detail,
    )


def component_from_api_report(name: str, report_dict: "dict[str, Any] | None", *, mandatory: bool = True) -> ComponentResult:
    """Build a ComponentResult from CaleeMobile-Regression's --report json
    shape (shared by the Client API suite and the mobile Android/iPhone UI
    suites -- see api/caleemobile_regression/reporting.py and
    ui/run_ui_suite.py in that repo).

    Each step may carry "mandatory" (bool, default True when absent -- the
    API report's steps don't have this concept at all yet, so absence must
    never be read as "optional") and "skipCategory" (informational only;
    see ui/run_ui_suite.py's classify_skip). A SKIP step is folded into
    the blocked count exactly when it's mandatory -- mirroring
    component_from_tablet_report's mandatory_skipped handling, so a suite
    containing passed tests plus one mandatory skipped test can never
    read as an overall pass, and a fixture-related skip (skipCategory
    "missing_fixture") is BLOCKED, never a product FAIL.
    """
    if report_dict is None:
        return ComponentResult(name=name, status=STATUS_NOT_RUN, mandatory=mandatory, detail=["Not executed."])
    counts = report_dict.get("counts", {})
    passed = counts.get("PASS", 0)
    failed = counts.get("FAIL", 0)
    blocked = counts.get("BLOCKED", 0)
    skipped = counts.get("SKIP", 0)
    steps = report_dict.get("steps", [])
    total = len(steps) or (passed + failed + blocked + skipped)
    mandatory_skipped = sum(
        1 for s in steps
        if s.get("status") == STATUS_SKIP_RAW and s.get("mandatory", True)
    )
    status = decide_status(passed=passed, failed=failed, blocked=blocked + mandatory_skipped, total=total)
    detail = [
        f"{s['name']}: {s.get('detail')}"
        for s in steps
        if s.get("status") in ("FAIL", "BLOCKED")
        or (s.get("status") == STATUS_SKIP_RAW and s.get("mandatory", True))
    ]
    return ComponentResult(
        name=name, status=status, mandatory=mandatory,
        passed=passed, failed=failed, blocked=blocked, skipped=skipped, detail=detail,
    )


def component_from_environment_report(
    name: str, report_dict: "dict[str, Any] | None", *, mandatory: bool = True
) -> ComponentResult:
    """Build a ComponentResult from `prepare`'s environment/results.json
    (cli.py's prepare command / run_context.py). Prepare is always
    mandatory -- an environment/fixture that was never verified ready must
    block the release the same as any other missing mandatory component,
    never just an informational note next to an otherwise-green result.

    Prepare only ever reports "pass" or "blocked" (never "fail" -- there is
    no product assertion here, only "was the environment/fixture ready").
    A status this function doesn't recognize is treated as blocked rather
    than silently trusted.
    """
    if report_dict is None:
        return ComponentResult(name=name, status=STATUS_NOT_RUN, mandatory=mandatory, detail=["Not executed."])
    status = report_dict.get("status")
    detail = list(report_dict.get("detail", []))
    if status not in (STATUS_PASS, STATUS_BLOCKED):
        detail = detail + [f"Unrecognized environment status {report_dict.get('status')!r}."]
        status = STATUS_BLOCKED
    return ComponentResult(name=name, status=status, mandatory=mandatory, detail=detail)


# Human-facing component names for the Priority 4/5/6 additions.
MACHINE_CONFIG_COMPONENT_NAME = "Machine configuration (config/machine.local.yaml)"
INSTALLATION_COMPONENT_NAME = "Calee tablet release installation"
RELEASE_CONFIG_COMPONENT_NAME = "Release configuration (machine + release-candidate composition)"


def component_from_release_config_report(
    name: str, report_dict: "dict[str, Any] | None", *, mandatory: bool = True
) -> ComponentResult:
    """Build a ComponentResult from the per-run release-config composition
    (Priority 1/3): the ONE effective configuration merging the machine (how/
    where) with the release candidate (what), including every conflict
    decision (profile disagreement, backend pin mismatch, missing required
    platform capability, ...). A composition that reports ``ok`` is PASS; any
    other status (a real conflict, or a missing/unreadable release-platforms.yaml)
    BLOCKS -- a release-config conflict is a setup/configuration blocker, never a
    product FAIL, but it must gate the release exactly like machine-config and
    installation: this is a pre-product gate, and no product test may run, or
    read as contributing to a PASS, once it is BLOCKED."""
    if report_dict is None:
        return ComponentResult(
            name=name, status=STATUS_NOT_RUN, mandatory=mandatory,
            detail=["No release-configuration composition was recorded for this run."],
        )
    status = report_dict.get("status")
    detail = list(report_dict.get("detail", []))
    if status == "ok":
        return ComponentResult(name=name, status=STATUS_PASS, mandatory=mandatory, detail=detail, evidence=report_dict)
    if status not in (STATUS_BLOCKED, "blocked", "invalid"):
        detail = detail + [f"Unrecognized release-config status {report_dict.get('status')!r}."]
    return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=mandatory, blocked=1, detail=detail, evidence=report_dict)


SUBSCRIBED_FIXTURE_COMPONENT_NAME = "Subscribed-calendar fixture"


def component_from_subscribed_fixture_report(
    name: str, report_dict: "dict[str, Any] | None", *, mandatory: bool = False
) -> ComponentResult:
    """Build a ComponentResult from the subscribed-fixture component
    (Priority 7): publication + bounded-polling observation evidence for the
    today-relative ICS (published mode), or the fixed-date/offline-only mode
    record (subscribed_publisher.SubscribedFixtureResult).

    ``mandatory`` defaults to False -- optional while scenarios/subscribed_
    calendar.yaml stays draft-unverified (Priority 7: "while the scenario
    remains draft, the component may be optional"). The caller (cli.py's
    consolidate) passes True once that scenario's promotion file records
    releaseSuiteEligible: true -- the component then automatically becomes
    mandatory, exactly like every other release-gating component.

    A ``status: "ok"`` report is PASS; any other status (a real BLOCKED
    publication/observation failure, or a missing/unreadable report) BLOCKS --
    an unrecognized status is never silently trusted as a pass."""
    if report_dict is None:
        return ComponentResult(
            name=name, status=STATUS_NOT_RUN, mandatory=mandatory,
            detail=["No subscribed-fixture evidence was recorded for this run."],
        )
    status = report_dict.get("status")
    detail = list(report_dict.get("detail", []))
    if status == "ok":
        return ComponentResult(name=name, status=STATUS_PASS, mandatory=mandatory, detail=detail, evidence=report_dict)
    if status != "blocked":
        detail = detail + [f"Unrecognized subscribed-fixture status {report_dict.get('status')!r}."]
    return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=mandatory, blocked=1, detail=detail, evidence=report_dict)


def component_from_machine_config_report(
    name: str, report_dict: "dict[str, Any] | None", *, mandatory: bool = True
) -> ComponentResult:
    """Build a ComponentResult from the per-run machine-config snapshot
    (Priority 4). The snapshot records the single authoritative configuration
    resolved for this run (backend, devices, package ids, release profile),
    with secrets excluded. A loaded+valid snapshot is PASS; a missing/invalid
    one BLOCKS -- the run's authoritative config was never established, so the
    release cannot be certified. There is no product "fail" here.
    """
    if report_dict is None:
        return ComponentResult(
            name=name, status=STATUS_NOT_RUN, mandatory=mandatory,
            detail=["No machine-configuration snapshot was recorded for this run."],
        )
    status = report_dict.get("status")
    detail = list(report_dict.get("detail", []))
    if status == "ok":
        return ComponentResult(name=name, status=STATUS_PASS, mandatory=mandatory, detail=detail, evidence=report_dict)
    if status not in (STATUS_BLOCKED, "blocked", "invalid"):
        detail = detail + [f"Unrecognized machine-config status {report_dict.get('status')!r}."]
    return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=mandatory, blocked=1, detail=detail, evidence=report_dict)


def component_from_installation_report(
    name: str, report_dict: "dict[str, Any] | None", *, mandatory: bool = True
) -> ComponentResult:
    """Build a ComponentResult from the run-scoped installation report
    (Priority 5/6): bundle verification + actual APK content/signer inspection +
    tablet pre-install inspection + install plan + execution + post-install
    verification, all under reports/runs/<run-id>/installation/results.json.

    Mapping (installer vocabulary -> release vocabulary):
      * ``ok``            -> PASS (the release was installed and verified);
      * ``fail``          -> FAIL (a genuine product-level install failure);
      * anything else     -> BLOCKED (bundle invalid, SDK tool missing, signer
                             mismatch, no device/adb, version/HOME mismatch).

    Installation is release-gating: a non-``ok`` install can never read as a
    release PASS, and a signer/version mismatch is BLOCKED, never 'fixed' by
    wiping data.
    """
    if report_dict is None:
        return ComponentResult(
            name=name, status=STATUS_NOT_RUN, mandatory=mandatory,
            detail=["Tablet release installation was not executed for this run."],
        )
    status = report_dict.get("status")
    detail = list(report_dict.get("detail", []))
    if status in ("ok", STATUS_PASS):
        return ComponentResult(name=name, status=STATUS_PASS, mandatory=mandatory, passed=1, detail=detail, evidence=report_dict)
    if status in ("fail", STATUS_FAIL):
        return ComponentResult(name=name, status=STATUS_FAIL, mandatory=mandatory, failed=1, detail=detail, evidence=report_dict)
    if status not in (STATUS_BLOCKED, "blocked", "invalid"):
        detail = detail + [f"Unrecognized installation status {report_dict.get('status')!r}."]
    return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=mandatory, blocked=1, detail=detail, evidence=report_dict)


def _normalize_backend(value: "Any | None") -> "str | None":
    """Normalize a backend URL for equality: trimmed, lower-cased, and with a
    single trailing slash removed, so `https://Hub.calee.com.au/` and
    `https://hub.calee.com.au` compare equal. Empty/whitespace -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("/"):
        text = text[:-1]
    return text.lower()


# Backend-evidence match verdicts (see backend_match_status). Distinct from
# the ComponentResult STATUS_* namespace above.
BACKEND_MATCH = "match"
BACKEND_MISMATCH = "mismatch"
BACKEND_MISSING_RESOLVED = "missing_resolved"
BACKEND_NO_EVIDENCE = "no_evidence"


def backend_match_status(
    requested: "Any | None", resolved: "Any | None", fixture: "Any | None"
) -> str:
    """Independently classify a mobile per-platform backend triple, WITHOUT
    trusting the mobile runner's exit code:

      * ``match``            -- every non-empty value agrees (and a resolved
                                backend is present -- the ground truth of what
                                the app actually talked to).
      * ``mismatch``         -- a resolved backend is present but at least one
                                other non-empty value disagrees with it.
      * ``missing_resolved`` -- something was requested/fixtured but the app
                                never reported a resolved backend, so we can't
                                certify which backend was actually tested.
      * ``no_evidence``      -- no backend value at all.

    Only ``match`` is safe for a release; the rest BLOCK for a mandatory
    platform (see backend_evidence_component)."""
    norm_resolved = _normalize_backend(resolved)
    non_empty = [
        v for v in (_normalize_backend(requested), norm_resolved, _normalize_backend(fixture))
        if v is not None
    ]
    if norm_resolved is None:
        return BACKEND_MISSING_RESOLVED if non_empty else BACKEND_NO_EVIDENCE
    return BACKEND_MATCH if len(set(non_empty)) == 1 else BACKEND_MISMATCH


def backend_evidence_component(
    name: str,
    report_dict: "dict[str, Any] | None",
    *,
    mandatory: bool,
    build_version: "str | None" = None,
    git_sha: "str | None" = None,
) -> "ComponentResult | None":
    """Build a release-gating backend-evidence component from a mobile
    per-platform UI report (CaleeMobile-Regression's run_ui_suite.py, which
    always records a ``backend`` block: requested/resolved/fixture -- see that
    repo's _write_report).

    Returns None when there is nothing to verify: the platform wasn't run
    (report_dict is None -- the platform's own component already renders that
    as NOT_RUN/blocked), or the report predates the backend-evidence contract
    (no ``backend`` key at all). A real release report always carries the
    ``backend`` block, so a real run is always verified here.

    This is the independent check Phase 4 requires: even if the mobile UI
    suite itself exited 0, a backend mismatch or a missing resolved backend
    for a mandatory platform BLOCKS the release. ``build_version``/``git_sha``
    are the consolidate-level CaleeMobile identity, used when the report
    doesn't embed its own -- they are surfaced in the evidence for audit, not
    used in the match decision."""
    if report_dict is None or "backend" not in report_dict:
        return None
    backend = report_dict.get("backend") or {}
    requested = backend.get("requested")
    resolved = backend.get("resolved")
    fixture = backend.get("fixture")
    match_status = backend_match_status(requested, resolved, fixture)
    status = STATUS_PASS if match_status == BACKEND_MATCH else STATUS_BLOCKED
    evidence = {
        "requested": requested,
        "resolved": resolved,
        "fixture": fixture,
        "matchStatus": match_status,
        "deviceId": report_dict.get("deviceId"),
        "buildVersion": report_dict.get("buildVersion") or build_version,
        "gitSha": report_dict.get("gitSha") or git_sha,
    }
    if match_status == BACKEND_MISMATCH:
        detail = [
            f"Backend mismatch: requested={requested!r}, resolved={resolved!r}, "
            f"fixture={fixture!r} -- the app did not talk to the prepared fixture backend."
        ]
    elif match_status in (BACKEND_MISSING_RESOLVED, BACKEND_NO_EVIDENCE):
        detail = [
            f"No resolved backend recorded (requested={requested!r}, fixture={fixture!r}) "
            f"-- cannot certify which backend was actually tested."
        ]
    else:
        detail = [f"Backend verified: {resolved} (requested/resolved/fixture agree)."]
    return ComponentResult(
        name=name, status=status, mandatory=mandatory, detail=detail, evidence=evidence,
    )


def component_from_manual_checks(checks: "list[ManualCheck]", *, name: str = "manual checks") -> ComponentResult:
    if not checks:
        return ComponentResult(name=name, status=STATUS_NOT_RUN, mandatory=True, detail=["No manual checks recorded."])
    failed = sum(1 for c in checks if c.mandatory and c.status == STATUS_FAIL)
    blocked = sum(1 for c in checks if c.mandatory and c.status in (STATUS_BLOCKED, None))
    passed = sum(1 for c in checks if c.status == STATUS_PASS)
    status = decide_status(passed=passed, failed=failed, blocked=blocked, total=len(checks))
    detail = [
        f"{c.title}: {c.status or 'not recorded'}" + (f" ({c.note})" if c.note else "")
        for c in checks
        if c.mandatory and c.status != STATUS_PASS
    ]
    return ComponentResult(name=name, status=status, mandatory=True, passed=passed, failed=failed, blocked=blocked, detail=detail)


# The per-flow status strings a sync-smoke report uses (see
# calee_regression/sync_smoke.py). A different namespace from this module's
# lowercase ComponentResult STATUS_* above -- named separately so the mapping
# in component_from_sync_report is never confused for a ComponentResult status.
SYNC_FLOW_OK = "ok"
SYNC_FLOW_FAILED = "failed"
SYNC_FLOW_BLOCKED = "blocked"

SYNC_COMPONENT_NAME = "CaleeMobile cross-device synchronization"

SELECTOR_CONTRACT_COMPONENT_NAME = "CaleeMobile selector contract"


def component_from_selector_contract(
    name: str,
    report_dict: "dict[str, Any] | None",
    *,
    mandatory: bool = True,
    expected_git_sha: "str | None" = None,
    expected_version: "str | None" = None,
    expected_flutter_version: "str | None" = None,
    expected_release_run_id: "str | None" = None,
    component_dir: "Any | None" = None,
    expected_envelope_digest: "str | None" = None,
    now: "Any | None" = None,
) -> ComponentResult:
    """Build a ComponentResult from the recorded selector-contract gate report
    (Priority 1).

    ``expected_envelope_digest`` (Priority 7.8) is an OPTIONAL trusted provenance
    envelope digest anchored OUTSIDE the mutable bundle (a signed/immutable run
    manifest or release config). When supplied it is enforced against the
    recorded ``provenance.envelopeDigest`` so a coordinated re-hash of the bundle
    is caught -- a local envelope checksum alone cannot detect that.

    The ``selector-contract`` command (release gate) writes
    ``reports/runs/<run-id>/selector-contract/results.json`` with the raw
    machine-readable selector evidence embedded under ``evidence`` (plus release
    provenance). This re-validates that embedded evidence INDEPENDENTLY here --
    it does not merely trust the recorded ``status`` -- so a tampered report that
    claims ``status: passed`` but embeds evidence for the wrong SHA/version, on
    the wrong Flutter toolchain, with inconsistent counts, or with a
    stale/future/invalid timestamp still BLOCKS at consolidation. This is the
    second, independent gate; the first is the launcher's fail-fast BEFORE the
    mobile functional tests.

    Result rules (mirroring the rest of this module and docs/RELEASE_POLICY.md):
      * report absent (None) -> NOT_RUN (blocks when mandatory -- a release must
        never PASS without selector evidence);
      * evidence missing/malformed, or failing verification (wrong build,
        wrong toolchain, not PASS, missing selector, inconsistent counts,
        bad/stale timestamp, missing provenance) -> BLOCKED;
      * evidence valid for the expected build -> PASS.
    """
    from . import selector_evidence as se
    from . import selector_provenance as sp

    if expected_flutter_version is None:
        expected_flutter_version = se.EXPECTED_FLUTTER_VERSION

    if report_dict is None:
        return ComponentResult(
            name=name, status=STATUS_NOT_RUN, mandatory=mandatory,
            detail=["Not executed -- no selector-contract evidence recorded for this run."],
        )

    # Immutable source-provenance + adoption record (Priority 1, Problem B).
    # When present, the source evidence is the authoritative view, and the
    # record is independently re-verified here: its content digest is recomputed
    # (tampering with any field after adoption BLOCKS), its provenance rules are
    # re-checked, and the run scope is enforced via the adoption block -- NOT by
    # requiring the source artifact to carry this run's ID in its own fields.
    provenance = report_dict.get("provenance") if isinstance(report_dict.get("provenance"), dict) else None
    prov_problems: "list[str]" = []
    if provenance is not None:
        # Priority 3.6: recompute ALL digests at consolidation. The envelope +
        # semantic content digests are always recomputed; when the raw bundle
        # files are on disk (the GitHub artifact chain wrote them), their
        # raw-byte digests are re-hashed against the record too, so altering a
        # preserved byte after adoption BLOCKS here.
        result_bytes = None
        zip_bytes = None
        if component_dir is not None:
            from pathlib import Path as _Path

            cdir = _Path(component_dir)
            rp = cdir / sp.BUNDLE_RESULT_JSON
            zp = cdir / sp.BUNDLE_ARTIFACT_ZIP
            if rp.is_file():
                try:
                    result_bytes = rp.read_bytes()
                except OSError:
                    result_bytes = None
            if zp.is_file():
                try:
                    zip_bytes = zp.read_bytes()
                except OSError:
                    zip_bytes = None
        try:
            prov_problems = sp.verify_provenance_record(
                provenance, result_bytes=result_bytes, zip_bytes=zip_bytes,
                trusted_envelope_digest=expected_envelope_digest,
            )
        except sp.ProvenanceError as exc:
            prov_problems = [str(exc)]
        adoption = provenance.get("adoption") if isinstance(provenance.get("adoption"), dict) else {}
        adopted_run = str((adoption or {}).get("releaseRunId") or "").strip()
        if expected_release_run_id is not None:
            if not adopted_run:
                prov_problems.append("provenance adoption has no releaseRunId -- evidence is not tied to this release run.")
            elif adopted_run != str(expected_release_run_id).strip():
                prov_problems.append(
                    f"adoption releaseRunId {adopted_run!r} != current release run "
                    f"{str(expected_release_run_id).strip()!r} -- this evidence was adopted by a different run."
                )

    evidence = sp.source_evidence_of(provenance) if provenance is not None else report_dict.get("evidence")
    if not isinstance(evidence, dict):
        return ComponentResult(
            name=name, status=STATUS_BLOCKED, mandatory=mandatory, blocked=1,
            detail=["Selector-contract report has no embedded evidence to verify."],
        )

    try:
        result = se.parse_selector_contract_result(evidence)
    except se.SelectorEvidenceError as exc:
        # A digest mismatch (tampering) is still surfaced even when the tampered
        # evidence is now unparseable -- prov_problems is not lost here.
        return ComponentResult(
            name=name, status=STATUS_BLOCKED, mandatory=mandatory, blocked=1,
            detail=list(prov_problems) + [f"Selector-contract evidence is malformed: {exc}"],
        )

    # With the new provenance model, run provenance is enforced via the adoption
    # record above; build identity is verified directly against the preserved
    # source evidence. Legacy reports (no provenance record) keep the embedded
    # release-provenance requirement so older evidence is not silently relaxed.
    verdict = se.verify_selector_contract_evidence(
        result,
        expected_git_sha=expected_git_sha,
        expected_version=expected_version,
        expected_flutter_version=expected_flutter_version,
        expected_release_run_id=None if provenance is not None else expected_release_run_id,
        require_release_provenance=provenance is None,
        now=now,
    )
    evidence_summary = {
        "testedSha": result.tested_sha,
        "pubspecVersion": result.pubspec_version,
        "flutterVersion": result.flutter_version,
        "contract": result.contract,
        "selectorsChecked": result.selectors_checked,
        "selectorsPresent": result.selectors_present,
        "timestamp": result.timestamp,
        "releaseRunId": result.release_run_id,
        "workflowRunId": result.workflow_run_id,
        "generatedBy": result.generated_by,
        "regressionSha": result.regression_sha,
    }
    if verdict.ok and not prov_problems:
        detail = [
            f"Selector contract PASS for CaleeMobile {result.pubspec_version} @ {result.tested_sha} "
            f"({result.selectors_present}/{result.selectors_checked} selectors present, "
            f"Flutter {result.flutter_version})."
        ]
        return ComponentResult(
            name=name, status=STATUS_PASS, mandatory=mandatory, passed=1,
            detail=detail, evidence=evidence_summary,
        )
    return ComponentResult(
        name=name, status=STATUS_BLOCKED, mandatory=mandatory, blocked=1,
        detail=list(prov_problems) + list(verdict.problems), evidence=evidence_summary,
    )


def component_from_sync_report(
    name: str, report_dict: "dict[str, Any] | None", *, mandatory: bool = True
) -> ComponentResult:
    """Build a ComponentResult from a sync-smoke report (Workstream 1).

    The report is either the real flows shape written by the ``sync-smoke``
    command -- ``{"runId": ..., "flows": [{"flow": ..., "status":
    ok|failed|blocked, ...}, ...]}`` -- or a marker the command/launcher wrote
    when it could not (or was asked not to) run the flows: no in-scope mobile
    platform, missing verified backend/credentials, or an intentionally
    excluded (optional) release, carrying an explicit top-level ``status``.

    Result rules (mirroring the rest of this module and docs/RELEASE_POLICY.md):
      * report absent (None) -> NOT_RUN (blocks the release when mandatory --
        a missing mandatory sync must never read as a pass by omission);
      * a marker report with no flows -> its explicit top-level ``status`` (an
        unrecognized/absent one is BLOCKED, never a silent pass);
      * any flow FAILED -> FAIL (a real cross-device sync regression);
      * any flow BLOCKED (e.g. the tablet-mutation gap) -> BLOCKED;
      * flows present and all OK -> PASS.

    A mandatory sync that is missing, stale/rejected (handled upstream by
    run-ID validation, which turns a rejected report into report_dict=None here),
    BLOCKED, or FAILED therefore all prevent an overall PASS.
    """
    if report_dict is None:
        return ComponentResult(name=name, status=STATUS_NOT_RUN, mandatory=mandatory, detail=["Not executed."])
    flows = report_dict.get("flows") or []
    if not flows:
        # A marker report: trust the explicit status it recorded. An
        # unrecognized or absent status is treated as BLOCKED (never silently
        # trusted), the same defensive default component_from_environment_report
        # applies to an unrecognized environment status.
        recorded = report_dict.get("status")
        status = recorded if recorded in (STATUS_PASS, STATUS_FAIL, STATUS_BLOCKED, STATUS_NOT_RUN) else STATUS_BLOCKED
        detail = list(report_dict.get("detail", [])) or ["No synchronization flows were run."]
        return ComponentResult(
            name=name, status=status, mandatory=mandatory,
            blocked=1 if status == STATUS_BLOCKED else 0,
            failed=1 if status == STATUS_FAIL else 0,
            detail=detail,
        )
    passed = sum(1 for f in flows if f.get("status") == SYNC_FLOW_OK)
    failed = sum(1 for f in flows if f.get("status") == SYNC_FLOW_FAILED)
    blocked = sum(1 for f in flows if f.get("status") == SYNC_FLOW_BLOCKED)
    status = decide_status(passed=passed, failed=failed, blocked=blocked, total=len(flows))
    detail = [
        f"{f.get('flow', '?')}: {str(f.get('status', '?')).upper()}"
        for f in flows
        if f.get("status") != SYNC_FLOW_OK
    ]
    return ComponentResult(
        name=name, status=status, mandatory=mandatory,
        passed=passed, failed=failed, blocked=blocked, detail=detail,
    )


# Independent release-feature components (Workstream 3). Each declared release
# feature gets its OWN consolidated component built strictly from the test steps
# tagged with that feature -- never inferred from the broad Android/iOS (or
# tablet) component passing. The keys match config/release-platforms.yaml's
# release_features and the CALEE_RELEASE_FEATURE_* propagation (Workstream 1),
# and the per-step `feature` tag run_ui_suite.py / the kiosk-admin command emit.
FEATURE_COMPONENT_NAMES = {
    "meals": "CaleeMobile Meals",
    "onboarding": "Calee onboarding and display/mobile handoff",
    "google_calendar": "Google Calendar connection",
    "kiosk_admin": "CaleeShell kiosk/admin",
}


def _feature_evidence_source(report_dict: "dict[str, Any] | None", platform_label: "str | None") -> "tuple[list, dict]":
    """Extract (steps, context) from a component report for feature-evidence
    scanning. `context` records the device/platform, build SHA and backend so
    the feature component can show exactly which surface produced the evidence."""
    if not report_dict:
        return [], {}
    backend = (report_dict.get("backend") or {}).get("resolved")
    identity = report_dict.get("buildIdentity") or {}
    context = {
        "platform": report_dict.get("platform") or platform_label,
        "deviceId": report_dict.get("deviceId"),
        "buildSha": identity.get("gitSha"),
        "backend": backend,
        "reportPath": report_dict.get("reportPath"),
    }
    return list(report_dict.get("steps") or []), context


def component_from_feature_evidence(
    feature_key: str,
    name: str,
    *,
    mandatory: bool,
    sources: "list[tuple[dict[str, Any] | None, str | None]]",
) -> ComponentResult:
    """Build an INDEPENDENT per-feature component from feature-tagged step
    evidence (Workstream 3).

    ``sources`` is a list of ``(report_dict, platform_label)`` -- the in-scope
    reports whose steps might carry ``step["feature"] == feature_key`` (the
    mobile Android/iOS UI reports for meals/onboarding/google_calendar; the
    kiosk-admin report for kiosk_admin). Only steps tagged with this exact
    feature are used as evidence: a feature's PASS is NEVER inferred from the
    broad platform component passing.

    Result rules (mirroring the rest of this module and docs/RELEASE_POLICY.md):
      * no matching step evidence at all -> NOT_RUN (blocks when mandatory -- a
        mandatory feature with no evidence must never read as a pass by
        omission, and must never silently become an optional skip);
      * any tagged step FAILED -> FAIL (a real product regression in the
        feature);
      * any tagged step BLOCKED, or a mandatory tagged SKIP -> BLOCKED (an unmet
        prerequisite, e.g. the feature was unavailable and correctly reported
        ENVIRONMENT_BLOCKED/FIXTURE_MISSING);
      * tagged steps present with at least one PASS and no fail/block -> PASS.

    The `evidence` block records the configured applicability, the exact steps
    used, the device/platform(s), build SHA(s), backend(s), any BLOCKED
    prerequisite, and screenshot/report references where available.
    """
    applicability = "mandatory" if mandatory else "optional"
    matched: "list[dict]" = []
    contexts: "list[dict]" = []
    for report_dict, platform_label in sources:
        steps, context = _feature_evidence_source(report_dict, platform_label)
        feature_steps = [s for s in steps if s.get("feature") == feature_key]
        if feature_steps:
            contexts.append(context)
        for step in feature_steps:
            matched.append({
                "name": step.get("name", "?"),
                "status": step.get("status", "?"),
                "mandatory": step.get("mandatory", True),
                "skipCategory": step.get("skipCategory"),
                "platform": context.get("platform"),
                "deviceId": context.get("deviceId"),
                "detail": step.get("detail", ""),
                "screenshot": step.get("screenshot") or step.get("screenshotRef"),
            })

    def _evidence(status: str, blocked_prereq: "list[str]") -> dict:
        return {
            "feature": feature_key,
            "applicability": applicability,
            "executionStatus": status,
            "steps": matched,
            "platforms": sorted({c.get("platform") for c in contexts if c.get("platform")}),
            "devices": sorted({c.get("deviceId") for c in contexts if c.get("deviceId")}),
            "buildShas": sorted({c.get("buildSha") for c in contexts if c.get("buildSha")}),
            "backends": sorted({c.get("backend") for c in contexts if c.get("backend")}),
            "reportPaths": sorted({c.get("reportPath") for c in contexts if c.get("reportPath")}),
            "screenshots": [m["screenshot"] for m in matched if m.get("screenshot")],
            "blockedPrerequisite": blocked_prereq,
        }

    if not matched:
        # No feature-tagged evidence anywhere. A mandatory feature with no
        # evidence is NOT_RUN (blocks); an optional/excluded one is shown as an
        # explicit not-run so it's never silently omitted, but does not gate.
        detail = [
            f"No test step tagged for the '{feature_key}' feature was found in any in-scope "
            f"report. A {applicability} feature's result is derived only from its own tagged "
            f"steps -- never inferred from the broad platform component passing."
        ]
        return ComponentResult(
            name=name, status=STATUS_NOT_RUN, mandatory=mandatory, detail=detail,
            evidence=_evidence(STATUS_NOT_RUN, detail),
        )

    # Mobile UI / kiosk report step statuses are the uppercase raw namespace
    # ("PASS"/"FAIL"/"BLOCKED"/"SKIP"), distinct from this module's lowercase
    # ComponentResult statuses -- compare against the raw literals here.
    passed = sum(1 for s in matched if s["status"] == "PASS")
    failed = sum(1 for s in matched if s["status"] == "FAIL")
    blocked_steps = sum(1 for s in matched if s["status"] == "BLOCKED")
    mandatory_skipped = sum(1 for s in matched if s["status"] == STATUS_SKIP_RAW and s.get("mandatory", True))
    if passed == 0 and failed == 0 and blocked_steps == 0 and mandatory_skipped == 0:
        # The only evidence is optional skips (an optional/excluded feature that
        # was intentionally not exercised) -- that is NOT_RUN, not BLOCKED. A
        # mandatory feature can't legitimately reach here (an unavailable
        # mandatory feature is a mandatory skip -> mandatory_skipped>0 above);
        # if it somehow did, NOT_RUN still blocks it, so this never launders a
        # mandatory feature into a pass.
        status = STATUS_NOT_RUN
    else:
        status = decide_status(
            passed=passed, failed=failed, blocked=blocked_steps + mandatory_skipped, total=len(matched),
        )
    non_pass = [
        f"[{s.get('platform') or '?'}] {s['name']}: {s['status']}"
        + (f" — {s['detail']}" if s.get("detail") else "")
        for s in matched
        if s["status"] != "PASS"
    ]
    blocked_prereq = [
        f"[{s.get('platform') or '?'}] {s['name']}: {s['detail']}"
        for s in matched
        if s["status"] == "BLOCKED" or (s["status"] == STATUS_SKIP_RAW and s.get("mandatory", True))
    ]
    return ComponentResult(
        name=name, status=status, mandatory=mandatory,
        passed=passed, failed=failed, blocked=blocked_steps, skipped=sum(1 for s in matched if s["status"] == STATUS_SKIP_RAW),
        detail=non_pass, evidence=_evidence(status, blocked_prereq),
    )


# The identity fields that must not change between the pre-run and post-run
# snapshots for an in-scope app. A change in any of these during the run means
# the thing that was tested is not the thing being certified. See Phase 4.
_IDENTITY_FIELDS_CALEEMOBILE = ("gitSha", "buildVersion", "dirty")
_IDENTITY_FIELDS_TABLET = ("applicationId", "buildVersion", "versionCode", "gitSha")


def component_from_identity_stability(
    pre: "dict[str, Any] | None",
    post: "dict[str, Any] | None",
    *,
    require_caleemobile: bool,
    require_calee: bool,
    name: str = "Build identity stability (pre/post run)",
) -> "ComponentResult | None":
    """Compare the pre-run and post-run build-identity snapshots (Phase 4).

    An in-scope app whose identity changed during the run BLOCKS the release:
    the CaleeMobile source SHA / build changed, or the installed tablet
    package's applicationId / versionName / versionCode / SHA changed while it
    was under test -- so what was tested is not what is being certified.

    Snapshots are ``{"caleemobile": {...to_dict...}, "tablet": {...}}`` as
    written by the ``build-identity --phase pre|post`` command. Returns None
    when neither snapshot was captured (legacy/ad-hoc consolidation with no
    identity evidence -- the full launcher always captures both). When exactly
    one snapshot is present the capture is incomplete, which BLOCKS.
    """
    if pre is None and post is None:
        return None
    if pre is None or post is None:
        which = "post-run" if pre is not None else "pre-run"
        return ComponentResult(
            name=name, status=STATUS_BLOCKED, mandatory=True,
            detail=[
                f"Incomplete build-identity capture: the {which} snapshot is missing "
                f"-- cannot prove the build was stable across the run."
            ],
        )
    checks = []
    if require_caleemobile:
        checks.append(("CaleeMobile", "caleemobile", _IDENTITY_FIELDS_CALEEMOBILE))
    if require_calee:
        checks.append(("Calee tablet", "tablet", _IDENTITY_FIELDS_TABLET))
    changed = []
    for label, key, fields in checks:
        pre_app = pre.get(key) or {}
        post_app = post.get(key) or {}
        for field_name in fields:
            pre_value = pre_app.get(field_name)
            post_value = post_app.get(field_name)
            if pre_value != post_value:
                changed.append(
                    f"{label} {field_name} changed during the run: "
                    f"{pre_value!r} -> {post_value!r}"
                )
    status = STATUS_BLOCKED if changed else STATUS_PASS
    return ComponentResult(name=name, status=status, mandatory=True, detail=changed)


def component_from_caleemobile_sha_agreement(
    values: "dict[str, Any]",
    *,
    required: bool,
    name: str = "CaleeMobile commit SHA agreement",
) -> "ComponentResult | None":
    """Cross-check every CaleeMobile Git SHA the run observed (Phase 5).

    ``values`` maps a human label ("Android UI report", "iPhone UI report",
    "pre-run", "post-run", "expected release", "detected") to the SHA seen
    there (or None when that source didn't provide one). All non-empty values
    must be the same full SHA:

      * required but no SHA present anywhere -> BLOCKED (nothing to certify);
      * any present SHA is abbreviated/ambiguous -> BLOCKED;
      * two present SHAs disagree -> BLOCKED (which build was really tested?);
      * otherwise -> PASS.

    Returns None when nothing is in scope and no SHA is present (nothing to
    say). This is what makes the exact commit -- embedded into each Android/
    iOS UI report at execution time, plus the pre/post snapshots and the
    expected release SHA -- all agree before a release can PASS.
    """
    present = {label: str(v).strip() for label, v in values.items() if v}
    if not present:
        if required:
            return ComponentResult(
                name=name, status=STATUS_BLOCKED, mandatory=True,
                detail=["No CaleeMobile Git SHA was recorded anywhere -- cannot certify which commit was tested."],
            )
        return None
    abbreviated = {label: sha for label, sha in present.items() if not is_full_git_sha(sha)}
    if abbreviated:
        detail = [
            f"{label}: {sha!r} is abbreviated/ambiguous (need the full 40-character SHA)."
            for label, sha in sorted(abbreviated.items())
        ]
        return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=True, detail=detail,
                               evidence=dict(present))
    distinct = sorted(set(present.values()))
    if len(distinct) > 1:
        detail = [f"{label}: {sha}" for label, sha in sorted(present.items())]
        detail.insert(0, "CaleeMobile Git SHA disagreement across sources -- the wrong commit may have been tested:")
        return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=True, detail=detail,
                               evidence=dict(present))
    return ComponentResult(
        name=name, status=STATUS_PASS, mandatory=True,
        detail=[f"All recorded CaleeMobile SHAs agree: {distinct[0]}."],
        evidence=dict(present),
    )


def _waiver_is_valid(waiver: "dict | None") -> bool:
    """A dirty-tree waiver is valid only when it names WHY, WHO, and WHEN
    (reason, approver, timestamp all non-empty). See Workstream 3 / Waiver."""
    if not isinstance(waiver, dict):
        return False
    return all(str(waiver.get(k, "") or "").strip() for k in ("reason", "approver", "timestamp"))


def component_from_release_intent(
    *,
    production: bool,
    caleemobile_in_scope: bool,
    tablet_in_scope: bool,
    caleeshell_in_scope: bool,
    expected_caleemobile_build_version: "str | None" = None,
    expected_caleemobile_git_sha: "str | None" = None,
    expected_calee_build_version: "str | None" = None,
    expected_calee_git_sha: "str | None" = None,
    expected_calee_application_id: "str | None" = None,
    expected_calee_version_code: "str | None" = None,
    expected_caleeshell_version: "str | None" = None,
    detected_calee_application_id: "str | None" = None,
    detected_calee_version_code: "str | None" = None,
    detected_caleeshell_version: "str | None" = None,
    tablet_source_sha_available: bool = False,
    caleemobile_dirty: bool = False,
    calee_dirty: bool = False,
    waiver: "dict | None" = None,
    name: str = "Release identity intent (production)",
) -> "ComponentResult | None":
    """Prove the INTENDED release identity was stated up front (Workstream 3).

    For a production release profile the *expected* identity is required, not
    merely checked-if-present: an in-scope app whose expected SHA/version/package
    identity was never configured BLOCKS, because consistency of the observed
    build is not evidence of release *intent* -- the target must be stated. The
    expected/detected version+SHA *match* itself is enforced by
    component_from_build_identity (it already receives the expected values); this
    component adds the "must be configured at all" gate, the abbreviated-SHA gate
    on the expectations, the application-id/versionCode/CaleeShell match not
    covered there, and the dirty-tree waiver audit.

    Returns None for a non-production profile (nothing to enforce here).
    """
    if not production:
        return None

    evidence = {
        "profile": "production",
        "expectedCaleeMobileGitSha": expected_caleemobile_git_sha,
        "expectedCaleeMobileBuildVersion": expected_caleemobile_build_version,
        "expectedTabletVersionName": expected_calee_build_version,
        "expectedTabletApplicationId": expected_calee_application_id,
        "expectedTabletVersionCode": expected_calee_version_code,
        "expectedTabletGitSha": expected_calee_git_sha,
        "expectedCaleeShellVersion": expected_caleeshell_version,
    }
    evidence = {k: v for k, v in evidence.items() if v is not None}

    def blocked(detail: "list[str]") -> ComponentResult:
        return ComponentResult(name=name, status=STATUS_BLOCKED, mandatory=True, detail=detail, evidence=evidence)

    missing: "list[str]" = []
    abbreviated: "list[str]" = []
    # A present-but-unrecognisable expected version (``""`` slips through as
    # missing; ``"latest"``/``"0.3"`` do not) can never be safely matched against
    # a detected value, so it BLOCKS as its own class of misconfiguration rather
    # than silently "not matching" later (Workstream 2).
    malformed: "list[str]" = []
    if caleemobile_in_scope:
        if not expected_caleemobile_git_sha:
            missing.append("expected CaleeMobile Git SHA")
        elif not is_full_git_sha(expected_caleemobile_git_sha):
            abbreviated.append(f"expected CaleeMobile Git SHA {expected_caleemobile_git_sha!r}")
        if not expected_caleemobile_build_version:
            missing.append("expected CaleeMobile version/build")
        elif not is_wellformed_version(expected_caleemobile_build_version):
            malformed.append(f"expected CaleeMobile version/build {expected_caleemobile_build_version!r}")
    if tablet_in_scope:
        if not expected_calee_build_version:
            missing.append("expected tablet versionName")
        elif not is_wellformed_version(expected_calee_build_version):
            malformed.append(f"expected tablet versionName {expected_calee_build_version!r}")
        if not expected_calee_application_id:
            missing.append("expected tablet application id")
        if not expected_calee_version_code:
            missing.append("expected tablet versionCode")
        # The tablet source SHA is required only where the source/build pipeline
        # can actually provide it (a tablet source checkout was found and a SHA
        # detected) -- you cannot state an expectation the pipeline can't produce.
        if tablet_source_sha_available:
            if not expected_calee_git_sha:
                missing.append("expected tablet source Git SHA")
            elif not is_full_git_sha(expected_calee_git_sha):
                abbreviated.append(f"expected tablet source Git SHA {expected_calee_git_sha!r}")
    if caleeshell_in_scope:
        if not expected_caleeshell_version:
            missing.append("expected CaleeShell version")
        elif not is_wellformed_version(expected_caleeshell_version):
            malformed.append(f"expected CaleeShell version {expected_caleeshell_version!r}")

    if abbreviated:
        return blocked(
            [f"{a} is abbreviated/ambiguous; a production release requires the full 40-character SHA." for a in abbreviated]
        )
    if malformed:
        return blocked(
            [f"{m} is not a well-formed version identity; a production release requires a recognisable "
             f"version (e.g. 0.0.23+23, founder-v0.3.24)." for m in malformed]
        )
    if missing:
        return blocked([
            "Production release is missing required expected identity: "
            + ", ".join(missing)
            + ". Consistency of the observed build is not evidence of release intent -- state the intended target."
        ])

    # Match the expectations this component owns (application id / versionCode /
    # CaleeShell version) against the detected values. A missing detected value
    # or a mismatch BLOCKS (the wrong build may have been tested).
    mismatches: "list[str]" = []
    if tablet_in_scope:
        for label, expected_v, detected_v in (
            ("tablet application id", expected_calee_application_id, detected_calee_application_id),
            ("tablet versionCode", expected_calee_version_code, detected_calee_version_code),
        ):
            if not detected_v:
                mismatches.append(f"{label}: expected {expected_v!r} but none was detected")
            elif not _versions_match(detected_v, expected_v):
                mismatches.append(f"{label}: expected {expected_v!r} but detected {detected_v!r}")
    if caleeshell_in_scope:
        if not detected_caleeshell_version:
            mismatches.append(f"CaleeShell version: expected {expected_caleeshell_version!r} but none was detected")
        elif not _versions_match(detected_caleeshell_version, expected_caleeshell_version):
            mismatches.append(
                f"CaleeShell version: expected {expected_caleeshell_version!r} but detected {detected_caleeshell_version!r}"
            )
    if mismatches:
        return blocked(["The wrong build may have been tested -- " + "; ".join(mismatches) + "."])

    # A dirty tree in a production release needs a named waiver.
    dirty_apps = []
    if caleemobile_in_scope and caleemobile_dirty:
        dirty_apps.append("CaleeMobile")
    if tablet_in_scope and calee_dirty:
        dirty_apps.append("Calee tablet")
    if dirty_apps:
        if not _waiver_is_valid(waiver):
            return blocked([
                f"{' and '.join(dirty_apps)} build has uncommitted changes; a production release requires a named "
                f"waiver (reason, approver, timestamp) to approve a dirty tree. None (or an incomplete one) was provided."
            ])
        evidence["waiver"] = {
            "reason": waiver.get("reason"), "approver": waiver.get("approver"), "timestamp": waiver.get("timestamp"),
        }
        return ComponentResult(
            name=name, status=STATUS_PASS, mandatory=True, evidence=evidence,
            detail=[
                f"Intended release identity is fully specified. Dirty tree ({' and '.join(dirty_apps)}) approved by "
                f"waiver: {waiver.get('approver')} at {waiver.get('timestamp')} -- {waiver.get('reason')}."
            ],
        )

    return ComponentResult(
        name=name, status=STATUS_PASS, mandatory=True, evidence=evidence,
        detail=["Intended release identity is fully specified (expected SHA/version/package identity configured and matched)."],
    )


def build_release_report(
    *,
    environment: "dict[str, Any] | None" = None,
    tablet: "dict[str, Any] | None" = None,
    mobile_api: "dict[str, Any] | None" = None,
    mobile_android_ui: "dict[str, Any] | None" = None,
    mobile_ios_ui: "dict[str, Any] | None" = None,
    sync: "dict[str, Any] | None" = None,
    subscribed_fixture: "dict[str, Any] | None" = None,
    subscribed_fixture_mandatory: "bool | None" = None,
    kiosk_admin: "dict[str, Any] | None" = None,
    installation: "dict[str, Any] | None" = None,
    installation_mandatory: "bool | None" = None,
    machine_config: "dict[str, Any] | None" = None,
    machine_config_mandatory: "bool | None" = None,
    release_config: "dict[str, Any] | None" = None,
    release_config_mandatory: "bool | None" = None,
    feature_profile: "dict[str, bool] | None" = None,
    manual_checks: "list[ManualCheck] | None" = None,
    meta: "dict[str, Any] | None" = None,
    generated_at: "str | None" = None,
    android_mandatory: bool = True,
    ios_mandatory: bool = True,
    sync_mandatory: "bool | None" = None,
    selector_contract: "dict[str, Any] | None" = None,
    selector_contract_mandatory: "bool | None" = None,
    selector_contract_dir: "Any | None" = None,
    calee_build_version: "str | None" = None,
    expected_calee_build_version: "str | None" = None,
    caleemobile_build_version: "str | None" = None,
    expected_caleemobile_build_version: "str | None" = None,
    calee_git_sha: "str | None" = None,
    expected_calee_git_sha: "str | None" = None,
    caleemobile_git_sha: "str | None" = None,
    expected_caleemobile_git_sha: "str | None" = None,
    calee_dirty: bool = False,
    caleemobile_dirty: bool = False,
    calee_identity_available: bool = True,
    caleemobile_identity_available: bool = True,
    calee_version_code: "str | None" = None,
    calee_application_id: "str | None" = None,
    caleeshell_version: "str | None" = None,
    require_calee_identity: bool = False,
    require_calee_package_identity: bool = False,
    require_caleemobile_identity: bool = False,
    require_caleemobile_git_sha: bool = False,
    allow_dirty: bool = False,
    mobile_exit_floors: "dict[str, int | None] | None" = None,
    extra_components: "list[ComponentResult] | None" = None,
) -> ReleaseReport:
    """`android_mandatory`/`ios_mandatory` come from the technical owner's
    release-platform profile (calee_regression/release_platforms.py),
    never a hard-coded default here -- an omitted platform selection must
    default to mandatory=True (the release-gating, safe default), the same
    "default must be required" rule applied everywhere else in this
    framework. See docs/RELEASE_POLICY.md and Workstream 9.

    `expected_calee_build_version`/`expected_caleemobile_build_version` are
    optional technical-owner-configured expectations; when given, a
    mismatch against the detected `calee_build_version`/
    `caleemobile_build_version` BLOCKS the release (see
    component_from_build_version_match).

    `environment` (prepare's environment/results.json) is always
    mandatory -- unlike every other component here, there is no
    "environment is optional for this release" concept. See
    component_from_environment_report and Workstream 4.

    `sync_mandatory` (Workstream 1) controls the cross-device synchronization
    component: True -> release-gating (a missing/BLOCKED/FAILED sync prevents a
    PASS), False -> shown but optional, None -> not included at all (the
    legacy/ad-hoc caller doesn't deal with sync). The `consolidate` CLI always
    passes a concrete True/False from the release feature profile
    (release_features.synchronization), so a real release always includes the
    sync component -- never silently omitted -- while unit tests that don't
    exercise sync leave it None.
    """
    # A recorded exit-code floor (from the run manifest's worst-wins history)
    # can only make a mobile component worse, never better -- so a later run
    # can never overwrite an earlier FAIL's report with a PASS. See
    # _apply_exit_floor and run_context.worst_exit_code (Phase 3).
    floors = mobile_exit_floors or {}
    components = [
        component_from_environment_report("Test environment and regression fixture", environment, mandatory=True),
        component_from_tablet_report("Calee tablet", tablet, mandatory=True),
        _apply_exit_floor(
            component_from_api_report("CaleeMobile Client API", mobile_api, mandatory=True),
            floors.get("mobile-api"),
        ),
        _apply_exit_floor(
            component_from_api_report("CaleeMobile Android UI", mobile_android_ui, mandatory=android_mandatory),
            floors.get("mobile-android"),
        ),
        _apply_exit_floor(
            component_from_api_report("CaleeMobile iPhone UI", mobile_ios_ui, mandatory=ios_mandatory),
            floors.get("mobile-ios"),
        ),
        component_from_manual_checks(manual_checks or []),
    ]

    # CaleeMobile selector contract (Priority 1). A mobile-functional
    # precondition, so it reads right after the environment component and before
    # the tablet/mobile components. Included exactly when the caller made an
    # explicit mandatory/optional decision (selector_contract_mandatory is not
    # None); the consolidate CLI passes True for a real release run (and
    # auto-includes it whenever a selector-contract report exists), while unit
    # tests that don't exercise it leave it None. A mandatory selector contract
    # that is missing, malformed, for the wrong build, or otherwise invalid then
    # BLOCKS the overall status like any other mandatory component -- a release
    # can never PASS without valid selector evidence for the exact build.
    if selector_contract_mandatory is not None:
        sel_expected_sha = expected_caleemobile_git_sha or caleemobile_git_sha
        sel_expected_version = expected_caleemobile_build_version or caleemobile_build_version
        sel_release_run_id = (meta or {}).get("runId")
        components.insert(
            1,
            component_from_selector_contract(
                SELECTOR_CONTRACT_COMPONENT_NAME, selector_contract,
                mandatory=selector_contract_mandatory,
                expected_git_sha=sel_expected_sha,
                expected_version=sel_expected_version,
                expected_release_run_id=sel_release_run_id,
                component_dir=selector_contract_dir,
            ),
        )

    # Independent per-platform backend verification (Phase 4). Inserted right
    # after the platform UI components so the report reads platform -> its
    # backend evidence. This does NOT rely on the mobile runner's exit code:
    # a backend mismatch or a missing resolved backend for a mandatory
    # platform BLOCKS the release even if that platform's UI checks "passed".
    # `insert_at` is computed from the current list length (not a fixed index)
    # so it stays "just before manual checks" regardless of whether the
    # selector-contract component above was inserted.
    insert_at = len(components) - 1  # before manual checks (always the last base component)
    for evidence_name, evidence_report, evidence_mandatory in (
        ("CaleeMobile Android UI backend", mobile_android_ui, android_mandatory),
        ("CaleeMobile iPhone UI backend", mobile_ios_ui, ios_mandatory),
    ):
        evidence_component = backend_evidence_component(
            evidence_name, evidence_report, mandatory=evidence_mandatory,
            build_version=caleemobile_build_version, git_sha=caleemobile_git_sha,
        )
        if evidence_component is not None:
            components.insert(insert_at, evidence_component)
            insert_at += 1

    # Cross-device synchronization (Workstream 1). Inserted after the platform
    # UI + backend-evidence components and immediately BEFORE manual checks --
    # matching the execution order (...Android/iOS UI -> sync -> manual checks).
    # Included exactly when the caller made an explicit mandatory/optional
    # decision (sync_mandatory is not None); the CLI always does, so a real
    # release always shows a sync component. A mandatory sync that is missing,
    # BLOCKED or FAILED then gates the overall status like any other component.
    if sync_mandatory is not None:
        components.insert(
            insert_at,
            component_from_sync_report(SYNC_COMPONENT_NAME, sync, mandatory=sync_mandatory),
        )
        insert_at += 1

    # Subscribed-calendar fixture (Priority 7). Included exactly when the
    # caller made an explicit mandatory/optional decision; cli.py's
    # consolidate always does (optional while the scenario stays draft,
    # automatically mandatory once promotion.load_promotion(...).
    # release_suite_eligible is true). A mandatory-and-BLOCKED subscribed-
    # fixture then gates the overall status like any other component.
    if subscribed_fixture_mandatory is not None:
        components.insert(
            insert_at,
            component_from_subscribed_fixture_report(
                SUBSCRIBED_FIXTURE_COMPONENT_NAME, subscribed_fixture, mandatory=subscribed_fixture_mandatory,
            ),
        )
        insert_at += 1

    # Independent release-feature components (Workstream 3). Included exactly
    # when the caller passes a feature profile (the consolidate CLI always does,
    # from config/release-platforms.yaml's release_features; unit tests that
    # don't exercise features leave it None). Each feature's result is derived
    # ONLY from its own feature-tagged step evidence -- meals/onboarding/
    # google_calendar from the Android/iOS UI reports, kiosk/admin from the
    # kiosk-admin report -- never inferred from the broad platform component. A
    # mandatory feature with no matching evidence becomes NOT_RUN and blocks.
    if feature_profile is not None:
        mobile_sources = [(mobile_android_ui, "android"), (mobile_ios_ui, "ios")]
        feature_sources = {
            "meals": mobile_sources,
            "onboarding": mobile_sources,
            "google_calendar": mobile_sources,
            "kiosk_admin": [(kiosk_admin, "tablet")],
        }
        for feature_key in ("meals", "onboarding", "google_calendar", "kiosk_admin"):
            if feature_key not in feature_profile:
                continue
            components.insert(
                insert_at,
                component_from_feature_evidence(
                    feature_key,
                    FEATURE_COMPONENT_NAMES[feature_key],
                    mandatory=feature_profile[feature_key],
                    sources=feature_sources[feature_key],
                ),
            )
            insert_at += 1

    # Build identity (Phase 3). When an app's identity is required (in scope
    # for this release) it must be known, or the release BLOCKS -- a PASS must
    # prove which build/commit was actually tested. A configured expectation
    # that doesn't match, or an unapproved dirty build, also BLOCKS.
    for identity_component in (
        component_from_build_identity(
            "Calee tablet build identity",
            detected_version=calee_build_version, expected_version=expected_calee_build_version,
            detected_git_sha=calee_git_sha, expected_git_sha=expected_calee_git_sha,
            dirty=calee_dirty, available=calee_identity_available, required=require_calee_identity,
            require_package_identity=require_calee_package_identity,
            allow_dirty=allow_dirty, version_code=calee_version_code,
            application_id=calee_application_id, caleeshell_version=caleeshell_version,
        ),
        component_from_build_identity(
            "CaleeMobile build identity",
            detected_version=caleemobile_build_version, expected_version=expected_caleemobile_build_version,
            detected_git_sha=caleemobile_git_sha, expected_git_sha=expected_caleemobile_git_sha,
            dirty=caleemobile_dirty, available=caleemobile_identity_available,
            required=require_caleemobile_identity, require_git_sha=require_caleemobile_git_sha,
            allow_dirty=allow_dirty,
        ),
    ):
        if identity_component is not None:
            components.append(identity_component)

    # Caller-supplied extra components (e.g. the pre/post build-identity
    # stability check, Phase 4) are appended last so they gate the overall
    # status the same as any built-in component.
    for extra in extra_components or []:
        if extra is not None:
            components.append(extra)

    # Tablet release installation (Priority 6), the machine-config snapshot
    # (Priority 4), and the release-config composition (Priority 1/3).
    # Prepended so the report reads config -> installation -> release-config ->
    # environment -> tests (matching execution order: machine-config and
    # installation happen in launcher "00"; release-config happens right after,
    # at the start of launcher "06"). Included only when the caller made an
    # explicit mandatory/optional decision (the consolidate CLI always does for
    # a full release; unit tests that don't exercise them leave all three None,
    # so existing tests are unaffected). A mandatory installation that is
    # BLOCKED/FAILED/missing, a missing/invalid machine-config snapshot, or a
    # BLOCKED/missing release-config composition then gates the overall status
    # like any other mandatory component -- a release-config conflict is a
    # pre-product gate, never a product FAIL, but it blocks a PASS exactly like
    # one.
    if release_config_mandatory is not None:
        components.insert(
            0, component_from_release_config_report(
                RELEASE_CONFIG_COMPONENT_NAME, release_config, mandatory=release_config_mandatory
            ),
        )
    if installation_mandatory is not None:
        components.insert(
            0, component_from_installation_report(
                INSTALLATION_COMPONENT_NAME, installation, mandatory=installation_mandatory
            ),
        )
    if machine_config_mandatory is not None:
        components.insert(
            0, component_from_machine_config_report(
                MACHINE_CONFIG_COMPONENT_NAME, machine_config, mandatory=machine_config_mandatory
            ),
        )

    mandatory_statuses = [c.status for c in components if c.mandatory]
    if any(s == STATUS_FAIL for s in mandatory_statuses):
        overall = STATUS_FAIL
    elif any(s in (STATUS_BLOCKED, STATUS_NOT_RUN) for s in mandatory_statuses):
        overall = STATUS_BLOCKED
    else:
        overall = STATUS_PASS

    manual_checks = list(manual_checks or [])
    return ReleaseReport(
        overall_status=overall,
        components=components,
        manual_checks=manual_checks,
        meta=dict(meta or {}),
        generated_at=generated_at or (time.strftime("%Y-%m-%d %H:%M:%S")),
        summary=_build_summary(components, manual_checks, overall),
    )


_STATUS_COLORS = {
    STATUS_PASS: "#1a7f37",
    STATUS_FAIL: "#cf222e",
    STATUS_BLOCKED: "#8250df",
    STATUS_NOT_RUN: "#6e7781",
}


def _escape(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_EVIDENCE_LABELS = {
    "requested": "Requested backend",
    "resolved": "Resolved backend",
    "fixture": "Fixture backend",
    "matchStatus": "Match status",
    "deviceId": "Device ID",
    "buildVersion": "Build version",
    "expectedBuildVersion": "Expected build version",
    "gitSha": "Git SHA",
    "expectedGitSha": "Expected Git SHA",
    "dirty": "Uncommitted changes",
    "versionCode": "Version code",
    "applicationId": "Application ID",
    "caleeShellVersion": "CaleeShell version",
    "available": "Identity available",
    "source": "Detected from",
}


def _evidence_html(evidence: dict) -> str:
    """Render a component's structured evidence (backend triple, or build
    identity) as a labelled table. Keys are shown in insertion order with a
    friendly label where known, falling back to the raw key. Used for both the
    Phase 4 per-platform backend evidence and the Phase 3 build identity."""
    cells = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#57606a'>{_escape(_EVIDENCE_LABELS.get(key, key))}</td>"
        f"<td style='padding:2px 0'>{_escape('—' if value in (None, '') else value)}</td></tr>"
        for key, value in evidence.items()
    )
    return f"<table style='margin:6px 0;border-collapse:collapse'>{cells}</table>"


def write_json(report: ReleaseReport, path: Path) -> None:
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")


def write_html(report: ReleaseReport, path: Path) -> None:
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Calee Regression — Consolidated Release Report</title>",
        "<style>"
        "body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#fff;color:#1f2328;margin:0;padding:24px;}"
        "h1{margin-top:0;} .summary{padding:12px 16px;border:1px solid #d0d7de;border-radius:6px;margin-bottom:24px;background:#f6f8fa;}"
        ".component{border:1px solid #d0d7de;border-radius:6px;margin-bottom:16px;padding:12px 16px;}"
        ".detail{padding:4px 0;color:#57606a;}"
        "</style></head><body>",
        f"<h1>Calee Regression — Consolidated Release Report: "
        f"<span style='color:{_STATUS_COLORS.get(report.overall_status, '#1f2328')}'>{_escape(report.overall_status.upper())}</span></h1>",
        f"<div class='summary'>Generated: {_escape(report.generated_at)}<br>",
    ]
    for key, value in report.meta.items():
        parts.append(f"{_escape(key)}: {_escape(value)}<br>")
    parts.append("</div>")
    if report.summary.get("suggestedNextAction"):
        parts.append(
            f"<div class='summary'><b>Suggested next action:</b> {_escape(report.summary['suggestedNextAction'])}</div>"
        )
    for component in report.components:
        color = _STATUS_COLORS.get(component.status, "#1f2328")
        mandatory_label = "mandatory" if component.mandatory else "optional"
        parts.append(
            f"<div class='component'><h2 style='color:{color}'>{_escape(component.name)} "
            f"[{_escape(component.status.upper())}] ({mandatory_label})</h2>"
            f"<div>passed={component.passed} failed={component.failed} "
            f"blocked={component.blocked} skipped={component.skipped}</div>"
        )
        if component.evidence is not None:
            parts.append(_evidence_html(component.evidence))
        for line in component.detail:
            parts.append(f"<div class='detail'>{_escape(line)}</div>")
        parts.append("</div>")
    if report.manual_checks:
        parts.append("<h2>Manual guided checks</h2>")
        for check in report.manual_checks:
            parts.append(
                f"<div class='component'><b>{_escape(check.title)}</b> — "
                f"{_escape(check.status or 'not recorded')}<div>{_escape(check.instruction)}</div>"
                f"<div>Expected: {_escape(check.expected_result)}</div>"
            )
            if check.note:
                parts.append(f"<div class='detail'>Note: {_escape(check.note)}</div>")
            parts.append("</div>")
    parts.append("</body></html>")
    path.write_text("".join(parts), encoding="utf-8")


def write_junit(report: ReleaseReport, path: Path) -> None:
    testsuite = ET.Element(
        "testsuite",
        {
            "name": "calee-consolidated-release",
            "tests": str(len(report.components)),
            "failures": str(sum(1 for c in report.components if c.status == STATUS_FAIL)),
            "errors": str(sum(1 for c in report.components if c.status in (STATUS_BLOCKED, STATUS_NOT_RUN))),
        },
    )
    for component in report.components:
        testcase = ET.SubElement(testsuite, "testcase", {"classname": "release", "name": component.name})
        if component.status == STATUS_FAIL:
            failure = ET.SubElement(testcase, "failure", {"message": "; ".join(component.detail) or "failed"})
            failure.text = "; ".join(component.detail)
        elif component.status in (STATUS_BLOCKED, STATUS_NOT_RUN):
            error = ET.SubElement(testcase, "error", {"message": "; ".join(component.detail) or component.status})
            error.text = "; ".join(component.detail)
    tree = ET.ElementTree(testsuite)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_release_bundle(
    report: ReleaseReport, out_dir: Path, *, build_label: str, evidence_paths: "list[Path] | None" = None
) -> Path:
    """Writes json/html/junit into out_dir and zips them (+ evidence) into a
    `Calee-Regression-YYYY-MM-DD-BUILD-<STATUS>.zip` release bundle."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "consolidated-report.json"
    html_path = out_dir / "consolidated-report.html"
    junit_path = out_dir / "consolidated-report.junit.xml"
    write_json(report, json_path)
    write_html(report, html_path)
    write_junit(report, junit_path)

    date_str = time.strftime("%Y-%m-%d")
    status_label = report.overall_status.upper()
    bundle_name = f"Calee-Regression-{date_str}-{_sanitize_for_filename(build_label)}-{status_label}.zip"
    bundle_path = out_dir / bundle_name
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in (json_path, html_path, junit_path):
            zf.write(file_path, arcname=file_path.name)
        seen_arcs: "set[str]" = set()
        for evidence_path in evidence_paths or []:
            if evidence_path.is_file():
                # Priority 9: preserve the containing directory in the arcname
                # (evidence/<component>/<filename>, matching the
                # reports/runs/<run-id>/<component>/<filename> convention
                # every producer already follows) -- NOT just the basename.
                # Every component's own report is named "results.json", so
                # basename-only arcnames collided and silently dropped all
                # but one component's evidence from the ZIP.
                arc = f"evidence/{evidence_path.parent.name}/{evidence_path.name}"
                # Dedup identical arcnames (e.g. a screenshot referenced twice)
                # so the ZIP never carries duplicate entries.
                if arc in seen_arcs:
                    continue
                seen_arcs.add(arc)
                zf.write(evidence_path, arcname=arc)
    return bundle_path


def collect_step_diagnostic_paths(report_dict: "dict[str, Any] | None") -> "list[Path]":
    """Existing row-diagnostic files (screenshots + page sources) referenced by a
    runner report's steps, so they travel into the release ZIP (Priority 5.9).

    Reads ``scenarios[].steps[].screenshot_path`` / ``page_source_path`` and
    returns the ones that exist on disk, de-duplicated, order-preserving."""
    out: "list[Path]" = []
    seen: "set[str]" = set()
    if not isinstance(report_dict, dict):
        return out
    for scenario in report_dict.get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        for step in scenario.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for key in ("screenshot_path", "page_source_path"):
                value = step.get(key)
                if not value or value in seen:
                    continue
                path = Path(value)
                if path.is_file():
                    seen.add(value)
                    out.append(path)
    return out
