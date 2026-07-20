"""Priority 3 -- one authoritative effective RELEASE configuration.

Composes the machine config (how/where) with the release candidate
(release-platforms.yaml: what) under one precedence rule, records the result +
every conflict decision to reports/runs/<run-id>/release-config/results.json,
and BLOCKS on any machine/release conflict. All offline.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from calee_regression import cli
from calee_regression import release_config as rc
from calee_regression.machine_config import MachineConfig
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS
from calee_regression.release_platforms import ExpectedBuildIdentity, ReleaseFeatures, ReleasePlatforms


def _machine(**over):
    base = dict(
        tablet_serial="TAB123",
        expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee",
        caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir="~/Calee-Releases/current",
        backend_url="https://hub-staging.calee.com.au",
        release_profile="staging",
        report_dir="reports",
        mobile_platforms=["android", "ios"],
        iphone_device="00008110-DEADBEEF",
        android_device="R5CANDROID",
        allow_caleeshell_technical=True,
    )
    base.update(over)
    return MachineConfig(**base)


def _compose(machine=None, platforms=None, features=None, expected=None, **kw):
    return rc.compose_effective_release_config(
        machine or _machine(),
        platforms or ReleasePlatforms(),
        features or ReleaseFeatures(),
        expected or ExpectedBuildIdentity(),
        **kw,
    )


def _blocking(cfg):
    return [c for c in cfg.conflicts if c.blocking]


def test_capable_machine_matching_release_composes_ok():
    cfg = _compose(run_id="release-20260720-101010-abc123", release_id="2026.07.20-rc2")
    assert cfg.ok, cfg.detail
    assert set(cfg.enabled_platforms) == {"tablet", "android", "ios"}
    assert "synchronization" in cfg.enabled_features
    assert _blocking(cfg) == []
    assert cfg.run_id == "release-20260720-101010-abc123"
    assert cfg.release_id == "2026.07.20-rc2"


def test_release_requires_ios_but_machine_has_no_iphone_blocks():
    cfg = _compose(machine=_machine(iphone_device=None, mobile_platforms=["android"]))
    assert not cfg.ok
    assert any(c.axis == "platform:ios" for c in _blocking(cfg))


def test_release_requires_android_but_machine_lacks_it_blocks():
    cfg = _compose(machine=_machine(mobile_platforms=["ios"]))
    assert not cfg.ok
    assert any(c.axis == "platform:android" for c in _blocking(cfg))


def test_release_requires_tablet_but_machine_has_no_serial_blocks():
    cfg = _compose(machine=_machine(tablet_serial=None))
    assert not cfg.ok
    assert any(c.axis == "platform:tablet" for c in _blocking(cfg))


def test_machine_capable_of_more_than_release_requires_is_narrowed_not_blocked():
    # Release requires only tablet; machine can also do android+ios -> narrowed.
    cfg = _compose(platforms=ReleasePlatforms(tablet=True, mobile_android=False, mobile_ios=False))
    assert cfg.ok, cfg.detail
    assert cfg.enabled_platforms == ["tablet"]
    narrowed = [c for c in cfg.conflicts if c.resolution == rc.RES_NARROWED]
    assert {c.axis for c in narrowed} == {"platform:android", "platform:ios"}


def test_profile_disagreement_blocks():
    # Machine says production; release candidate is staging -> conflict.
    cfg = _compose(machine=_machine(release_profile="production"),
                   expected=ExpectedBuildIdentity(production=False))
    assert not cfg.ok
    assert any(c.axis == "profile" for c in _blocking(cfg))


def test_kiosk_required_but_machine_not_authorised_blocks():
    cfg = _compose(machine=_machine(allow_caleeshell_technical=False))
    assert not cfg.ok
    assert any(c.axis == "feature:kiosk_admin" for c in _blocking(cfg))


def test_kiosk_not_required_on_unauthorised_machine_is_ok():
    cfg = _compose(machine=_machine(allow_caleeshell_technical=False),
                   features=ReleaseFeatures(kiosk_admin=False))
    assert cfg.ok, cfg.detail
    assert "kiosk_admin" not in cfg.enabled_features


def test_backend_pin_mismatch_blocks():
    cfg = _compose(expected_backend="https://hub-prod.calee.com.au")
    assert not cfg.ok
    assert any(c.axis == "backend" for c in _blocking(cfg))


def test_backend_pin_agreement_is_selected():
    cfg = _compose(expected_backend="https://hub-staging.calee.com.au")
    assert cfg.ok, cfg.detail
    assert cfg.selected_backend == "https://hub-staging.calee.com.au"


def test_effective_config_records_all_required_fields():
    cfg = _compose(
        run_id="release-20260720-101010-abc123", release_id="2026.07.20-rc2",
        expected=ExpectedBuildIdentity(
            calee_build_version="founder-v0.3.26", calee_git_sha="a" * 40,
            caleeshell_version="founder-v0.2.12", caleemobile_build_version="0.0.23+23",
            caleemobile_git_sha="b" * 40, production=False,
        ),
    )
    d = cfg.to_dict()
    # machine selections
    assert d["machineSelections"]["tabletSerial"] == "TAB123"
    assert d["machineSelections"]["homeActivity"] == "com.viso.caleeshell/.ui.LauncherActivity"
    assert d["machineSelections"]["reportRoot"] == "reports"
    # release selections
    assert set(d["releaseSelections"]["enabledPlatforms"]) == {"tablet", "android", "ios"}
    assert d["releaseSelections"]["profile"] == "staging"
    assert d["releaseSelections"]["expectedIdentities"]["calee"]["gitSha"] == "a" * 40
    assert d["releaseSelections"]["expectedIdentities"]["caleeShell"]["version"] == "founder-v0.2.12"
    assert d["releaseSelections"]["expectedIdentities"]["caleeMobile"]["gitSha"] == "b" * 40
    # device ids
    assert d["deviceIds"] == {"tablet": "TAB123", "ios": "00008110-DEADBEEF", "android": "R5CANDROID"}
    # conflict decisions are all recorded
    assert d["conflicts"]


def test_device_id_for_helper():
    cfg = _compose()
    assert cfg.device_id_for("ios") == "00008110-DEADBEEF"
    assert cfg.device_id_for("android") == "R5CANDROID"
    assert cfg.device_id_for("tablet") == "TAB123"


# ── CLI: release-config writes run-scoped results.json ─────────────────────


def _write_machine_yaml(tmp_path, **over):
    import yaml
    data = dict(
        tablet_serial="TAB123", expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee", caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir="~/Calee-Releases/current",
        backend_url="https://hub-staging.calee.com.au", release_profile="staging",
        report_dir="reports", mobile_platforms=["android", "ios"],
        iphone_device="00008110-DEADBEEF", android_device="R5CANDROID",
        allow_caleeshell_technical=True,
    )
    data.update(over)
    p = tmp_path / "machine.local.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _write_platforms_yaml(tmp_path, body):
    p = tmp_path / "release-platforms.yaml"
    p.write_text(body)
    return p


def test_cli_release_config_writes_results_and_passes(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path)
    platforms = _write_platforms_yaml(tmp_path, "release_platforms:\n  tablet: true\n  mobile_android: true\n  mobile_ios: true\n")
    run_id = "release-20260720-101010-abc123"
    monkeypatch.setenv("CALEE_RUN_ID", run_id)
    # Redirect the repo root used by the CLI to tmp_path so reports land there.
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--release-platforms", str(platforms), "--run-id", run_id],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    results_json = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    assert results_json.is_file()
    payload = json.loads(results_json.read_text())
    assert payload["status"] == "ok"
    assert set(payload["releaseSelections"]["enabledPlatforms"]) == {"tablet", "android", "ios"}
    assert payload["deviceIds"]["ios"] == "00008110-DEADBEEF"
    # eval-able output drives downstream execution.
    assert "RELEASE_PLATFORM_IOS=true" in result.output
    assert "RELEASE_IPHONE_DEVICE=00008110-DEADBEEF" in result.output


def test_cli_release_config_blocks_on_conflict(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path, mobile_platforms=["android"], iphone_device="")
    platforms = _write_platforms_yaml(tmp_path, "release_platforms:\n  tablet: true\n  mobile_android: true\n  mobile_ios: true\n")
    run_id = "release-20260720-101010-def456"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--release-platforms", str(platforms), "--run-id", run_id],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    payload = json.loads((tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json").read_text())
    assert payload["status"] == "blocked"
    assert any(c["axis"] == "platform:ios" and c["blocking"] for c in payload["conflicts"])


def test_cli_release_config_blocks_on_noncanonical_package_id(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path, calee_package_id="com.evil.calee")
    platforms = _write_platforms_yaml(tmp_path, "release_platforms:\n  tablet: true\n  mobile_android: false\n  mobile_ios: false\n")
    run_id = "release-20260720-101010-pkg789"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--release-platforms", str(platforms), "--run-id", run_id],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    payload = json.loads((tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json").read_text())
    assert any(c["axis"] == "packageId:calee" and c["blocking"] for c in payload["conflicts"])


# ── P4: the effective config controls the installer command arrays ─────────


def _write_tester_yaml(tmp_path, **over):
    import yaml
    data = dict(
        appium_url="http://127.0.0.1:4723/wd/hub", device_name="Calee Test Tablet",
        udid="emulator-5554", apk_path="/tmp/calee.apk", app_package="com.viso.calee",
        app_activity=".ui.HomeActivity", shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity", launch_strategy="direct_activity",
        start_action="com.viso.calee.action.START", expected_state="fresh",
    )
    data.update(over)
    p = tmp_path / "tester.local.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _plan_bundle(tmp_path):
    import hashlib
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    calee_bytes = b"calee-apk-bytes"
    shell_bytes = b"caleeshell-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    (bundle / "caleeshell.apk").write_bytes(shell_bytes)
    sha = lambda b: hashlib.sha256(b).hexdigest()
    manifest = {
        "releaseId": "2026.07.20-rc2",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                  "versionCode": 326, "gitSha": "a" * 40, "apk": "calee.apk", "sha256": sha(calee_bytes)},
        "caleeShell": {"included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                       "versionCode": 212, "gitSha": "b" * 40, "apk": "caleeshell.apk", "sha256": sha(shell_bytes)},
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{sha(calee_bytes)}  calee.apk\n{sha(shell_bytes)}  caleeshell.apk\n")
    return bundle


def test_configured_home_and_start_reach_install_plan_argv(tmp_path):
    # A tester config with NON-DEFAULT HOME activity + START action must drive
    # the installer's command arrays, not the module defaults.
    tester = _write_tester_yaml(
        tmp_path, shell_activity=".ui.CustomKioskLauncher", start_action="com.viso.calee.action.CUSTOM_START",
    )
    bundle = _plan_bundle(tmp_path)
    report = tmp_path / "plan.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--config", str(tester),
         "--serial", "TAB1", "--plan-only", "--report", str(report)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    plan = json.loads(report.read_text())["plan"]
    set_home = next(s for s in plan["steps"] if s["label"] == "set-home")
    assert "com.viso.caleeshell/.ui.CustomKioskLauncher" in set_home["argv"]
    verify_launch = next(s for s in plan["steps"] if s["label"] == "verify-calee-launch")
    assert "com.viso.calee.action.CUSTOM_START" in verify_launch["argv"]
