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
from dataclasses import dataclass, field
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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "mandatory": self.mandatory,
            "passed": self.passed,
            "failed": self.failed,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "detail": list(self.detail),
        }


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
    """Build a ComponentResult from CaleeMobile-Regression's --report json shape."""
    if report_dict is None:
        return ComponentResult(name=name, status=STATUS_NOT_RUN, mandatory=mandatory, detail=["Not executed."])
    counts = report_dict.get("counts", {})
    passed = counts.get("PASS", 0)
    failed = counts.get("FAIL", 0)
    blocked = counts.get("BLOCKED", 0)
    skipped = counts.get("SKIP", 0)
    total = len(report_dict.get("steps", [])) or (passed + failed + blocked + skipped)
    status = decide_status(passed=passed, failed=failed, blocked=blocked, total=total)
    detail = [
        f"{s['name']}: {s.get('detail')}"
        for s in report_dict.get("steps", [])
        if s.get("status") in ("FAIL", "BLOCKED")
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
    components = [
        component_from_environment_report("Test environment and regression fixture", environment, mandatory=True),
        component_from_tablet_report("Calee tablet", tablet, mandatory=True),
        component_from_api_report("CaleeMobile Client API", mobile_api, mandatory=True),
        component_from_api_report("CaleeMobile Android UI", mobile_android_ui, mandatory=android_mandatory),
        component_from_api_report("CaleeMobile iPhone UI", mobile_ios_ui, mandatory=ios_mandatory),
        component_from_manual_checks(manual_checks or []),
    ]

    for build_component in (
        component_from_build_version_match(
            name="Calee build version", expected=expected_calee_build_version, detected=calee_build_version,
        ),
        component_from_build_version_match(
            name="CaleeMobile build version", expected=expected_caleemobile_build_version, detected=caleemobile_build_version,
        ),
    ):
        if build_component is not None:
            components.append(build_component)

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
