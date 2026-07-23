"""Fixture-only vs tablet-environment preparation (this session's Workstream
1): fixture preparation never needs Appium; tablet preparation validates
Appium/ADB/device/APK without touching the fixture; the full-release
`prepare` stays strict (Appium + preflight first, then the SAME fixture flow).
"""

from __future__ import annotations

import json
import re

import pytest
from click.testing import CliRunner

from calee_regression import appium_lifecycle, cli, preflight
from calee_regression.fixture_bridge import FixtureBridgeError
from calee_regression.models import DoctorCheck, EXIT_BLOCKED, EXIT_SUCCESS

_RUN_ID_RE = re.compile(r"Run ID: (\S+)")

CREDS = ["--fixture-base-url", "https://staging.calee.invalid",
         "--fixture-email", "e@example.com", "--fixture-password", "pw"]


def _report(tmp_path, output, component="environment"):
    run_id = _RUN_ID_RE.search(output).group(1)
    path = tmp_path / "reports" / "runs" / run_id / component / "results.json"
    return json.loads(path.read_text())


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)


@pytest.fixture
def _fixture_bridge_ok(monkeypatch):
    monkeypatch.setattr(
        cli, "run_fixture_action",
        lambda action, **kwargs: f"{action} ok (version=REG-9)")


def _appium_down(monkeypatch):
    def _fail(cfg, **kwargs):
        raise AssertionError("Appium must not be consulted by prepare-fixture")
    monkeypatch.setattr(cli, "_ensure_appium_or_echo_blocked", _fail)
    monkeypatch.setattr(
        appium_lifecycle, "is_appium_healthy",
        lambda base_url, timeout_seconds=5: False)


# ── prepare-fixture: Appium-independent ────────────────────────────────────
def test_prepare_fixture_passes_with_appium_completely_unavailable(
        tmp_path, monkeypatch, _fixture_bridge_ok):
    _appium_down(monkeypatch)
    result = CliRunner().invoke(cli.main, ["prepare-fixture", *CREDS, "--suite", "calendar"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    report = _report(tmp_path, result.output)
    assert report["reportType"] == "fixture-preparation"
    assert report["reportSchemaVersion"] == 1
    assert report["preparationScope"] == "fixture-only"
    assert report["fixtureVerificationStatus"] == "ok"
    assert report["fixtureVersion"] == "REG-9"
    assert report["targetEnvironment"] == "https://staging.calee.invalid"
    # never a credential in the report
    text = json.dumps(report)
    assert "pw" not in text.split('"')  # value never present as a field value


def test_prepare_fixture_blocks_without_credentials(tmp_path, monkeypatch):
    _appium_down(monkeypatch)
    monkeypatch.delenv("CALEE_API_BASE", raising=False)
    monkeypatch.delenv("CALEE_TEST_EMAIL", raising=False)
    monkeypatch.delenv("CALEE_TEST_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "_fill_credentials_from_providers", lambda e, p: (None, None, None))
    result = CliRunner().invoke(cli.main, ["prepare-fixture"])
    assert result.exit_code == EXIT_BLOCKED
    report = _report(tmp_path, result.output)
    assert report["fixtureResetStatus"] == "blocked_missing_credentials"


def test_prepare_fixture_blocks_on_failed_verification(tmp_path, monkeypatch):
    _appium_down(monkeypatch)

    def bridge(action, **kwargs):
        if action == "verify":
            raise FixtureBridgeError("verify failed")
        return "reset ok (version=REG-9)"

    monkeypatch.setattr(cli, "run_fixture_action", bridge)
    result = CliRunner().invoke(cli.main, ["prepare-fixture", *CREDS])
    assert result.exit_code == EXIT_BLOCKED
    report = _report(tmp_path, result.output)
    assert report["fixtureVerificationStatus"] == "blocked"


# ── prepare-tablet-environment: tooling only, fixture untouched ────────────
def test_prepare_tablet_environment_validates_tooling_without_fixture(
        tmp_path, monkeypatch):
    def _no_fixture(action, **kwargs):
        raise AssertionError("tablet preparation must never touch the fixture")
    monkeypatch.setattr(cli, "run_fixture_action", _no_fixture)
    monkeypatch.setattr(cli, "_ensure_appium_or_echo_blocked", lambda cfg, **k: True)
    monkeypatch.setattr(preflight, "run_doctor", lambda cfg: [
        DoctorCheck(name="adb", status="ok", message="ok"),
        DoctorCheck(name="device", status="ok", message="connected"),
    ])
    config = tmp_path / "t.yaml"
    config.write_text(
        'appium_url: "http://127.0.0.1:4723/wd/hub"\ndevice_name: "t"\nudid: "e-5554"\n'
        'apk_path: "/tmp/a.apk"\napp_package: "com.viso.calee"\napp_activity: ".ui.H"\n'
        'shell_package: "s"\nshell_activity: ".s"\nlaunch_strategy: "direct_activity"\n'
        'start_action: "a"\nexpected_state: "fresh"\n')
    result = CliRunner().invoke(cli.main, ["prepare-tablet-environment", "--config", str(config)])
    assert result.exit_code == EXIT_SUCCESS, result.output
    report = _report(tmp_path, result.output, component="tablet-environment")
    assert report["reportType"] == "tablet-environment-preparation"
    assert report["status"] == "pass"
    assert {c["name"] for c in report["checks"]} == {"adb", "device"}


def test_prepare_tablet_environment_blocks_when_appium_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ensure_appium_or_echo_blocked", lambda cfg, **k: False)
    config = tmp_path / "t.yaml"
    config.write_text(
        'appium_url: "http://127.0.0.1:4723/wd/hub"\ndevice_name: "t"\nudid: "e-5554"\n'
        'apk_path: "/tmp/a.apk"\napp_package: "com.viso.calee"\napp_activity: ".ui.H"\n'
        'shell_package: "s"\nshell_activity: ".s"\nlaunch_strategy: "direct_activity"\n'
        'start_action: "a"\nexpected_state: "fresh"\n')
    result = CliRunner().invoke(cli.main, ["prepare-tablet-environment", "--config", str(config)])
    assert result.exit_code == EXIT_BLOCKED
    report = _report(tmp_path, result.output, component="tablet-environment")
    assert report["status"] == "blocked"


# ── full-release prepare stays strict ──────────────────────────────────────
def test_full_release_prepare_still_blocks_when_appium_unavailable(
        tmp_path, monkeypatch, _fixture_bridge_ok):
    monkeypatch.setattr(cli, "_ensure_appium_or_echo_blocked", lambda cfg, **k: False)
    config = tmp_path / "t.yaml"
    config.write_text(
        'appium_url: "http://127.0.0.1:4723/wd/hub"\ndevice_name: "t"\nudid: "e-5554"\n'
        'apk_path: "/tmp/a.apk"\napp_package: "com.viso.calee"\napp_activity: ".ui.H"\n'
        'shell_package: "s"\nshell_activity: ".s"\nlaunch_strategy: "direct_activity"\n'
        'start_action: "a"\nexpected_state: "fresh"\n')
    result = CliRunner().invoke(cli.main, ["prepare", "--config", str(config), *CREDS])
    assert result.exit_code == EXIT_BLOCKED
    report = _report(tmp_path, result.output)
    assert report["preparationScope"] == "full-release"
