"""Tests for the `prepare` CLI command ("01 Prepare Test Environment").

Mocks preflight.run_doctor and fixture_bridge.run_fixture_action so this
covers the command's own decision logic (never claim success/READY it
can't back up) without needing a real device, Appium server, or backend.
"""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

from calee_regression import appium_lifecycle, cli, preflight
from calee_regression.fixture_bridge import FixtureBridgeError
from calee_regression.models import DoctorCheck, EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

_RUN_ID_RE = re.compile(r"Run ID: (\S+)")


def _run_id_from_output(output: str) -> str:
    match = _RUN_ID_RE.search(output)
    assert match, f"prepare did not print a Run ID:\n{output}"
    return match.group(1)


def _environment_report(tmp_path, output: str) -> dict:
    run_id = _run_id_from_output(output)
    path = tmp_path / "reports" / "runs" / run_id / "environment" / "results.json"
    return json.loads(path.read_text())


@pytest.fixture(autouse=True)
def _appium_already_healthy(monkeypatch):
    # These tests cover `prepare`'s own fixture-preparation decision logic
    # (see test_appium_lifecycle.py for Appium lifecycle coverage itself)
    # -- assume Appium is already up so a real network call is never made.
    monkeypatch.setattr(appium_lifecycle, "is_appium_healthy", lambda base_url, timeout_seconds=5: True)


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    # REPO_ROOT drives where the run workspace (reports/runs/<run-id>/) is
    # created -- redirect it under tmp_path so these tests never write into
    # this checkout's working tree.
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)


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
    report = _environment_report(tmp_path, result.output)
    assert report["status"] == "blocked"
    assert any("adb not found" in d for d in report["detail"])


def test_prepare_succeeds_with_skip_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(cli.main, ["prepare", "--config", _config_path(tmp_path), "--skip-fixture"])
    assert result.exit_code == EXIT_SUCCESS
    assert "skipped" in result.output.lower()
    report = _environment_report(tmp_path, result.output)
    assert report["status"] == "pass"


def test_prepare_blocks_without_fixture_credentials_configured(tmp_path, monkeypatch):
    # This is the core fix for the "prepare can return success/READY when
    # fixture credentials were absent" defect: for a release-gating run
    # (no explicit bypass), missing credentials must BLOCK, never silently
    # claim READY.
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    monkeypatch.delenv("CALEE_API_BASE", raising=False)
    monkeypatch.delenv("CALEE_TEST_EMAIL", raising=False)
    monkeypatch.delenv("CALEE_TEST_PASSWORD", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli.main, ["prepare", "--config", _config_path(tmp_path)])
    assert result.exit_code == EXIT_BLOCKED
    assert "blocked" in result.output.lower()
    assert "fixture credentials are not configured" in result.output.lower()
    report = _environment_report(tmp_path, result.output)
    assert report["status"] == "blocked"


def test_prepare_resets_and_verifies_fixture_when_credentials_given(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    calls = []

    def _fake_action(action, **kwargs):
        calls.append(action)
        if action == "reset":
            return "Fixture reset OK (version=regression-fixture-v1, prepared_at=2024-01-01)"
        return "Fixture verify OK (version=regression-fixture-v1)"

    monkeypatch.setattr(cli, "run_fixture_action", _fake_action)
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
    assert calls == ["reset", "verify"]  # verify must run after a successful reset
    assert "secret" not in result.output  # never expose the password

    status = _environment_report(tmp_path, result.output)
    assert status["status"] == "pass"
    assert status["fixtureVersion"] == "regression-fixture-v1"
    assert status["fixtureResetStatus"] == "ok"
    assert status["fixtureVerificationStatus"] == "ok"
    assert status["targetEnvironment"] == "https://hub-dev.calee.com.au"
    assert "runId" in status
    assert "secret" not in json.dumps(status)
    assert "demo@example.com" not in json.dumps(status)


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
    report = _environment_report(tmp_path, result.output)
    assert report["status"] == "blocked"


def test_prepare_blocks_when_fixture_verification_fails_after_successful_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])

    def _fake_action(action, **kwargs):
        if action == "reset":
            return "Fixture reset OK (version=regression-fixture-v1, prepared_at=2024-01-01)"
        raise FixtureBridgeError("REG-EVENT-RECURRING-001 missing")

    monkeypatch.setattr(cli, "run_fixture_action", _fake_action)
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
    assert "verification failed" in result.output.lower()

    status = _environment_report(tmp_path, result.output)
    assert status["status"] == "blocked"
    assert status["fixtureResetStatus"] == "ok"
    assert status["fixtureVerificationStatus"] == "blocked"


def test_prepare_allow_no_fixture_alias_works_like_skip_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(cli.main, ["prepare", "--config", _config_path(tmp_path), "--allow-no-fixture"])
    assert result.exit_code == EXIT_SUCCESS
    assert "skipped" in result.output.lower()


def test_prepare_rejects_skip_fixture_for_a_suite_that_requires_the_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["prepare", "--config", _config_path(tmp_path), "--allow-no-fixture", "--suite", "tablet-full"],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "cannot be silently skipped" in result.output.lower()


def test_prepare_allows_skip_fixture_for_a_suite_that_does_not_require_the_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["prepare", "--config", _config_path(tmp_path), "--allow-no-fixture", "--suite", "smoke-fresh"],
    )
    assert result.exit_code == EXIT_SUCCESS


def test_prepare_rejects_unknown_suite_name(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["prepare", "--config", _config_path(tmp_path), "--allow-no-fixture", "--suite", "not-a-real-suite"],
    )
    assert result.exit_code == EXIT_INVALID_CONFIG


def test_prepare_with_explicit_run_id_uses_it_verbatim(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["prepare", "--config", _config_path(tmp_path), "--skip-fixture", "--run-id", "release-test-fixed-id"],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "Run ID: release-test-fixed-id" in result.output
    report_path = tmp_path / "reports" / "runs" / "release-test-fixed-id" / "environment" / "results.json"
    assert report_path.is_file()
    manifest_path = tmp_path / "reports" / "runs" / "release-test-fixed-id" / "run-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["runId"] == "release-test-fixed-id"
    assert manifest["exitCodes"]["environment"] == EXIT_SUCCESS


def test_prepare_rejects_invalid_run_id(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [DoctorCheck("adb_available", "ok", "found")])
    runner = CliRunner()
    result = runner.invoke(
        cli.main,
        ["prepare", "--config", _config_path(tmp_path), "--skip-fixture", "--run-id", "not valid!"],
    )
    assert result.exit_code == EXIT_INVALID_CONFIG
