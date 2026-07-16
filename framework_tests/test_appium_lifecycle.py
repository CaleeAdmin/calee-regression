"""Tests for automatic Appium lifecycle management (Workstream 8).

All Appium/process interaction is faked (no real Appium binary or network
needed) so these run anywhere, including CI.
"""

from __future__ import annotations

import signal

import pytest
from click.testing import CliRunner

from calee_regression import cli
from calee_regression.appium_lifecycle import (
    AppiumHandle,
    AppiumLifecycleError,
    ensure_appium_running,
    start_appium,
    stop_appium,
    stop_appium_from_pid_file,
)

BASE_URL = "http://127.0.0.1:4723/wd/hub"


class _FakeProcess:
    def __init__(self, pid=4242, exits_immediately=False, returncode=1):
        self.pid = pid
        self._exits_immediately = exits_immediately
        self._returncode_value = returncode
        self.returncode = None
        self.terminated = False

    def poll(self):
        if self._exits_immediately:
            self.returncode = self._returncode_value
            return self.returncode
        return None

    def terminate(self):
        self.terminated = True


def test_existing_healthy_appium_is_left_alone_and_nothing_is_started(tmp_path):
    calls = {"popen": 0}

    def fake_popen(*a, **k):
        calls["popen"] += 1
        return _FakeProcess()

    handle = ensure_appium_running(
        base_url=BASE_URL,
        log_path=tmp_path / "appium.log",
        pid_file=tmp_path / "appium.pid",
        is_healthy=lambda url, timeout_seconds=5: True,
    )

    assert handle.started_by_us is False
    assert handle.pid is None
    assert calls["popen"] == 0
    assert not (tmp_path / "appium.pid").exists()


def test_appium_successfully_started_becomes_healthy_and_pid_file_is_written(tmp_path):
    fake_proc = _FakeProcess(pid=9999)
    health_calls = {"n": 0}

    def fake_is_healthy(url, timeout_seconds=5):
        health_calls["n"] += 1
        return health_calls["n"] >= 2  # unhealthy first check, healthy after "starting"

    handle = ensure_appium_running(
        base_url=BASE_URL,
        log_path=tmp_path / "appium.log",
        pid_file=tmp_path / "appium.pid",
        is_healthy=fake_is_healthy,
        popen=lambda *a, **k: fake_proc,
        which=lambda name: "/usr/local/bin/appium",
        poll_interval_seconds=0,
    )

    assert handle.started_by_us is True
    assert handle.pid == 9999
    assert (tmp_path / "appium.pid").read_text().strip() == "9999"
    assert (tmp_path / "appium.log").parent.is_dir()


def test_appium_startup_timeout_raises_blocked_error_and_cleans_up(tmp_path):
    fake_proc = _FakeProcess(pid=123)

    with pytest.raises(AppiumLifecycleError, match="did not become ready"):
        start_appium(
            base_url=BASE_URL,
            log_path=tmp_path / "appium.log",
            pid_file=tmp_path / "appium.pid",
            is_healthy=lambda url, timeout_seconds=5: False,
            popen=lambda *a, **k: fake_proc,
            which=lambda name: "/usr/local/bin/appium",
            ready_timeout_seconds=0.05,
            poll_interval_seconds=0.01,
        )

    assert fake_proc.terminated is True
    assert not (tmp_path / "appium.pid").exists()


def test_appium_exiting_immediately_raises_blocked_error(tmp_path):
    fake_proc = _FakeProcess(pid=7, exits_immediately=True, returncode=127)

    with pytest.raises(AppiumLifecycleError, match="exited immediately"):
        start_appium(
            base_url=BASE_URL,
            log_path=tmp_path / "appium.log",
            pid_file=tmp_path / "appium.pid",
            is_healthy=lambda url, timeout_seconds=5: False,
            popen=lambda *a, **k: fake_proc,
            which=lambda name: "/usr/local/bin/appium",
            ready_timeout_seconds=5,
            poll_interval_seconds=0.01,
        )
    assert not (tmp_path / "appium.pid").exists()


def test_missing_appium_executable_raises_clear_blocked_message(tmp_path):
    with pytest.raises(AppiumLifecycleError, match="not found"):
        start_appium(
            base_url=BASE_URL,
            log_path=tmp_path / "appium.log",
            pid_file=tmp_path / "appium.pid",
            which=lambda name: None,
            popen=lambda *a, **k: pytest.fail("popen must not be called when appium executable is missing"),
        )


def test_log_path_parent_directory_is_created(tmp_path):
    nested_log = tmp_path / "reports" / "nested" / "appium.log"
    fake_proc = _FakeProcess(pid=1)
    start_appium(
        base_url=BASE_URL,
        log_path=nested_log,
        pid_file=tmp_path / "appium.pid",
        is_healthy=lambda url, timeout_seconds=5: True,
        popen=lambda *a, **k: fake_proc,
        which=lambda name: "/usr/local/bin/appium",
        poll_interval_seconds=0,
    )
    assert nested_log.parent.is_dir()
    assert nested_log.exists()


def test_stop_appium_kills_only_the_recorded_pid(tmp_path):
    pid_file = tmp_path / "appium.pid"
    pid_file.write_text("555")
    killed = []

    handle = AppiumHandle(started_by_us=True, pid=555, pid_file=pid_file, log_path=None, base_url=BASE_URL)
    stop_appium(handle, kill=lambda pid, sig: killed.append((pid, sig)))

    assert killed == [(555, signal.SIGTERM)]
    assert not pid_file.exists()


def test_stop_appium_is_a_noop_when_started_by_us_is_false(tmp_path):
    pid_file = tmp_path / "appium.pid"
    pid_file.write_text("555")
    killed = []

    handle = AppiumHandle(started_by_us=False, pid=None, pid_file=None, log_path=None, base_url=BASE_URL)
    stop_appium(handle, kill=lambda pid, sig: killed.append((pid, sig)))

    assert killed == []
    # A pre-existing, unrelated Appium's pid file (if one happened to
    # exist for some other reason) must never be touched.
    assert pid_file.exists()


def test_stop_appium_refuses_to_kill_if_pid_file_no_longer_matches(tmp_path):
    # Simulates: our process's pid file got overwritten (e.g. a second
    # launcher run started a different Appium) -- must not guess and kill
    # whatever PID is recorded now.
    pid_file = tmp_path / "appium.pid"
    pid_file.write_text("999")  # a different PID than what we started
    killed = []

    handle = AppiumHandle(started_by_us=True, pid=555, pid_file=pid_file, log_path=None, base_url=BASE_URL)
    stop_appium(handle, kill=lambda pid, sig: killed.append((pid, sig)))

    assert killed == []


def test_stop_appium_from_pid_file_stops_and_removes_file(tmp_path):
    pid_file = tmp_path / "appium.pid"
    pid_file.write_text("321")
    killed = []

    stopped = stop_appium_from_pid_file(pid_file, kill=lambda pid, sig: killed.append((pid, sig)))

    assert stopped is True
    assert killed == [(321, signal.SIGTERM)]
    assert not pid_file.exists()


def test_stop_appium_from_pid_file_is_noop_when_no_pid_file_exists(tmp_path):
    killed = []
    stopped = stop_appium_from_pid_file(tmp_path / "does-not-exist.pid", kill=lambda pid, sig: killed.append(pid))
    assert stopped is False
    assert killed == []


def test_stop_appium_cli_command_stops_only_what_it_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_appium_pid_path", lambda: tmp_path / "appium.pid")
    (tmp_path / "appium.pid").write_text("42")
    killed = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))

    runner = CliRunner()
    result = runner.invoke(cli.main, ["stop-appium"])

    assert result.exit_code == 0
    assert killed == [(42, signal.SIGTERM)]
    assert not (tmp_path / "appium.pid").exists()


def test_stop_appium_cli_command_is_a_clean_noop_when_nothing_was_started(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_appium_pid_path", lambda: tmp_path / "appium.pid")
    killed = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))

    runner = CliRunner()
    result = runner.invoke(cli.main, ["stop-appium"])

    assert result.exit_code == 0
    assert killed == []
    assert "Nothing to stop" in result.output
