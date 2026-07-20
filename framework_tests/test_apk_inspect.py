"""Offline tests for actual APK content + signer inspection (Priority 5).

No real signed APK, SDK tool, or device is involved: tool discovery goes
through an injected ``which`` and every tool/adb call through an injected
runner returning fixture output. This locks in identity/signer parsing, the
manifest cross-check, signer comparison, and the whole-bundle pre-install gate.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from calee_regression import apk_inspect as ai
from calee_regression.apk_inspect import (
    ApkInspection,
    SignerReadResult,
    ToolResult,
    classify_signer,
    inspect_apk,
    parse_aapt2_badging,
    parse_apksigner_certs,
    parse_pm_path,
    preinstall_inspect_bundle,
    read_installed_signer,
    verify_identity_matches,
)
from calee_regression.release_installer import verify_release_bundle

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40
CALEE_SIGNER = "1" * 64
SHELL_SIGNER = "2" * 64


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── fixtures / fakes ──────────────────────────────────────────────────────


def _which(available):
    return lambda name: (f"/usr/bin/{name}" if name in available else None)


def _aapt2_badging(app_id, version_code, version_name):
    return (
        f"package: name='{app_id}' versionCode='{version_code}' versionName='{version_name}' "
        f"compileSdkVersion='34'\n"
        "application-label:'Calee'\n"
    )


def _apksigner_certs(digest):
    return (
        "Signer #1 certificate DN: CN=Calee Release\n"
        f"Signer #1 certificate SHA-256 digest: {digest}\n"
        "Signer #1 certificate SHA-1 digest: deadbeef\n"
        "Signer #1 key algorithm: rsaEncryption\n"
    )


class FakeRunner:
    """Routes argv to fixture output based on the tool + subcommand."""

    def __init__(self, *, identities=None, signers=None, badging=None, rc_overrides=None):
        # identities: {apk_substr: (app_id, code, name)} for aapt2/apkanalyzer
        self.identities = identities or {}
        self.signers = signers or {}   # {apk_substr: digest}
        self.badging = badging if badging is not None else True
        self.rc_overrides = rc_overrides or {}
        self.calls = []

    def _identity_for(self, argv):
        for key, ident in self.identities.items():
            if any(key in a for a in argv):
                return ident
        return None

    def _signer_for(self, argv):
        for key, digest in self.signers.items():
            if any(key in a for a in argv):
                return digest
        return None

    def __call__(self, argv):
        self.calls.append(list(argv))
        tool = os.path.basename(argv[0])
        if tool == "aapt2" and "badging" in argv:
            ident = self._identity_for(argv)
            if ident is None:
                return ToolResult(1, "", "no such file")
            return ToolResult(0, _aapt2_badging(*ident))
        if tool == "apkanalyzer" and "manifest" in argv:
            ident = self._identity_for(argv)
            if ident is None:
                return ToolResult(1, "", "no manifest")
            app_id, code, name = ident
            field = argv[argv.index("manifest") + 1]
            value = {"application-id": app_id, "version-code": code, "version-name": name}[field]
            return ToolResult(0, f"{value}\n")
        if tool == "apksigner" and "verify" in argv:
            digest = self._signer_for(argv)
            if digest is None:
                return ToolResult(1, "", "DOES NOT VERIFY")
            return ToolResult(0, _apksigner_certs(digest))
        return ToolResult(127, "", f"unexpected argv {argv}")


# ── pure parsers ──────────────────────────────────────────────────────────


def test_parse_aapt2_badging():
    app_id, code, name = parse_aapt2_badging(_aapt2_badging("com.viso.calee", "325", "founder-v0.3.25"))
    assert app_id == "com.viso.calee"
    assert code == "325"
    assert name == "founder-v0.3.25"


def test_parse_apksigner_certs_tolerates_spacing_and_case():
    assert parse_apksigner_certs(_apksigner_certs(CALEE_SIGNER)) == CALEE_SIGNER
    assert parse_apksigner_certs("Signer #1 certificate SHA256 digest:  " + CALEE_SIGNER.upper()) == CALEE_SIGNER
    assert parse_apksigner_certs("nothing here") is None


def test_parse_pm_path():
    out = "package:/data/app/~~ab==/com.viso.calee-xy==/base.apk\n"
    assert parse_pm_path(out) == "/data/app/~~ab==/com.viso.calee-xy==/base.apk"
    assert parse_pm_path("") is None


# ── inspect_apk ───────────────────────────────────────────────────────────


def _write_apk(tmp_path, name="calee.apk", data=b"calee-apk-bytes"):
    apk = tmp_path / name
    apk.write_bytes(data)
    return apk


def test_inspect_apk_happy_via_aapt2(tmp_path):
    apk = _write_apk(tmp_path)
    runner = FakeRunner(
        identities={"calee.apk": ("com.viso.calee", "325", "founder-v0.3.25")},
        signers={"calee.apk": CALEE_SIGNER},
    )
    insp = inspect_apk(apk, "calee", manifest_git_sha=CALEE_SHA, which=_which({"aapt2", "apksigner"}), runner=runner)
    assert insp.ok, insp.detail
    assert insp.application_id == "com.viso.calee"
    assert insp.version_name == "founder-v0.3.25"
    assert insp.version_code == "325"
    assert insp.signer_sha256 == CALEE_SIGNER
    assert insp.apk_sha256 == _sha256(b"calee-apk-bytes")
    assert insp.manifest_git_sha == CALEE_SHA
    assert insp.tool_used == "aapt2"


def test_inspect_apk_happy_via_apkanalyzer(tmp_path):
    apk = _write_apk(tmp_path)
    runner = FakeRunner(
        identities={"calee.apk": ("com.viso.calee", "325", "founder-v0.3.25")},
        signers={"calee.apk": CALEE_SIGNER},
    )
    insp = inspect_apk(apk, "calee", which=_which({"apkanalyzer", "apksigner"}), runner=runner)
    assert insp.ok, insp.detail
    assert insp.tool_used == "apkanalyzer"
    assert insp.application_id == "com.viso.calee"


def test_inspect_apk_blocks_when_identity_tools_absent(tmp_path):
    apk = _write_apk(tmp_path)
    runner = FakeRunner(signers={"calee.apk": CALEE_SIGNER})
    insp = inspect_apk(apk, "calee", which=_which({"apksigner"}), runner=runner)  # no aapt2/apkanalyzer
    assert insp.status == ai.STATUS_BLOCKED
    assert any("apkanalyzer nor aapt2" in d for d in insp.detail)
    assert any("Install the Android SDK" in d for d in insp.detail)
    # The APK file hash is still computed (that is what was actually read).
    assert insp.apk_sha256 == _sha256(b"calee-apk-bytes")


def test_inspect_apk_blocks_when_apksigner_absent(tmp_path):
    apk = _write_apk(tmp_path)
    runner = FakeRunner(identities={"calee.apk": ("com.viso.calee", "325", "founder-v0.3.25")})
    insp = inspect_apk(apk, "calee", which=_which({"aapt2"}), runner=runner)  # no apksigner
    assert insp.status == ai.STATUS_BLOCKED
    assert any("apksigner is not on PATH" in d for d in insp.detail)


# ── manifest cross-check ──────────────────────────────────────────────────


def test_verify_identity_matches_ok():
    insp = ApkInspection(key="calee", apk_path="x", status=ai.STATUS_OK,
                         application_id="com.viso.calee", version_name="founder-v0.3.25", version_code="325")
    problems = verify_identity_matches(insp, expected_package="com.viso.calee",
                                       manifest_version_name="founder-v0.3.25", manifest_version_code=325)
    assert problems == []


def test_verify_identity_flags_wrong_package():
    insp = ApkInspection(key="calee", apk_path="x", status=ai.STATUS_OK,
                         application_id="com.evil.calee", version_name="founder-v0.3.25", version_code="325")
    problems = verify_identity_matches(insp, expected_package="com.viso.calee",
                                       manifest_version_name="founder-v0.3.25", manifest_version_code=325)
    assert any("applicationId" in p for p in problems)


def test_verify_identity_flags_version_and_code_mismatch():
    insp = ApkInspection(key="calee", apk_path="x", status=ai.STATUS_OK,
                         application_id="com.viso.calee", version_name="founder-v0.3.24", version_code="324")
    problems = verify_identity_matches(insp, expected_package="com.viso.calee",
                                       manifest_version_name="founder-v0.3.25", manifest_version_code=325)
    assert any("versionName" in p for p in problems)
    assert any("versionCode" in p for p in problems)


# ── signer comparison ─────────────────────────────────────────────────────


def test_classify_signer_ok_and_mismatch_and_unknown_and_absent():
    ok = classify_signer(CALEE_SIGNER, SignerReadResult(ai.SIGNER_OK, digest=CALEE_SIGNER))
    assert ok[0] == ai.SIGNER_OK
    mismatch = classify_signer(CALEE_SIGNER, SignerReadResult(ai.SIGNER_OK, digest=SHELL_SIGNER))
    assert mismatch[0] == ai.SIGNER_MISMATCH
    unknown = classify_signer(CALEE_SIGNER, SignerReadResult(ai.SIGNER_UNKNOWN, detail="no device"))
    assert unknown[0] == ai.SIGNER_UNKNOWN
    absent = classify_signer(CALEE_SIGNER, SignerReadResult(ai.SIGNER_NOT_INSTALLED))
    assert absent[0] == ai.SIGNER_NOT_INSTALLED


def test_read_installed_signer_happy(tmp_path):
    def adb(argv):
        if "pm" in argv and "path" in argv:
            return ToolResult(0, "package:/data/app/com.viso.calee-1/base.apk\n")
        return ToolResult(1, "", "unexpected")

    def apksigner(argv):
        return ToolResult(0, _apksigner_certs(CALEE_SIGNER))

    pulled = {}

    def pull(dev, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"pulled-apk")
        pulled["dev"] = dev
        return ToolResult(0, "1 file pulled")

    res = read_installed_signer(
        "com.viso.calee", serial="TAB1", adb_runner=adb, apksigner_runner=apksigner,
        pull=pull, which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_OK
    assert res.digest == CALEE_SIGNER
    assert pulled["dev"] == "/data/app/com.viso.calee-1/base.apk"


def test_read_installed_signer_not_installed(tmp_path):
    def adb(argv):
        return ToolResult(0, "\n")  # pm path prints nothing -> not installed

    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_NOT_INSTALLED


def test_read_installed_signer_no_device(tmp_path):
    def adb(argv):
        return ToolResult(1, "", "error: no devices/emulators found")

    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_UNKNOWN


# ── whole-bundle pre-install gate ─────────────────────────────────────────


def _bundle(tmp_path):
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
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(calee_bytes)}  calee.apk\n{_sha256(shell_bytes)}  caleeshell.apk\n"
    )
    return bundle


def _matching_runner():
    return FakeRunner(
        identities={
            "calee.apk": ("com.viso.calee", "325", "founder-v0.3.25"),
            "caleeshell.apk": ("com.viso.caleeshell", "212", "founder-v0.2.12"),
        },
        signers={"calee.apk": CALEE_SIGNER, "caleeshell.apk": SHELL_SIGNER},
    )


def test_preinstall_ok_when_actual_matches_manifest(tmp_path):
    v = verify_release_bundle(_bundle(tmp_path))
    assert v.ok
    result = preinstall_inspect_bundle(v, which=_which({"aapt2", "apksigner"}), runner=_matching_runner())
    assert result.status == ai.STATUS_OK, result.detail
    assert {a.key for a in result.apps} == {"calee", "caleeShell"}
    # No installed-signer reader supplied -> recorded as not compared, not a block.
    assert result.signers["calee"]["classification"] == "not_compared"


def test_preinstall_blocks_when_tools_absent(tmp_path):
    v = verify_release_bundle(_bundle(tmp_path))
    result = preinstall_inspect_bundle(v, which=_which(set()), runner=_matching_runner())
    assert result.status == ai.STATUS_BLOCKED
    assert any("Install the Android SDK" in d for d in result.detail)


def test_preinstall_invalid_when_actual_package_differs(tmp_path):
    v = verify_release_bundle(_bundle(tmp_path))
    runner = FakeRunner(
        identities={
            # calee.apk actually contains a DIFFERENT application id
            "calee.apk": ("com.evil.calee", "325", "founder-v0.3.25"),
            "caleeshell.apk": ("com.viso.caleeshell", "212", "founder-v0.2.12"),
        },
        signers={"calee.apk": CALEE_SIGNER, "caleeshell.apk": SHELL_SIGNER},
    )
    result = preinstall_inspect_bundle(v, which=_which({"aapt2", "apksigner"}), runner=runner)
    assert result.status == ai.STATUS_INVALID
    assert any("applicationId" in d for d in result.detail)


def test_preinstall_blocks_on_signer_mismatch_and_never_uninstalls(tmp_path):
    v = verify_release_bundle(_bundle(tmp_path))

    def reader(pkg):
        # The device has a DIFFERENT signer than the release APK.
        return SignerReadResult(ai.SIGNER_OK, digest="f" * 64)

    result = preinstall_inspect_bundle(
        v, installed_signer_reader=reader, which=_which({"aapt2", "apksigner"}), runner=_matching_runner()
    )
    assert result.status == ai.STATUS_BLOCKED
    assert result.signers["calee"]["classification"] == ai.SIGNER_MISMATCH
    assert any("BLOCKED" in d for d in result.detail)


def test_preinstall_ok_when_installed_signer_matches(tmp_path):
    v = verify_release_bundle(_bundle(tmp_path))
    digests = {"com.viso.calee": CALEE_SIGNER, "com.viso.caleeshell": SHELL_SIGNER}

    def reader(pkg):
        return SignerReadResult(ai.SIGNER_OK, digest=digests[pkg])

    result = preinstall_inspect_bundle(
        v, installed_signer_reader=reader, which=_which({"aapt2", "apksigner"}), runner=_matching_runner()
    )
    assert result.status == ai.STATUS_OK, result.detail
    assert result.signers["calee"]["classification"] == ai.SIGNER_OK


def test_preinstall_first_time_install_has_no_signer_conflict(tmp_path):
    v = verify_release_bundle(_bundle(tmp_path))

    def reader(pkg):
        return SignerReadResult(ai.SIGNER_NOT_INSTALLED)

    result = preinstall_inspect_bundle(
        v, installed_signer_reader=reader, which=_which({"aapt2", "apksigner"}), runner=_matching_runner()
    )
    assert result.status == ai.STATUS_OK
    assert result.signers["calee"]["classification"] == ai.SIGNER_NOT_INSTALLED


def test_preinstall_refuses_unverified_bundle(tmp_path):
    bad = verify_release_bundle(tmp_path / "nope")
    assert not bad.ok
    result = preinstall_inspect_bundle(bad, which=_which({"aapt2", "apksigner"}), runner=_matching_runner())
    assert result.status == ai.STATUS_INVALID
