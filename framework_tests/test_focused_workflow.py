"""WS9: the permanent focused-verify orchestration.

Pure-orchestration tests (no device/subprocess) plus a CLI wiring smoke test
with the subprocess runner + Appium hooks faked.
"""

from __future__ import annotations

from types import SimpleNamespace

from calee_regression import focused_workflow as fw, models


def _steps(*ids, requires_appium=True):
    return [fw.FocusedStep(id=i, title=i, command=["x"], requires_appium=requires_appium) for i in ids]


def _available(state="started"):
    return SimpleNamespace(available=True, state=state)


# ── aggregation + exit precedence ──────────────────────────────────────────
def test_all_pass_is_pass():
    events = []
    summary, code = fw.run_focused_verify(
        steps=_steps("a", "b"), ensure_appium=_available,
        run_step=lambda s: 0, stop_appium=lambda: events.append("stop"),
    )
    assert summary["status"] == "pass"
    assert code == models.EXIT_SUCCESS
    assert events == ["stop"]  # stopped exactly once


def test_any_fail_dominates():
    summary, code = fw.run_focused_verify(
        steps=_steps("a", "b"), ensure_appium=_available,
        run_step=lambda s: 1 if s.id == "b" else 0, stop_appium=lambda: None,
    )
    assert summary["status"] == "fail"
    assert code == models.EXIT_REGRESSION


def test_blocked_without_fail_is_blocked():
    summary, code = fw.run_focused_verify(
        steps=_steps("a"), ensure_appium=_available,
        run_step=lambda s: 3, stop_appium=lambda: None,
    )
    assert summary["status"] == "blocked"
    assert code == models.EXIT_BLOCKED


# ── framework-owned Appium lifecycle ───────────────────────────────────────
def test_appium_stopped_once_after_all_steps_never_between():
    order = []

    def run_step(step):
        order.append(f"run:{step.id}")
        return 0

    def stop():
        order.append("stop")

    fw.run_focused_verify(
        steps=_steps("standard", "diagnostic", "api"), ensure_appium=_available,
        run_step=run_step, stop_appium=stop,
    )
    # Every step runs BEFORE the single stop -- Appium is never stopped between
    # the standard and diagnostic attempts.
    assert order == ["run:standard", "run:diagnostic", "run:api", "stop"]


def test_stop_runs_even_when_a_step_raises():
    stopped = []

    def run_step(step):
        raise RuntimeError("boom")

    summary, code = fw.run_focused_verify(
        steps=_steps("a"), ensure_appium=_available,
        run_step=run_step, stop_appium=lambda: stopped.append(True),
    )
    assert stopped == [True]  # cleanup in finally
    assert summary["steps"][0]["status"] == "blocked"


def test_appium_unavailable_blocks_tablet_steps_but_runs_independent_ones():
    ran = []
    steps = [
        fw.FocusedStep(id="tablet", title="tablet", command=["x"], requires_appium=True),
        fw.FocusedStep(id="api", title="api", command=["x"], requires_appium=False),
    ]
    summary, code = fw.run_focused_verify(
        steps=steps, ensure_appium=lambda: SimpleNamespace(available=False, state="unavailable"),
        run_step=lambda s: ran.append(s.id) or 0, stop_appium=lambda: None,
    )
    assert ran == ["api"]  # the appium-independent step still ran
    tablet = [s for s in summary["steps"] if s["id"] == "tablet"][0]
    assert tablet["status"] == "blocked"


def test_summary_declares_not_a_release_certification():
    summary, _ = fw.run_focused_verify(
        steps=_steps("a"), ensure_appium=_available, run_step=lambda s: 0, stop_appium=lambda: None,
    )
    assert "not-a-release-certification" in summary["certification"]


# ── explicit prerequisites (this session's Workstream 4) ───────────────────
def test_failed_prerequisite_marks_dependent_blocked_not_run_with_reference():
    steps = [
        fw.FocusedStep(id="a", title="a", command=["x"], requires_appium=False),
        fw.FocusedStep(id="b", title="b", command=["x"], requires_appium=False, requires=("a",)),
    ]
    ran = []
    summary, code = fw.run_focused_verify(
        steps=steps, ensure_appium=_available,
        run_step=lambda s: ran.append(s.id) or 3, stop_appium=lambda: None,
    )
    assert ran == ["a"]  # b never started
    b = [s for s in summary["steps"] if s["id"] == "b"][0]
    assert b["status"] == fw.STATUS_BLOCKED_NOT_RUN
    assert b["blockedBy"] == "a"
    assert "'a'" in b["detail"] and "blocked" in b["detail"]
    assert code == models.EXIT_BLOCKED


def test_independent_branches_are_not_suppressed_by_a_failure():
    steps = [
        fw.FocusedStep(id="std", title="std", command=["x"], requires_appium=False),
        fw.FocusedStep(id="diag", title="diag", command=["x"], requires_appium=False),
    ]
    summary, _ = fw.run_focused_verify(
        steps=steps, ensure_appium=_available,
        run_step=lambda s: 1 if s.id == "std" else 0, stop_appium=lambda: None,
    )
    by_id = {s["id"]: s for s in summary["steps"]}
    # A standard failure never suppresses the diagnostic branch.
    assert by_id["std"]["status"] == "fail"
    assert by_id["diag"]["status"] == "pass"


def test_seeded_prerequisite_result_gates_dependents():
    fixture = fw.FocusedResult(id="fixture", title="fixture", status=fw.STATUS_BLOCKED, exit_code=3)
    steps = [fw.FocusedStep(id="api", title="api", command=["x"], requires_appium=False, requires=("fixture",))]
    summary, _ = fw.run_focused_verify(
        steps=steps, ensure_appium=_available, run_step=lambda s: 0,
        stop_appium=lambda: None, initial_results=[fixture],
    )
    api = [s for s in summary["steps"] if s["id"] == "api"][0]
    assert api["status"] == fw.STATUS_BLOCKED_NOT_RUN


# ── four-state exit contract (this session's Workstream 10) ────────────────
def test_child_exit_2_is_not_rewritten_to_blocked():
    summary, code = fw.run_focused_verify(
        steps=_steps("a", requires_appium=False), ensure_appium=_available,
        run_step=lambda s: 2, stop_appium=lambda: None,
    )
    assert summary["steps"][0]["status"] == fw.STATUS_INVALID_CONFIG
    assert code == models.EXIT_INVALID_CONFIG


def test_precedence_fail_beats_invalid_config_beats_blocked():
    assert fw.aggregate_status(["fail", "invalid_config", "blocked", "pass"]) == "fail"
    assert fw.aggregate_status(["invalid_config", "blocked", "pass"]) == "invalid_config"
    assert fw.aggregate_status(["blocked_not_run", "pass"]) == "blocked"
    assert fw.aggregate_status(["pass", "pass"]) == "pass"


def test_unexpected_child_exit_code_records_the_exact_code():
    summary, code = fw.run_focused_verify(
        steps=_steps("a", requires_appium=False), ensure_appium=_available,
        run_step=lambda s: 77, stop_appium=lambda: None,
    )
    step = summary["steps"][0]
    assert step["status"] == "blocked"
    assert step["exitCode"] == 77
    assert "77" in step["detail"]
    assert code == models.EXIT_BLOCKED


# ── report validation hook (this session's Workstream 7) ───────────────────
def test_pass_exit_with_invalid_report_is_downgraded_to_blocked():
    validation = SimpleNamespace(ok=False, problems=["run-ID mismatch"], digest="d", report_path="p")
    summary, code = fw.run_focused_verify(
        steps=_steps("a", requires_appium=False), ensure_appium=_available,
        run_step=lambda s: 0, stop_appium=lambda: None,
        validate_step=lambda step, exit_code: validation,
    )
    step = summary["steps"][0]
    assert step["status"] == "blocked"
    assert step["validationProblems"] == ["run-ID mismatch"]
    assert step["reportSha256"] == "d"
    assert code == models.EXIT_BLOCKED


def test_valid_report_keeps_product_result_and_binds_digest():
    validation = SimpleNamespace(ok=True, problems=[], digest="abc123", report_path="p")
    summary, code = fw.run_focused_verify(
        steps=_steps("a", requires_appium=False), ensure_appium=_available,
        run_step=lambda s: 1, stop_appium=lambda: None,
        validate_step=lambda step, exit_code: validation,
    )
    step = summary["steps"][0]
    assert step["status"] == "fail"  # a proven product FAIL stays FAIL
    assert step["reportSha256"] == "abc123"
    assert code == models.EXIT_REGRESSION


def test_summary_is_typed_versioned_and_never_certifying():
    summary, _ = fw.run_focused_verify(
        steps=_steps("a"), ensure_appium=_available, run_step=lambda s: 0, stop_appium=lambda: None,
    )
    assert summary["reportType"] == fw.SUMMARY_REPORT_TYPE
    assert summary["reportSchemaVersion"] == fw.SUMMARY_SCHEMA_VERSION
    assert summary["certificationEligible"] is False
