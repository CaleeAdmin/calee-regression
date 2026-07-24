"""Host execution-capability report (Workstream 7).

Proves ``host-capabilities`` classifies a host deterministically -- a cloud
container as ``OFFLINE_FRAMEWORK_ONLY`` and a fully-equipped Mac as
physical-qualification-capable -- distinguishes unavailable / not-configured /
unsupported-on-host, and NEVER reveals a secret value.
"""

from __future__ import annotations

import json
import types

from click.testing import CliRunner

from calee_regression import host_capabilities as hc
from calee_regression.cli import main


def _which_factory(available):
    avail = set(available)
    return lambda name: (f"/usr/bin/{name}" if name in avail else None)


def _proc(stdout):
    return types.SimpleNamespace(stdout=stdout, returncode=0)


def _cloud_report(**over):
    kw = dict(which=_which_factory(set()), env={}, system="Linux", machine="x86_64", release="6.x", hostname="vm")
    kw.update(over)
    return hc.gather_host_capabilities(**kw)


def _mac_report(**over):
    tools = {"adb", "appium", "flutter", "xcrun", "security", "idevice_id"}
    kw = dict(
        which=_which_factory(tools),
        env={"CALEE_API_BASE": "https://hub-dev.example", "CALEE_TEST_EMAIL": "t@e.co", "CALEE_TEST_PASSWORD": "x"},
        system="Darwin", machine="arm64", release="23.0", hostname="yiwen-mac",
        adb_runner=lambda argv: _proc("List of devices attached\nA26603\tdevice\n"),
        idevice_runner=lambda argv: _proc("00008120-000221A611D3401E\n"),
    )
    kw.update(over)
    return hc.gather_host_capabilities(**kw)


# ── classification ──────────────────────────────────────────────────────────
def test_cloud_linux_is_offline_framework_only():
    r = _cloud_report()
    assert r["executionCapability"] == hc.OFFLINE_FRAMEWORK_ONLY
    assert r["hostCategory"] == "linux"
    assert r["toolchains"]["adb"]["status"] == hc.UNAVAILABLE
    assert r["toolchains"]["appium"]["status"] == hc.UNAVAILABLE
    # Xcode/Keychain cannot exist off macOS -> a DISTINCT reason code.
    assert r["toolchains"]["xcode"]["status"] == hc.UNSUPPORTED_ON_HOST
    assert r["macosKeychain"]["status"] == hc.UNSUPPORTED_ON_HOST
    assert r["backend"]["status"] == hc.NOT_CONFIGURED


def test_equipped_mac_is_physical_capable():
    r = _mac_report()
    assert r["executionCapability"] == hc.PHYSICAL_QUALIFICATION_CAPABLE
    assert r["hostCategory"] == "macos"
    assert r["physicalQualification"]["tablet"]["capable"] is True
    assert r["physicalQualification"]["android"]["capable"] is True
    assert r["physicalQualification"]["ios"]["capable"] is True
    assert r["toolchains"]["xcode"]["status"] == hc.AVAILABLE
    assert r["macosKeychain"]["status"] == hc.AVAILABLE
    assert r["backend"]["status"] == hc.AVAILABLE


def test_partial_tooling_distinguishes_unavailable_from_unsupported():
    # A Mac with adb+appium but no Flutter/Xcode: tablet-capable, not ios.
    r = _mac_report(which=_which_factory({"adb", "appium"}))
    assert r["executionCapability"] == hc.PHYSICAL_QUALIFICATION_CAPABLE
    assert r["physicalQualification"]["tablet"]["capable"] is True
    assert r["physicalQualification"]["ios"]["capable"] is False
    assert r["toolchains"]["flutter"]["status"] == hc.UNAVAILABLE  # could exist, absent
    assert r["toolchains"]["xcode"]["status"] == hc.UNAVAILABLE     # macOS, xcrun just missing


# ── device enumeration (read-only, injected) ────────────────────────────────
def test_android_and_ios_devices_counted_from_injected_runners():
    r = _mac_report()
    assert r["devices"]["android"]["status"] == hc.AVAILABLE
    assert r["devices"]["android"]["count"] == 1
    assert r["devices"]["ios"]["status"] == hc.AVAILABLE
    assert r["devices"]["ios"]["count"] == 1


def test_no_android_devices_reported_when_none_connected():
    r = _mac_report(adb_runner=lambda argv: _proc("List of devices attached\n\n"))
    assert r["devices"]["android"]["status"] == hc.UNAVAILABLE
    assert r["devices"]["android"]["count"] == 0


# ── secrets are never revealed ──────────────────────────────────────────────
def test_report_never_contains_a_secret_value():
    secret = "TOP-SECRET-passw0rd"
    r = _mac_report(env={
        "CALEE_API_BASE": "https://hub-dev.example",
        "CALEE_TEST_EMAIL": "tester@example.com",
        "CALEE_TEST_PASSWORD": secret,
    })
    blob = json.dumps(r) + hc.render_text(r)
    assert secret not in blob
    assert "tester@example.com" not in blob  # not even the email value
    # but PRESENCE of the source is reported
    creds = {c["name"]: c for c in r["credentialSources"]["credentials"]}
    assert creds["regression_password"]["status"] == hc.AVAILABLE
    assert creds["regression_password"]["source"] == "environment"


def test_credential_source_unavailable_when_absent_off_macos():
    r = _cloud_report(env={})
    creds = {c["name"]: c for c in r["credentialSources"]["credentials"]}
    assert creds["regression_username"]["status"] == hc.UNAVAILABLE
    assert r["credentialSources"]["macosKeychain"] == hc.UNSUPPORTED_ON_HOST


def test_keychain_capable_credentials_on_mac_without_env():
    r = _mac_report(env={})  # no env creds, but Keychain present
    creds = {c["name"]: c for c in r["credentialSources"]["credentials"]}
    assert creds["regression_password"]["status"] == hc.AVAILABLE
    assert creds["regression_password"]["source"] == "keychain-capable"


# ── determinism + provenance ────────────────────────────────────────────────
def test_report_is_deterministic_and_carries_interpreter_provenance():
    a = _cloud_report()
    b = _cloud_report()
    assert a == b
    assert a["python"]["pythonExecutable"]
    assert a["schemaVersion"] == hc.SCHEMA_VERSION


# ── CLI ─────────────────────────────────────────────────────────────────────
def test_cli_host_capabilities_json_is_valid_and_exit_zero():
    res = CliRunner().invoke(main, ["host-capabilities", "--format", "json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["report"] == "host-capabilities"
    assert payload["executionCapability"] in (hc.OFFLINE_FRAMEWORK_ONLY, hc.PHYSICAL_QUALIFICATION_CAPABLE)


def test_cli_host_capabilities_text_renders():
    res = CliRunner().invoke(main, ["host-capabilities", "--format", "text"])
    assert res.exit_code == 0, res.output
    assert "execution capability" in res.output
