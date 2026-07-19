"""Cross-device sync-smoke orchestration (Workstream 11).

Wires together three independently-testable legs for one release run:
  - API: the Calee Client API, via CaleeMobile-Regression's
    sync_smoke_actions.py bridge script (subprocess -- see
    build_real_environment(), mirrors fixture_bridge.py's pattern).
  - CaleeMobile: a single Dart integration_test flow file, via
    CaleeMobile-Regression's run_ui_suite.py (subprocess).
  - Tablet: this repo's own CaleeDriver/Appium session (in-process).

Every step is bounded (polling.poll_until, never a bare sleep) and every
step's outcome is recorded as a SyncStepEvidence -- source operation,
expected vs. observed state, timeout, polling attempts, device/build info,
screenshot paths, API response excerpts (see docs/RELEASE_POLICY.md).

Honesty limits (see docs/TABLET_MUTATION_COVERAGE_GAPS.md): tablet-side
mutation (editing a calendar event, reopening a task) is not yet possible
-- the resource ids it needs were never confirmed against the real Calee
app. Every flow below still exercises every OTHER leg for real and marks
exactly the blocked step as such; it never fabricates success for a step
that did not actually run. The orchestration logic itself (this module) is
fully unit-tested with fakes -- see framework_tests/test_sync_smoke.py --
independent of whether a real device/backend is available.

run_calendar_appearance_sync_flow (calee-hub-core PATCH
/client/v1/calendars/{id}/appearance; Calee PR CaleeAdmin/Calee#977) carries
two further, DISTINCT honesty limits on top of the tablet-mutation gap
above -- see docs/CALENDAR_APPEARANCE_REGRESSION.md:
  - No colour-reading primitive exists in CaleeDriver at all (not even for
    reading, let alone mutating) -- see appium_driver.py. The tablet-side
    colour-verification step is therefore unconditionally BLOCKED, not
    contingent on env wiring like the steps below.
  - build_real_environment() does not yet wire real implementations of the
    api_set_calendar_appearance/api_get_calendar/api_trigger_calendar_refresh
    callables this flow needs -- doing so needs new CaleeMobile-Regression
    API actions that do not exist today, and CaleeMobile-Regression is out
    of scope for this change. These callables are OPTIONAL on
    SyncSmokeEnvironment (default None); every step that needs one records
    BLOCKED honestly when it is absent, exactly like the tablet-mutation
    steps below do for their own gap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import sync_smoke_bridge
from .polling import poll_until

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"

_STATUS_PRECEDENCE = {STATUS_FAILED: 2, STATUS_BLOCKED: 1, STATUS_OK: 0}

TABLET_MUTATION_GAP_DETAIL = (
    "Tablet-side mutation needs resource ids that have never been confirmed against the real "
    "Calee app -- see docs/TABLET_MUTATION_COVERAGE_GAPS.md. Not attempted."
)

COLOR_ASSERTION_GAP_DETAIL = (
    "No colour-reading primitive exists in CaleeDriver (calee_regression/appium_driver.py offers "
    "only id/text presence, tap, type, clear) -- a tablet-side colour assertion cannot be "
    "implemented without inventing an action the driver doesn't have. See "
    "docs/CALENDAR_APPEARANCE_REGRESSION.md. Not attempted."
)

APPEARANCE_API_NOT_WIRED_DETAIL = (
    "build_real_environment() does not wire this leg yet -- it needs new CaleeMobile-Regression "
    "API actions (set-calendar-appearance / get-calendar / trigger-calendar-refresh) that do not "
    "exist in sync_smoke_actions.py today, and CaleeMobile-Regression is out of scope for this "
    "change. The orchestration logic itself is fully exercised with fakes in "
    "framework_tests/test_sync_smoke.py. Not attempted."
)


@dataclass
class SyncStepEvidence:
    step: str
    surface: str  # "api" | "caleemobile" | "tablet"
    status: str  # ok | blocked | failed
    expected_state: str
    observed_state: "str | None" = None
    timeout_seconds: "float | None" = None
    polling_attempts: "int | None" = None
    device_id: "str | None" = None
    build_version: "str | None" = None
    screenshot_paths: list = field(default_factory=list)
    api_response_excerpt: "str | None" = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "surface": self.surface,
            "status": self.status,
            "expectedState": self.expected_state,
            "observedState": self.observed_state,
            "timeoutSeconds": self.timeout_seconds,
            "pollingAttempts": self.polling_attempts,
            "deviceId": self.device_id,
            "buildVersion": self.build_version,
            "screenshotPaths": list(self.screenshot_paths),
            "apiResponseExcerpt": self.api_response_excerpt,
            "detail": self.detail,
        }


@dataclass
class SyncFlowResult:
    flow: str
    steps: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def status(self) -> str:
        if not self.steps:
            return STATUS_BLOCKED
        return max((s.status for s in self.steps), key=lambda s: _STATUS_PRECEDENCE[s])

    def to_dict(self) -> dict:
        return {
            "flow": self.flow,
            "status": self.status,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class SyncSmokeEnvironment:
    """Injectable operations for one sync-smoke run.

    Real callers get this from build_real_environment(); tests construct
    one directly with fakes -- see framework_tests/test_sync_smoke.py. This
    is the seam that keeps the flow-sequencing logic below testable without
    a real backend, CaleeMobile device, or tablet/Appium session.
    """

    api_create_event: "Callable[[str], dict[str, Any]]"  # title -> {"id": ..., "title": ...}
    api_get_event: "Callable[[str], dict[str, Any]]"  # id -> {"found": bool, ...}
    api_delete_event: "Callable[[str], dict[str, Any]]"  # id -> {"found": False, ...}
    api_reopen_task: "Callable[[str], dict[str, Any]]"  # id -> {"completed": False, ...}
    tablet_text_present: "Callable[[str], bool]"  # single-shot check, no internal polling
    run_mobile_complete_task: "Callable[[], bool]"  # True if the flow passed
    run_mobile_complete_chore: "Callable[[], bool]"
    device_id: "str | None" = None
    build_version: "str | None" = None
    take_screenshot: "Callable[[str], str] | None" = None  # name -> path; None if unavailable
    # Calendar-appearance flow callables (run_calendar_appearance_sync_flow).
    # Optional and default None -- unlike every callable above,
    # build_real_environment() does not yet wire real implementations (see
    # APPEARANCE_API_NOT_WIRED_DETAIL); a None here makes the flow record the
    # dependent steps BLOCKED instead of raising. Tests supply fakes for all
    # three to exercise the full orchestration logic.
    api_set_calendar_appearance: "Callable[[str, dict], dict] | None" = None  # (calendar_id, {field: value}) -> updated calendar
    api_get_calendar: "Callable[[str], dict] | None" = None  # calendar_id -> calendar dict (name/color/sourceName/capabilities/...)
    api_trigger_calendar_refresh: "Callable[[str], dict] | None" = None  # calendar_id -> refresh result


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _maybe_screenshot(env: SyncSmokeEnvironment, name: str) -> list:
    if env.take_screenshot is None:
        return []
    try:
        return [env.take_screenshot(name)]
    except Exception:
        return []


def run_event_sync_flow(
    env: SyncSmokeEnvironment,
    *,
    run_id: str = "adhoc",
    timeout_seconds: float = 60.0,
    interval_seconds: float = 2.0,
) -> SyncFlowResult:
    """create via API -> poll tablet -> [modify on tablet: BLOCKED] -> delete via API -> poll both for deletion.

    The "modify on tablet" leg is recorded BLOCKED, never skipped silently
    and never faked as passing -- see module docstring. Every other leg
    runs for real against whatever env provides.
    """
    steps: list = []
    started = _now()
    title = f"REG-SYNC-SMOKE-EVENT-{run_id}"

    try:
        created = env.api_create_event(title)
        event_id = created["id"]
        steps.append(
            SyncStepEvidence(
                step="create_via_api", surface="api", status=STATUS_OK,
                expected_state=f"event {title!r} created", observed_state=str(created),
                api_response_excerpt=str(created)[:500],
            )
        )
    except Exception as exc:
        steps.append(
            SyncStepEvidence(
                step="create_via_api", surface="api", status=STATUS_FAILED,
                expected_state=f"event {title!r} created", detail=str(exc),
            )
        )
        return SyncFlowResult(flow="event-sync", steps=steps, started_at=started, finished_at=_now())

    poll_created = poll_until(
        lambda: env.tablet_text_present(title), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="poll_tablet_for_creation", surface="tablet",
            status=STATUS_OK if poll_created.succeeded else STATUS_FAILED,
            expected_state=f"{title!r} visible on tablet",
            observed_state="visible" if poll_created.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_created.attempts,
            device_id=env.device_id, build_version=env.build_version,
            screenshot_paths=_maybe_screenshot(env, "sync_event_after_create"),
        )
    )

    steps.append(
        SyncStepEvidence(
            step="modify_on_tablet", surface="tablet", status=STATUS_BLOCKED,
            expected_state="event title changed via a tablet-side edit", detail=TABLET_MUTATION_GAP_DETAIL,
        )
    )

    try:
        deleted = env.api_delete_event(event_id)
        steps.append(
            SyncStepEvidence(
                step="delete_via_api", surface="api", status=STATUS_OK,
                expected_state=f"event {event_id!r} deleted", observed_state=str(deleted),
                api_response_excerpt=str(deleted)[:500],
            )
        )
    except Exception as exc:
        steps.append(
            SyncStepEvidence(
                step="delete_via_api", surface="api", status=STATUS_FAILED,
                expected_state=f"event {event_id!r} deleted", detail=str(exc),
            )
        )
        return SyncFlowResult(flow="event-sync", steps=steps, started_at=started, finished_at=_now())

    poll_gone_tablet = poll_until(
        lambda: not env.tablet_text_present(title), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="verify_deletion_on_tablet", surface="tablet",
            status=STATUS_OK if poll_gone_tablet.succeeded else STATUS_FAILED,
            expected_state=f"{title!r} no longer visible on tablet",
            observed_state="gone" if poll_gone_tablet.succeeded else "still visible after timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_gone_tablet.attempts,
            device_id=env.device_id, build_version=env.build_version,
            screenshot_paths=_maybe_screenshot(env, "sync_event_after_delete"),
        )
    )

    poll_gone_api = poll_until(
        lambda: not env.api_get_event(event_id).get("found"),
        timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="verify_deletion_via_api", surface="api",
            status=STATUS_OK if poll_gone_api.succeeded else STATUS_FAILED,
            expected_state=f"event {event_id!r} absent via API",
            observed_state="absent" if poll_gone_api.succeeded else "still found via API after timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_gone_api.attempts,
            api_response_excerpt=str(poll_gone_api.last_observed)[:500],
        )
    )

    return SyncFlowResult(flow="event-sync", steps=steps, started_at=started, finished_at=_now())


def run_task_sync_flow(
    env: SyncSmokeEnvironment,
    *,
    task_id: "str | None" = None,
    timeout_seconds: float = 60.0,
    interval_seconds: float = 2.0,
) -> SyncFlowResult:
    """poll tablet baseline -> complete on mobile -> poll tablet -> [reopen on tablet: BLOCKED,
    falls back to an API-based reopen purely as cleanup] -> verify final state via API.

    `task_id` is the fixture's REG-TASK-OPEN-001 id, needed only for the
    API-based cleanup fallback (the mobile leg locates the task by its
    fixture title, not by id -- see sync_task_complete_test.dart). Pass
    None if it isn't known; the cleanup step is then recorded BLOCKED too
    instead of guessing an id.
    """
    steps: list = []
    started = _now()
    title = "REG-TASK-OPEN-001"

    poll_baseline = poll_until(
        lambda: env.tablet_text_present(title), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="poll_tablet_baseline", surface="tablet",
            status=STATUS_OK if poll_baseline.succeeded else STATUS_FAILED,
            expected_state=f"{title!r} visible on tablet before any change",
            observed_state="visible" if poll_baseline.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_baseline.attempts,
            device_id=env.device_id, build_version=env.build_version,
        )
    )

    try:
        mobile_ok = env.run_mobile_complete_task()
    except Exception as exc:
        mobile_ok = False
        steps.append(
            SyncStepEvidence(
                step="complete_on_mobile", surface="caleemobile", status=STATUS_FAILED,
                expected_state=f"{title!r} completed via CaleeMobile", detail=str(exc),
            )
        )
    else:
        steps.append(
            SyncStepEvidence(
                step="complete_on_mobile", surface="caleemobile",
                status=STATUS_OK if mobile_ok else STATUS_FAILED,
                expected_state=f"{title!r} completed via CaleeMobile",
                observed_state="sync_task_complete_test.dart passed" if mobile_ok else "flow did not pass",
                device_id=env.device_id, build_version=env.build_version,
            )
        )

    # A weak/partial signal only: on the tablet, "completed" vs "open" is a
    # drawable swap on the row's ivIcon (item_task_list.xml), not a distinct
    # resource id Appium can assert -- so the authoritative completed-state
    # check stays here on the API/mobile side. See
    # docs/TABLET_MUTATION_COVERAGE_GAPS.md. This step proves the task didn't
    # disappear/error on the tablet, not that its completed state is visually
    # reflected there.
    poll_after_complete = poll_until(
        lambda: env.tablet_text_present(title), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="poll_tablet_after_complete_weak_signal", surface="tablet",
            status=STATUS_OK if poll_after_complete.succeeded else STATUS_FAILED,
            expected_state=f"{title!r} still visible on tablet (title-presence only, not a completed-state check)",
            observed_state="visible" if poll_after_complete.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_after_complete.attempts,
            device_id=env.device_id, build_version=env.build_version,
            detail="Weak/partial signal -- see docs/TABLET_MUTATION_COVERAGE_GAPS.md.",
        )
    )

    steps.append(
        SyncStepEvidence(
            step="reopen_on_tablet", surface="tablet", status=STATUS_BLOCKED,
            expected_state=f"{title!r} reopened via a tablet-side action", detail=TABLET_MUTATION_GAP_DETAIL,
        )
    )

    if mobile_ok and task_id:
        try:
            reopened = env.api_reopen_task(task_id)
            steps.append(
                SyncStepEvidence(
                    step="reopen_via_api_cleanup_fallback", surface="api", status=STATUS_OK,
                    expected_state=f"task {task_id!r} reopened (cleanup, not the tested tablet behavior)",
                    observed_state=str(reopened), api_response_excerpt=str(reopened)[:500],
                )
            )
        except Exception as exc:
            steps.append(
                SyncStepEvidence(
                    step="reopen_via_api_cleanup_fallback", surface="api", status=STATUS_FAILED,
                    expected_state=f"task {task_id!r} reopened (cleanup, not the tested tablet behavior)",
                    detail=str(exc),
                )
            )
    elif mobile_ok:
        steps.append(
            SyncStepEvidence(
                step="reopen_via_api_cleanup_fallback", surface="api", status=STATUS_BLOCKED,
                expected_state="task reopened (cleanup, not the tested tablet behavior)",
                detail="No task id was supplied for the cleanup fallback -- the fixture may be left "
                       "completed until the next `prepare` (fixture reset).",
            )
        )

    return SyncFlowResult(flow="task-sync", steps=steps, started_at=started, finished_at=_now())


def run_chore_sync_flow(
    env: SyncSmokeEnvironment,
    *,
    timeout_seconds: float = 60.0,
    interval_seconds: float = 2.0,
) -> SyncFlowResult:
    """poll tablet baseline -> complete-then-un-complete on mobile (self-contained,
    self-cleaning -- see sync_chore_complete_test.dart) -> poll tablet again.

    No tablet leg is BLOCKED here: unlike event/task, the mobile-side
    complete/un-complete toggle is fully confirmed and bidirectional (see
    CaleeMobile's ChoresController.toggleChoreCompletion), so this flow
    never needs a tablet-side mutation at all. The tablet checks are a
    weak/partial signal (title-presence only, see the task flow's
    docstring) since there is no confirmed tablet indicator for a chore's
    completed state either.
    """
    steps: list = []
    started = _now()
    title = "REG-CHORE-REPEATING-001"

    poll_baseline = poll_until(
        lambda: env.tablet_text_present(title), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="poll_tablet_baseline", surface="tablet",
            status=STATUS_OK if poll_baseline.succeeded else STATUS_FAILED,
            expected_state=f"{title!r} visible on tablet before any change",
            observed_state="visible" if poll_baseline.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_baseline.attempts,
            device_id=env.device_id, build_version=env.build_version,
        )
    )

    try:
        mobile_ok = env.run_mobile_complete_chore()
    except Exception as exc:
        mobile_ok = False
        steps.append(
            SyncStepEvidence(
                step="complete_then_uncomplete_on_mobile", surface="caleemobile", status=STATUS_FAILED,
                expected_state=f"{title!r} completed then un-completed via CaleeMobile", detail=str(exc),
            )
        )
    else:
        steps.append(
            SyncStepEvidence(
                step="complete_then_uncomplete_on_mobile", surface="caleemobile",
                status=STATUS_OK if mobile_ok else STATUS_FAILED,
                expected_state=f"{title!r} completed then un-completed via CaleeMobile",
                observed_state="sync_chore_complete_test.dart passed" if mobile_ok else "flow did not pass",
                device_id=env.device_id, build_version=env.build_version,
            )
        )

    poll_final = poll_until(
        lambda: env.tablet_text_present(title), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="poll_tablet_final_weak_signal", surface="tablet",
            status=STATUS_OK if poll_final.succeeded else STATUS_FAILED,
            expected_state=f"{title!r} still visible on tablet (title-presence only)",
            observed_state="visible" if poll_final.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_final.attempts,
            device_id=env.device_id, build_version=env.build_version,
            detail="Weak/partial signal -- see docs/TABLET_MUTATION_COVERAGE_GAPS.md.",
        )
    )

    return SyncFlowResult(flow="chore-sync", steps=steps, started_at=started, finished_at=_now())


def run_calendar_appearance_sync_flow(
    env: SyncSmokeEnvironment,
    *,
    calendar_id: str = "regression:regsub",
    run_id: str = "adhoc",
    new_color: str = "#3E7BFA",
    timeout_seconds: float = 60.0,
    interval_seconds: float = 2.0,
) -> SyncFlowResult:
    """capture a baseline -> rename via API/mobile -> poll tablet for the new
    name -> change colour via API -> verify it persisted -> [verify the
    colour change on the tablet: BLOCKED, no colour-reading primitive] ->
    trigger a provider/subscription refresh -> verify the local name+colour
    override survived it (API and tablet) and the provider's own sourceName
    was never touched -> confirm the calendar's events still report as
    non-editable (API and a tablet weak signal).

    Models the genuinely cross-device half of the calendar appearance-
    editing contract (calee-hub-core PATCH /client/v1/calendars/{id}/appearance;
    Calee PR CaleeAdmin/Calee#977, commit
    f1b92ddae9275cb0abea0f6df34126930e3aa71d) that a single YAML scenario
    structurally cannot express -- this repo's ScenarioRunner drives exactly
    one CaleeDriver/one device per run (runner.py:536). See
    scenarios/calendar_appearance_subscription.yaml (and its owned/
    shared-readonly siblings) for the single-device-observable half: does
    the tablet show the right dialog/note for the right calendar type.

    Design choices, and why:

    * The rename/recolour "via API" leg stands in for "via API or
      CaleeMobile" -- both ultimately call the same PATCH .../appearance
      endpoint, and CaleeMobile-Regression has no dedicated appearance-
      editing UI flow to shell out to yet. This is the same substitution
      run_event_sync_flow already makes ("created via API" stands for
      "created from off-tablet").
    * Colour is changed via the API leg, never the tablet leg: CaleeDriver
      has no colour-reading primitive at all (see
      docs/CALENDAR_APPEARANCE_REGRESSION.md), so there is no honest way to
      verify a tablet-INITIATED colour change either happened or
      propagated. The colour change is instead exercised for real via the
      API (set -> a fresh GET confirms the persisted hex value, the same
      colour-independent proxy the YAML scenarios use), while the tablet-
      side colour-verification step stays permanently BLOCKED and says so
      -- never faked as passing, and NOT contingent on env wiring the way
      the steps below are (it can never become attemptable just because a
      real API/tablet session is available).
    * sourceName preservation (the "external-provider name-change-
      preserves-provider-name" requirement) is checked against a baseline
      captured before any mutation, proving the rename only ever changed
      the local Calee display, never the provider's own metadata -- exactly
      what appearanceMode subscription_mapping/external_calendar promise.

    Every callable this flow needs beyond tablet_text_present is OPTIONAL on
    SyncSmokeEnvironment (defaults to None): unlike the event/task/chore
    flows, build_real_environment() does not yet wire real implementations
    (see APPEARANCE_API_NOT_WIRED_DETAIL) -- when one is None, every step
    that needs it records BLOCKED honestly instead of raising, and steps
    that don't need it still run for real. The orchestration/evidence-
    recording logic itself is fully exercised with fakes in
    framework_tests/test_sync_smoke.py.
    """
    steps: list = []
    started = _now()
    new_name = f"REG-SYNC-SMOKE-CALENDAR-APPEARANCE-{run_id}"

    baseline: "dict | None" = None
    if env.api_get_calendar is None:
        steps.append(
            SyncStepEvidence(
                step="capture_baseline_via_api", surface="api", status=STATUS_BLOCKED,
                expected_state=f"a baseline GET of {calendar_id!r} before any appearance change",
                detail=APPEARANCE_API_NOT_WIRED_DETAIL,
            )
        )
    else:
        try:
            baseline = env.api_get_calendar(calendar_id)
            steps.append(
                SyncStepEvidence(
                    step="capture_baseline_via_api", surface="api", status=STATUS_OK,
                    expected_state=f"a baseline GET of {calendar_id!r} before any appearance change",
                    observed_state=str(baseline)[:500], api_response_excerpt=str(baseline)[:500],
                )
            )
        except Exception as exc:
            steps.append(
                SyncStepEvidence(
                    step="capture_baseline_via_api", surface="api", status=STATUS_FAILED,
                    expected_state=f"a baseline GET of {calendar_id!r} before any appearance change",
                    detail=str(exc),
                )
            )

    if env.api_set_calendar_appearance is None:
        steps.append(
            SyncStepEvidence(
                step="rename_via_api", surface="api", status=STATUS_BLOCKED,
                expected_state=f"calendar {calendar_id!r} appearance name set to {new_name!r}",
                detail=APPEARANCE_API_NOT_WIRED_DETAIL,
            )
        )
        return SyncFlowResult(flow="calendar-appearance-sync", steps=steps, started_at=started, finished_at=_now())

    try:
        renamed = env.api_set_calendar_appearance(calendar_id, {"name": new_name})
        steps.append(
            SyncStepEvidence(
                step="rename_via_api", surface="api", status=STATUS_OK,
                expected_state=f"calendar {calendar_id!r} appearance name set to {new_name!r}",
                observed_state=str(renamed), api_response_excerpt=str(renamed)[:500],
            )
        )
    except Exception as exc:
        steps.append(
            SyncStepEvidence(
                step="rename_via_api", surface="api", status=STATUS_FAILED,
                expected_state=f"calendar {calendar_id!r} appearance name set to {new_name!r}", detail=str(exc),
            )
        )
        return SyncFlowResult(flow="calendar-appearance-sync", steps=steps, started_at=started, finished_at=_now())

    poll_name = poll_until(
        lambda: env.tablet_text_present(new_name), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="poll_tablet_for_renamed_calendar", surface="tablet",
            status=STATUS_OK if poll_name.succeeded else STATUS_FAILED,
            expected_state=f"{new_name!r} visible on the tablet's calendar list",
            observed_state="visible" if poll_name.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_name.attempts,
            device_id=env.device_id, build_version=env.build_version,
            screenshot_paths=_maybe_screenshot(env, "calendar_appearance_after_rename"),
        )
    )

    try:
        env.api_set_calendar_appearance(calendar_id, {"color": new_color})
        steps.append(
            SyncStepEvidence(
                step="change_color_via_api", surface="api", status=STATUS_OK,
                expected_state=f"calendar {calendar_id!r} appearance colour set to {new_color!r}",
                observed_state=f"requested {new_color!r}",
            )
        )
    except Exception as exc:
        steps.append(
            SyncStepEvidence(
                step="change_color_via_api", surface="api", status=STATUS_FAILED,
                expected_state=f"calendar {calendar_id!r} appearance colour set to {new_color!r}", detail=str(exc),
            )
        )
        return SyncFlowResult(flow="calendar-appearance-sync", steps=steps, started_at=started, finished_at=_now())

    if env.api_get_calendar is None:
        steps.append(
            SyncStepEvidence(
                step="verify_color_persisted_via_api", surface="api", status=STATUS_BLOCKED,
                expected_state=f"a fresh GET of {calendar_id!r} reports colour {new_color!r}",
                detail=APPEARANCE_API_NOT_WIRED_DETAIL,
            )
        )
    else:
        poll_color = poll_until(
            lambda: env.api_get_calendar(calendar_id), timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds, is_success=lambda observed: observed.get("color") == new_color,
        )
        steps.append(
            SyncStepEvidence(
                step="verify_color_persisted_via_api", surface="api",
                status=STATUS_OK if poll_color.succeeded else STATUS_FAILED,
                expected_state=f"a fresh GET of {calendar_id!r} reports colour {new_color!r}",
                observed_state=str(poll_color.last_observed)[:500],
                timeout_seconds=timeout_seconds, polling_attempts=poll_color.attempts,
                api_response_excerpt=str(poll_color.last_observed)[:500],
            )
        )

    # A DIFFERENT, permanent gap from tablet-mutation above: no colour-
    # reading primitive exists in CaleeDriver at all, so this can never be
    # attempted regardless of env wiring -- see COLOR_ASSERTION_GAP_DETAIL.
    steps.append(
        SyncStepEvidence(
            step="verify_color_change_on_tablet", surface="tablet", status=STATUS_BLOCKED,
            expected_state="the tablet visually reflects the new colour", detail=COLOR_ASSERTION_GAP_DETAIL,
        )
    )

    if env.api_trigger_calendar_refresh is None:
        steps.append(
            SyncStepEvidence(
                step="trigger_provider_refresh_via_api", surface="api", status=STATUS_BLOCKED,
                expected_state=f"calendar {calendar_id!r} refreshed from its provider/subscription source",
                detail=APPEARANCE_API_NOT_WIRED_DETAIL,
            )
        )
        return SyncFlowResult(flow="calendar-appearance-sync", steps=steps, started_at=started, finished_at=_now())

    try:
        refreshed = env.api_trigger_calendar_refresh(calendar_id)
        steps.append(
            SyncStepEvidence(
                step="trigger_provider_refresh_via_api", surface="api", status=STATUS_OK,
                expected_state=f"calendar {calendar_id!r} refreshed from its provider/subscription source",
                observed_state=str(refreshed), api_response_excerpt=str(refreshed)[:500],
            )
        )
    except Exception as exc:
        steps.append(
            SyncStepEvidence(
                step="trigger_provider_refresh_via_api", surface="api", status=STATUS_FAILED,
                expected_state=f"calendar {calendar_id!r} refreshed from its provider/subscription source",
                detail=str(exc),
            )
        )
        return SyncFlowResult(flow="calendar-appearance-sync", steps=steps, started_at=started, finished_at=_now())

    if env.api_get_calendar is None:
        steps.append(
            SyncStepEvidence(
                step="verify_override_survives_refresh_via_api", surface="api", status=STATUS_BLOCKED,
                expected_state=f"{calendar_id!r} still reports name {new_name!r} and colour {new_color!r} after refresh",
                detail=APPEARANCE_API_NOT_WIRED_DETAIL,
            )
        )
    else:
        poll_survives = poll_until(
            lambda: env.api_get_calendar(calendar_id), timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            is_success=lambda observed: observed.get("name") == new_name and observed.get("color") == new_color,
        )
        steps.append(
            SyncStepEvidence(
                step="verify_override_survives_refresh_via_api", surface="api",
                status=STATUS_OK if poll_survives.succeeded else STATUS_FAILED,
                expected_state=f"{calendar_id!r} still reports name {new_name!r} and colour {new_color!r} after refresh",
                observed_state=str(poll_survives.last_observed)[:500],
                timeout_seconds=timeout_seconds, polling_attempts=poll_survives.attempts,
                api_response_excerpt=str(poll_survives.last_observed)[:500],
            )
        )

    if baseline is None:
        steps.append(
            SyncStepEvidence(
                step="verify_source_name_preserved_via_api", surface="api", status=STATUS_BLOCKED,
                expected_state="sourceName unchanged from its pre-rename baseline (proves the rename was local-only)",
                detail="No baseline was captured (capture_baseline_via_api was blocked or failed) -- "
                       "nothing to compare against.",
            )
        )
    else:
        original_source_name = baseline.get("sourceName")
        poll_source = poll_until(
            lambda: env.api_get_calendar(calendar_id), timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            is_success=lambda observed: observed.get("sourceName") == original_source_name,
        )
        steps.append(
            SyncStepEvidence(
                step="verify_source_name_preserved_via_api", surface="api",
                status=STATUS_OK if poll_source.succeeded else STATUS_FAILED,
                expected_state=f"sourceName remains {original_source_name!r} -- the rename only ever changed "
                                "the local Calee display, never the provider's own metadata",
                observed_state=str(poll_source.last_observed)[:500],
                timeout_seconds=timeout_seconds, polling_attempts=poll_source.attempts,
                api_response_excerpt=str(poll_source.last_observed)[:500],
            )
        )

    # Tablet-side survival is checked by NAME only -- colour still has no
    # tablet-side assertion primitive (see verify_color_change_on_tablet).
    poll_survives_tablet = poll_until(
        lambda: env.tablet_text_present(new_name), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="verify_override_survives_refresh_on_tablet", surface="tablet",
            status=STATUS_OK if poll_survives_tablet.succeeded else STATUS_FAILED,
            expected_state=f"{new_name!r} (name only -- see the colour gap above) still visible on the tablet after refresh",
            observed_state="visible" if poll_survives_tablet.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_survives_tablet.attempts,
            device_id=env.device_id, build_version=env.build_version,
            screenshot_paths=_maybe_screenshot(env, "calendar_appearance_after_refresh"),
        )
    )

    if env.api_get_calendar is None:
        steps.append(
            SyncStepEvidence(
                step="verify_events_non_editable_via_api", surface="api", status=STATUS_BLOCKED,
                expected_state=f"{calendar_id!r} capabilities.canEditEvents remains false throughout",
                detail=APPEARANCE_API_NOT_WIRED_DETAIL,
            )
        )
    else:
        try:
            observed = env.api_get_calendar(calendar_id)
            still_non_editable = not bool((observed.get("capabilities") or {}).get("canEditEvents"))
            steps.append(
                SyncStepEvidence(
                    step="verify_events_non_editable_via_api", surface="api",
                    status=STATUS_OK if still_non_editable else STATUS_FAILED,
                    expected_state=f"{calendar_id!r} capabilities.canEditEvents remains false throughout",
                    observed_state=str(observed)[:500], api_response_excerpt=str(observed)[:500],
                )
            )
        except Exception as exc:
            steps.append(
                SyncStepEvidence(
                    step="verify_events_non_editable_via_api", surface="api", status=STATUS_FAILED,
                    expected_state=f"{calendar_id!r} capabilities.canEditEvents remains false throughout",
                    detail=str(exc),
                )
            )

    # Weak/partial signal only -- the same idiom run_task_sync_flow/
    # run_chore_sync_flow use for a tablet-side check with no dedicated
    # assertion primitive. This proves the calendar's events are still
    # rendering on the tablet, not independently that Edit/Delete controls
    # are absent from any one event's detail dialog (that shape is already
    # proven, single-device, by scenarios/subscribed_calendar.yaml's
    # fail_if_id btnEventDetailEdit/btnEventDetailDelete for the
    # subscription type). The authoritative non-editable check for this
    # flow is verify_events_non_editable_via_api above.
    poll_events_visible = poll_until(
        lambda: env.tablet_text_present(new_name), timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
    )
    steps.append(
        SyncStepEvidence(
            step="verify_events_non_editable_on_tablet_weak_signal", surface="tablet",
            status=STATUS_OK if poll_events_visible.succeeded else STATUS_FAILED,
            expected_state=f"{calendar_id!r} still renders on the tablet (title-presence only, not an editability check)",
            observed_state="visible" if poll_events_visible.succeeded else "not visible before timeout",
            timeout_seconds=timeout_seconds, polling_attempts=poll_events_visible.attempts,
            device_id=env.device_id, build_version=env.build_version,
            detail="Weak/partial signal -- see docs/CALENDAR_APPEARANCE_REGRESSION.md.",
        )
    )

    return SyncFlowResult(flow="calendar-appearance-sync", steps=steps, started_at=started, finished_at=_now())


def build_real_environment(
    *,
    repo_root: Path,
    base_url: str,
    email: str,
    password: str,
    platform: str,
    report_dir: Path,
    tablet_driver: "Any | None",
    device_id: "str | None" = None,
    build_version: "str | None" = None,
) -> SyncSmokeEnvironment:
    """Assembles a real, subprocess/Appium-backed environment.

    `tablet_driver` should be an already-started CaleeDriver (see
    appium_driver.py) -- this function does not manage that session's
    lifecycle, matching ScenarioRunner's separation: whoever starts the
    session decides when to quit() it, possibly across several sync flows.
    Pass None if no tablet session is available; every tablet_text_present
    call then returns False (recorded as a real failed poll, never faked).
    """

    def _tablet_text_present(text: str) -> bool:
        if tablet_driver is None:
            return False
        return tablet_driver.text_present(text)

    def _screenshot(name: str) -> str:
        target = report_dir / "screenshots" / f"{name}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        tablet_driver.screenshot(target)
        return str(target)

    return SyncSmokeEnvironment(
        api_create_event=lambda title: sync_smoke_bridge.create_scratch_event(
            repo_root=repo_root, base_url=base_url, email=email, password=password, title=title,
        ),
        api_get_event=lambda event_id: sync_smoke_bridge.get_event(
            repo_root=repo_root, base_url=base_url, email=email, password=password, event_id=event_id,
        ),
        api_delete_event=lambda event_id: sync_smoke_bridge.delete_event(
            repo_root=repo_root, base_url=base_url, email=email, password=password, event_id=event_id,
        ),
        api_reopen_task=lambda task_id: sync_smoke_bridge.reopen_task(
            repo_root=repo_root, base_url=base_url, email=email, password=password, task_id=task_id,
        ),
        tablet_text_present=_tablet_text_present,
        run_mobile_complete_task=lambda: sync_smoke_bridge.run_mobile_flow(
            repo_root=repo_root, target=sync_smoke_bridge.SYNC_TASK_COMPLETE_TARGET, platform=platform,
            email=email, password=password, report_dir=report_dir, device_id=device_id,
        ),
        run_mobile_complete_chore=lambda: sync_smoke_bridge.run_mobile_flow(
            repo_root=repo_root, target=sync_smoke_bridge.SYNC_CHORE_COMPLETE_TARGET, platform=platform,
            email=email, password=password, report_dir=report_dir, device_id=device_id,
        ),
        device_id=device_id,
        build_version=build_version,
        take_screenshot=_screenshot if tablet_driver is not None else None,
    )


def run_all_sync_flows(
    env: SyncSmokeEnvironment,
    *,
    run_id: str = "adhoc",
    task_id: "str | None" = None,
    calendar_id: str = "regression:regsub",
    timeout_seconds: float = 60.0,
    interval_seconds: float = 2.0,
) -> "list[SyncFlowResult]":
    """Runs every sync-smoke flow for one release run, in order.

    Every flow's report feeds into the SAME `sync-smoke` CLI component/
    report (`reports/runs/<run-id>/sync/results.json`), so
    run_calendar_appearance_sync_flow rides the existing, already-release-
    gating `release_features.synchronization` feature (see
    docs/SUITE_REFERENCE.md's "sync-smoke" section and
    docs/RELEASE_POLICY.md) -- component_from_sync_report in
    consolidated_report.py is flow-count-agnostic, so no separate feature
    flag or consolidation wiring is needed for a 4th flow to be
    release-gating the same way the first three already are.
    """
    return [
        run_event_sync_flow(env, run_id=run_id, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds),
        run_task_sync_flow(
            env, task_id=task_id, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
        ),
        run_chore_sync_flow(env, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds),
        run_calendar_appearance_sync_flow(
            env, calendar_id=calendar_id, run_id=run_id,
            timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
        ),
    ]
