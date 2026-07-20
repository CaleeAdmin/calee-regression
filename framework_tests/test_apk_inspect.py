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


# ── Priority 1: unknown installed signer is a HARD BLOCK ───────────────────
#
# The reader classifies every "cannot authoritatively read the installed
# signer" case as SIGNER_UNKNOWN; the whole-bundle gate then BLOCKS, and the
# installer runs no install command. These lock in the state machine for the
# exact failure list in Priority 1.


def _reader_adb(*, pm=None, pm_rc=0, pm_err=""):
    """Fake adb whose `pm path` step returns the given output/rc/stderr."""

    def adb(argv):
        if "pm" in argv and "path" in argv:
            return ToolResult(pm_rc, pm if pm is not None else "", pm_err)
        return ToolResult(1, "", f"unexpected {argv}")

    return adb


def _installed_apk_path():
    return "package:/data/app/com.viso.calee-1/base.apk\n"


def test_read_signer_adb_pull_failure_is_unknown(tmp_path):
    adb = _reader_adb(pm=_installed_apk_path())

    def pull(dev, dest):
        return ToolResult(1, "", "adb: error: failed to copy: remote object does not exist")

    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        pull=pull, which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_UNKNOWN
    assert "pull" in res.detail.lower()


def test_read_signer_package_manager_failure_is_unknown_not_absent(tmp_path):
    # pm path FAILED (package manager unreachable) -- must NOT be read as
    # "not installed"; the package may be installed with an unreadable signer.
    adb = _reader_adb(
        pm="", pm_rc=1,
        pm_err="Error: Could not access the Package Manager. Is the system running?",
    )
    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_UNKNOWN, res.detail
    assert res.status != ai.SIGNER_NOT_INSTALLED


def test_read_signer_clean_absence_stays_not_installed(tmp_path):
    # Modern adb: a missing package returns rc 1 with EMPTY output. That is a
    # clean "not installed" and must NOT be over-blocked as UNKNOWN.
    adb = _reader_adb(pm="", pm_rc=1, pm_err="")
    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_NOT_INSTALLED, res.detail


def test_read_signer_missing_apksigner_is_unknown(tmp_path):
    adb = _reader_adb(pm=_installed_apk_path())
    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        which=_which(set()), work_dir=tmp_path,  # apksigner NOT on PATH
    )
    assert res.status == ai.SIGNER_UNKNOWN
    assert "apksigner" in res.detail.lower()


def test_read_signer_digest_missing_is_unknown(tmp_path):
    adb = _reader_adb(pm=_installed_apk_path())

    def pull(dev, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"pulled")
        return ToolResult(0, "1 file pulled")

    # apksigner runs cleanly but prints no SHA-256 digest line.
    def apksigner(argv):
        return ToolResult(0, "Signer #1 certificate DN: CN=Calee\n")

    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=apksigner,
        pull=pull, which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_UNKNOWN
    assert "digest" in res.detail.lower()


def test_read_signer_device_offline_is_unknown(tmp_path):
    adb = _reader_adb(pm="", pm_rc=1, pm_err="error: device offline")
    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_UNKNOWN


def test_read_signer_device_unauthorized_is_unknown(tmp_path):
    adb = _reader_adb(pm="", pm_rc=1, pm_err="error: device unauthorized")
    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=lambda a: ToolResult(0, ""),
        which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_UNKNOWN


def test_read_signer_unreadable_installed_apk_is_unknown(tmp_path):
    adb = _reader_adb(pm=_installed_apk_path())

    def pull(dev, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"corrupt")
        return ToolResult(0, "1 file pulled")

    # apksigner cannot parse the pulled APK (returns an error, no digest).
    def apksigner(argv):
        return ToolResult(1, "", "DOES NOT VERIFY / unable to read APK")

    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=apksigner,
        pull=pull, which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_UNKNOWN


def test_read_signer_matching_and_mismatching(tmp_path):
    # Reader returns OK + a digest; classify decides match vs mismatch.
    adb = _reader_adb(pm=_installed_apk_path())

    def pull(dev, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"apk")
        return ToolResult(0, "ok")

    def apksigner(argv):
        return ToolResult(0, _apksigner_certs(CALEE_SIGNER))

    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb, apksigner_runner=apksigner,
        pull=pull, which=_which({"apksigner"}), work_dir=tmp_path,
    )
    assert res.status == ai.SIGNER_OK and res.digest == CALEE_SIGNER
    assert classify_signer(CALEE_SIGNER, res)[0] == ai.SIGNER_OK
    assert classify_signer(SHELL_SIGNER, res)[0] == ai.SIGNER_MISMATCH


def test_read_signer_cleans_up_temp_workspace_by_default():
    # With no work_dir supplied, the reader mints a temp dir; it must be gone
    # after the read (no leftover pulled APK).
    captured = {}

    def pull(dev, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"apk")
        captured["dir"] = Path(dest).parent
        return ToolResult(0, "ok")

    adb = _reader_adb(pm=_installed_apk_path())
    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb,
        apksigner_runner=lambda a: ToolResult(0, _apksigner_certs(CALEE_SIGNER)),
        pull=pull, which=_which({"apksigner"}),  # no work_dir -> internal temp
    )
    assert res.status == ai.SIGNER_OK
    assert not captured["dir"].exists(), "temporary pulled-APK workspace was not cleaned up"


def test_read_signer_retains_temp_workspace_when_requested():
    captured = {}

    def pull(dev, dest):
        from pathlib import Path
        Path(dest).write_bytes(b"apk")
        captured["dest"] = Path(dest)
        return ToolResult(0, "ok")

    adb = _reader_adb(pm=_installed_apk_path())
    res = read_installed_signer(
        "com.viso.calee", adb_runner=adb,
        apksigner_runner=lambda a: ToolResult(0, _apksigner_certs(CALEE_SIGNER)),
        pull=pull, which=_which({"apksigner"}), retain_diagnostics=True,
    )
    assert res.status == ai.SIGNER_OK
    assert captured["dest"].exists(), "diagnostic retention should keep the pulled APK"
    # Clean up what the test asked to retain.
    import shutil
    shutil.rmtree(captured["dest"].parent, ignore_errors=True)


def test_preinstall_blocks_on_unknown_signer(tmp_path):
    v = verify_release_bundle(_bundle(tmp_path))

    def reader(pkg):
        # The installed signer could not be authoritatively read.
        return SignerReadResult(ai.SIGNER_UNKNOWN, detail="pm path could not be read")

    result = preinstall_inspect_bundle(
        v, installed_signer_reader=reader, which=_which({"aapt2", "apksigner"}), runner=_matching_runner()
    )
    assert result.status == ai.STATUS_BLOCKED, result.detail
    assert result.signers["calee"]["classification"] == ai.SIGNER_UNKNOWN
    # The block must be surfaced in the human-readable detail.
    assert any("could not" in d.lower() or "unknown" in d.lower() for d in result.detail)
