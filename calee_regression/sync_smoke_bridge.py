"""Subprocess bridges for Workstream 11's sync-smoke orchestration.

Two sibling-repo entry points, mirroring fixture_bridge.py's pattern
exactly (never a direct cross-repo Python import -- calee-regression and
CaleeMobile-Regression are independent packages/venvs):

  - CaleeMobile-Regression/api/sync_smoke_actions.py for the API leg
    (create/get/delete a scratch event, reopen a task).
  - CaleeMobile-Regression/ui/run_ui_suite.py for the CaleeMobile leg
    (runs one narrow Dart integration_test flow file).

Every failure funnels through SyncSmokeBridgeError so callers have exactly
one thing to catch and map to a failed/blocked step -- see sync_smoke.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .fixture_bridge import DEFAULT_SIBLING_NAME

SYNC_TASK_COMPLETE_TARGET = "integration_test/flows/sync_task_complete_test.dart"
SYNC_CHORE_COMPLETE_TARGET = "integration_test/flows/sync_chore_complete_test.dart"


class SyncSmokeBridgeError(Exception):
    """Raised when a sync-smoke subprocess bridge itself can't run or
    reports failure. Callers should record this as a failed/blocked step
    (see sync_smoke.py), never crash the whole orchestration run."""


def _find_sibling_with_marker(repo_root: Path, *, marker_relative_path: str) -> "Path | None":
    """Like fixture_bridge.find_sibling_repo, but checks for whatever file
    this specific bridge actually needs -- fixture_bridge's own version is
    hardcoded to check for api/manage_fixture.py regardless of its `name`
    param, so it isn't reusable here for sync_smoke_actions.py/
    run_ui_suite.py without giving a false "not found" for a checkout that
    genuinely has those files but (in a test double) not manage_fixture.py.
    """
    candidate = repo_root.parent / DEFAULT_SIBLING_NAME
    if (candidate / marker_relative_path).is_file():
        return candidate
    return None


def _run_api_action(
    action: str,
    *,
    repo_root: Path,
    base_url: str,
    email: str,
    password: str,
    extra_args: "list[str] | None" = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    sibling = _find_sibling_with_marker(repo_root, marker_relative_path="api/sync_smoke_actions.py")
    if sibling is None:
        raise SyncSmokeBridgeError(
            f"{DEFAULT_SIBLING_NAME} was not found as a sibling directory of this repo "
            f"(expected ../{DEFAULT_SIBLING_NAME}/api/sync_smoke_actions.py)."
        )

    with tempfile.NamedTemporaryFile(mode="r", suffix=".json", delete=False) as tmp:
        report_path = Path(tmp.name)
    try:
        cmd = [
            sys.executable, "sync_smoke_actions.py", action,
            "--base-url", base_url, "--email", email, "--password", password,
            "--report", str(report_path),
        ]
        cmd.extend(extra_args or [])
        try:
            result = subprocess.run(
                cmd, cwd=str(sibling / "api"), capture_output=True, text=True, timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SyncSmokeBridgeError(f"sync_smoke_actions.py {action} timed out after {timeout_seconds}s.") from exc
        except OSError as exc:
            raise SyncSmokeBridgeError(f"Could not run sync_smoke_actions.py: {exc}") from exc

        if result.returncode != 0:
            raise SyncSmokeBridgeError(
                f"sync_smoke_actions.py {action} did not succeed (exit code {result.returncode}).\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncSmokeBridgeError(f"sync_smoke_actions.py {action} did not write a readable report: {exc}") from exc
    finally:
        report_path.unlink(missing_ok=True)


def create_scratch_event(*, repo_root: Path, base_url: str, email: str, password: str, title: str) -> dict[str, Any]:
    return _run_api_action(
        "create-scratch-event", repo_root=repo_root, base_url=base_url, email=email, password=password,
        extra_args=["--title", title],
    )


def get_event(*, repo_root: Path, base_url: str, email: str, password: str, event_id: str) -> dict[str, Any]:
    return _run_api_action(
        "get-event", repo_root=repo_root, base_url=base_url, email=email, password=password,
        extra_args=["--event-id", event_id],
    )


def delete_event(*, repo_root: Path, base_url: str, email: str, password: str, event_id: str) -> dict[str, Any]:
    return _run_api_action(
        "delete-event", repo_root=repo_root, base_url=base_url, email=email, password=password,
        extra_args=["--event-id", event_id],
    )


def reopen_task(*, repo_root: Path, base_url: str, email: str, password: str, task_id: str) -> dict[str, Any]:
    return _run_api_action(
        "reopen-task", repo_root=repo_root, base_url=base_url, email=email, password=password,
        extra_args=["--task-id", task_id],
    )


def run_mobile_flow(
    *,
    repo_root: Path,
    target: str,
    platform: str,
    email: str,
    password: str,
    report_dir: Path,
    device_id: "str | None" = None,
    timeout_seconds: int = 600,
) -> bool:
    """Runs one narrow Dart integration_test flow file via run_ui_suite.py.

    Returns whether it passed. Any BLOCKED/tooling problem (missing
    Flutter, no device, credential problems already surfaced elsewhere)
    also returns False here -- the caller records the step as failed with
    the log path for a human to inspect; see sync_smoke.py's honesty note
    about this being a real leg attempt, not a fabricated pass.
    """
    sibling = _find_sibling_with_marker(repo_root, marker_relative_path="ui/run_ui_suite.py")
    if sibling is None:
        raise SyncSmokeBridgeError(
            f"{DEFAULT_SIBLING_NAME} was not found as a sibling directory of this repo "
            f"(expected ../{DEFAULT_SIBLING_NAME}/ui/run_ui_suite.py)."
        )
    ui_dir = sibling / "ui"

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{Path(target).stem}-results.json"
    log_path = report_dir / f"{Path(target).stem}.log"

    cmd = [
        sys.executable, "run_ui_suite.py",
        "--platform", platform,
        "--target", target,
        "--report", str(report_path),
        "--log", str(log_path),
        "--email", email,
        "--password", password,
    ]
    if device_id:
        cmd.extend(["--device-id", device_id])

    try:
        result = subprocess.run(cmd, cwd=str(ui_dir), capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise SyncSmokeBridgeError(f"run_ui_suite.py --target {target} timed out after {timeout_seconds}s.") from exc
    except OSError as exc:
        raise SyncSmokeBridgeError(f"Could not run run_ui_suite.py: {exc}") from exc

    return result.returncode == 0
