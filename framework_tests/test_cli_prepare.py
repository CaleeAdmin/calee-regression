"""Tests for the `prepare` CLI command ("01 Prepare Test Environment").

Mocks preflight.run_doctor and fixture_bridge.run_fixture_action so this
covers the command's own decision logic (never claim success it can't back
up) without needing a real device, Appium server, or backend.
"""

from __future__ import annotations

from click.testing import CliRunner

from calee_regression import cli, preflight
from calee_regression.fixture_bridge import FixtureBridgeError
from calee_regression.models import DoctorCheck, EXIT_BLOCKED, EXIT_SUCCESS

_CONFIG_YAML = """
appium_url: "http://127.0.0.1:4723/wd/hub"
device_name: "Calee Test Tablet"
udid: "emulator-5554"
apk_path: "/tmp/calee.apk"
app_package: "com.viso.calee"
app_activity: ".ui.HomeActivity"
shell_package: "com.viso.caleeshell"
shell_activity: ".ui.LauncherActivity"
launch_strategy: "direct_activity"
start_action: "com.viso.calee.action.START"
expected_state: "fresh"
"""


def _config_path(tmp_path):
    path = tmp_path / "tester.local.yaml"
    path.write_text(_CONFIG_YAML)
    return str(path)


def test_prepare_blocks_on_preflight_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        preflight, "run_doctor",
        lambda cfg: [DoctorCheck("adb_available", "error", "adb not found")],
    )
    runner = CliRunner()
    result = runner.invoke(cli.main, ["prepare", "--config", _config_path(tmp_path)])
    assert result.exit_code == EXIT_BLOCKED
    assert "not ready" in result.output.lower()


def test_prepare_succeeds_with_skip_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(cli.main, ["prepare", "--config", _config_path(tmp_path), "--skip-fixture"])
    assert result.exit_code == EXIT_SUCCESS
    assert "skipped" in result.output.lower()


def test_prepare_succeeds_without_fixture_credentials_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    monkeypatch.delenv("CALEE_API_BASE", raising=False)
    monkeypatch.delenv("CALEE_TEST_EMAIL", raising=False)
    monkeypatch.delenv("CALEE_TEST_PASSWORD", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli.main, ["prepare", "--config", _config_path(tmp_path)])
    assert result.exit_code == EXIT_SUCCESS
    assert "fixture reset skipped" in result.output.lower()


def test_prepare_resets_fixture_when_credentials_given(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    monkeypatch.setattr(cli, "run_fixture_action", lambda action, **kwargs: "Fixture reset OK (version=v1)")
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "prepare", "--config", _config_path(tmp_path),
            "--fixture-base-url", "https://hub-dev.calee.com.au",
            "--fixture-email", "demo@example.com",
            "--fixture-password", "secret",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "environment ready" in result.output.lower()


def test_prepare_blocks_when_fixture_reset_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])

    def _raise(action, **kwargs):
        raise FixtureBridgeError("could not log in")

    monkeypatch.setattr(cli, "run_fixture_action", _raise)
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        [
            "prepare", "--config", _config_path(tmp_path),
            "--fixture-base-url", "https://hub-dev.calee.com.au",
            "--fixture-email", "demo@example.com",
            "--fixture-password", "secret",
        ],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "blocked" in result.output.lower()
