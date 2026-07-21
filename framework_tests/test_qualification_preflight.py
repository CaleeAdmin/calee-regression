"""Priority 9 (this session) -- technical-owner qualification preflight.
Every check is read-only/injectable; these tests run fully offline, with no
real device/Appium/Flutter/Keychain/network required.
"""

from __future__ import annotations

import json

import pytest
import yaml

from calee_regression import qualification_preflight as qp

_VALID_MACHINE = {
    "tablet_serial": "TAB123", "expected_tablet_state": "logged_in_tablet",
    "calee_package_id": "com.viso.calee", "caleeshell_package_id": "com.viso.caleeshell",
    "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
    "calee_launch_action": "com.viso.calee.action.START",
    "release_bundle_dir": "~/Calee-Releases/current", "backend_url": "https://hub-dev.calee.com.au",
    "release_profile": "staging", "report_dir": "reports", "mobile_platforms": ["android"],
}


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _write_machine_config(tmp_path, **overrides):
    data = dict(_VALID_MACHINE, **overrides)
    path = tmp_path / "machine.local.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# ── individual checks ────────────────────────────────────────────────────


def test_check_machine_config_missing_is_blocked(tmp_path):
    result = qp.check_machine_config(tmp_path / "nope.yaml")
    assert result.status == qp.STATUS_BLOCKED


def test_check_machine_config_valid_is_ready(tmp_path):
    path = _write_machine_config(tmp_path)
    result = qp.check_machine_config(path)
    assert result.status == qp.STATUS_READY
    assert "hunter2" not in result.detail  # sanity: no secret ever appears


def test_check_machine_config_with_secret_is_blocked(tmp_path):
    path = _write_machine_config(tmp_path, regression_password="hunter2")
    result = qp.check_machine_config(path)
    assert result.status == qp.STATUS_BLOCKED
    assert "hunter2" not in result.detail


def test_check_report_root_missing_is_warning(tmp_path):
    result = qp.check_report_root(str(tmp_path / "nonexistent-reports"), env={})
    assert result.status == qp.STATUS_WARNING


def test_check_report_root_existing_writable_is_ready(tmp_path):
    (tmp_path / "reports").mkdir()
    result = qp.check_report_root(str(tmp_path / "reports"), env={})
    assert result.status == qp.STATUS_READY


def test_check_report_root_filesystem_root_is_blocked():
    result = qp.check_report_root("/", env={})
    assert result.status == qp.STATUS_BLOCKED


def test_check_android_sdk_found_via_which():
    result = qp.check_android_sdk(which=lambda name: "/usr/bin/adb" if name == "adb" else None, env={})
    assert result.status == qp.STATUS_READY


def test_check_android_sdk_not_found_is_blocked():
    # env={} makes this hermetic -- without it, a CI runner with a real
    # ANDROID_HOME/platform-tools/adb on disk would pass via the env-var
    # branch regardless of the faked `which`, exactly as happened in CI.
    result = qp.check_android_sdk(which=lambda name: None, env={})
    assert result.status == qp.STATUS_BLOCKED


def test_check_android_sdk_found_via_android_home_env(tmp_path):
    (tmp_path / "platform-tools").mkdir()
    (tmp_path / "platform-tools" / "adb").write_text("")
    result = qp.check_android_sdk(which=lambda name: None, env={"ANDROID_HOME": str(tmp_path)})
    assert result.status == qp.STATUS_READY


def test_check_adb_devices_none_connected_is_blocked():
    availability, serial_check, connected = qp.check_adb_devices(
        "TAB123", adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\n"),
    )
    assert availability.status == qp.STATUS_BLOCKED
    assert connected == []


def test_check_adb_devices_expected_serial_connected_is_ready():
    availability, serial_check, connected = qp.check_adb_devices(
        "TAB123", adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
    )
    assert availability.status == qp.STATUS_READY
    assert serial_check.status == qp.STATUS_READY
    assert connected == ["TAB123"]


def test_check_adb_devices_wrong_serial_connected_is_blocked():
    availability, serial_check, connected = qp.check_adb_devices(
        "TAB123", adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nOTHER\tdevice\n"),
    )
    assert availability.status == qp.STATUS_READY
    assert serial_check.status == qp.STATUS_BLOCKED


def test_check_adb_devices_no_expected_serial_is_warning():
    _, serial_check, _ = qp.check_adb_devices(
        None, adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nX\tdevice\n"),
    )
    assert serial_check.status == qp.STATUS_WARNING


def test_check_adb_devices_adb_not_runnable_is_blocked():
    def _boom(argv):
        raise OSError("adb not found")

    availability, serial_check, connected = qp.check_adb_devices("TAB123", adb_runner=_boom)
    assert availability.status == qp.STATUS_BLOCKED
    assert connected == []


def test_check_appium_no_url_is_warning():
    assert qp.check_appium(None).status == qp.STATUS_WARNING


def test_check_appium_reachable_is_ready():
    result = qp.check_appium("http://127.0.0.1:4723/wd/hub", opener=lambda url: object())
    assert result.status == qp.STATUS_READY


def test_check_appium_unreachable_is_blocked():
    def _boom(url):
        raise OSError("connection refused")

    result = qp.check_appium("http://127.0.0.1:4723/wd/hub", opener=_boom)
    assert result.status == qp.STATUS_BLOCKED


def _flutter_version_runner(version=qp.EXPECTED_FLUTTER_VERSION):
    import json as _json
    return lambda argv: _FakeCompletedProcess(stdout=_json.dumps({"frameworkVersion": version, "dartSdkVersion": "3.5.0"}))


def test_check_flutter_found_with_pinned_version_is_ready():
    result = qp.check_flutter(
        which=lambda name: "/usr/bin/flutter" if name == "flutter" else None, runner=_flutter_version_runner(),
    )
    assert result.status == qp.STATUS_READY


def test_check_flutter_missing_is_blocked():
    assert qp.check_flutter(which=lambda name: None).status == qp.STATUS_BLOCKED


def test_check_flutter_wrong_version_is_blocked():
    # Priority 7 requirement 8: EXACT pinned version, not just presence.
    result = qp.check_flutter(
        which=lambda name: "/usr/bin/flutter" if name == "flutter" else None,
        runner=_flutter_version_runner(version="3.10.0"),
    )
    assert result.status == qp.STATUS_BLOCKED
    assert "3.10.0" in result.detail


def test_check_flutter_unparseable_version_output_is_blocked():
    result = qp.check_flutter(
        which=lambda name: "/usr/bin/flutter" if name == "flutter" else None,
        runner=lambda argv: _FakeCompletedProcess(stdout="not json"),
    )
    assert result.status == qp.STATUS_BLOCKED


def test_check_mobile_devices_for_scope_android_required_and_present():
    checks = qp.check_mobile_devices_for_scope(["android"], android_connected=["TAB123"])
    assert checks[0].status == qp.STATUS_READY


def test_check_mobile_devices_for_scope_android_required_and_absent():
    checks = qp.check_mobile_devices_for_scope(["android"], android_connected=[])
    assert checks[0].status == qp.STATUS_BLOCKED


def test_check_mobile_devices_for_scope_ios_required_and_unknown():
    checks = qp.check_mobile_devices_for_scope(["ios"], android_connected=[], iphone_available=None)
    assert checks[0].status == qp.STATUS_WARNING


def test_check_mobile_devices_for_scope_ios_required_and_absent():
    checks = qp.check_mobile_devices_for_scope(["ios"], android_connected=[], iphone_available=False)
    assert checks[0].status == qp.STATUS_BLOCKED


def test_check_mobile_devices_for_scope_not_in_scope_produces_no_check():
    checks = qp.check_mobile_devices_for_scope(["tablet"], android_connected=[])
    assert checks == []


def test_check_sibling_checkout_missing_is_blocked(tmp_path):
    result = qp.check_sibling_checkout("caleemobile_checkout", tmp_path / "CaleeMobile")
    assert result.status == qp.STATUS_BLOCKED


def test_check_sibling_checkout_present_is_ready(tmp_path):
    d = tmp_path / "CaleeMobile"
    d.mkdir()
    (d / ".git").mkdir()
    result = qp.check_sibling_checkout("caleemobile_checkout", d)
    assert result.status == qp.STATUS_READY


def test_check_sibling_checkout_not_a_git_repo_is_warning(tmp_path):
    d = tmp_path / "CaleeMobile"
    d.mkdir()
    result = qp.check_sibling_checkout("caleemobile_checkout", d)
    assert result.status == qp.STATUS_WARNING


def test_check_keychain_credentials_present_via_injected_resolver():
    from calee_regression import credentials as credentials_mod

    resolver = credentials_mod.default_resolver(injected={
        "regression_username": "tester@example.com", "regression_password": "hunter2",
    })
    result = qp.check_keychain_credentials(resolver=resolver)
    assert result.status == qp.STATUS_READY
    assert "hunter2" not in result.detail
    assert "tester@example.com" not in result.detail


def test_check_keychain_credentials_missing_is_blocked():
    from calee_regression import credentials as credentials_mod

    resolver = credentials_mod.CredentialResolver([credentials_mod.EnvironmentProvider({})])
    result = qp.check_keychain_credentials(resolver=resolver)
    assert result.status == qp.STATUS_BLOCKED


def test_check_ics_publisher_config_no_section_is_warning():
    assert qp.check_ics_publisher_config(None).status == qp.STATUS_WARNING


def test_check_ics_publisher_config_offline_only_is_warning():
    assert qp.check_ics_publisher_config({"mode": "offline-only"}).status == qp.STATUS_WARNING


def test_check_ics_publisher_config_published_valid_is_ready():
    result = qp.check_ics_publisher_config({
        "mode": "published", "publisher": "webdav", "public_url": "https://example.com/reg_sub.ics",
    })
    assert result.status == qp.STATUS_READY


def test_check_ics_publisher_config_published_invalid_url_is_blocked():
    result = qp.check_ics_publisher_config({
        "mode": "published", "publisher": "webdav", "public_url": "http://example.com/reg_sub.ics",
    })
    assert result.status == qp.STATUS_BLOCKED


def test_check_public_ics_url_reachable_is_ready():
    result = qp.check_public_ics_url("https://example.com/reg_sub.ics", opener=lambda url: object())
    assert result.status == qp.STATUS_READY


def test_check_public_ics_url_unreachable_is_blocked():
    def _boom(url):
        raise OSError("404")

    result = qp.check_public_ics_url("https://example.com/reg_sub.ics", opener=_boom)
    assert result.status == qp.STATUS_BLOCKED


def test_check_distributed_build_evidence_availability_none_given_is_warning():
    assert qp.check_distributed_build_evidence_availability(None).status == qp.STATUS_WARNING


def test_check_distributed_build_evidence_availability_valid(tmp_path):
    import datetime

    evidence = {
        "schemaVersion": 2, "component": "caleemobile-distributed-build-acceptance",
        "provider": "app_store_connect", "channel": "testflight", "distributedBuildId": "TF-1",
        "releaseId": "r1", "testedGitSha": "a" * 40, "testedVersion": "0.0.24+24",
        "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatedBy": "provider-api", "sourceDigest": "sha256:" + "1" * 64,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    result = qp.check_distributed_build_evidence_availability(path)
    assert result.status == qp.STATUS_READY


def test_check_distributed_build_evidence_availability_malformed(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"generatedBy": "manual_claim"}))
    result = qp.check_distributed_build_evidence_availability(path)
    assert result.status == qp.STATUS_BLOCKED


def test_check_frozen_candidate_ability_none_given_is_warning():
    assert qp.check_frozen_candidate_ability(None).status == qp.STATUS_WARNING


def test_check_frozen_candidate_ability_valid_bundle(tmp_path):
    import hashlib

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    calee_bytes = b"calee-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    sha = hashlib.sha256(calee_bytes).hexdigest()
    (bundle / "release-manifest.json").write_text(json.dumps({
        "releaseId": "r1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": "a" * 40, "apk": "calee.apk", "sha256": sha},
    }))
    (bundle / "checksums.sha256").write_text(f"{sha}  calee.apk\n")
    result = qp.check_frozen_candidate_ability(bundle)
    assert result.status == qp.STATUS_READY


def test_check_frozen_candidate_ability_invalid_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = qp.check_frozen_candidate_ability(bundle)
    assert result.status == qp.STATUS_BLOCKED


def test_check_manual_check_definitions_missing_is_blocked(tmp_path):
    result = qp.check_manual_check_definitions(tmp_path / "nope.json")
    assert result.status == qp.STATUS_BLOCKED


def test_check_manual_check_definitions_valid(tmp_path):
    path = tmp_path / "manual-checks.json"
    path.write_text(json.dumps([{"title": "t", "instruction": "i"}]))
    result = qp.check_manual_check_definitions(path)
    assert result.status == qp.STATUS_READY


# ── report aggregation ───────────────────────────────────────────────────


def test_preflight_report_overall_warning_when_a_check_warns_and_none_blocked():
    # Priority 7 requirement 17: any warning prevents an unqualified READY --
    # this is the corrected behaviour. The PREVIOUS behaviour (any number of
    # WARNINGs still reported READY) was exactly the fail-open defect this
    # closes; do not reintroduce it.
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_WARNING, "meh"),
    ])
    assert report.overall == qp.STATUS_WARNING
    assert report.to_dict()["overall"] == "WARNING"
    assert report.to_dict()["warnedCapabilities"] == ["b"]


def test_preflight_report_overall_ready_only_when_every_check_is_ready():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_READY, "ok"),
    ])
    assert report.overall == qp.STATUS_READY
    assert report.to_dict()["overall"] == "READY"
    assert report.to_dict()["blockedCapabilities"] == []
    assert report.to_dict()["warnedCapabilities"] == []


def test_preflight_report_overall_blocked_beats_warning():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_WARNING, "meh"),
        qp.PreflightCheck("b", qp.STATUS_BLOCKED, "nope"),
    ])
    assert report.overall == qp.STATUS_BLOCKED
    assert report.to_dict()["blockedCapabilities"] == ["b"]


def test_preflight_report_overall_blocked_when_any_blocked():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_BLOCKED, "nope"),
    ])
    assert report.overall == qp.STATUS_BLOCKED
    assert report.to_dict()["overall"] == "BLOCKED"


# ── full orchestration (injected seams, fully offline) ──────────────────


def _multiplex_subprocess_runner(argv):
    import json as _json

    if argv[:1] == ["/usr/bin/flutter"] or (argv and argv[0].endswith("flutter")):
        return _FakeCompletedProcess(stdout=_json.dumps({"frameworkVersion": qp.EXPECTED_FLUTTER_VERSION, "dartSdkVersion": "3.5.0"}))
    if argv[:1] == ["git"]:
        return _FakeCompletedProcess(stdout="a" * 40)
    return _FakeCompletedProcess(stdout="")


def test_run_qualification_preflight_end_to_end_offline(tmp_path):
    config_path = _write_machine_config(tmp_path)
    (tmp_path / "reports").mkdir()

    report = qp.run_qualification_preflight(
        config_path=config_path,
        repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        subprocess_runner=_multiplex_subprocess_runner,
        env={},
    )
    data = report.to_dict()
    assert data["overall"] in ("READY", "WARNING", "BLOCKED")
    assert "blockedCapabilities" in data
    assert "warnedCapabilities" in data
    names = {c["name"] for c in data["checks"]}
    assert "machine_config" in names
    assert "report_root" in names
    assert "android_sdk_tools" in names
    assert "android_build_tools" in names
    assert "adb_device_availability" in names
    assert "expected_tablet_serial" in names
    assert "appium" in names
    assert "appium_drivers" in names
    assert "flutter" in names
    assert "caleemobile_checkout" in names
    assert "caleemobile_regression_checkout" in names
    assert "keychain_credentials" in names
    assert "external_ics_publisher" in names
    assert "public_ics_url" in names
    assert "ingestion_api_bridge" in names
    assert "selector_ci_evidence_availability" in names
    assert "distributed_build_evidence_availability" in names
    assert "frozen_candidate_ability" in names
    assert "manual_check_definitions" in names
    assert "main_ci_evidence" in names
    assert "main_ci_artifact_authenticated" in names
    # Secret-free output, no matter what.
    dumped = json.dumps(data)
    assert "hunter2" not in dumped


def test_run_qualification_preflight_never_mutates_anything(tmp_path):
    """No APK install, no fixture write, no product API call -- proven here
    by the fact that nothing this test doesn't explicitly create appears on
    disk afterwards."""
    config_path = _write_machine_config(tmp_path)
    before = set(p.name for p in tmp_path.iterdir())
    qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout=""),
        which=lambda name: None,
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network")),
        env={},
    )
    after = set(p.name for p in tmp_path.iterdir())
    # reports/ may legitimately not exist yet (report_root check is read-only
    # and does not create it) -- the set of top-level entries must be
    # unchanged either way.
    assert after == before
