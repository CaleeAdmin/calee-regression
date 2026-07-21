"""Priority 7 (this session) -- qualification preflight derives every
required check from the ACTUAL release candidate (a composed effective
release configuration, exactly like the real launcher would produce), never
merely from the machine's own declared capability scope.

Covers, end-to-end via run_qualification_preflight with a real (schema-v2)
bundle:

  * the tablet's own serial does not satisfy Android-mobile readiness;
  * a required iPhone device whose connectivity state is unknown BLOCKS
    (never merely warns);
  * the configured iphone_device UDID must match exactly -- "some iPhone is
    connected" is not enough;
  * kiosk_admin required by the release but unauthorised on the machine
    BLOCKS via the composed configuration's own conflict detection;
  * distributed-build evidence for a DIFFERENT release blocks;
  * `overall` never reports READY while anything is BLOCKED or WARNING.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from calee_regression import qualification_preflight as qp

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40
CALEEMOBILE_SHA = "c" * 40
CALEE_SIGNER = "1" * 64
SHELL_SIGNER = "2" * 64
CALEE_BYTES = b"calee-apk-bytes"
SHELL_BYTES = b"caleeshell-apk-bytes"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_v2_bundle(tmp_path, *, platforms, profile="production", release_id="2026.07.21-rc1", kiosk_admin=True):
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    (bundle / "calee.apk").write_bytes(CALEE_BYTES)
    (bundle / "caleeshell.apk").write_bytes(SHELL_BYTES)
    manifest = {
        "schemaVersion": 2,
        "releaseId": release_id,
        "profile": profile,
        "backend": "https://hub.calee.com.au",
        "platforms": platforms,
        "features": {
            "synchronization": True, "meals": True, "onboarding": True,
            "googleCalendar": False, "kioskAdmin": kiosk_admin, "notifications": True,
        },
        "tabletSolution": {
            "calee": {
                "installArtifact": True, "apk": "calee.apk", "sha256": _sha256(CALEE_BYTES),
                "expectedInstalled": {
                    "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                    "versionCode": 325, "gitSha": CALEE_SHA, "signerSha256": CALEE_SIGNER,
                },
            },
            "caleeShell": {
                "installArtifact": True, "apk": "caleeshell.apk", "sha256": _sha256(SHELL_BYTES),
                "expectedInstalled": {
                    "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                    "versionCode": 212, "gitSha": SHELL_SHA, "signerSha256": SHELL_SIGNER,
                },
            },
        },
        "caleeMobile": {
            "version": "0.0.24+24", "gitSha": CALEEMOBILE_SHA,
            "selectorEvidenceRequired": True, "distributedBuildAcceptanceRequired": True,
        },
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(CALEE_BYTES)}  calee.apk\n{_sha256(SHELL_BYTES)}  caleeshell.apk\n"
    )
    return bundle


def _write_machine_config(tmp_path, **overrides):
    data = dict(
        tablet_serial="TAB123", expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee", caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir=str(tmp_path), backend_url="https://hub.calee.com.au",
        release_profile="production", report_dir="reports", mobile_platforms=["android", "ios"],
        allow_caleeshell_technical=True,
    )
    data.update(overrides)
    path = tmp_path / "machine.local.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _flutter_ok_runner(argv):
    if argv and "flutter" in argv[0]:
        return _FakeCompletedProcess(stdout=json.dumps({"frameworkVersion": qp.EXPECTED_FLUTTER_VERSION, "dartSdkVersion": "3.5.0"}))
    return _FakeCompletedProcess(stdout="")


def _no_network_opener(url):
    raise OSError("no network in test")


def test_tablet_serial_does_not_satisfy_android_mobile_readiness(tmp_path, monkeypatch):
    """Priority 7 requirement 5: a machine with ONLY a tablet_serial (no
    distinct android_device) connected must not be reported READY for a
    release that requires Android-mobile."""
    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": True, "mobileIos": False})
    machine = _write_machine_config(tmp_path, mobile_platforms=["android"])  # no android_device configured

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=_no_network_opener, subprocess_runner=_flutter_ok_runner, env={},
    )
    android_check = next(c for c in report.checks if c.name == "android_device_for_scope")
    assert android_check.status == qp.STATUS_BLOCKED
    assert "tablet" in android_check.detail.lower()
    assert report.overall == qp.STATUS_BLOCKED


def test_tablet_serial_reused_as_android_device_still_blocks(tmp_path, monkeypatch):
    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": True, "mobileIos": False})
    machine = _write_machine_config(tmp_path, mobile_platforms=["android"], android_device="TAB123")  # same as tablet!

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=_no_network_opener, subprocess_runner=_flutter_ok_runner, env={},
    )
    android_check = next(c for c in report.checks if c.name == "android_device_for_scope")
    assert android_check.status == qp.STATUS_BLOCKED
    assert "SAME serial" in android_check.detail


def test_distinct_android_device_connected_is_ready(tmp_path):
    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": True, "mobileIos": False})
    machine = _write_machine_config(tmp_path, mobile_platforms=["android"], android_device="R5CANDROID")

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(
            stdout="List of devices attached\nTAB123\tdevice\nR5CANDROID\tdevice\n",
        ),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=_no_network_opener, subprocess_runner=_flutter_ok_runner, env={},
    )
    android_check = next(c for c in report.checks if c.name == "android_device_for_scope")
    assert android_check.status == qp.STATUS_READY


def test_required_ios_with_undeterminable_device_state_blocks_not_warns(tmp_path):
    """Priority 7 requirement 7: idevice_id not installed (state unknown)
    for a release that REQUIRES iOS must BLOCK, never merely WARN."""
    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": False, "mobileIos": True})
    machine = _write_machine_config(tmp_path, mobile_platforms=["ios"], iphone_device="00008110-DEADBEEF")

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        # idevice_id deliberately absent from `which` -> state cannot be determined.
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=_no_network_opener, subprocess_runner=_flutter_ok_runner, env={},
    )
    iphone_check = next(c for c in report.checks if c.name == "iphone_device_for_scope")
    assert iphone_check.status == qp.STATUS_BLOCKED
    assert report.overall == qp.STATUS_BLOCKED


def test_required_ios_matches_configured_udid_exactly(tmp_path):
    """Priority 7 requirement 6: "some iPhone exists" is not enough -- the
    CONFIGURED iphone_device UDID specifically must be present."""
    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": False, "mobileIos": True})
    machine = _write_machine_config(tmp_path, mobile_platforms=["ios"], iphone_device="00008110-DEADBEEF")

    def which(name):
        return {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter", "idevice_id": "/usr/bin/idevice_id"}.get(name)

    def subprocess_runner(argv):
        if argv and "idevice_id" in argv[0]:
            return _FakeCompletedProcess(stdout="some-other-iphone-udid\n")
        return _flutter_ok_runner(argv)

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=which, http_opener=_no_network_opener, subprocess_runner=subprocess_runner, env={},
    )
    iphone_check = next(c for c in report.checks if c.name == "iphone_device_for_scope")
    assert iphone_check.status == qp.STATUS_BLOCKED
    assert "00008110-DEADBEEF" in iphone_check.detail


def test_required_ios_configured_udid_connected_is_ready(tmp_path):
    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": False, "mobileIos": True})
    machine = _write_machine_config(tmp_path, mobile_platforms=["ios"], iphone_device="00008110-DEADBEEF")

    def which(name):
        return {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter", "idevice_id": "/usr/bin/idevice_id"}.get(name)

    def subprocess_runner(argv):
        if argv and "idevice_id" in argv[0]:
            return _FakeCompletedProcess(stdout="00008110-DEADBEEF\n")
        return _flutter_ok_runner(argv)

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=which, http_opener=_no_network_opener, subprocess_runner=subprocess_runner, env={},
    )
    iphone_check = next(c for c in report.checks if c.name == "iphone_device_for_scope")
    assert iphone_check.status == qp.STATUS_READY


def test_kiosk_admin_required_but_unauthorised_blocks_via_composed_conflict(tmp_path):
    bundle = _write_v2_bundle(
        tmp_path, platforms={"tablet": True, "mobileAndroid": False, "mobileIos": False}, kiosk_admin=True,
    )
    machine = _write_machine_config(tmp_path, mobile_platforms=[], allow_caleeshell_technical=False)

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=_no_network_opener, subprocess_runner=_flutter_ok_runner, env={},
    )
    conflict_checks = [c for c in report.checks if c.name.startswith("release_scope_conflict:")]
    assert any("kiosk_admin" in c.name for c in conflict_checks)
    assert all(c.status == qp.STATUS_BLOCKED for c in conflict_checks if "kiosk_admin" in c.name)
    assert report.overall == qp.STATUS_BLOCKED


def test_distributed_build_evidence_for_a_different_release_blocks(tmp_path):
    """Priority 7 requirement 13 / Priority 8 required test 20: distributed
    evidence bound to ANOTHER release's identity must BLOCK, even though the
    file itself is well-formed."""
    import datetime

    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": False, "mobileIos": False}, release_id="2026.07.21-rc1")
    machine = _write_machine_config(tmp_path, mobile_platforms=[])

    evidence = {
        "schemaVersion": 2, "component": "caleemobile-distributed-build-acceptance",
        "provider": "app_store_connect", "channel": "testflight", "distributedBuildId": "TF-1",
        "releaseId": "SOME-OTHER-RELEASE", "testedGitSha": "d" * 40, "testedVersion": "9.9.9",
        "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatedBy": "provider-api", "sourceDigest": "sha256:" + "1" * 64,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    evidence_path = tmp_path / "distributed-build-evidence.json"
    evidence_path.write_text(json.dumps(evidence))

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, distributed_build_evidence_path=evidence_path, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=_no_network_opener, subprocess_runner=_flutter_ok_runner, env={},
    )
    dbe_check = next(c for c in report.checks if c.name == "distributed_build_evidence_availability")
    assert dbe_check.status == qp.STATUS_BLOCKED
    assert report.overall == qp.STATUS_BLOCKED


def test_overall_never_ready_while_any_warning_present_in_full_orchestration(tmp_path):
    """Priority 7 requirement 17, exercised end-to-end: a release scope that
    triggers at least one WARNING (e.g. no public_url configured, no
    subscribed-calendar requirement) and zero BLOCKED must report WARNING,
    never an unqualified READY."""
    bundle = _write_v2_bundle(tmp_path, platforms={"tablet": True, "mobileAndroid": False, "mobileIos": False}, kiosk_admin=False)
    machine = _write_machine_config(tmp_path, mobile_platforms=[], allow_caleeshell_technical=True)

    report = qp.run_qualification_preflight(
        config_path=machine, bundle_path=bundle, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=_no_network_opener, subprocess_runner=_flutter_ok_runner, env={},
    )
    if report.overall != qp.STATUS_BLOCKED:
        assert report.overall == qp.STATUS_WARNING
    assert report.overall != qp.STATUS_READY or not any(c.status == qp.STATUS_WARNING for c in report.checks)
