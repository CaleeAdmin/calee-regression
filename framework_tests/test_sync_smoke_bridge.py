"""Tests for calee_regression/sync_smoke_bridge.py -- the subprocess
bridges Workstream 11's sync-smoke orchestration shells out to.

Mirrors test_fixture_bridge.py's pattern exactly: a tiny fake sibling
script written to tmp_path, real subprocess execution against it -- no
real network, credentials, device, or CaleeMobile-Regression checkout
needed.
"""

from __future__ import annotations

import stat

import pytest

from calee_regression.sync_smoke_bridge import (
    SyncSmokeBridgeError,
    create_scratch_event,
    delete_event,
    get_calendar,
    get_event,
    reopen_task,
    run_mobile_flow,
    set_calendar_appearance,
)


def _make_sibling_with_api_script(tmp_path, script_body: str):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    api_dir = tmp_path / "CaleeMobile-Regression" / "api"
    api_dir.mkdir(parents=True)
    script = api_dir / "sync_smoke_actions.py"
    script.write_text(script_body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return repo_root


def _make_sibling_with_ui_script(tmp_path, script_body: str):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    ui_dir = tmp_path / "CaleeMobile-Regression" / "ui"
    ui_dir.mkdir(parents=True)
    script = ui_dir / "run_ui_suite.py"
    script.write_text(script_body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return repo_root


# A fake sync_smoke_actions.py that writes a canned JSON payload to
# whatever --report path it's given, and echoes back --title/--event-id/
# --task-id/--calendar-id/--name/--color so tests can assert the CLI args
# were built correctly.
_FAKE_API_SCRIPT = """
import argparse
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument("action")
parser.add_argument("--base-url")
parser.add_argument("--email")
parser.add_argument("--password")
parser.add_argument("--title", default=None)
parser.add_argument("--event-id", default=None)
parser.add_argument("--task-id", default=None)
parser.add_argument("--calendar-id", default=None)
parser.add_argument("--name", default=None)
parser.add_argument("--color", default=None)
parser.add_argument("--report")
args = parser.parse_args()

if args.action == "create-scratch-event":
    payload = {"found": True, "id": "evt_fake_1", "title": args.title, "calendarId": "cal_fake"}
elif args.action == "get-event":
    payload = {"found": True, "id": args.event_id, "title": "whatever"}
elif args.action == "delete-event":
    payload = {"found": False, "id": args.event_id, "alreadyGone": False}
elif args.action == "reopen-task":
    payload = {"found": True, "id": args.task_id, "completed": False}
elif args.action == "get-calendar":
    payload = {"found": True, "id": args.calendar_id, "name": "whatever", "color": "#000000"}
else:  # set-calendar-appearance
    payload = {
        "id": args.calendar_id,
        "name": args.name if args.name is not None else "unchanged-name",
        "color": args.color if args.color is not None else "unchanged-color",
        "_sentFields": sorted(k for k, v in [("name", args.name), ("color", args.color)] if v is not None),
    }

with open(args.report, "w") as f:
    json.dump(payload, f)
sys.exit(0)
"""

_FAILING_API_SCRIPT = """
import sys
print("BLOCKED: could not log in", file=sys.stderr)
sys.exit(3)
"""


def test_create_scratch_event_returns_parsed_report(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAKE_API_SCRIPT)
    result = create_scratch_event(
        repo_root=repo_root, base_url="https://x", email="a@x", password="p", title="REG-SYNC-SMOKE-EVENT-t1",
    )
    assert result == {"found": True, "id": "evt_fake_1", "title": "REG-SYNC-SMOKE-EVENT-t1", "calendarId": "cal_fake"}


def test_get_event_returns_parsed_report(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAKE_API_SCRIPT)
    result = get_event(repo_root=repo_root, base_url="https://x", email="a@x", password="p", event_id="evt_1")
    assert result["found"] is True
    assert result["id"] == "evt_1"


def test_delete_event_returns_parsed_report(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAKE_API_SCRIPT)
    result = delete_event(repo_root=repo_root, base_url="https://x", email="a@x", password="p", event_id="evt_1")
    assert result["found"] is False


def test_reopen_task_returns_parsed_report(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAKE_API_SCRIPT)
    result = reopen_task(repo_root=repo_root, base_url="https://x", email="a@x", password="p", task_id="task_1")
    assert result == {"found": True, "id": "task_1", "completed": False}


def test_get_calendar_returns_parsed_report(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAKE_API_SCRIPT)
    result = get_calendar(
        repo_root=repo_root, base_url="https://x", email="a@x", password="p", calendar_id="regression:regsub",
    )
    assert result["id"] == "regression:regsub"
    assert result["found"] is True


def test_set_calendar_appearance_sends_only_supplied_fields(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAKE_API_SCRIPT)
    result = set_calendar_appearance(
        repo_root=repo_root, base_url="https://x", email="a@x", password="p",
        calendar_id="regression:regsub", fields={"color": "#00A878"},
    )
    assert result["_sentFields"] == ["color"]
    assert result["color"] == "#00A878"


def test_set_calendar_appearance_can_send_both_fields(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAKE_API_SCRIPT)
    result = set_calendar_appearance(
        repo_root=repo_root, base_url="https://x", email="a@x", password="p",
        calendar_id="regression:regsub", fields={"name": "New Name", "color": "#00A878"},
    )
    assert result["_sentFields"] == ["color", "name"]
    assert result["name"] == "New Name"
    assert result["color"] == "#00A878"


def test_raises_when_sibling_missing(tmp_path):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    with pytest.raises(SyncSmokeBridgeError, match="was not found as a sibling"):
        get_event(repo_root=repo_root, base_url="https://x", email="a@x", password="p", event_id="evt_1")


def test_raises_on_nonzero_exit_with_stderr_included(tmp_path):
    repo_root = _make_sibling_with_api_script(tmp_path, _FAILING_API_SCRIPT)
    with pytest.raises(SyncSmokeBridgeError, match="could not log in"):
        get_event(repo_root=repo_root, base_url="https://x", email="a@x", password="p", event_id="evt_1")


# ── CaleeMobile leg ──────────────────────────────────────────────────────

_PASSING_UI_SCRIPT = "import sys\nsys.exit(0)\n"
_FAILING_UI_SCRIPT = "import sys\nsys.exit(3)\n"


def test_run_mobile_flow_returns_true_on_success(tmp_path):
    repo_root = _make_sibling_with_ui_script(tmp_path, _PASSING_UI_SCRIPT)
    ok = run_mobile_flow(
        repo_root=repo_root, target="integration_test/flows/sync_task_complete_test.dart", platform="android",
        email="a@x", password="p", report_dir=tmp_path / "reports",
    )
    assert ok is True


def test_run_mobile_flow_returns_false_on_nonzero_exit(tmp_path):
    repo_root = _make_sibling_with_ui_script(tmp_path, _FAILING_UI_SCRIPT)
    ok = run_mobile_flow(
        repo_root=repo_root, target="integration_test/flows/sync_task_complete_test.dart", platform="android",
        email="a@x", password="p", report_dir=tmp_path / "reports",
    )
    assert ok is False


def test_run_mobile_flow_raises_when_sibling_ui_dir_missing(tmp_path):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    with pytest.raises(SyncSmokeBridgeError, match="was not found as a sibling"):
        run_mobile_flow(
            repo_root=repo_root, target="integration_test/flows/sync_task_complete_test.dart", platform="android",
            email="a@x", password="p", report_dir=tmp_path / "reports",
        )
