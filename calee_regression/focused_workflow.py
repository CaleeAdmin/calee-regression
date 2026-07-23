"""Permanent focused post-fix verification workflow.

One permanent command (`calee_regression focused-verify`) orchestrates, under
a SINGLE fresh run id: credential preflight, Appium-independent fixture
preparation, the standard + diagnostic recurring-calendar tablet scenarios,
the focused stop-repeating API scenario twice, a focused iPhone environment
check, and one immutable aggregate summary.

This session's refinements:

  * explicit step prerequisites (``FocusedStep.requires``) instead of
    ordering alone: a failed prerequisite marks every dependent step
    ``blocked_not_run`` naming the exact prerequisite and its report, while
    INDEPENDENT branches keep running (a standard-tablet failure never
    suppresses diagnostic evidence; API attempt 1 never suppresses attempt 2);
  * the four-state exit contract is preserved end to end: child exit 2
    (invalid invocation) is classified ``invalid_config`` -- never silently
    rewritten to a tooling blocker -- and aggregate precedence is
    FAIL > INVALID_CONFIG > BLOCKED > PASS;
  * every child report is validated (type/schema/run/backend/fixture/status
    against exit code) via an injectable ``validate_step`` hook; a PASS/FAIL
    exit that fails validation becomes BLOCKED -- malformed evidence can
    never be converted to a product result;
  * the framework still OWNS the Appium lifecycle: ensured once up front,
    stopped exactly once in a ``finally`` -- never between the standard and
    diagnostic tablet attempts;
  * this makes NO release-certification claim.

The orchestration itself is a pure function (`run_focused_verify`) with
injected step-runner / Appium / validation hooks, so every branch is
unit-testable with no device.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_REGRESSION, EXIT_SUCCESS

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_BLOCKED = "blocked"
STATUS_INVALID_CONFIG = "invalid_config"
STATUS_BLOCKED_NOT_RUN = "blocked_not_run"

SUMMARY_REPORT_TYPE = "focused-verify-summary"
SUMMARY_SCHEMA_VERSION = 2


def classify_exit_code(code: int) -> str:
    """Map a child process exit code to a status, preserving the framework's
    four-state contract: 0 PASS, 1 product FAIL, 2 invalid invocation/config
    (kept distinguishable -- never silently rewritten to a blocker), anything
    else (3, signals, unexpected codes) BLOCKED."""
    if code == EXIT_REGRESSION:
        return STATUS_FAIL
    if code == EXIT_SUCCESS:
        return STATUS_PASS
    if code == EXIT_INVALID_CONFIG:
        return STATUS_INVALID_CONFIG
    return STATUS_BLOCKED


def aggregate_status(statuses) -> str:
    """Aggregate precedence: a verified product FAIL dominates (it must stay
    visible even next to blockers), then INVALID_CONFIG, then any BLOCKED
    (including blocked_not_run), else PASS. Empty -> BLOCKED (nothing ran)."""
    statuses = list(statuses)
    if not statuses:
        return STATUS_BLOCKED
    if any(s == STATUS_FAIL for s in statuses):
        return STATUS_FAIL
    if any(s == STATUS_INVALID_CONFIG for s in statuses):
        return STATUS_INVALID_CONFIG
    if any(s in (STATUS_BLOCKED, STATUS_BLOCKED_NOT_RUN) for s in statuses):
        return STATUS_BLOCKED
    return STATUS_PASS


_STATUS_TO_EXIT = {
    STATUS_PASS: EXIT_SUCCESS,
    STATUS_FAIL: EXIT_REGRESSION,
    STATUS_INVALID_CONFIG: EXIT_INVALID_CONFIG,
    STATUS_BLOCKED: EXIT_BLOCKED,
    STATUS_BLOCKED_NOT_RUN: EXIT_BLOCKED,
}


def status_to_exit_code(status: str) -> int:
    return _STATUS_TO_EXIT.get(status, EXIT_BLOCKED)


@dataclass
class FocusedStep:
    """One orchestrated step. ``command`` is the argv list a real runner would
    execute; ``mode`` labels a tablet step's certification mode;
    ``requires_appium`` marks steps that must not run when the endpoint is
    unavailable; ``requires`` names the step ids whose PASS this step depends
    on (explicit prerequisites, not ordering); ``timeout_seconds`` bounds the
    supervised child."""

    id: str
    title: str
    command: list
    mode: "str | None" = None
    requires_appium: bool = True
    requires: tuple = ()
    timeout_seconds: "float | None" = None
    metadata: dict = field(default_factory=dict)


@dataclass
class FocusedResult:
    id: str
    title: str
    status: str
    exit_code: "int | None"
    mode: "str | None" = None
    detail: str = ""
    report_path: "str | None" = None
    report_sha256: "str | None" = None
    validation_problems: "list[str]" = field(default_factory=list)
    supervision: "dict | None" = None
    blocked_by: "str | None" = None
    # How this step's evidence came to be in THIS invocation's summary:
    # "executed" (ran now) or "reused" (a prior invocation's immutable report,
    # referenced by original path + digest -- see focused_resume.py).
    evidence: str = "executed"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "exitCode": self.exit_code,
            "mode": self.mode,
            "detail": self.detail,
            "reportPath": self.report_path,
            "reportSha256": self.report_sha256,
            "validationProblems": list(self.validation_problems),
            "supervision": self.supervision,
            "blockedBy": self.blocked_by,
            "evidence": self.evidence,
        }


def _unmet_prerequisites(step: FocusedStep, statuses: "dict[str, str]") -> "list[str]":
    return [req for req in step.requires if statuses.get(req) != STATUS_PASS]


def run_focused_verify(
    *,
    steps,
    ensure_appium,
    run_step,
    stop_appium,
    validate_step=None,
    initial_results=None,
    log=lambda _msg: None,
    stop_on_failure: bool = False,
):
    """Run the focused-verify orchestration.

    ``ensure_appium() -> object`` with ``.available`` and ``.state`` ensures
    the Appium endpoint ONCE. ``run_step(step) -> int | object`` executes one
    step and returns its exit code, or an object with ``.exit_code`` and an
    optional ``.to_dict()`` supervision record (see focused_supervision).
    ``validate_step(step, exit_code) -> ReportValidation | None`` validates the
    step's child report against the same-run verified context; a PASS/FAIL
    exit whose report fails validation is downgraded to BLOCKED (malformed
    evidence never becomes a product result). ``initial_results`` seeds
    already-executed prerequisite results (e.g. the fixture preparation that
    ran before the steps were even built). ``stop_appium()`` is called EXACTLY
    ONCE, in a ``finally`` -- never between steps.

    Returns ``(summary_dict, exit_code)`` with FAIL > INVALID_CONFIG >
    BLOCKED > PASS precedence. Never raises for a step failure.
    """
    results: "list[FocusedResult]" = list(initial_results or [])
    statuses: "dict[str, str]" = {r.id: r.status for r in results}
    appium_state = "unknown"
    appium_available = False
    try:
        lifecycle = ensure_appium()
        appium_available = bool(getattr(lifecycle, "available", False))
        appium_state = getattr(lifecycle, "state", "unknown")
        log(f"Appium lifecycle: {appium_state} (available={appium_available})")

        for step in steps:
            unmet = _unmet_prerequisites(step, statuses)
            if unmet:
                blocking = unmet[0]
                blocking_result = next((r for r in results if r.id == blocking), None)
                ref = blocking_result.report_path if blocking_result else None
                detail = (
                    f"prerequisite step {blocking!r} did not pass "
                    f"(status: {statuses.get(blocking, 'not_run')}"
                    + (f", report: {ref}" if ref else "")
                    + ") -- step not started. No silent skip: this is an explicit blocked_not_run."
                )
                result = FocusedResult(
                    id=step.id, title=step.title, status=STATUS_BLOCKED_NOT_RUN, exit_code=None,
                    mode=step.mode, detail=detail, blocked_by=blocking,
                )
                results.append(result)
                statuses[step.id] = result.status
                log(f"   {step.title}: BLOCKED_NOT_RUN ({detail})")
                continue
            if step.requires_appium and not appium_available:
                result = FocusedResult(
                    id=step.id, title=step.title, status=STATUS_BLOCKED, exit_code=None,
                    mode=step.mode, detail="Appium endpoint unavailable -- step not started.",
                    blocked_by="appium",
                )
                results.append(result)
                statuses[step.id] = result.status
                continue
            log(f"-> {step.title}")
            try:
                outcome = run_step(step)
            except Exception as exc:  # noqa: BLE001 -- a runner failure blocks that step, never crashes the run
                result = FocusedResult(
                    id=step.id, title=step.title, status=STATUS_BLOCKED, exit_code=None,
                    mode=step.mode, detail=f"step runner raised: {exc}",
                )
                results.append(result)
                statuses[step.id] = result.status
                continue
            supervision = None
            if isinstance(outcome, int) or outcome is None:
                code = outcome
            else:
                code = getattr(outcome, "exit_code", None)
                to_dict = getattr(outcome, "to_dict", None)
                supervision = to_dict() if callable(to_dict) else None
            if code is None:
                status = STATUS_BLOCKED
                detail = "child produced no exit code (timeout/kill before exit)"
            else:
                status = classify_exit_code(code)
                detail = "" if status in (STATUS_PASS, STATUS_FAIL) else f"child exited {code}"
                if status == STATUS_BLOCKED and code not in (EXIT_BLOCKED,):
                    detail = f"unexpected child exit code {code} -- recorded as BLOCKED"
            result = FocusedResult(
                id=step.id, title=step.title, status=status, exit_code=code,
                mode=step.mode, detail=detail, supervision=supervision,
            )
            if validate_step is not None:
                try:
                    validation = validate_step(step, code)
                except Exception as exc:  # noqa: BLE001 -- a validator crash blocks, never passes
                    validation = None
                    result.status = STATUS_BLOCKED
                    result.detail = f"report validation raised: {exc}"
                if validation is not None:
                    result.report_path = validation.report_path
                    result.report_sha256 = validation.digest
                    result.validation_problems = list(validation.problems)
                    if not validation.ok and result.status in (STATUS_PASS, STATUS_FAIL):
                        # Exit/report disagreement or malformed evidence: a
                        # product result stands only when a valid report
                        # proves it.
                        result.status = STATUS_BLOCKED
                        result.detail = (
                            "child report failed validation: " + "; ".join(validation.problems)
                        )
            results.append(result)
            statuses[step.id] = result.status
            log(f"   {step.title}: {result.status.upper()} (exit {code})")
            if stop_on_failure and result.status == STATUS_FAIL:
                log("   stop-on-failure: halting further steps")
                break
    finally:
        # Framework-owned cleanup: stop Appium ONCE, only here -- never between
        # the standard and diagnostic attempts.
        try:
            stop_appium()
            log("Appium: framework-owned cleanup done")
        except Exception as exc:  # noqa: BLE001 -- cleanup failure must not mask results
            log(f"Appium cleanup warning: {exc}")

    overall = aggregate_status([r.status for r in results])
    summary = {
        "reportType": SUMMARY_REPORT_TYPE,
        "reportSchemaVersion": SUMMARY_SCHEMA_VERSION,
        "appiumLifecycle": {"state": appium_state, "available": appium_available},
        "steps": [r.to_dict() for r in results],
        "counts": _status_counts(results),
        "status": overall,
        # An explicit, unmissable note that this is NOT a release certification.
        "certificationEligible": False,
        "certification": "not-a-release-certification (focused post-fix verification only)",
    }
    return summary, status_to_exit_code(overall)


def _status_counts(results) -> dict:
    counts: dict = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts
