"""Tests for installed-artifact identity attestation (installed_artifact.py)
with a fake adb runner and fake SDK tools -- no device, no network, no real
subprocess. Also covers the tablet-execution policy (mismatch blocks;
unproven blocks certification only) and the no-filesystem-search guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calee_regression import installed_artifact as ia
from calee_regression.apk_inspect import ToolResult

PKG = "com.viso.calee"

DUMPSYS_OK = f"""Packages:
  Package [{PKG}] (abc123):
    versionCode=25 minSdk=26 targetSdk=34
    versionName=0.3.25
    lastUpdateTime=2026-07-20 11:22:33
"""

PM_PATH_OK = "package:/data/app/~~x/{}-1/base.apk\n".format(PKG)

AAPT2_BADGING = (
    f"package: name='{PKG}' versionCode='25' versionName='0.3.25' platformBuildVersionName='14'\n"
)
APKSIGNER_CERTS = "Signer #1 certificate SHA-256 digest: " + "ab" * 32 + "\n"


class FakeAdb:
    """A fake adb command runner keyed on the subcommand; records every argv
    so tests can assert what was (and was not) queried."""

    def __init__(self, responses: dict):
        self.responses = dict(responses)
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        for key, result in self.responses.items():
            if key in " ".join(argv):
                return result
        return ToolResult(returncode=1, stderr="unexpected adb call")


def _fake_tools(tmp_path):
    """A fake `which` + tool runner presenting aapt2/apksigner for the
    expected-identity read (apk_inspect)."""
    def which(name):
        return f"/fake/{name}" if name in ("aapt2", "apksigner") else None

    def runner(argv):
        if "/fake/aapt2" in argv[0]:
            return ToolResult(returncode=0, stdout=AAPT2_BADGING)
        if "/fake/apksigner" in argv[0]:
            return ToolResult(returncode=0, stdout=APKSIGNER_CERTS)
        return ToolResult(returncode=127, stderr="unknown tool")

    return which, runner


@pytest.fixture
def apk(tmp_path) -> Path:
    apk = tmp_path / "calee.apk"
    apk.write_bytes(b"not a real apk, but hashable bytes")
    return apk


def _reconcile(apk, adb, tmp_path, **overrides):
    which, runner = _fake_tools(tmp_path)
    kwargs = dict(
        apk_path=apk, app_package=PKG, serial="TABLET-1",
        adb_runner=adb, which=which, tool_runner=runner,
    )
    kwargs.update(overrides)
    return ia.reconcile(**kwargs)


# ── verified / mismatch ────────────────────────────────────────────────────
def test_matching_identity_is_verified(apk, tmp_path):
    adb = FakeAdb({
        "dumpsys package": ToolResult(0, stdout=DUMPSYS_OK),
        "pm path": ToolResult(0, stdout=PM_PATH_OK),
    })
    result = _reconcile(apk, adb, tmp_path)
    assert result.status == ia.STATUS_VERIFIED
    assert result.expected["applicationId"] == PKG
    assert result.expected["apkSha256"]  # the configured APK's bytes were hashed
    assert result.installed["versionCode"] == "25"
    assert result.installed["lastUpdateTime"] == "2026-07-20 11:22:33"
    assert "versionCode" in result.reason
    # the device serial was passed through to every adb query
    assert all(call[1:3] == ["-s", "TABLET-1"] for call in adb.calls)


def test_version_code_mismatch_names_the_field(apk, tmp_path):
    adb = FakeAdb({
        "dumpsys package": ToolResult(0, stdout=DUMPSYS_OK.replace("versionCode=25", "versionCode=24")),
        "pm path": ToolResult(0, stdout=PM_PATH_OK),
    })
    result = _reconcile(apk, adb, tmp_path)
    assert result.status == ia.STATUS_MISMATCH
    assert result.mismatched_fields == ["versionCode"]
    assert any("expected '25'" in d and "'24'" in d for d in result.detail)


# ── unproven ───────────────────────────────────────────────────────────────
def test_package_not_installed_is_unproven_with_reason(apk, tmp_path):
    adb = FakeAdb({
        "dumpsys package": ToolResult(0, stdout=f"Unable to find package: {PKG}\n"),
        "pm path": ToolResult(0, stdout=""),
    })
    result = _reconcile(apk, adb, tmp_path)
    assert result.status == ia.STATUS_UNPROVEN
    assert "not appear to be installed" in result.reason


def test_adb_absent_is_unproven(apk, tmp_path):
    adb = FakeAdb({"dumpsys package": ToolResult(127, stderr="adb not found")})
    result = _reconcile(apk, adb, tmp_path)
    assert result.status == ia.STATUS_UNPROVEN
    assert "adb" in result.reason


def test_dumpsys_parse_failure_is_unproven(apk, tmp_path):
    adb = FakeAdb({
        "dumpsys package": ToolResult(0, stdout="garbage the parser cannot use"),
        "pm path": ToolResult(0, stdout=PM_PATH_OK),
    })
    result = _reconcile(apk, adb, tmp_path)
    assert result.status == ia.STATUS_UNPROVEN
    assert "parsed" in result.reason


def test_missing_configuration_is_unproven_never_a_crash(tmp_path):
    adb = FakeAdb({})
    assert ia.reconcile(apk_path=None, app_package=PKG, adb_runner=adb).status == ia.STATUS_UNPROVEN
    assert ia.reconcile(apk_path="x.apk", app_package=None, adb_runner=adb).status == ia.STATUS_UNPROVEN
    missing = ia.reconcile(
        apk_path=tmp_path / "nope.apk", app_package=PKG, adb_runner=adb
    )
    assert missing.status == ia.STATUS_UNPROVEN
    assert "no filesystem search" in missing.reason
    assert adb.calls == []  # nothing was even queried


def test_no_filesystem_search_is_performed(apk, tmp_path, monkeypatch):
    # Only the configured apk_path may be opened -- a sibling APK next to it
    # must never be discovered/read.
    decoy = apk.parent / "decoy.apk"
    decoy.write_bytes(b"decoy")
    opened = []
    real_open = Path.open

    def spying_open(self, *args, **kwargs):
        if str(self).endswith(".apk"):
            opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spying_open)
    adb = FakeAdb({
        "dumpsys package": ToolResult(0, stdout=DUMPSYS_OK),
        "pm path": ToolResult(0, stdout=PM_PATH_OK),
    })
    _reconcile(apk, adb, tmp_path)
    assert all(path == str(apk) for path in opened), opened


# ── policy ─────────────────────────────────────────────────────────────────
def test_mismatch_blocks_tablet_execution_in_every_mode():
    result = ia.ReconcileResult(status=ia.STATUS_MISMATCH, mismatched_fields=["versionName"])
    for certifying in (True, False):
        blocked, note = ia.blocks_tablet_execution(result, certifying=certifying)
        assert blocked and "versionName" in note


def test_unproven_blocks_certification_but_not_diagnostic():
    result = ia.ReconcileResult(status=ia.STATUS_UNPROVEN, reason="no device")
    assert ia.blocks_tablet_execution(result, certifying=True)[0] is True
    blocked, _ = ia.blocks_tablet_execution(result, certifying=False)
    assert blocked is False  # diagnostic proceeds, remains non-certifying


def test_verified_never_blocks():
    result = ia.ReconcileResult(status=ia.STATUS_VERIFIED)
    assert ia.blocks_tablet_execution(result, certifying=True) == (False, "")


# ── evidence / iPhone-side identity ────────────────────────────────────────
def test_to_dict_is_json_shaped_and_secret_free(apk, tmp_path):
    adb = FakeAdb({
        "dumpsys package": ToolResult(0, stdout=DUMPSYS_OK),
        "pm path": ToolResult(0, stdout=PM_PATH_OK),
    })
    payload = _reconcile(apk, adb, tmp_path).to_dict()
    assert payload["status"] == "verified"
    assert set(payload) == {
        "status", "expected", "installed", "mismatchedFields", "reason", "detail",
        "iphoneObserved",
    }


def test_iphone_observed_identity_reads_only_the_checkout(tmp_path):
    repo = tmp_path / "CaleeMobile"
    repo.mkdir()
    (repo / "pubspec.yaml").write_text("name: calee\nversion: 0.0.22+22\n")
    observed = ia.iphone_observed_identity(
        repo, head_sha=lambda p: "abc123", dirty=lambda p: True
    )
    assert observed == {
        "source": "caleemobile-checkout", "repoPath": str(repo),
        "gitSha": "abc123", "gitDirty": True, "pubspecVersion": "0.0.22+22",
    }
    assert ia.iphone_observed_identity(tmp_path / "absent") is None
