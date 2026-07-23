"""First-class tablet diagnostic mode (Workstream 6).

Pins down:
  * Appium capability construction as a PURE function -- standard vs skip
    (appium:skipDeviceInitialization) -- so it is testable without appium/device;
  * config validation of device_initialization_mode (default standard, no
    automatic fallback to skip);
  * the certification/execution-mode block every tablet report embeds;
  * the consolidator NEVER treating a diagnostic (or ambiguous legacy) tablet
    run as release-certifying evidence.
"""

from __future__ import annotations

import json

import pytest
import yaml

from calee_regression import config as config_mod
from calee_regression.appium_driver import build_appium_capabilities
from calee_regression.config import Config
from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_PASS,
    component_from_tablet_report,
    diagnostic_tablet_block_reason,
)
from calee_regression.models import (
    DEVICE_INIT_SKIP,
    DEVICE_INIT_STANDARD,
    ScenarioResult,
    SuiteResult,
    certification_block,
)
from calee_regression.reporting import ReportBuilder


def _config(**overrides) -> Config:
    base = dict(
        appium_url="http://127.0.0.1:4723/wd/hub",
        device_name="Calee Test Tablet",
        udid="emulator-5554",
        apk_path="/tmp/calee.apk",
        app_package="com.viso.calee",
        app_activity=".ui.HomeActivity",
        shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity",
        launch_strategy="direct_activity",
        start_action="com.viso.calee.action.START",
    )
    base.update(overrides)
    return Config(**base)


# ── capability construction (pure, no appium/device) ──────────────────────


def test_standard_mode_has_no_skip_capability():
    caps = build_appium_capabilities(_config(device_initialization_mode=DEVICE_INIT_STANDARD))
    assert caps["platformName"] == "Android"
    assert caps["appium:automationName"] == "UiAutomator2"
    assert caps["appium:udid"] == "emulator-5554"
    assert caps["appium:noReset"] is True
    assert "appium:skipDeviceInitialization" not in caps


def test_default_config_is_standard():
    caps = build_appium_capabilities(_config())
    assert "appium:skipDeviceInitialization" not in caps


def test_skip_mode_sets_skip_device_initialization():
    caps = build_appium_capabilities(_config(device_initialization_mode=DEVICE_INIT_SKIP))
    assert caps["appium:skipDeviceInitialization"] is True


def test_normal_launcher_adds_app_package_and_activity():
    caps = build_appium_capabilities(_config(launch_strategy="normal_launcher"))
    assert caps["appium:appPackage"] == "com.viso.calee"
    assert caps["appium:appActivity"] == ".ui.HomeActivity"


def test_non_normal_launcher_omits_app_package():
    caps = build_appium_capabilities(_config(launch_strategy="direct_activity"))
    assert "appium:appPackage" not in caps


def test_skip_mode_with_normal_launcher():
    caps = build_appium_capabilities(
        _config(launch_strategy="normal_launcher", device_initialization_mode=DEVICE_INIT_SKIP)
    )
    assert caps["appium:appPackage"] == "com.viso.calee"
    assert caps["appium:skipDeviceInitialization"] is True


# ── certification block ────────────────────────────────────────────────────


def test_certification_block_standard():
    block = certification_block(DEVICE_INIT_STANDARD)
    assert block == {
        "deviceInitializationMode": "standard",
        "diagnosticMode": False,
        "certificationEligible": True,
    }


def test_certification_block_skip_is_diagnostic_and_not_eligible():
    block = certification_block(DEVICE_INIT_SKIP)
    assert block["diagnosticMode"] is True
    assert block["certificationEligible"] is False


# ── config validation ──────────────────────────────────────────────────────


def _write(tmp_path, data):
    path = tmp_path / "tester.local.yaml"
    base = {
        "appium_url": "http://127.0.0.1:4723/wd/hub",
        "device_name": "Calee Test Tablet",
        "udid": "emulator-5554",
        "apk_path": "/tmp/calee.apk",
        "app_package": "com.viso.calee",
        "app_activity": ".ui.HomeActivity",
        "shell_package": "com.viso.caleeshell",
        "shell_activity": ".ui.LauncherActivity",
        "launch_strategy": "direct_activity",
        "start_action": "com.viso.calee.action.START",
    }
    base.update(data)
    with path.open("w") as f:
        yaml.safe_dump(base, f)
    return path


def test_config_defaults_to_standard(tmp_path):
    cfg = config_mod.load_config(_write(tmp_path, {}))
    assert cfg.device_initialization_mode == DEVICE_INIT_STANDARD


def test_config_accepts_skip(tmp_path):
    cfg = config_mod.load_config(_write(tmp_path, {"device_initialization_mode": "skip"}))
    assert cfg.device_initialization_mode == DEVICE_INIT_SKIP


def test_config_rejects_invalid_mode(tmp_path):
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load_config(_write(tmp_path, {"device_initialization_mode": "sometimes"}))
    assert "device_initialization_mode" in str(exc.value)


# ── report embeds certification block ──────────────────────────────────────


def _suite():
    return SuiteResult(
        name="smoke",
        scenarios=[ScenarioResult(name="s1", file="scenarios/s1.yaml", status="passed")],
        started_at="t0",
        finished_at="t1",
    )


def test_report_embeds_standard_certification(tmp_path):
    cfg = _config(report_dir=str(tmp_path / "reports"))
    rb = ReportBuilder(cfg, run_name="smoke")
    rb.write(_suite())
    data = json.loads((rb.dir / "results.json").read_text())
    assert data["diagnosticMode"] is False
    assert data["certificationEligible"] is True
    assert data["deviceInitializationMode"] == "standard"
    assert "reportSchemaVersion" in data


def test_report_embeds_diagnostic_certification(tmp_path):
    cfg = _config(report_dir=str(tmp_path / "reports"), device_initialization_mode=DEVICE_INIT_SKIP)
    rb = ReportBuilder(cfg, run_name="smoke")
    rb.write(_suite())
    data = json.loads((rb.dir / "results.json").read_text())
    assert data["diagnosticMode"] is True
    assert data["certificationEligible"] is False


# ── consolidation never certifies a diagnostic run ─────────────────────────


def _passing_tablet_report(**extra):
    report = {
        "name": "tablet",
        "passed_count": 3,
        "failed_count": 0,
        "blocked_count": 0,
        "skipped_count": 0,
        "scenarios": [
            {"name": "a", "status": "passed", "mandatory": True},
            {"name": "b", "status": "passed", "mandatory": True},
            {"name": "c", "status": "passed", "mandatory": True},
        ],
    }
    report.update(extra)
    return report


def test_standard_all_pass_certifies():
    report = _passing_tablet_report(**certification_block(DEVICE_INIT_STANDARD))
    component = component_from_tablet_report("Calee tablet", report)
    assert component.status == STATUS_PASS
    assert diagnostic_tablet_block_reason(report) is None


def test_diagnostic_all_pass_is_blocked_not_certified():
    report = _passing_tablet_report(**certification_block(DEVICE_INIT_SKIP))
    component = component_from_tablet_report("Calee tablet", report)
    assert component.status == STATUS_BLOCKED  # a diagnostic PASS never certifies
    assert "DIAGNOSTIC" in " ".join(component.detail)


def test_legacy_report_without_fields_still_certifies():
    # A pre-diagnostic report (no certification fields) keeps working -- the only
    # mode that existed then was standard.
    report = _passing_tablet_report()
    assert diagnostic_tablet_block_reason(report) is None
    assert component_from_tablet_report("Calee tablet", report).status == STATUS_PASS


def test_ambiguous_partial_metadata_blocks():
    # diagnosticMode absent but certificationEligible True alone, or contradictory
    # values, must never be inferred as eligible.
    report = _passing_tablet_report(certificationEligible=True)  # no diagnosticMode
    assert diagnostic_tablet_block_reason(report) is not None
    assert component_from_tablet_report("Calee tablet", report).status == STATUS_BLOCKED


def test_diagnostic_fail_stays_fail():
    report = {
        "name": "tablet",
        "passed_count": 1,
        "failed_count": 1,
        "blocked_count": 0,
        "skipped_count": 0,
        "scenarios": [
            {"name": "a", "status": "passed", "mandatory": True},
            {"name": "b", "status": "failed", "mandatory": True},
        ],
        **certification_block(DEVICE_INIT_SKIP),
    }
    # A real product FAIL still fails closed even in diagnostic mode.
    assert component_from_tablet_report("Calee tablet", report).status == "fail"
