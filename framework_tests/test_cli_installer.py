"""CLI-level tests for the installer commands (verify-release-bundle,
inspect-tablet, install-tablet-release). No device is available, so
inspect/install exercise the honest BLOCKED path; verify and --plan-only run
fully offline.
"""

from __future__ import annotations

import hashlib
import json

from click.testing import CliRunner

from calee_regression import cli
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle(tmp_path, *, corrupt=False):
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    calee_bytes = b"calee-apk-bytes"
    shell_bytes = b"caleeshell-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    (bundle / "caleeshell.apk").write_bytes(shell_bytes)
    manifest = {
        "releaseId": "2026.07.20-rc1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": _sha256(calee_bytes)},
        "caleeShell": {"included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                       "versionCode": 212, "gitSha": SHELL_SHA, "apk": "caleeshell.apk", "sha256": _sha256(shell_bytes)},
    }
    if corrupt:
        manifest["calee"]["sha256"] = "0" * 64
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(calee_bytes)}  calee.apk\n{_sha256(shell_bytes)}  caleeshell.apk\n"
    )
    return bundle


def test_verify_release_bundle_ok(tmp_path):
    bundle = _bundle(tmp_path)
    report = tmp_path / "verify.json"
    result = CliRunner().invoke(cli.main, ["verify-release-bundle", "--bundle", str(bundle), "--report", str(report)])
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "verified" in result.output
    payload = json.loads(report.read_text())
    assert payload["status"] == "ok"
    assert payload["releaseId"] == "2026.07.20-rc1"


def test_verify_release_bundle_invalid_lists_problems(tmp_path):
    bundle = _bundle(tmp_path, corrupt=True)
    result = CliRunner().invoke(cli.main, ["verify-release-bundle", "--bundle", str(bundle)])
    assert result.exit_code == EXIT_INVALID_CONFIG
    assert "SHA-256 mismatch" in result.output


def test_install_tablet_release_refuses_invalid_bundle(tmp_path):
    bundle = _bundle(tmp_path, corrupt=True)
    result = CliRunner().invoke(cli.main, ["install-tablet-release", "--bundle", str(bundle)])
    assert result.exit_code == EXIT_INVALID_CONFIG
    assert "refusing to install" in result.output.lower()


def test_install_tablet_release_plan_only_writes_ordered_plan(tmp_path):
    bundle = _bundle(tmp_path)
    report = tmp_path / "plan.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--plan-only", "--report", str(report)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(report.read_text())
    labels = [s["label"] for s in payload["plan"]["steps"]]
    assert labels.index("install-calee") < labels.index("install-caleeshell")
    assert "reboot" in labels


def test_install_tablet_release_blocks_without_a_device(tmp_path, monkeypatch):
    # Force the adb runner to behave as if no device/adb is present, so the CLI
    # takes the honest BLOCKED path rather than trying to reach a real device.
    from calee_regression import release_installer

    monkeypatch.setattr(
        release_installer, "real_adb_runner",
        lambda argv, **kw: release_installer.AdbResult(returncode=127, stderr="adb executable not found"),
    )
    bundle = _bundle(tmp_path)
    result = CliRunner().invoke(cli.main, ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1"])
    assert result.exit_code == EXIT_BLOCKED
    assert "BLOCKED" in result.output


def test_inspect_tablet_blocks_without_a_device(monkeypatch):
    from calee_regression import release_installer

    monkeypatch.setattr(
        release_installer, "real_adb_runner",
        lambda argv, **kw: release_installer.AdbResult(returncode=1, stderr="error: no devices/emulators found"),
    )
    result = CliRunner().invoke(cli.main, ["inspect-tablet", "--serial", "TAB1"])
    assert result.exit_code == EXIT_BLOCKED
    assert "BLOCKED" in result.output
