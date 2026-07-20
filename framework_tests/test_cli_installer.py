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


def test_install_blocks_and_runs_no_install_when_signer_unknown(tmp_path, monkeypatch):
    """Priority 1: an unreadable installed signer (SIGNER_UNKNOWN) BLOCKS before
    any install command runs. The pre-install inspection returns BLOCKED and the
    installer never reaches execute_install_plan."""
    from calee_regression import apk_inspect, release_installer

    # Force the installed-signer read to be UNKNOWN for every package, while the
    # APK *content* inspection passes (so the block is the signer, not tooling).
    real_preinstall = apk_inspect.preinstall_inspect_bundle

    def _matching_which(name):
        return f"/usr/bin/{name}" if name in {"aapt2", "apksigner"} else None

    class _ContentRunner:
        def __call__(self, argv):
            import os
            tool = os.path.basename(argv[0])
            if tool == "aapt2" and "badging" in argv:
                apk = next((a for a in argv if a.endswith(".apk")), "")
                if "caleeshell" in apk:
                    return apk_inspect.ToolResult(0, "package: name='com.viso.caleeshell' versionCode='212' versionName='founder-v0.2.12'\n")
                return apk_inspect.ToolResult(0, "package: name='com.viso.calee' versionCode='325' versionName='founder-v0.3.25'\n")
            if tool == "apksigner" and "verify" in argv:
                return apk_inspect.ToolResult(0, "Signer #1 certificate SHA-256 digest: " + ("1" * 64) + "\n")
            return apk_inspect.ToolResult(127, "", "unexpected")

    def patched_preinstall(verification, *, installed_signer_reader=None, which=None, runner=None):
        unknown_reader = lambda pkg: apk_inspect.SignerReadResult(
            apk_inspect.SIGNER_UNKNOWN, detail="pm path could not be read -- may be installed"
        )
        return real_preinstall(
            verification, installed_signer_reader=unknown_reader,
            which=_matching_which, runner=_ContentRunner(),
        )

    monkeypatch.setattr(apk_inspect, "preinstall_inspect_bundle", patched_preinstall)

    executed = {"called": False}

    def _spy_execute(*args, **kwargs):
        executed["called"] = True
        raise AssertionError("execute_install_plan must NOT run when the signer is UNKNOWN")

    monkeypatch.setattr(release_installer, "execute_install_plan", _spy_execute)

    bundle = _bundle(tmp_path)
    report = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main, ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--report", str(report)]
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert executed["called"] is False
    payload = json.loads(report.read_text())
    assert payload["status"] == "blocked"
    assert payload["apkInspection"]["signers"]["calee"]["classification"] == "unknown"
