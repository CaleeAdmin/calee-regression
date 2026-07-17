"""Tests for calee_regression/sync_smoke.py (Workstream 11).

Every test injects fake callables via SyncSmokeEnvironment -- no real
subprocess, network, device, or Appium session is ever touched here. This
locks in the orchestration/evidence-recording LOGIC (sequencing, which leg
is honestly BLOCKED vs. exercised for real, FAIL-beats-BLOCKED precedence)
independent of whether a real backend/device happens to be available.
"""

from __future__ import annotations

from calee_regression.sync_smoke import (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_OK,
    SyncFlowResult,
    SyncSmokeEnvironment,
    SyncStepEvidence,
    run_all_sync_flows,
    run_chore_sync_flow,
    run_event_sync_flow,
    run_task_sync_flow,
)


def _make_env(**overrides):
    created_events = {}
    next_id = [1]

    def default_create_event(title):
        event_id = f"evt_{next_id[0]}"
        next_id[0] += 1
        created_events[event_id] = title
        return {"id": event_id, "title": title}

    def default_get_event(event_id):
        if event_id in created_events:
            return {"found": True, "id": event_id, "title": created_events[event_id]}
        return {"found": False, "id": event_id}

    def default_delete_event(event_id):
        created_events.pop(event_id, None)
        return {"found": False, "id": event_id}

    kwargs = dict(
        api_create_event=default_create_event,
        api_get_event=default_get_event,
        api_delete_event=default_delete_event,
        api_reopen_task=lambda task_id: {"id": task_id, "completed": False},
        tablet_text_present=lambda text: text in created_events.values() or text.startswith("REG-TASK") or text.startswith("REG-CHORE"),
        run_mobile_complete_task=lambda: True,
        run_mobile_complete_chore=lambda: True,
        device_id="emulator-5554",
        build_version="1.2.3",
    )
    kwargs.update(overrides)
    return SyncSmokeEnvironment(**kwargs)


# ── SyncFlowResult.status precedence ──────────────────────────────────────


def test_status_is_blocked_when_no_steps_ran():
    assert SyncFlowResult(flow="x").status == STATUS_BLOCKED


def test_status_ok_when_every_step_ok():
    result = SyncFlowResult(
        flow="x",
        steps=[SyncStepEvidence(step="a", surface="api", status=STATUS_OK, expected_state="e")],
    )
    assert result.status == STATUS_OK


def test_status_blocked_beats_ok():
    result = SyncFlowResult(
        flow="x",
        steps=[
            SyncStepEvidence(step="a", surface="api", status=STATUS_OK, expected_state="e"),
            SyncStepEvidence(step="b", surface="tablet", status=STATUS_BLOCKED, expected_state="e"),
        ],
    )
    assert result.status == STATUS_BLOCKED


def test_status_failed_beats_blocked_and_ok():
    result = SyncFlowResult(
        flow="x",
        steps=[
            SyncStepEvidence(step="a", surface="api", status=STATUS_OK, expected_state="e"),
            SyncStepEvidence(step="b", surface="tablet", status=STATUS_BLOCKED, expected_state="e"),
            SyncStepEvidence(step="c", surface="caleemobile", status=STATUS_FAILED, expected_state="e"),
        ],
    )
    assert result.status == STATUS_FAILED


def test_to_dict_shapes():
    evidence = SyncStepEvidence(
        step="a", surface="api", status=STATUS_OK, expected_state="e", observed_state="o",
        timeout_seconds=5.0, polling_attempts=2, device_id="d", build_version="v",
        screenshot_paths=["/tmp/x.png"], api_response_excerpt="{}", detail="note",
    )
    d = evidence.to_dict()
    assert d["step"] == "a"
    assert d["screenshotPaths"] == ["/tmp/x.png"]

    result = SyncFlowResult(flow="event-sync", steps=[evidence], started_at="t0", finished_at="t1")
    rd = result.to_dict()
    assert rd["flow"] == "event-sync"
    assert rd["status"] == STATUS_OK
    assert len(rd["steps"]) == 1


# ── Event flow ─────────────────────────────────────────────────────────


def test_event_flow_modify_on_tablet_is_always_blocked_and_dominates_overall_status():
    env = _make_env()
    result = run_event_sync_flow(env, run_id="test-001", timeout_seconds=1, interval_seconds=0.01)

    modify_step = next(s for s in result.steps if s.step == "modify_on_tablet")
    assert modify_step.status == STATUS_BLOCKED
    assert "TABLET_MUTATION_COVERAGE_GAPS" in modify_step.detail
    # Every other step succeeded, but one honest BLOCKED step must still
    # keep the overall flow from reading as a clean pass.
    assert result.status == STATUS_BLOCKED


def test_event_flow_create_delete_and_tablet_polls_all_succeed_for_real():
    env = _make_env()
    result = run_event_sync_flow(env, run_id="test-002", timeout_seconds=1, interval_seconds=0.01)

    by_step = {s.step: s for s in result.steps}
    assert by_step["create_via_api"].status == STATUS_OK
    assert by_step["poll_tablet_for_creation"].status == STATUS_OK
    assert by_step["delete_via_api"].status == STATUS_OK
    assert by_step["verify_deletion_on_tablet"].status == STATUS_OK
    assert by_step["verify_deletion_via_api"].status == STATUS_OK


def test_event_flow_stops_after_api_create_failure():
    env = _make_env(api_create_event=lambda title: (_ for _ in ()).throw(RuntimeError("network down")))
    result = run_event_sync_flow(env, run_id="test-003", timeout_seconds=1, interval_seconds=0.01)

    assert len(result.steps) == 1
    assert result.steps[0].step == "create_via_api"
    assert result.steps[0].status == STATUS_FAILED
    assert result.status == STATUS_FAILED


def test_event_flow_stops_after_api_delete_failure_but_still_attempted_the_blocked_modify_step():
    env = _make_env(api_delete_event=lambda event_id: (_ for _ in ()).throw(RuntimeError("gone")))
    result = run_event_sync_flow(env, run_id="test-004", timeout_seconds=1, interval_seconds=0.01)

    steps_by_name = [s.step for s in result.steps]
    assert "modify_on_tablet" in steps_by_name
    assert "delete_via_api" in steps_by_name
    assert "verify_deletion_on_tablet" not in steps_by_name
    assert result.status == STATUS_FAILED


def test_event_flow_records_failed_when_tablet_never_shows_the_created_event():
    env = _make_env(tablet_text_present=lambda text: False)
    result = run_event_sync_flow(env, run_id="test-005", timeout_seconds=0.05, interval_seconds=0.01)

    by_step = {s.step: s for s in result.steps}
    assert by_step["poll_tablet_for_creation"].status == STATUS_FAILED
    assert by_step["poll_tablet_for_creation"].polling_attempts >= 1
    # Cleanup (delete) is still attempted even though the tablet check failed.
    assert by_step["delete_via_api"].status == STATUS_OK
    assert result.status == STATUS_FAILED


# ── Task flow ──────────────────────────────────────────────────────────


def test_task_flow_reopen_on_tablet_is_always_blocked():
    env = _make_env()
    result = run_task_sync_flow(env, task_id="task_1", timeout_seconds=1, interval_seconds=0.01)

    reopen_step = next(s for s in result.steps if s.step == "reopen_on_tablet")
    assert reopen_step.status == STATUS_BLOCKED
    assert result.status == STATUS_BLOCKED


def test_task_flow_falls_back_to_api_cleanup_when_task_id_known():
    calls = []
    env = _make_env(api_reopen_task=lambda task_id: calls.append(task_id) or {"id": task_id, "completed": False})
    result = run_task_sync_flow(env, task_id="task_reg_open_001", timeout_seconds=1, interval_seconds=0.01)

    assert calls == ["task_reg_open_001"]
    cleanup_step = next(s for s in result.steps if s.step == "reopen_via_api_cleanup_fallback")
    assert cleanup_step.status == STATUS_OK


def test_task_flow_cleanup_is_blocked_not_attempted_when_task_id_unknown():
    calls = []
    env = _make_env(api_reopen_task=lambda task_id: calls.append(task_id))
    result = run_task_sync_flow(env, task_id=None, timeout_seconds=1, interval_seconds=0.01)

    assert calls == []
    cleanup_step = next(s for s in result.steps if s.step == "reopen_via_api_cleanup_fallback")
    assert cleanup_step.status == STATUS_BLOCKED


def test_task_flow_skips_cleanup_entirely_when_mobile_completion_itself_failed():
    env = _make_env(run_mobile_complete_task=lambda: False)
    result = run_task_sync_flow(env, task_id="task_1", timeout_seconds=1, interval_seconds=0.01)

    step_names = [s.step for s in result.steps]
    assert "reopen_via_api_cleanup_fallback" not in step_names
    complete_step = next(s for s in result.steps if s.step == "complete_on_mobile")
    assert complete_step.status == STATUS_FAILED
    assert result.status == STATUS_FAILED


def test_task_flow_records_failure_when_mobile_flow_raises():
    env = _make_env(run_mobile_complete_task=lambda: (_ for _ in ()).throw(RuntimeError("flutter crashed")))
    result = run_task_sync_flow(env, task_id="task_1", timeout_seconds=1, interval_seconds=0.01)

    complete_step = next(s for s in result.steps if s.step == "complete_on_mobile")
    assert complete_step.status == STATUS_FAILED
    assert "flutter crashed" in complete_step.detail


# ── Chore flow ─────────────────────────────────────────────────────────


def test_chore_flow_has_no_blocked_steps_when_everything_succeeds():
    env = _make_env()
    result = run_chore_sync_flow(env, timeout_seconds=1, interval_seconds=0.01)

    assert all(s.status != STATUS_BLOCKED for s in result.steps)
    assert result.status == STATUS_OK


def test_chore_flow_fails_when_mobile_toggle_flow_fails():
    env = _make_env(run_mobile_complete_chore=lambda: False)
    result = run_chore_sync_flow(env, timeout_seconds=1, interval_seconds=0.01)

    complete_step = next(s for s in result.steps if s.step == "complete_then_uncomplete_on_mobile")
    assert complete_step.status == STATUS_FAILED
    assert result.status == STATUS_FAILED


# ── run_all_sync_flows ────────────────────────────────────────────────


def test_run_all_sync_flows_returns_all_three_in_order():
    env = _make_env()
    results = run_all_sync_flows(env, run_id="test-all", task_id="task_1", timeout_seconds=1, interval_seconds=0.01)

    assert [r.flow for r in results] == ["event-sync", "task-sync", "chore-sync"]
