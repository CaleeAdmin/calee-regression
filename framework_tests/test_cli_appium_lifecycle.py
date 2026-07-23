"""WS1: every tablet command (run / suite / run-repeat) owns its Appium
lifecycle -- it ensures the endpoint is available before creating a session and
never depends on a prior `prepare` in the same shell.

All Appium/process interaction is faked; no real Appium binary or device.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from calee_regression import cli, models
from calee_regression.appium_lifecycle import AppiumHandle, AppiumLifecycleError

BASE_URL = "http://127.0.0.1:4723/wd/hub"


def _cfg():
    return SimpleNamespace(
        appium_url=BASE_URL,
        report_dir="/tmp/does-not-matter",
        device_initialization_mode=models.DEVICE_INIT_STANDARD,
        allow_release_technical=True,
    )


# ── _ensure_appium_for_command: the four dispositions ──────────────────────
def test_already_running_is_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_appium_pid_path", lambda: tmp_path / "appium.pid")
    monkeypatch.setattr(
        cli.appium_lifecycle, "ensure_appium_running",
        lambda **k: AppiumHandle(started_by_us=False, pid=None, pid_file=None, log_path=None, base_url=BASE_URL),
    )
    state = cli._ensure_appium_for_command(_cfg(), is_healthy=lambda url, **k: True)
    assert state.available is True
    assert state.state == "already_running"


def test_started_when_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_appium_pid_path", lambda: tmp_path / "appium.pid")
    monkeypatch.setattr(
        cli.appium_lifecycle, "ensure_appium_running",
        lambda **k: AppiumHandle(started_by_us=True, pid=42, pid_file=tmp_path / "appium.pid", log_path=None, base_url=BASE_URL),
    )
    # No prior pid file -> a fresh start, not a restart.
    state = cli._ensure_appium_for_command(_cfg(), is_healthy=lambda url, **k: False)
    assert state.available is True
    assert state.state == "started"


def test_restarted_when_stale_pid_and_unhealthy(monkeypatch, tmp_path):
    pid_file = tmp_path / "appium.pid"
    pid_file.write_text("999")  # a framework-started server that has since died
    monkeypatch.setattr(cli, "_appium_pid_path", lambda: pid_file)
    monkeypatch.setattr(
        cli.appium_lifecycle, "ensure_appium_running",
        lambda **k: AppiumHandle(started_by_us=True, pid=43, pid_file=pid_file, log_path=None, base_url=BASE_URL),
    )
    state = cli._ensure_appium_for_command(_cfg(), is_healthy=lambda url, **k: False)
    assert state.available is True
    assert state.state == "restarted"


def test_unavailable_when_start_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_appium_pid_path", lambda: tmp_path / "appium.pid")

    def boom(**k):
        raise AppiumLifecycleError("appium not found")

    monkeypatch.setattr(cli.appium_lifecycle, "ensure_appium_running", boom)
    state = cli._ensure_appium_for_command(_cfg(), is_healthy=lambda url, **k: False)
    assert state.available is False
    assert state.state == "unavailable"
    assert "appium not found" in state.detail


# ── Command gating: no product scenario starts when Appium is unavailable ──
class _FakeReportBuilder:
    def __init__(self, config, run_name, repo_root=None, out_dir=None):
        self.run_name = run_name

    def diff_dir(self):
        return __import__("pathlib").Path("/tmp")

    def write(self, suite_result):
        return __import__("pathlib").Path("/tmp/report")


class _FakeRunner:
    called = False

    def __init__(self, *a, **k):
        pass

    def run_scenarios(self, *a, **k):
        _FakeRunner.called = True
        result = models.SuiteResult(name="x")
        result.scenarios.append(
            models.ScenarioResult(name="s", file="s", status=models.STATUS_PASSED, steps=[])
        )
        return result


def _patch_common(monkeypatch):
    monkeypatch.setattr(cli, "_load_config_or_exit", lambda p: _cfg())
    monkeypatch.setattr(cli.reporting, "ReportBuilder", _FakeReportBuilder)
    monkeypatch.setattr(cli, "ScenarioRunner", _FakeRunner)
    _FakeRunner.called = False


def test_run_blocks_when_appium_unavailable(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ensure_appium_for_command",
        lambda cfg, **k: cli.AppiumLifecycleState(False, "unavailable", BASE_URL, "down"),
    )
    result = CliRunner().invoke(cli.main, ["run", "--config", "x", "--scenario", "calendar_smoke.yaml"])
    assert result.exit_code == models.EXIT_BLOCKED
    assert _FakeRunner.called is False  # no scenario ever started


def test_run_proceeds_when_appium_available(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ensure_appium_for_command",
        lambda cfg, **k: cli.AppiumLifecycleState(True, "started", BASE_URL),
    )
    result = CliRunner().invoke(cli.main, ["run", "--config", "x", "--scenario", "calendar_smoke.yaml"])
    assert _FakeRunner.called is True
    assert result.exit_code == models.EXIT_SUCCESS


def test_run_repeat_blocks_when_appium_unavailable(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cli, "_ensure_appium_for_command",
        lambda cfg, **k: cli.AppiumLifecycleState(False, "unavailable", BASE_URL, "down"),
    )
    result = CliRunner().invoke(
        cli.main,
        ["run-repeat", "--config", "x", "--scenario", "calendar_smoke.yaml", "--repeat-count", "2"],
    )
    assert result.exit_code == models.EXIT_BLOCKED
    assert _FakeRunner.called is False
