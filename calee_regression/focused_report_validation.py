"""Child-report validation for focused-verify (this session's Workstream 7).

The orchestrator must never trust a child's process exit code alone: every
child must leave a typed, versioned report whose identity (run, backend,
fixture, purpose, device) matches the SAME-RUN verified context, whose status
agrees with the exit code, and which can never claim release-certification
eligibility. Any disagreement or malformation is BLOCKED -- malformed
evidence can never be converted to a PASS, and a product FAIL stands only
when a valid report proves it.

Each validated report's SHA-256 digest is recorded so the focused summary is
evidence-bound (Workstream 8).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import EXIT_BLOCKED, EXIT_REGRESSION, EXIT_SUCCESS

# Report types focused-verify consumes, with the schema versions this
# validator explicitly supports. An unlisted type or version BLOCKS.
SUPPORTED_REPORTS = {
    "fixture-preparation": {1},
    "tablet-targeted-repeat": {1},
    "mobile-api-suite": {1},
    "mobile-api-stop-repeating-transition": {1},
    "mobile-ui-file": {1},
    "focused-verify-summary": {2},
}

_STATUS_TO_EXIT = {
    "pass": EXIT_SUCCESS,
    "fail": EXIT_REGRESSION,
    "blocked": EXIT_BLOCKED,
}


@dataclass
class ReportValidation:
    """The outcome of validating one child report."""

    ok: bool
    problems: "list[str]" = field(default_factory=list)
    digest: "str | None" = None
    report: "dict | None" = None
    report_path: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "reportSha256": self.digest,
            "reportPath": self.report_path,
        }


def sha256_of_file(path: Path) -> "str | None":
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _normalized_status(report: dict) -> "str | None":
    status = report.get("status")
    if not isinstance(status, str):
        return None
    return status.strip().lower()


def _report_backend(report: dict) -> "str | None":
    backend = report.get("backend")
    if isinstance(backend, dict):
        return backend.get("requested") or backend.get("resolved")
    if isinstance(backend, str):
        return backend
    return report.get("targetEnvironment")


def _report_run_id(report: dict) -> "str | None":
    return report.get("releaseRunId") or report.get("runId")


def validate_child_report(
    path: Path,
    *,
    expected_type: str,
    child_exit_code: "int | None" = None,
    expected_run_id: "str | None" = None,
    expected_release_id: "str | None" = None,
    expected_backend: "str | None" = None,
    expected_fixture_version: "str | None" = None,
    expected_purpose: "str | None" = None,
    expected_device_id: "str | None" = None,
    supported_reports: "dict | None" = None,
) -> ReportValidation:
    """Validate one child report against the same-run verified context.

    Only the expectations actually passed are enforced (a report type that
    doesn't carry a device id isn't checked for one), but the ENVELOPE checks
    (existence, type, schema version, status/exit consistency, certification
    ineligibility) always run. Every problem is a BLOCK, never a silent pass.
    """
    supported = supported_reports if supported_reports is not None else SUPPORTED_REPORTS
    result = ReportValidation(ok=False, report_path=str(path))

    if expected_type not in supported:
        result.problems.append(f"report type {expected_type!r} is not supported by this validator")
        return result
    if not path.is_file():
        result.problems.append(
            f"child report {path} does not exist"
            + (f" although the child exited {child_exit_code}" if child_exit_code is not None else "")
        )
        return result
    result.digest = sha256_of_file(path)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.problems.append(f"child report {path} is not valid JSON: {exc}")
        return result
    if not isinstance(report, dict):
        result.problems.append(f"child report {path} is not a JSON object")
        return result
    result.report = report

    actual_type = report.get("reportType")
    if actual_type != expected_type:
        result.problems.append(f"reportType {actual_type!r} != expected {expected_type!r}")
    schema_version = report.get("reportSchemaVersion")
    if schema_version not in supported.get(expected_type, set()):
        result.problems.append(
            f"reportSchemaVersion {schema_version!r} is not supported for {expected_type!r} "
            f"(supported: {sorted(supported.get(expected_type, set()))})"
        )

    if expected_run_id is not None:
        actual_run = _report_run_id(report)
        if actual_run != expected_run_id:
            result.problems.append(f"run identity {actual_run!r} != this run {expected_run_id!r}")
    if expected_release_id is not None:
        actual_release = report.get("releaseId")
        if actual_release != expected_release_id:
            result.problems.append(f"releaseId {actual_release!r} != this run's {expected_release_id!r}")
    if expected_backend is not None:
        actual_backend = _report_backend(report)
        if (actual_backend or "").rstrip("/") != expected_backend.rstrip("/"):
            result.problems.append(f"backend {actual_backend!r} != verified backend {expected_backend!r}")
    if expected_fixture_version is not None:
        actual_fixture = report.get("fixtureVersion")
        if actual_fixture != expected_fixture_version:
            result.problems.append(
                f"fixtureVersion {actual_fixture!r} != verified {expected_fixture_version!r}"
            )
    if expected_purpose is not None:
        actual_purpose = report.get("executionPurpose") or (report.get("executionContext") or {}).get("executionPurpose")
        if actual_purpose != expected_purpose:
            result.problems.append(f"executionPurpose {actual_purpose!r} != expected {expected_purpose!r}")
    if expected_device_id is not None:
        actual_device = report.get("deviceId") or (report.get("provenance") or {}).get("deviceId")
        if actual_device != expected_device_id:
            result.problems.append(f"deviceId {actual_device!r} != expected {expected_device_id!r}")

    if report.get("certificationEligible") is True:
        result.problems.append(
            "report claims certificationEligible=true; a focused child can never be "
            "release-certification eligible"
        )

    status = _normalized_status(report)
    if status is None:
        result.problems.append("report has no string 'status'")
    elif child_exit_code is not None:
        expected_exit = _STATUS_TO_EXIT.get(status)
        if expected_exit is None:
            result.problems.append(f"report status {status!r} is not a recognized status")
        elif expected_exit != child_exit_code:
            result.problems.append(
                f"report status {status!r} implies exit {expected_exit} but the child exited "
                f"{child_exit_code}; exit/report disagreement blocks"
            )

    result.ok = not result.problems
    return result
