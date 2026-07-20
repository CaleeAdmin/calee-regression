"""Priority 2 -- verify the COMPLETE Calee tablet solution after every update.

A release may replace Calee only, CaleeShell only, or both -- but after reboot
the whole solution must be verified for BOTH apps (package present, expected
version, trusted signer, plus Calee's START action resolving and CaleeShell
being HOME). An unchanged app is never ignored: it retains an expected installed
identity that must still hold. All offline: an injected fake AdbRunner + signer
reader, no device.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from calee_regression import release_installer as ri
from calee_regression.release_installer import (
    AppRelease,
    AdbResult,
    CALEE_PACKAGE_ID,
    CALEESHELL_PACKAGE_ID,
    parse_manifest,
    verify_release_bundle,
    verify_tablet_solution,
)

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40
CALEE_SIGNER = "1" * 64
SHELL_SIGNER = "2" * 64


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── expected-identity builders ─────────────────────────────────────────────


def _calee(*, install_artifact=True, version_name="founder-v0.3.26", version_code=326, signer=CALEE_SIGNER):
    return AppRelease(
        key="calee", included=install_artifact, install_artifact=install_artifact,
        package_id=CALEE_PACKAGE_ID, version_name=version_name, version_code=version_code,
        git_sha=CALEE_SHA, signer_sha256=signer, has_expected=True,
    )


def _shell(*, install_artifact=True, version_name="founder-v0.2.12", version_code=212, signer=SHELL_SIGNER):
    return AppRelease(
        key="caleeShell", included=install_artifact, install_artifact=install_artifact,
        package_id=CALEESHELL_PACKAGE_ID, version_name=version_name, version_code=version_code,
        git_sha=SHELL_SHA, signer_sha256=signer, has_expected=True,
    )


class FakeTablet:
    """A device fake for verify_tablet_solution. Configure what's installed, the
    HOME resolution, and whether the Calee START action resolves to Calee."""

    def __init__(self, *, installed, home=CALEESHELL_PACKAGE_ID, start_resolves=CALEE_PACKAGE_ID, device=True):
        # installed: {pkg: (versionName, versionCode)}; absent pkg => not installed.
        self.installed = installed
        self.home = home
        self.start_resolves = start_resolves
        self.device = device
        self.calls: "list[list[str]]" = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if "get-state" in argv:
            return AdbResult(0, "device\n") if self.device else AdbResult(1, "", "error: no devices/emulators found")
        if "dumpsys" in argv and "package" in argv:
            pkg = argv[-1]
            if pkg in self.installed:
                vn, vc = self.installed[pkg]
                return AdbResult(0, f"    versionName={vn}\n    versionCode={vc}\n")
            return AdbResult(0, "")  # not installed
        if "resolve-activity" in argv:
            if "-c" in argv:  # HOME category
                return AdbResult(0, f"  packageName={self.home}\n")
            if "-a" in argv:  # Calee START action
                return AdbResult(0, f"  packageName={self.start_resolves}\n")
        return AdbResult(0, "")


def _signer_reader(digests):
    """digests: {pkg: digest|None}. Returns a duck-typed SignerReadResult."""
    def read(pkg):
        return SimpleNamespace(digest=digests.get(pkg), detail="fake read")
    return read


def _healthy(**overrides):
    """A tablet with both apps at expected versions, HOME=CaleeShell, START=Calee."""
    base = dict(
        installed={
            CALEE_PACKAGE_ID: ("founder-v0.3.26", "326"),
            CALEESHELL_PACKAGE_ID: ("founder-v0.2.12", "212"),
        },
    )
    base.update(overrides)
    return FakeTablet(**base)


def _both_signers_ok():
    return _signer_reader({CALEE_PACKAGE_ID: CALEE_SIGNER, CALEESHELL_PACKAGE_ID: SHELL_SIGNER})


def _blocked_checks(result):
    return [(c.app, c.check) for c in result.checks if c.status == ri.CHECK_BLOCKED]


# ── the ten Priority-2 scenarios ───────────────────────────────────────────


def test_calee_only_update_with_healthy_existing_caleeshell_ok():
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=False),
        runner, installed_signer_reader=_both_signers_ok(),
    )
    assert result.ok, result.detail
    assert _blocked_checks(result) == []


def test_calee_only_update_with_missing_caleeshell_blocks():
    runner = _healthy(installed={CALEE_PACKAGE_ID: ("founder-v0.3.26", "326")})  # no CaleeShell
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=False),
        runner, installed_signer_reader=_signer_reader({CALEE_PACKAGE_ID: CALEE_SIGNER}),
    )
    assert not result.ok
    assert ("caleeShell", "present") in _blocked_checks(result)


def test_calee_only_update_with_wrong_home_blocks():
    runner = _healthy(home=CALEE_PACKAGE_ID)  # HOME wrongly resolves to Calee
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=False),
        runner, installed_signer_reader=_both_signers_ok(),
    )
    assert not result.ok
    assert ("caleeShell", "home") in _blocked_checks(result)


def test_caleeshell_only_update_with_healthy_existing_calee_ok():
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(install_artifact=False), _shell(install_artifact=True),
        runner, installed_signer_reader=_both_signers_ok(),
    )
    assert result.ok, result.detail


def test_caleeshell_only_update_with_missing_calee_blocks():
    runner = _healthy(installed={CALEESHELL_PACKAGE_ID: ("founder-v0.2.12", "212")})  # no Calee
    result = verify_tablet_solution(
        _calee(install_artifact=False), _shell(install_artifact=True),
        runner, installed_signer_reader=_signer_reader({CALEESHELL_PACKAGE_ID: SHELL_SIGNER}),
    )
    assert not result.ok
    assert ("calee", "present") in _blocked_checks(result)


def test_caleeshell_only_update_where_calee_start_does_not_resolve_blocks():
    runner = _healthy(start_resolves="com.android.settings")  # START resolves elsewhere
    result = verify_tablet_solution(
        _calee(install_artifact=False), _shell(install_artifact=True),
        runner, installed_signer_reader=_both_signers_ok(),
    )
    assert not result.ok
    assert ("calee", "launch-action") in _blocked_checks(result)


def test_both_app_update_ok():
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=True),
        runner, installed_signer_reader=_both_signers_ok(),
    )
    assert result.ok, result.detail


def test_first_complete_installation_ok():
    # Nothing was installed before; the release installs BOTH apps, and after
    # reboot both are present at the expected identity.
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=True),
        runner, installed_signer_reader=_both_signers_ok(), release_id="2026.07.20-rc2",
    )
    assert result.ok, result.detail
    assert result.release_id == "2026.07.20-rc2"
    # Both apps are recorded as present in the evidence.
    present = {(c.app) for c in result.checks if c.check == "present" and c.status == ri.CHECK_OK}
    assert present == {"calee", "caleeShell"}


def test_unchanged_app_version_mismatch_blocks():
    # Calee-only update; the UNCHANGED CaleeShell is at the wrong version.
    runner = _healthy(installed={
        CALEE_PACKAGE_ID: ("founder-v0.3.26", "326"),
        CALEESHELL_PACKAGE_ID: ("founder-v0.2.11", "211"),  # expected 212
    })
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=False),
        runner, installed_signer_reader=_both_signers_ok(),
    )
    assert not result.ok
    assert ("caleeShell", "version") in _blocked_checks(result)


def test_unchanged_app_signer_mismatch_blocks():
    # Calee-only update; the UNCHANGED CaleeShell has an untrusted signer.
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=False),
        runner,
        installed_signer_reader=_signer_reader({
            CALEE_PACKAGE_ID: CALEE_SIGNER,
            CALEESHELL_PACKAGE_ID: "f" * 64,  # not the expected SHELL_SIGNER
        }),
    )
    assert not result.ok
    assert ("caleeShell", "signer") in _blocked_checks(result)


# ── contract guards ────────────────────────────────────────────────────────


def test_missing_expected_identity_for_either_app_blocks():
    # A release that fails to declare CaleeShell's expected identity cannot
    # verify the complete solution -> BLOCKED (not silently OK).
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(install_artifact=True), None,
        runner, installed_signer_reader=_both_signers_ok(),
    )
    assert not result.ok
    assert ("caleeShell", "expected-identity") in _blocked_checks(result)


def test_no_device_blocks_honestly():
    runner = _healthy(device=False)
    result = verify_tablet_solution(_calee(), _shell(), runner, installed_signer_reader=_both_signers_ok())
    assert not result.ok
    assert "device" in result.detail.lower()


def test_unreadable_installed_signer_blocks():
    # Signer trust cannot be proven (reader returns no digest) -> BLOCKED.
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(install_artifact=True), _shell(install_artifact=False),
        runner,
        installed_signer_reader=_signer_reader({CALEE_PACKAGE_ID: CALEE_SIGNER, CALEESHELL_PACKAGE_ID: None}),
    )
    assert not result.ok
    assert ("caleeShell", "signer") in _blocked_checks(result)


def test_signer_not_compared_when_no_expected_digest_or_reader():
    # No expected signer digest declared -> recorded as not_compared, not a
    # silent pass and not a hard block on its own.
    runner = _healthy()
    result = verify_tablet_solution(
        _calee(signer=None), _shell(signer=None), runner, installed_signer_reader=None,
    )
    assert result.ok, result.detail
    not_compared = {(c.app) for c in result.checks if c.check == "signer" and c.status == ri.CHECK_NOT_COMPARED}
    assert not_compared == {"calee", "caleeShell"}


# ── manifest schema: installArtifact vs expectedInstalled ──────────────────


def test_manifest_separates_install_artifact_from_expected_installed():
    raw = {
        "releaseId": "2026.07.20-rc2",
        "tabletSolution": {},  # tolerated/ignored; apps read from top-level keys below
        "calee": {
            "installArtifact": True,
            "apk": "calee.apk",
            "sha256": "c" * 64,
            "expectedInstalled": {
                "packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                "versionCode": 326, "gitSha": CALEE_SHA, "signerSha256": CALEE_SIGNER,
            },
        },
        "caleeShell": {
            "installArtifact": False,
            "expectedInstalled": {
                "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                "versionCode": 212, "gitSha": SHELL_SHA, "signerSha256": SHELL_SIGNER,
            },
        },
    }
    manifest, errors = parse_manifest(raw)
    assert errors == [], errors
    calee, shell = manifest.calee, manifest.caleeshell
    # Calee ships an artifact; CaleeShell does not, but BOTH retain an expected
    # installed identity.
    assert calee.install_artifact is True and shell.install_artifact is False
    assert calee.has_expected and shell.has_expected
    assert shell.version_name == "founder-v0.2.12" and shell.signer_sha256 == SHELL_SIGNER
    # Install set is Calee only; expected (verify) set is both.
    assert [a.key for a in manifest.included_apps()] == ["calee"]
    assert {a.key for a in manifest.expected_apps()} == {"calee", "caleeShell"}


def test_unchanged_app_may_not_carry_an_install_artifact():
    raw = {
        "releaseId": "r",
        "calee": {"installArtifact": True, "apk": "calee.apk", "sha256": "c" * 64,
                  "expectedInstalled": {"packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                                        "versionCode": 326, "gitSha": CALEE_SHA}},
        "caleeShell": {"installArtifact": False, "apk": "shell.apk", "sha256": "d" * 64,
                       "expectedInstalled": {"packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                                             "versionCode": 212, "gitSha": SHELL_SHA}},
    }
    manifest, errors = parse_manifest(raw)
    assert any("must not carry an install artifact" in e for e in errors), errors


def test_partial_bundle_retains_expected_identity_for_unchanged_app(tmp_path):
    # A Calee-only bundle (installArtifact:false CaleeShell) verifies at the
    # bundle level and RETAINS CaleeShell's expected identity for later checks.
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    calee_bytes = b"calee-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    manifest = {
        "releaseId": "2026.07.20-rc2",
        "calee": {
            "installArtifact": True, "apk": "calee.apk", "sha256": _sha256(calee_bytes),
            "expectedInstalled": {"packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                                  "versionCode": 326, "gitSha": CALEE_SHA, "signerSha256": CALEE_SIGNER},
        },
        "caleeShell": {
            "installArtifact": False,
            "expectedInstalled": {"packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                                  "versionCode": 212, "gitSha": SHELL_SHA, "signerSha256": SHELL_SIGNER},
        },
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{_sha256(calee_bytes)}  calee.apk\n")

    v = verify_release_bundle(bundle)
    assert v.ok, v.errors
    # Only Calee is an install artifact...
    assert {a.key for a in v.verified_apps} == {"calee"}
    # ...but BOTH apps' expected identities are retained for the solution check.
    assert {a.key for a in v.expected_apps()} == {"calee", "caleeShell"}
    assert v.expected_app("caleeShell").signer_sha256 == SHELL_SIGNER


# ── CLI wiring: install-tablet-release verifies the complete solution ───────


def _partial_bundle(tmp_path):
    """A Calee-only bundle (installArtifact:false CaleeShell, both expected)."""
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    calee_bytes = b"calee-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    manifest = {
        "releaseId": "2026.07.20-rc2",
        "calee": {
            "installArtifact": True, "apk": "calee.apk", "sha256": _sha256(calee_bytes),
            "expectedInstalled": {"packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                                  "versionCode": 326, "gitSha": CALEE_SHA, "signerSha256": CALEE_SIGNER},
        },
        "caleeShell": {
            "installArtifact": False,
            "expectedInstalled": {"packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                                  "versionCode": 212, "gitSha": SHELL_SHA, "signerSha256": SHELL_SIGNER},
        },
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{_sha256(calee_bytes)}  calee.apk\n")
    return bundle


def _drive_install(tmp_path, monkeypatch, *, tablet):
    """Drive install-tablet-release past the offline gates so the Priority-2
    complete-solution verification runs against ``tablet`` (a FakeTablet)."""
    import json as _json
    from click.testing import CliRunner
    from calee_regression import cli, apk_inspect, release_installer

    # Content+signer inspection passes (tested elsewhere); force OK here.
    ok_inspection = apk_inspect.PreinstallInspection(status=apk_inspect.STATUS_OK)
    monkeypatch.setattr(apk_inspect, "preinstall_inspect_bundle", lambda *a, **k: ok_inspection)
    # A matching installed-signer reader for the solution's signer-trust check.
    monkeypatch.setattr(
        apk_inspect, "device_installed_signer_reader",
        lambda **kw: _signer_reader({CALEE_PACKAGE_ID: CALEE_SIGNER, CALEESHELL_PACKAGE_ID: SHELL_SIGNER}),
    )
    # Install execution + pre-install inspection succeed; the solution check is
    # what we exercise, driven by the injected fake device.
    monkeypatch.setattr(
        release_installer, "inspect_tablet",
        lambda runner, **kw: release_installer.TabletInspection(status=release_installer.STATUS_OK, serial=kw.get("serial")),
    )
    monkeypatch.setattr(
        release_installer, "execute_install_plan",
        lambda plan, verification, runner, **kw: release_installer.InstallExecution(
            status=release_installer.STATUS_OK, release_id=plan.release_id, serial=plan.serial
        ),
    )
    monkeypatch.setattr(release_installer, "real_adb_runner", tablet)

    report = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main, ["install-tablet-release", "--bundle", str(_partial_bundle(tmp_path)),
                   "--serial", "TAB1", "--report", str(report)]
    )
    payload = _json.loads(report.read_text()) if report.exists() else {}
    return result, payload


def test_cli_partial_bundle_verifies_both_apps_and_passes(tmp_path, monkeypatch):
    from calee_regression.models import EXIT_SUCCESS
    result, payload = _drive_install(tmp_path, monkeypatch, tablet=_healthy())
    assert result.exit_code == EXIT_SUCCESS, result.output
    sv = payload["solutionVerification"]
    assert sv["status"] == "ok"
    # Both apps were verified even though only Calee was installed.
    assert {c["app"] for c in sv["checks"] if c["check"] == "present"} == {"calee", "caleeShell"}


def test_cli_partial_bundle_blocks_when_unchanged_caleeshell_missing(tmp_path, monkeypatch):
    from calee_regression.models import EXIT_BLOCKED
    tablet = _healthy(installed={CALEE_PACKAGE_ID: ("founder-v0.3.26", "326")})  # CaleeShell gone
    result, payload = _drive_install(tmp_path, monkeypatch, tablet=tablet)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert payload["status"] == "blocked"
    assert payload["solutionVerification"]["status"] == "blocked"
    assert any(c["app"] == "caleeShell" and c["check"] == "present" and c["status"] == "blocked"
               for c in payload["solutionVerification"]["checks"])
