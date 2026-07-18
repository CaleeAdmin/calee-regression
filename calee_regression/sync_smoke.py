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
    timeout_seconds: float = 60.0,
    interval_seconds: float = 2.0,
) -> "list[SyncFlowResult]":
    return [
        run_event_sync_flow(env, run_id=run_id, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds),
        run_task_sync_flow(
            env, task_id=task_id, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds,
        ),
        run_chore_sync_flow(env, timeout_seconds=timeout_seconds, interval_seconds=interval_seconds),
    ]
