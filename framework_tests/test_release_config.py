"""Priority 3 -- one authoritative effective RELEASE configuration.

Composes the machine config (how/where) with the release candidate
(release-platforms.yaml: what) under one precedence rule, records the result +
every conflict decision to reports/runs/<run-id>/release-config/results.json,
and BLOCKS on any machine/release conflict. All offline.
"""

from __future__ import annotations

import json

import pytest
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


def test_no_bundle_manifest_defaults_to_schema_version_1():
    cfg = _compose()
    assert cfg.schema_version == 1
    assert cfg.to_dict()["schemaVersion"] == 1


# ── Priority 2/3: schema-v2 bundle manifest is authoritative for scope ─────


def _v2_manifest_obj(**overrides):
    from calee_regression.release_installer import parse_manifest as _pm
    raw = {
        "schemaVersion": 2, "releaseId": "2026.07.20-rc3", "profile": "staging",
        "backend": "https://hub-staging.calee.com.au",
        "platforms": {"tablet": True, "mobileAndroid": True, "mobileIos": True},
        "features": {"synchronization": True, "meals": True, "onboarding": True,
                     "googleCalendar": True, "kioskAdmin": True, "notifications": True},
        "tabletSolution": {
            "calee": {"installArtifact": True, "apk": "calee.apk", "sha256": "0" * 64,
                      "expectedInstalled": {"packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                                            "versionCode": 326, "gitSha": "a" * 40, "signerSha256": "1" * 64}},
            "caleeShell": {"installArtifact": False,
                           "expectedInstalled": {"packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                                                 "versionCode": 212, "gitSha": "b" * 40, "signerSha256": "2" * 64}},
        },
        "caleeMobile": {"version": "0.0.24+24", "gitSha": "c" * 40,
                        "selectorEvidenceRequired": True, "distributedBuildAcceptanceRequired": True},
    }
    raw.update(overrides)
    manifest, errors = _pm(raw)
    assert errors == [], errors
    return manifest


def test_v2_bundle_manifest_drives_scope_without_release_platforms_yaml():
    bundle = _v2_manifest_obj()
    cfg = _compose(
        machine=_machine(mobile_platforms=["android", "ios"]),
        # Deliberately pass release-platforms-yaml-sourced objects that DISAGREE
        # with the bundle -- they must be ignored entirely for schema v2.
        platforms=ReleasePlatforms(tablet=False, mobile_android=False, mobile_ios=False),
        features=ReleaseFeatures(kiosk_admin=False),
        expected=ExpectedBuildIdentity(production=True),
        bundle_manifest=bundle,
    )
    assert cfg.ok, cfg.detail
    assert cfg.schema_version == 2
    assert set(cfg.enabled_platforms) == {"tablet", "android", "ios"}
    assert cfg.profile == "staging"
    assert cfg.selected_backend == "https://hub-staging.calee.com.au"
    assert cfg.release_id == "2026.07.20-rc3"
    assert cfg.expected_identities["calee"]["gitSha"] == "a" * 40
    assert cfg.expected_identities["calee"]["signerSha256"] == "1" * 64
    assert cfg.expected_identities["caleeMobile"]["buildVersion"] == "0.0.24+24"


def test_v2_bundle_manifest_expected_identities_include_signer_and_caleemobile_flags():
    bundle = _v2_manifest_obj()
    cfg = _compose(bundle_manifest=bundle)
    assert cfg.expected_identities["caleeShell"]["signerSha256"] == "2" * 64
    assert cfg.expected_identities["caleeMobile"]["buildVersion"] == "0.0.24+24"
    assert cfg.expected_identities["caleeMobile"]["gitSha"] == "c" * 40
    assert cfg.expected_identities["caleeMobile"]["selectorEvidenceRequired"] is True
    assert cfg.expected_identities["caleeMobile"]["distributedBuildAcceptanceRequired"] is True


def test_v2_bundle_manifest_records_full_identity_matrix_rows():
    bundle = _v2_manifest_obj()
    cfg = _compose(bundle_manifest=bundle)
    fields = {c.axis for c in cfg.conflicts}
    for expected_field in (
        "releaseId", "profile", "backend", "platforms.tablet", "platforms.mobileAndroid", "platforms.mobileIos",
        "features.synchronization", "features.kioskAdmin", "features.notifications",
        "calee.packageId", "calee.versionName", "calee.versionCode",
        "calee.gitSha", "calee.signerSha256", "caleeShell.versionName", "caleeShell.signerSha256",
        "caleeMobile.version", "caleeMobile.gitSha", "caleeMobile.selectorEvidenceRequired",
        "caleeMobile.distributedBuildAcceptanceRequired",
    ):
        assert expected_field in fields, f"{expected_field} missing from comparison matrix: {sorted(fields)}"
    row = next(c for c in cfg.conflicts if c.axis == "calee.gitSha")
    d = row.to_dict()
    assert d["field"] == "calee.gitSha" and d["result"] == "agree" and d["blocking"] is False
    assert d["sourceA"] == "release-bundle-manifest" and d["sourceB"] == "release-bundle-manifest"


def test_v2_release_id_override_mismatch_blocks():
    bundle = _v2_manifest_obj()
    cfg = _compose(bundle_manifest=bundle, release_id="a-totally-different-release-id")
    assert not cfg.ok
    row = next(c for c in cfg.conflicts if c.axis == "releaseId")
    assert row.blocking is True
    assert row.resolution == rc.RES_CONFLICT
    # The bundle manifest's release ID wins -- never silently overridden.
    assert cfg.release_id == "2026.07.20-rc3"


def test_v2_release_id_override_matching_does_not_block():
    bundle = _v2_manifest_obj()
    cfg = _compose(bundle_manifest=bundle, release_id="2026.07.20-rc3")
    row = next(c for c in cfg.conflicts if c.axis == "releaseId")
    assert row.blocking is False and row.resolution == rc.RES_AGREE


def test_v2_manifest_incomplete_platform_scope_blocks_missing_ios_device():
    bundle = _v2_manifest_obj(platforms={"tablet": True, "mobileAndroid": True, "mobileIos": True})
    cfg = _compose(machine=_machine(iphone_device=None, mobile_platforms=["android"]), bundle_manifest=bundle)
    assert not cfg.ok
    assert any(c.axis == "platform:ios" and c.blocking for c in cfg.conflicts)


# ── Priority 2: schema-v1 bundle manifest cross-checked against release-platforms.yaml ──


def _v1_manifest_obj(**calee_overrides):
    from calee_regression.release_installer import parse_manifest as _pm
    calee = {
        "included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
        "versionCode": 326, "gitSha": "a" * 40, "apk": "calee.apk", "sha256": "0" * 64,
    }
    calee.update(calee_overrides)
    manifest, errors = _pm({
        "releaseId": "2026.07.20-rc3",
        "calee": calee,
        "caleeShell": {"included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                       "versionCode": 212, "gitSha": "b" * 40, "apk": "caleeshell.apk", "sha256": "1" * 64},
    })
    assert errors == [], errors
    return manifest


def test_v1_bundle_manifest_records_deprecation_warning():
    bundle = _v1_manifest_obj()
    cfg = _compose(bundle_manifest=bundle)
    assert cfg.schema_version == 1
    assert any("DEPRECATED" in d and "schemaVersion 2" in d for d in cfg.detail)


def test_v1_bundle_manifest_agreeing_identity_does_not_block():
    bundle = _v1_manifest_obj()
    cfg = _compose(
        bundle_manifest=bundle,
        expected=ExpectedBuildIdentity(calee_build_version="founder-v0.3.26", calee_git_sha="a" * 40, calee_version_code="326"),
    )
    assert cfg.ok, [c.explanation for c in cfg.conflicts if c.blocking]
    row = next(c for c in cfg.conflicts if c.axis == "calee.gitSha")
    assert row.resolution == rc.RES_AGREE


def test_v1_bundle_manifest_disagreeing_git_sha_blocks():
    bundle = _v1_manifest_obj(gitSha="a" * 40)
    cfg = _compose(bundle_manifest=bundle, expected=ExpectedBuildIdentity(calee_git_sha="f" * 40))
    assert not cfg.ok
    row = next(c for c in cfg.conflicts if c.axis == "calee.gitSha")
    assert row.blocking is True and row.resolution == rc.RES_CONFLICT
    assert row.source_a == "release-bundle-manifest" and row.source_b == "release-platforms.yaml"


def test_v1_bundle_manifest_disagreeing_version_name_blocks():
    bundle = _v1_manifest_obj(versionName="founder-v0.3.26")
    cfg = _compose(bundle_manifest=bundle, expected=ExpectedBuildIdentity(calee_build_version="founder-v0.3.99"))
    assert not cfg.ok
    row = next(c for c in cfg.conflicts if c.axis == "calee.versionName")
    assert row.blocking is True


def test_v1_bundle_manifest_disagreeing_version_code_blocks():
    bundle = _v1_manifest_obj(versionCode=326)
    cfg = _compose(bundle_manifest=bundle, expected=ExpectedBuildIdentity(calee_version_code="999"))
    assert not cfg.ok
    row = next(c for c in cfg.conflicts if c.axis == "calee.versionCode")
    assert row.blocking is True


def test_v1_bundle_manifest_caleeshell_version_disagreement_blocks():
    bundle = _v1_manifest_obj()
    cfg = _compose(bundle_manifest=bundle, expected=ExpectedBuildIdentity(caleeshell_version="founder-v9.9.9"))
    assert not cfg.ok
    row = next(c for c in cfg.conflicts if c.axis == "caleeShell.versionName")
    assert row.blocking is True


def test_v1_bundle_manifest_with_no_expected_identity_configured_is_release_only_not_blocking():
    bundle = _v1_manifest_obj()
    cfg = _compose(bundle_manifest=bundle, expected=ExpectedBuildIdentity())
    assert cfg.ok, [c.explanation for c in cfg.conflicts if c.blocking]
    row = next(c for c in cfg.conflicts if c.axis == "calee.gitSha")
    assert row.resolution == rc.RES_MACHINE_ONLY


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


# ── Priority 1: CLI --bundle wiring + reuse-not-recompute (launcher 06) ────


def _write_v2_bundle(tmp_path, **overrides):
    import hashlib
    bundle = tmp_path / "release-bundle"
    bundle.mkdir()
    calee_bytes = b"calee-cli-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    manifest = {
        "schemaVersion": 2, "releaseId": "2026.07.20-rc9", "profile": "staging",
        "backend": "https://hub-staging.calee.com.au",
        "platforms": {"tablet": True, "mobileAndroid": True, "mobileIos": True},
        "features": {"synchronization": True, "meals": True, "onboarding": True,
                     "googleCalendar": True, "kioskAdmin": True, "notifications": True},
        "tabletSolution": {
            "calee": {"installArtifact": True, "apk": "calee.apk", "sha256": hashlib.sha256(calee_bytes).hexdigest(),
                      "expectedInstalled": {"packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                                            "versionCode": 326, "gitSha": "a" * 40, "signerSha256": "1" * 64}},
            "caleeShell": {"installArtifact": False,
                           "expectedInstalled": {"packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                                                 "versionCode": 212, "gitSha": "b" * 40, "signerSha256": "2" * 64}},
        },
        "caleeMobile": {"version": "0.0.24+24", "gitSha": "c" * 40,
                        "selectorEvidenceRequired": True, "distributedBuildAcceptanceRequired": True},
    }
    manifest.update(overrides)
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{hashlib.sha256(calee_bytes).hexdigest()}  calee.apk\n")
    return bundle


def test_cli_release_config_bundle_flag_wires_schema_v2_end_to_end(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path, mobile_platforms=["android", "ios"])
    bundle = _write_v2_bundle(tmp_path)
    run_id = "release-20260720-101010-v2bundle"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads((tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json").read_text())
    assert payload["schemaVersion"] == 2
    assert payload["releaseId"] == "2026.07.20-rc9"
    assert payload["releaseSelections"]["selectedBackend"] == "https://hub-staging.calee.com.au"
    assert "RELEASE_PLATFORM_TABLET=true" in result.output


def test_cli_release_config_bundle_flag_blocks_on_invalid_bundle(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path)
    bundle = _write_v2_bundle(tmp_path)
    (bundle / "release-manifest.json").write_text(
        (bundle / "release-manifest.json").read_text().replace("2026.07.20-rc9", "")
    )
    run_id = "release-20260720-101010-badbundle"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    payload = json.loads((tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json").read_text())
    assert payload["status"] == "blocked"
    assert "bundleVerification" in payload


def test_cli_release_config_without_bundle_flag_ignores_unrelated_machine_release_bundle_dir(tmp_path, monkeypatch):
    # A machine config's release_bundle_dir (used by install-tablet-release)
    # must NOT make a bundle-less `release-config` call start requiring a
    # real bundle on disk -- only an explicit --bundle opts into that.
    machine = _write_machine_yaml(tmp_path, release_bundle_dir="~/Nonexistent-Bundle-Dir")
    platforms = _write_platforms_yaml(tmp_path, "release_platforms:\n  tablet: true\n  mobile_android: true\n  mobile_ios: true\n")
    run_id = "release-20260720-101010-nobundle"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--release-platforms", str(platforms), "--run-id", run_id],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_cli_release_config_second_call_same_run_reuses_evidence_not_recompute(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path)
    bundle = _write_v2_bundle(tmp_path)
    run_id = "release-20260720-101010-reuse1"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    first = CliRunner().invoke(
        cli.main, ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )
    assert first.exit_code == EXIT_SUCCESS, first.output
    first_payload = json.loads((tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json").read_text())

    # Second call for the SAME run: no --bundle, no --config even -- as
    # launcher 06 calls it after 00 already composed. Must reuse, not
    # recompute (a recompute with no --config would BLOCK on a missing
    # machine.local.yaml, proving this path never reaches composition).
    second = CliRunner().invoke(cli.main, ["release-config", "--run-id", run_id])
    assert second.exit_code == EXIT_SUCCESS, second.output
    assert "Reusing this run's already-composed" in second.output
    second_payload = json.loads((tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json").read_text())
    assert second_payload == first_payload
    assert "RELEASE_PLATFORM_TABLET=true" in second.output


def test_cli_release_config_reuse_of_blocked_evidence_stays_blocked(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path, iphone_device="", mobile_platforms=["android"])
    platforms = _write_platforms_yaml(tmp_path, "release_platforms:\n  tablet: true\n  mobile_android: true\n  mobile_ios: true\n")
    run_id = "release-20260720-101010-reuseblocked"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    first = CliRunner().invoke(
        cli.main, ["release-config", "--config", str(machine), "--release-platforms", str(platforms), "--run-id", run_id],
    )
    assert first.exit_code == EXIT_BLOCKED

    second = CliRunner().invoke(cli.main, ["release-config", "--run-id", run_id])
    assert second.exit_code == EXIT_BLOCKED, second.output
    assert "already-BLOCKED" in second.output


def test_cli_release_config_reuse_rejects_wrong_run_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    run_id = "release-20260720-101010-wrongrun"
    workspace_root = tmp_path / "reports" / "runs" / run_id / "release-config"
    workspace_root.mkdir(parents=True)
    (workspace_root / "results.json").write_text(json.dumps({
        "runId": "release-20260720-101010-someOTHERrun", "status": "ok",
        "machineSelections": {}, "releaseSelections": {}, "deviceIds": {}, "conflicts": [],
    }))
    result = CliRunner().invoke(cli.main, ["release-config", "--run-id", run_id])
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "rejected" in result.output.lower()


def test_cli_release_config_reuse_rejects_malformed_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    run_id = "release-20260720-101010-malformed1"
    workspace_root = tmp_path / "reports" / "runs" / run_id / "release-config"
    workspace_root.mkdir(parents=True)
    (workspace_root / "results.json").write_text(json.dumps({"runId": run_id, "status": "ok"}))  # missing required keys
    result = CliRunner().invoke(cli.main, ["release-config", "--run-id", run_id])
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "malformed" in result.output.lower()


def test_cli_release_config_reuse_rejects_unreadable_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    run_id = "release-20260720-101010-unreadable1"
    workspace_root = tmp_path / "reports" / "runs" / run_id / "release-config"
    workspace_root.mkdir(parents=True)
    (workspace_root / "results.json").write_text("{not valid json")
    result = CliRunner().invoke(cli.main, ["release-config", "--run-id", run_id])
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "unreadable" in result.output.lower()


# ---------------------------------------------------------------------------
# Priority 2 requirement 7: a malformed legacy release-platforms.yaml must
# never block a valid schema-v2 bundle -- release-config does not even load
# the legacy file for a v2 bundle.
# ---------------------------------------------------------------------------


def test_cli_release_config_v2_bundle_ignores_malformed_legacy_release_platforms_yaml(tmp_path, monkeypatch):
    machine = _write_machine_yaml(tmp_path, mobile_platforms=["android", "ios"])
    # Tab-indented YAML is a parse error (PyYAML rejects tabs for indentation).
    malformed_platforms = _write_platforms_yaml(tmp_path, "release_platforms:\n\ttablet: true\n")
    # Sanity check: this file really is malformed, so a v1/bare run WOULD block on it.
    import yaml as _yaml
    with pytest.raises(_yaml.YAMLError):
        _yaml.safe_load(malformed_platforms.read_text())

    bundle = _write_v2_bundle(tmp_path)
    run_id = "release-20260720-101010-v2ignoreslegacy"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--release-platforms", str(malformed_platforms),
         "--bundle", str(bundle), "--run-id", run_id],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads((tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json").read_text())
    assert payload["schemaVersion"] == 2
    assert payload["status"] == "ok"


def test_v2_platforms_features_expected_derives_from_composition_not_legacy():
    # Priority 2 requirement 3/4/9: cli._v2_platforms_features_expected is the
    # single choke point consolidate/selector-contract/sync-smoke/kiosk-admin
    # all use to prefer a schema-v2 composition over config/release-
    # platforms.yaml. Deliberately different values than any legacy default
    # prove the composition alone drives the result.
    composition = {
        "schemaVersion": 2,
        "releaseSelections": {
            "profile": "production",
            "enabledPlatforms": ["tablet", "ios"],  # android deliberately EXCLUDED
            "enabledFeatures": ["meals", "kiosk_admin"],  # sync/onboarding/google_calendar EXCLUDED
            "expectedIdentities": {
                "calee": {
                    "buildVersion": "founder-v0.9.9", "gitSha": "9" * 40,
                    "applicationId": "com.viso.calee", "versionCode": 999,
                    "signerSha256": "8" * 64,
                },
                "caleeShell": {"version": "founder-v0.8.8", "gitSha": "7" * 40, "signerSha256": "6" * 64},
                "caleeMobile": {"buildVersion": "0.0.99+99", "gitSha": "5" * 40},
            },
        },
    }
    platforms, features, expected = cli._v2_platforms_features_expected(composition)
    assert platforms.tablet is True and platforms.mobile_ios is True and platforms.mobile_android is False
    assert features.meals is True and features.kiosk_admin is True
    assert features.synchronization is False and features.onboarding is False and features.google_calendar is False
    assert expected.calee_build_version == "founder-v0.9.9"
    assert expected.calee_git_sha == "9" * 40
    assert expected.caleeshell_version == "founder-v0.8.8"
    assert expected.caleemobile_git_sha == "5" * 40
    assert expected.production is True


def test_cli_release_config_v1_bundleless_still_blocks_on_malformed_legacy_yaml(tmp_path, monkeypatch):
    # requirement 8: schema-v1 (no --bundle at all) must keep using and
    # cross-checking the legacy file -- a malformed one still BLOCKS.
    machine = _write_machine_yaml(tmp_path)
    malformed_platforms = _write_platforms_yaml(tmp_path, "release_platforms:\n\ttablet: true\n")
    run_id = "release-20260720-101010-v1stillblocks"
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(
        cli.main,
        ["release-config", "--config", str(machine), "--release-platforms", str(malformed_platforms),
         "--run-id", run_id],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output


# ── Priority 2 (this session): resolve_selector_evidence_required ──────────


def test_selector_evidence_production_with_mobile_in_scope_is_mandatory_regardless_of_manifest():
    assert rc.resolve_selector_evidence_required(
        profile="production", enabled_platforms=["tablet", "android"], schema_version=2,
        manifest_required=False,
    ) is True


def test_selector_evidence_production_with_ios_in_scope_is_mandatory():
    assert rc.resolve_selector_evidence_required(
        profile="production", enabled_platforms=["ios"], schema_version=2, manifest_required=False,
    ) is True


def test_selector_evidence_non_production_v2_honours_manifest_true():
    assert rc.resolve_selector_evidence_required(
        profile="staging", enabled_platforms=["android"], schema_version=2, manifest_required=True,
    ) is True


def test_selector_evidence_non_production_v2_honours_manifest_false():
    assert rc.resolve_selector_evidence_required(
        profile="staging", enabled_platforms=["android"], schema_version=2, manifest_required=False,
    ) is False


def test_selector_evidence_schema_v1_uses_legacy_mobile_in_scope_default():
    assert rc.resolve_selector_evidence_required(
        profile="staging", enabled_platforms=["android"], schema_version=1, manifest_required=None,
    ) is True


def test_selector_evidence_schema_v1_no_mobile_in_scope_is_not_applicable():
    assert rc.resolve_selector_evidence_required(
        profile="staging", enabled_platforms=["tablet"], schema_version=1, manifest_required=None,
    ) is None


def test_selector_evidence_v2_no_manifest_opinion_falls_back_to_mobile_in_scope():
    assert rc.resolve_selector_evidence_required(
        profile="staging", enabled_platforms=["android"], schema_version=2, manifest_required=None,
    ) is True


def test_selector_evidence_no_mobile_platform_at_all_is_not_applicable():
    assert rc.resolve_selector_evidence_required(
        profile="production", enabled_platforms=[], schema_version=2, manifest_required=True,
    ) is True  # manifest opinion still applies even with no mobile platform, if explicitly stated
    assert rc.resolve_selector_evidence_required(
        profile="staging", enabled_platforms=[], schema_version=1, manifest_required=None,
    ) is None


def test_selector_evidence_production_but_no_mobile_platform_defers_to_manifest():
    # Production alone doesn't force it -- only production WITH a mobile
    # platform in scope does (a tablet-only production release has nothing
    # selector-dependent to verify).
    assert rc.resolve_selector_evidence_required(
        profile="production", enabled_platforms=["tablet"], schema_version=2, manifest_required=False,
    ) is False


# ── Priority 5 (this session): release_selections_digest ───────────────────


def test_release_selections_digest_is_deterministic_and_key_order_independent():
    a = {"profile": "staging", "enabledPlatforms": ["tablet", "android"], "expectedIdentities": {"x": 1}}
    b = {"expectedIdentities": {"x": 1}, "enabledPlatforms": ["tablet", "android"], "profile": "staging"}
    assert rc.release_selections_digest(a) == rc.release_selections_digest(b)


def test_release_selections_digest_changes_with_content():
    a = {"profile": "staging", "enabledPlatforms": ["tablet"]}
    b = {"profile": "production", "enabledPlatforms": ["tablet"]}
    assert rc.release_selections_digest(a) != rc.release_selections_digest(b)


def test_effective_release_config_to_dict_embeds_release_config_digest(tmp_path):
    machine = MachineConfig(
        tablet_serial="TAB1", expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee", caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir=str(tmp_path), backend_url="https://hub-dev.calee.com.au",
        release_profile="staging", report_dir="reports", mobile_platforms=["android"],
        android_device="R5C", allow_caleeshell_technical=False,
    )
    cfg = rc.compose_effective_release_config(
        machine, ReleasePlatforms(tablet=True, mobile_android=True, mobile_ios=False),
        ReleaseFeatures(kiosk_admin=False), ExpectedBuildIdentity(),
        run_id="release-test-digest", release_id="r1",
    )
    d = cfg.to_dict()
    assert d["releaseConfigDigest"] == rc.release_selections_digest(d["releaseSelections"])
    assert d["releaseConfigDigest"].startswith("sha256:")
