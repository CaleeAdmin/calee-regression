"""WS9: the permanent focused-verify orchestration.

Pure-orchestration tests (no device/subprocess) plus a CLI wiring smoke test
with the subprocess runner + Appium hooks faked.
"""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from calee_regression import cli, focused_workflow as fw, models


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


# ── CLI wiring ─────────────────────────────────────────────────────────────
def test_focused_verify_cli_builds_steps_and_owns_appium(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_load_config_or_exit", lambda p: SimpleNamespace(
        appium_url="http://127.0.0.1:4723/wd/hub", device_initialization_mode="standard"))
    monkeypatch.setattr(cli, "_resolved_report_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_ensure_appium_for_command", lambda cfg, **k: cli.AppiumLifecycleState(True, "started", "u"))
    stops = []
    monkeypatch.setattr(cli.appium_lifecycle, "stop_appium_from_pid_file", lambda p: stops.append(p))

    captured = []

    def fake_call(command, **kwargs):
        captured.append(command)
        return 0  # every child passes

    monkeypatch.setattr(cli.subprocess, "call", fake_call)

    result = CliRunner().invoke(cli.main, ["focused-verify", "--config", "x", "--tablet-repeat", "2"])
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    # Appium stopped exactly once (framework-owned cleanup).
    assert len(stops) == 1
    # Standard + diagnostic tablet steps were built with the right mode flag.
    joined = [" ".join(c) for c in captured]
    assert any("run-repeat" in j and "--device-initialization standard" in j for j in joined)
    assert any("run-repeat" in j and "--device-initialization skip" in j for j in joined)
    # Focused API suite twice (immutable invocations) + iPhone target.
    assert sum("caleemobile_regression" in j and "chores-stop-repeating" in j for j in joined) == 2
    assert any("run_ui_suite.py" in j and "app_boot_test.dart" in j for j in joined)
    # No credential ever appears on a child's argv.
    assert not any("password" in j.lower() for j in joined)
