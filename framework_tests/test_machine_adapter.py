"""Tests for the machine-config adapter (Priority 4): machine.local.yaml is the
single authoritative source, reconciled with the legacy tester config with
every override recorded."""

from __future__ import annotations

from calee_regression import machine_adapter
from calee_regression.machine_config import MachineConfig


def _machine(**over):
    base = dict(
        tablet_serial="TAB123",
        expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee",
        caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir="~/Calee-Releases/current",
        backend_url="https://hub-dev.calee.com.au",
        release_profile="production",
        report_dir="reports",
        mobile_platforms=["android", "ios"],
        iphone_device="",
        allow_caleeshell_technical=False,
    )
    base.update(over)
    return MachineConfig(**base)


def test_machine_only_when_no_legacy():
    eff = machine_adapter.reconcile(_machine(), None)
    assert eff.tester_config["udid"] == "TAB123"
    assert eff.tester_config["expected_state"] == "logged_in_tablet"
    assert eff.tester_config["app_package"] == "com.viso.calee"
    assert eff.tester_config["shell_package"] == "com.viso.caleeshell"
    assert eff.tester_config["shell_activity"] == ".ui.LauncherActivity"
    assert eff.tester_config["start_action"] == "com.viso.calee.action.START"
    # every applied field is recorded as machine_only
    assert all(r.resolution in ("machine_only",) for r in eff.reconciliations)


def test_agreeing_legacy_is_recorded_as_agree():
    legacy = {"udid": "TAB123", "appium_url": "http://x", "launch_strategy": "direct_activity"}
    eff = machine_adapter.reconcile(_machine(), legacy)
    udid_rec = next(r for r in eff.reconciliations if r.field == "udid")
    assert udid_rec.resolution == "agree"
    # non-overlapping legacy keys are preserved (lower-level commands keep working)
    assert eff.tester_config["appium_url"] == "http://x"
    assert eff.tester_config["launch_strategy"] == "direct_activity"


def test_conflicting_legacy_is_overridden_with_explanation():
    legacy = {"udid": "STALE-OTHER-SERIAL", "expected_state": "fresh"}
    eff = machine_adapter.reconcile(_machine(), legacy)
    # machine config wins...
    assert eff.tester_config["udid"] == "TAB123"
    assert eff.tester_config["expected_state"] == "logged_in_tablet"
    # ...and each override is recorded with the legacy value + an explanation.
    udid_rec = next(r for r in eff.reconciliations if r.field == "udid")
    assert udid_rec.resolution == "overridden"
    assert udid_rec.legacy_value == "STALE-OTHER-SERIAL"
    assert "OVERRODE" in udid_rec.explanation
    state_rec = next(r for r in eff.reconciliations if r.field == "expected_state")
    assert state_rec.resolution == "overridden"


def test_technical_permission_maps_onto_allow_release_technical():
    eff = machine_adapter.reconcile(_machine(allow_caleeshell_technical=True), {"allow_release_technical": False})
    assert eff.tester_config["allow_release_technical"] is True
    rec = next(r for r in eff.reconciliations if r.field == "allow_release_technical")
    assert rec.resolution == "overridden"


def test_snapshot_records_selected_backend_devices_packages_profile_no_secrets():
    eff = machine_adapter.reconcile(_machine(), {"udid": "TAB123"})
    snap = machine_adapter.snapshot(eff, machine_config_path="/x/machine.yaml", effective_tester_config_path="/x/eff.yaml")
    assert snap["status"] == "ok"
    sel = snap["selected"]
    assert sel["backendUrl"] == "https://hub-dev.calee.com.au"
    assert sel["releaseProfile"] == "production"
    assert sel["tabletSerial"] == "TAB123"
    assert sel["caleePackageId"] == "com.viso.calee"
    assert sel["caleeShellPackageId"] == "com.viso.caleeshell"
    assert sel["mobilePlatforms"] == ["android", "ios"]
    assert sel["releaseBundleDir"].endswith("Calee-Releases/current")
    # No secret keys anywhere in the snapshot.
    import json

    text = json.dumps(snap).lower()
    for marker in ("password", "secret", "token", "api_key", "apikey"):
        assert marker not in text
