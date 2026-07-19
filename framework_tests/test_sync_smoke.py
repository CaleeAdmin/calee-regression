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
    run_calendar_appearance_sync_flow,
    run_chore_sync_flow,
    run_event_sync_flow,
    run_partial_appearance_override_flow,
    run_task_sync_flow,
)

DEFAULT_APPEARANCE_CALENDAR_ID = "regression:regsub"
DEFAULT_PARTIAL_CALENDAR_ID = "regression:regsub-partial"


def _make_env(**overrides):
    created_events = {}
    next_id = [1]
    calendars = {
        DEFAULT_APPEARANCE_CALENDAR_ID: {
            "name": "REG-SUB Regression Subscription",
            "color": "#111111",
            "sourceName": "REG-SUB Regression Subscription",
            "capabilities": {"canEditEvents": False},
        },
        # Pristine second fixture for run_partial_appearance_override_flow:
        # no local overrides yet (name tracks sourceName, colour tracks
        # sourceColor).
        DEFAULT_PARTIAL_CALENDAR_ID: {
            "name": "REG-SUB-PARTIAL Subscription",
            "color": "#4285F4",
            "sourceName": "REG-SUB-PARTIAL Subscription",
            "sourceColor": "#4285F4",
            "capabilities": {"canEditEvents": False},
        },
    }

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

    def default_set_calendar_appearance(calendar_id, fields):
        cal = calendars.setdefault(
            calendar_id, {"name": "", "color": "", "sourceName": "", "capabilities": {"canEditEvents": False}},
        )
        cal.update(fields)
        return dict(cal)

    def default_get_calendar(calendar_id):
        return dict(calendars.get(calendar_id, {}))

    def default_trigger_calendar_refresh(calendar_id):
        return {"calendarId": calendar_id, "refreshed": True}

    def default_simulate_source_rename(calendar_id, new_source_name):
        # Effective name follows the source only while no local override
        # exists (name still equal to sourceName) -- mirrors the backend's
        # COALESCE semantics for subscription mappings.
        cal = calendars[calendar_id]
        if cal.get("name") == cal.get("sourceName"):
            cal["name"] = new_source_name
        cal["sourceName"] = new_source_name
        return {"calendarId": calendar_id, "sourceRenamedTo": new_source_name}

    def default_simulate_source_color_change(calendar_id, new_source_color):
        cal = calendars[calendar_id]
        if cal.get("color") == cal.get("sourceColor"):
            cal["color"] = new_source_color
        cal["sourceColor"] = new_source_color
        return {"calendarId": calendar_id, "sourceColorChangedTo": new_source_color}

    def default_tablet_text_present(text):
        return (
            text in created_events.values()
            or text.startswith("REG-TASK")
            or text.startswith("REG-CHORE")
            or any(text == cal.get("name") for cal in calendars.values())
        )

    kwargs = dict(
        api_create_event=default_create_event,
        api_get_event=default_get_event,
        api_delete_event=default_delete_event,
        api_reopen_task=lambda task_id: {"id": task_id, "completed": False},
        tablet_text_present=default_tablet_text_present,
        run_mobile_complete_task=lambda: True,
        run_mobile_complete_chore=lambda: True,
        device_id="emulator-5554",
        build_version="1.2.3",
        api_set_calendar_appearance=default_set_calendar_appearance,
        api_get_calendar=default_get_calendar,
        api_trigger_calendar_refresh=default_trigger_calendar_refresh,
        api_simulate_source_rename=default_simulate_source_rename,
        api_simulate_source_color_change=default_simulate_source_color_change,
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


# ── Calendar-appearance flow ──────────────────────────────────────────


def test_calendar_appearance_flow_color_verification_on_tablet_is_always_blocked_and_dominates_overall_status():
    env = _make_env()
    result = run_calendar_appearance_sync_flow(env, run_id="test-001", timeout_seconds=1, interval_seconds=0.01)

    color_step = next(s for s in result.steps if s.step == "verify_color_change_on_tablet")
    assert color_step.status == STATUS_BLOCKED
    assert "CALENDAR_APPEARANCE_REGRESSION" in color_step.detail
    # This step is a PERMANENT gap (no colour-reading primitive at all), not
    # contingent on env wiring -- it must still be blocked even when every
    # other leg is fully wired and succeeding, and that one honest BLOCKED
    # step must still keep the overall flow from reading as a clean pass.
    assert result.status == STATUS_BLOCKED


def test_calendar_appearance_flow_every_other_step_succeeds_for_real_when_fully_wired():
    env = _make_env()
    result = run_calendar_appearance_sync_flow(env, run_id="test-002", timeout_seconds=1, interval_seconds=0.01)

    by_step = {s.step: s for s in result.steps}
    for step_name in (
        "capture_baseline_via_api",
        "rename_via_api",
        "poll_tablet_for_renamed_calendar",
        "change_color_via_api",
        "verify_color_persisted_via_api",
        "trigger_provider_refresh_via_api",
        "verify_override_survives_refresh_via_api",
        "verify_source_name_preserved_via_api",
        "verify_override_survives_refresh_on_tablet",
        "verify_events_non_editable_via_api",
        "verify_events_non_editable_on_tablet_weak_signal",
    ):
        assert by_step[step_name].status == STATUS_OK, f"{step_name}: {by_step[step_name].detail}"


def test_calendar_appearance_flow_renamed_title_includes_run_id():
    env = _make_env()
    result = run_calendar_appearance_sync_flow(env, run_id="run-xyz", timeout_seconds=1, interval_seconds=0.01)

    rename_step = next(s for s in result.steps if s.step == "rename_via_api")
    assert "REG-SYNC-SMOKE-CALENDAR-APPEARANCE-run-xyz" in rename_step.observed_state


def test_calendar_appearance_flow_stops_after_rename_failure():
    env = _make_env(
        api_set_calendar_appearance=lambda cal_id, fields: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    result = run_calendar_appearance_sync_flow(env, run_id="test-003", timeout_seconds=1, interval_seconds=0.01)

    step_names = [s.step for s in result.steps]
    assert step_names == ["capture_baseline_via_api", "rename_via_api"]
    assert result.steps[-1].status == STATUS_FAILED
    assert result.status == STATUS_FAILED


def test_calendar_appearance_flow_stops_after_color_change_failure():
    calls = {"n": 0}

    def flaky_set_appearance(cal_id, fields):
        calls["n"] += 1
        if "color" in fields:
            raise RuntimeError("PATCH .../appearance rejected the colour")
        return {"id": cal_id, **fields}

    env = _make_env(api_set_calendar_appearance=flaky_set_appearance)
    result = run_calendar_appearance_sync_flow(env, run_id="test-004", timeout_seconds=1, interval_seconds=0.01)

    step_names = [s.step for s in result.steps]
    assert step_names == [
        "capture_baseline_via_api", "rename_via_api", "poll_tablet_for_renamed_calendar", "change_color_via_api",
    ]
    assert result.steps[-1].status == STATUS_FAILED
    assert result.status == STATUS_FAILED


def test_calendar_appearance_flow_stops_after_refresh_failure_but_still_ran_the_color_legs():
    env = _make_env(
        api_trigger_calendar_refresh=lambda cal_id: (_ for _ in ()).throw(RuntimeError("refresh endpoint 500")),
    )
    result = run_calendar_appearance_sync_flow(env, run_id="test-005", timeout_seconds=1, interval_seconds=0.01)

    step_names = [s.step for s in result.steps]
    assert "change_color_via_api" in step_names
    assert "verify_color_persisted_via_api" in step_names
    assert "verify_color_change_on_tablet" in step_names
    assert "trigger_provider_refresh_via_api" in step_names
    assert "verify_override_survives_refresh_via_api" not in step_names
    refresh_step = next(s for s in result.steps if s.step == "trigger_provider_refresh_via_api")
    assert refresh_step.status == STATUS_FAILED
    assert result.status == STATUS_FAILED


def test_calendar_appearance_flow_records_blocked_when_appearance_api_not_wired():
    # build_real_environment() does not wire these yet (APPEARANCE_API_NOT_WIRED_DETAIL)
    # -- a None callable must degrade to an honest BLOCKED step, never a crash
    # or a fabricated pass.
    env = _make_env(api_set_calendar_appearance=None, api_get_calendar=None, api_trigger_calendar_refresh=None)
    result = run_calendar_appearance_sync_flow(env, run_id="test-006", timeout_seconds=1, interval_seconds=0.01)

    by_step = {s.step: s for s in result.steps}
    assert by_step["capture_baseline_via_api"].status == STATUS_BLOCKED
    assert by_step["rename_via_api"].status == STATUS_BLOCKED
    for step in result.steps:
        assert "build_real_environment" in step.detail
    assert result.status == STATUS_BLOCKED


def test_calendar_appearance_flow_degrades_per_step_when_only_get_calendar_is_unwired():
    # A partial-wiring scenario distinct from "nothing is wired": proves each
    # step degrades independently based on the ONE callable it actually
    # needs, not a single all-or-nothing gate.
    env = _make_env(api_get_calendar=None)
    result = run_calendar_appearance_sync_flow(env, run_id="test-007", timeout_seconds=1, interval_seconds=0.01)

    by_step = {s.step: s for s in result.steps}
    for blocked_step in (
        "capture_baseline_via_api",
        "verify_color_persisted_via_api",
        "verify_override_survives_refresh_via_api",
        "verify_source_name_preserved_via_api",
        "verify_events_non_editable_via_api",
    ):
        assert by_step[blocked_step].status == STATUS_BLOCKED, blocked_step

    for real_step in (
        "rename_via_api",
        "poll_tablet_for_renamed_calendar",
        "change_color_via_api",
        "trigger_provider_refresh_via_api",
        "verify_override_survives_refresh_on_tablet",
        "verify_events_non_editable_on_tablet_weak_signal",
    ):
        assert by_step[real_step].status == STATUS_OK, f"{real_step}: {by_step[real_step].detail}"


def test_calendar_appearance_flow_source_name_check_fails_if_provider_metadata_actually_changes():
    # A genuine regression scenario: the refresh itself clobbers sourceName
    # (the provider's own metadata was mistakenly overwritten instead of
    # being left alone) -- this must FAIL, not silently pass.
    def refresh_that_clobbers_source_name(cal_id):
        env.api_set_calendar_appearance(cal_id, {"sourceName": "Some Other Provider Name"})
        return {"refreshed": True}

    env = _make_env(api_trigger_calendar_refresh=refresh_that_clobbers_source_name)
    result = run_calendar_appearance_sync_flow(env, run_id="test-008", timeout_seconds=0.05, interval_seconds=0.01)

    source_step = next(s for s in result.steps if s.step == "verify_source_name_preserved_via_api")
    assert source_step.status == STATUS_FAILED
    assert result.status == STATUS_FAILED


def test_calendar_appearance_flow_events_non_editable_check_fails_if_capability_flips_true():
    # Another genuine regression scenario: the calendar's events somehow
    # become editable mid-flow -- the non-editable contract must be actively
    # checked, not assumed.
    def get_calendar_editable(cal_id):
        return {
            "name": "whatever", "color": "whatever", "sourceName": "whatever",
            "capabilities": {"canEditEvents": True},
        }

    env = _make_env(api_get_calendar=get_calendar_editable)
    result = run_calendar_appearance_sync_flow(env, run_id="test-009", timeout_seconds=0.05, interval_seconds=0.01)

    editable_step = next(s for s in result.steps if s.step == "verify_events_non_editable_via_api")
    assert editable_step.status == STATUS_FAILED
    assert result.status == STATUS_FAILED


def test_calendar_appearance_flow_uses_a_custom_calendar_id():
    seen_ids = []

    def recording_set_appearance(cal_id, fields):
        seen_ids.append(cal_id)
        return {"id": cal_id, **fields}

    env = _make_env(api_set_calendar_appearance=recording_set_appearance)
    run_calendar_appearance_sync_flow(env, calendar_id="regression:custom", run_id="test-010", timeout_seconds=1, interval_seconds=0.01)

    assert seen_ids and all(cal_id == "regression:custom" for cal_id in seen_ids)


# ── run_all_sync_flows ────────────────────────────────────────────────


def test_run_all_sync_flows_returns_all_five_in_order():
    env = _make_env()
    results = run_all_sync_flows(env, run_id="test-all", task_id="task_1", timeout_seconds=1, interval_seconds=0.01)

    assert [r.flow for r in results] == [
        "event-sync",
        "task-sync",
        "chore-sync",
        "calendar-appearance-sync",
        "calendar-appearance-partial-override",
    ]


# ── run_partial_appearance_override_flow ──────────────────────────────


class _OverrideModelBackend:
    """Override-model fake: effective name/colour = local override or source
    value, exactly the subscription-mapping/external-calendar semantics the
    real backend implements with COALESCE / conditional SET.
    """

    def __init__(self):
        self.source_name = "REG-SUB-PARTIAL Subscription"
        self.source_color = "#4285F4"
        self.local_name = None
        self.local_color = None
        self.set_calls = []

    def get(self, calendar_id):
        return {
            "id": calendar_id,
            "name": self.local_name or self.source_name,
            "color": self.local_color or self.source_color,
            "sourceName": self.source_name,
            "sourceColor": self.source_color,
        }

    def set_appearance(self, calendar_id, fields):
        self.set_calls.append(dict(fields))
        if "name" in fields:
            self.local_name = fields["name"]
        if "color" in fields:
            self.local_color = fields["color"]
        return self.get(calendar_id)

    def set_appearance_backfilling(self, calendar_id, fields):
        # Adversarial: every PATCH rewrites BOTH fields, back-filling any
        # omitted one from the current effective value -- pinning overrides
        # the user never made. The flow must FAIL against this.
        current = self.get(calendar_id)
        self.set_calls.append(dict(fields))
        self.local_name = fields.get("name", current["name"])
        self.local_color = fields.get("color", current["color"])
        return self.get(calendar_id)

    def simulate_rename(self, calendar_id, new_source_name):
        self.source_name = new_source_name
        return {"sourceRenamedTo": new_source_name}

    def simulate_color_change(self, calendar_id, new_source_color):
        self.source_color = new_source_color
        return {"sourceColorChangedTo": new_source_color}


def _partial_env(backend, **overrides):
    kwargs = dict(
        api_set_calendar_appearance=backend.set_appearance,
        api_get_calendar=backend.get,
        api_trigger_calendar_refresh=lambda calendar_id: {"refreshed": True},
        api_simulate_source_rename=backend.simulate_rename,
        api_simulate_source_color_change=backend.simulate_color_change,
        tablet_text_present=lambda text: text == backend.get("any")["name"],
    )
    kwargs.update(overrides)
    return _make_env(**kwargs)


def test_partial_override_flow_all_ok_and_payloads_omit_unchanged_fields():
    backend = _OverrideModelBackend()
    env = _partial_env(backend)

    result = run_partial_appearance_override_flow(env, run_id="t1", timeout_seconds=1, interval_seconds=0.01)

    assert result.status == STATUS_OK, [s.to_dict() for s in result.steps if s.status != STATUS_OK]
    # The payload-shape contract itself: a colour-only edit sends EXACTLY
    # {"color": ...} (no back-filled name) and a name-only edit sends
    # EXACTLY {"name": ...}.
    assert backend.set_calls == [
        {"color": "#FF9500"},
        {"name": "REG-PARTIAL-LOCAL-NAME-t1"},
    ]
    # And the end state: local overrides for both, source values live.
    final = backend.get("x")
    assert final["name"] == "REG-PARTIAL-LOCAL-NAME-t1"
    assert final["color"] == "#FF9500"
    assert final["sourceName"] == "REG-PARTIAL-SOURCE-RENAME-t1"
    assert final["sourceColor"] == "#00A878"


def test_partial_override_flow_fails_against_a_backend_that_backfills_omitted_fields():
    backend = _OverrideModelBackend()
    env = _partial_env(backend, api_set_calendar_appearance=backend.set_appearance_backfilling)

    result = run_partial_appearance_override_flow(env, run_id="t2", timeout_seconds=0.2, interval_seconds=0.01)

    assert result.status == STATUS_FAILED
    failed = [s.step for s in result.steps if s.status == STATUS_FAILED]
    # The back-filled name pins a local override, so the upstream source
    # rename no longer propagates -- exactly where the flow must catch it.
    assert "verify_source_rename_propagates_and_color_override_remains_via_api" in failed


def test_partial_override_flow_fails_when_fixture_already_has_a_name_override():
    backend = _OverrideModelBackend()
    backend.local_name = "Someone already renamed me"
    env = _partial_env(backend)

    result = run_partial_appearance_override_flow(env, run_id="t3", timeout_seconds=0.2, interval_seconds=0.01)

    assert result.status == STATUS_FAILED
    assert result.steps[0].step == "verify_no_preexisting_name_override_via_api"
    assert result.steps[0].status == STATUS_FAILED
    assert len(result.steps) == 1


def test_partial_override_flow_blocks_honestly_when_source_simulation_is_not_wired():
    backend = _OverrideModelBackend()
    env = _partial_env(backend, api_simulate_source_rename=None, api_simulate_source_color_change=None)

    result = run_partial_appearance_override_flow(env, run_id="t4", timeout_seconds=1, interval_seconds=0.01)

    assert result.status == STATUS_BLOCKED
    blocked = [s for s in result.steps if s.status == STATUS_BLOCKED]
    assert [s.step for s in blocked] == ["simulate_source_rename_via_api"]
    assert "fixture plumbing" in blocked[0].detail
    # Everything before the missing simulator still ran for real.
    ok_steps = [s.step for s in result.steps if s.status == STATUS_OK]
    assert "change_color_only_via_api" in ok_steps
    assert "verify_color_only_edit_created_no_name_override_via_api" in ok_steps


def test_partial_override_flow_blocks_when_appearance_api_is_not_wired_at_all():
    env = _make_env(
        api_set_calendar_appearance=None,
        api_get_calendar=None,
        api_trigger_calendar_refresh=None,
        api_simulate_source_rename=None,
        api_simulate_source_color_change=None,
    )

    result = run_partial_appearance_override_flow(env, timeout_seconds=0.2, interval_seconds=0.01)

    assert result.status == STATUS_BLOCKED
    assert result.steps[0].step == "capture_baseline_via_api"
    assert "build_real_environment" in result.steps[0].detail
