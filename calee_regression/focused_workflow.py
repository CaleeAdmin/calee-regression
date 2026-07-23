"""Permanent focused post-fix verification workflow (Workstream 9).

Replaces the ad-hoc downloaded shell scripts a tester used to run after a fix.
One permanent command (`calee_regression focused-verify`) orchestrates, under a
SINGLE fresh run id:

  1. environment + fixture preparation;
  2. the standard recurring-calendar tablet scenario, twice;
  3. the diagnostic recurring-calendar tablet scenario, twice;
  4. the focused stop-repeating API scenario, twice;
  5. a focused iPhone environment / app-boot check;
  6. one aggregate diagnostic summary.

Key guarantees (see the acceptance criteria):
  * the framework OWNS the Appium lifecycle: it is ensured ONCE up front and
    stopped ONCE in a `finally` at the very end -- NEVER between the standard and
    diagnostic tablet attempts (the exact bug in run
    focused-next-20260723-163940-6d25db, where the repair path stopped Appium and
    never restarted it before the next `run-repeat`);
  * standard and diagnostic tablet reports stay separated (Workstream 4);
  * API and iPhone invocations are immutable and repeatable;
  * this makes NO full-release claim -- it is a focused post-fix check;
  * clear exit-code precedence: FAIL > BLOCKED > PASS;
  * credentials are never placed on any child's argv (they flow through the
    environment / Keychain, resolved by each child) and this never prompts on a
    TTY, so it can never wedge on a suspended read.

The orchestration itself is a pure function (`run_focused_verify`) with injected
step-runner / Appium hooks, so every branch is unit-testable with no device.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import EXIT_BLOCKED, EXIT_REGRESSION, EXIT_SUCCESS

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_BLOCKED = "blocked"


def classify_exit_code(code: int) -> str:
    """Map a child process exit code to a status, matching the framework
    contract: 1 -> FAIL (product regression), 0 -> PASS, anything else
    (BLOCKED/invalid-config/unexpected) -> BLOCKED."""
    if code == EXIT_REGRESSION:
        return STATUS_FAIL
    if code == EXIT_SUCCESS:
        return STATUS_PASS
    return STATUS_BLOCKED


def aggregate_status(statuses) -> str:
    """FAIL dominates, then BLOCKED, else PASS. Empty -> BLOCKED (nothing ran)."""
    statuses = list(statuses)
    if not statuses:
        return STATUS_BLOCKED
    if any(s == STATUS_FAIL for s in statuses):
        return STATUS_FAIL
    if any(s == STATUS_BLOCKED for s in statuses):
        return STATUS_BLOCKED
    return STATUS_PASS


_STATUS_TO_EXIT = {STATUS_PASS: EXIT_SUCCESS, STATUS_FAIL: EXIT_REGRESSION, STATUS_BLOCKED: EXIT_BLOCKED}


def status_to_exit_code(status: str) -> int:
    return _STATUS_TO_EXIT.get(status, EXIT_BLOCKED)


@dataclass
class FocusedStep:
    """One orchestrated step. ``command`` is the argv list a real runner would
    execute; ``mode`` labels a tablet step's certification mode; ``requires_appium``
    marks the steps that must not run when the endpoint is unavailable."""

    id: str
    title: str
    command: list
    mode: "str | None" = None
    requires_appium: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class FocusedResult:
    id: str
    title: str
    status: str
    exit_code: "int | None"
    mode: "str | None" = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "exitCode": self.exit_code,
            "mode": self.mode,
            "detail": self.detail,
        }


def run_focused_verify(
    *,
    steps,
    ensure_appium,
    run_step,
    stop_appium,
    log=lambda _msg: None,
    stop_on_failure: bool = False,
):
    """Run the focused-verify orchestration.

    ``ensure_appium() -> object`` with ``.available`` and ``.state`` ensures the
    Appium endpoint ONCE. ``run_step(step) -> int`` executes one step and returns
    its exit code. ``stop_appium()`` is called EXACTLY ONCE, in a ``finally`` --
    never between steps -- so a framework-started server is cleaned up only after
    every step (standard + diagnostic + API + iPhone) has finished.

    Returns ``(summary_dict, exit_code)``. ``summary_dict`` records the Appium
    lifecycle disposition and each step's result; the exit code follows
    FAIL > BLOCKED > PASS precedence. Never raises for a step failure -- a step
    that raises is recorded BLOCKED and the run continues to cleanup.
    """
    results: "list[FocusedResult]" = []
    appium_state = "unknown"
    appium_available = False
    try:
        lifecycle = ensure_appium()
        appium_available = bool(getattr(lifecycle, "available", False))
        appium_state = getattr(lifecycle, "state", "unknown")
        log(f"Appium lifecycle: {appium_state} (available={appium_available})")

        for step in steps:
            if step.requires_appium and not appium_available:
                results.append(
                    FocusedResult(
                        id=step.id, title=step.title, status=STATUS_BLOCKED, exit_code=None,
                        mode=step.mode, detail="Appium endpoint unavailable -- step not started.",
                    )
                )
                continue
            log(f"-> {step.title}")
            try:
                code = run_step(step)
            except Exception as exc:  # noqa: BLE001 -- a runner failure blocks that step, never crashes the run
                results.append(
                    FocusedResult(
                        id=step.id, title=step.title, status=STATUS_BLOCKED, exit_code=None,
                        mode=step.mode, detail=f"step runner raised: {exc}",
                    )
                )
                continue
            status = classify_exit_code(code)
            results.append(
                FocusedResult(id=step.id, title=step.title, status=status, exit_code=code, mode=step.mode)
            )
            log(f"   {step.title}: {status.upper()} (exit {code})")
            if stop_on_failure and status == STATUS_FAIL:
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
        "reportType": "focused-verify-summary",
        "appiumLifecycle": {"state": appium_state, "available": appium_available},
        "steps": [r.to_dict() for r in results],
        "status": overall,
        # An explicit, unmissable note that this is NOT a release certification.
        "certification": "not-a-release-certification (focused post-fix verification only)",
    }
    return summary, status_to_exit_code(overall)
