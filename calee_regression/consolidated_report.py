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


_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def is_full_git_sha(value: "Any | None") -> bool:
    """A full, unambiguous git commit SHA is exactly 40 hex characters. An
    abbreviated SHA (e.g. ``abc1234``) is ambiguous -- it can name more than
    one commit -- so it is never accepted as a release build identity. See
    Phase 5 (a CaleeMobile version alone spans multiple commits)."""
    return bool(value) and bool(_FULL_GIT_SHA_RE.match(str(value).strip()))


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


def build_release_report(
    *,
    environment: "dict[str, Any] | None" = None,
    tablet: "dict[str, Any] | None" = None,
    mobile_api: "dict[str, Any] | None" = None,
    mobile_android_ui: "dict[str, Any] | None" = None,
    mobile_ios_ui: "dict[str, Any] | None" = None,
    manual_checks: "list[ManualCheck] | None" = None,
    meta: "dict[str, Any] | None" = None,
    generated_at: "str | None" = None,
    android_mandatory: bool = True,
    ios_mandatory: bool = True,
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

    # Independent per-platform backend verification (Phase 4). Inserted right
    # after the platform UI components so the report reads platform -> its
    # backend evidence. This does NOT rely on the mobile runner's exit code:
    # a backend mismatch or a missing resolved backend for a mandatory
    # platform BLOCKS the release even if that platform's UI checks "passed".
    insert_at = 5  # after the iPhone UI component, before manual checks
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
        for evidence_path in evidence_paths or []:
            if evidence_path.is_file():
                zf.write(evidence_path, arcname=f"evidence/{evidence_path.name}")
    return bundle_path
