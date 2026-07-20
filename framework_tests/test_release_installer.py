"""Offline tests for the tablet release installer (Phase 2).

No adb, no device, no real APK signing is involved: bundles are built in
tmp_path with dummy APK bytes and correct SHA-256 checksums, and every adb
interaction goes through an injected fake AdbRunner. This locks in bundle
verification, install-command construction, ordering, and adb-output
classification independent of any device.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from calee_regression import release_installer as ri
from calee_regression.release_installer import (
    AdbResult,
    AppRelease,
    BundleVerification,
    ReleaseInstallerError,
    build_install_plan,
    classify_home_resolution,
    classify_install_output,
    classify_version_match,
    decide_downgrade,
    parse_installed_identity,
    parse_manifest,
    parse_resolved_package,
    verify_release_bundle,
)

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bundle(
    tmp_path,
    *,
    include_caleeshell=True,
    calee_bytes=b"calee-apk-bytes",
    shell_bytes=b"caleeshell-apk-bytes",
    manifest_overrides=None,
    checksums_override=None,
    extra_files=None,
    calee_apk_name="calee.apk",
    shell_apk_name="caleeshell.apk",
):
    """Build a valid release bundle, applying optional overrides for the
    negative cases. Returns the bundle directory Path."""
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    (bundle / calee_apk_name).write_bytes(calee_bytes)
    manifest = {
        "releaseId": "2026.07.20-rc1",
        "calee": {
            "included": True,
            "packageId": "com.viso.calee",
            "versionName": "founder-v0.3.25",
            "versionCode": 325,
            "gitSha": CALEE_SHA,
            "apk": calee_apk_name,
            "sha256": _sha256(calee_bytes),
        },
    }
    checksum_lines = [f"{_sha256(calee_bytes)}  {calee_apk_name}"]
    if include_caleeshell:
        (bundle / shell_apk_name).write_bytes(shell_bytes)
        manifest["caleeShell"] = {
            "included": True,
            "packageId": "com.viso.caleeshell",
            "versionName": "founder-v0.2.12",
            "versionCode": 212,
            "gitSha": SHELL_SHA,
            "apk": shell_apk_name,
            "sha256": _sha256(shell_bytes),
        }
        checksum_lines.append(f"{_sha256(shell_bytes)}  {shell_apk_name}")

    if manifest_overrides is not None:
        manifest_overrides(manifest)

    (bundle / "release-manifest.json").write_text(json.dumps(manifest, indent=2))
    checksums = checksums_override if checksums_override is not None else "\n".join(checksum_lines) + "\n"
    (bundle / "checksums.sha256").write_text(checksums)

    for name, content in (extra_files or {}).items():
        (bundle / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    return bundle


# ── bundle verification: happy path ──────────────────────────────────────


def test_valid_bundle_verifies_ok(tmp_path):
    result = verify_release_bundle(_write_bundle(tmp_path))
    assert result.ok, result.errors
    assert result.status == ri.STATUS_OK
    assert result.manifest.release_id == "2026.07.20-rc1"
    assert {a.key for a in result.verified_apps} == {"calee", "caleeShell"}


def test_calee_and_caleeshell_identities_recorded_separately(tmp_path):
    result = verify_release_bundle(_write_bundle(tmp_path))
    calee = result.app("calee")
    shell = result.app("caleeShell")
    assert calee.package_id == "com.viso.calee" and calee.version_name == "founder-v0.3.25"
    assert shell.package_id == "com.viso.caleeshell" and shell.version_name == "founder-v0.2.12"
    assert calee.git_sha != shell.git_sha


def test_optional_caleeshell_omission_is_allowed(tmp_path):
    result = verify_release_bundle(_write_bundle(tmp_path, include_caleeshell=False))
    assert result.ok, result.errors
    assert {a.key for a in result.verified_apps} == {"calee"}


def test_caleeshell_included_false_section_is_allowed(tmp_path):
    def _mark_excluded(m):
        m["caleeShell"] = {"included": False}

    result = verify_release_bundle(_write_bundle(tmp_path, include_caleeshell=False, manifest_overrides=_mark_excluded))
    assert result.ok, result.errors
    assert {a.key for a in result.verified_apps} == {"calee"}


# ── bundle verification: negative cases ──────────────────────────────────


def test_missing_apk_is_rejected(tmp_path):
    bundle = _write_bundle(tmp_path)
    (bundle / "calee.apk").unlink()
    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("missing from the bundle" in e for e in result.errors)


def test_invalid_checksum_is_rejected(tmp_path):
    def _corrupt(m):
        m["calee"]["sha256"] = "0" * 64

    result = verify_release_bundle(_write_bundle(tmp_path, manifest_overrides=_corrupt))
    assert not result.ok
    assert any("SHA-256 mismatch" in e for e in result.errors)


def test_checksums_file_disagreement_is_rejected(tmp_path):
    # Manifest sha is correct, but checksums.sha256 lists a wrong digest.
    result = verify_release_bundle(
        _write_bundle(tmp_path, checksums_override=f"{'0' * 64}  calee.apk\n")
    )
    assert not result.ok
    assert any("checksums.sha256" in e.lower() or "checksum" in e.lower() for e in result.errors)


def test_wrong_package_id_is_rejected(tmp_path):
    def _wrong_pkg(m):
        m["calee"]["packageId"] = "com.evil.calee"

    result = verify_release_bundle(_write_bundle(tmp_path, manifest_overrides=_wrong_pkg))
    assert not result.ok
    assert any("packageId must be" in e for e in result.errors)


def test_malformed_version_is_rejected(tmp_path):
    def _bad_version(m):
        m["calee"]["versionName"] = "latest"

    result = verify_release_bundle(_write_bundle(tmp_path, manifest_overrides=_bad_version))
    assert not result.ok
    assert any("not a recognisable version" in e for e in result.errors)


def test_abbreviated_git_sha_is_rejected(tmp_path):
    def _short_sha(m):
        m["calee"]["gitSha"] = "abc1234"

    result = verify_release_bundle(_write_bundle(tmp_path, manifest_overrides=_short_sha))
    assert not result.ok
    assert any("full 40-character Git SHA" in e for e in result.errors)


def test_non_positive_version_code_is_rejected(tmp_path):
    def _bad_code(m):
        m["calee"]["versionCode"] = 0

    result = verify_release_bundle(_write_bundle(tmp_path, manifest_overrides=_bad_code))
    assert not result.ok
    assert any("versionCode must be a positive integer" in e for e in result.errors)


def test_path_traversal_apk_name_is_rejected(tmp_path):
    def _traversal(m):
        m["calee"]["apk"] = "../evil.apk"

    result = verify_release_bundle(_write_bundle(tmp_path, manifest_overrides=_traversal))
    assert not result.ok
    # Rejected at schema level (not a plain *.apk filename) -- the key point is
    # it never resolves a file outside the bundle root.
    assert any("apk" in e and ("plain" in e or "safe in-bundle" in e) for e in result.errors)


def test_unexpected_executable_file_is_rejected(tmp_path):
    result = verify_release_bundle(_write_bundle(tmp_path, extra_files={"install.sh": "#!/bin/sh\nrm -rf /\n"}))
    assert not result.ok
    assert any("Unexpected file" in e for e in result.errors)


def test_unexpected_archive_file_is_rejected(tmp_path):
    result = verify_release_bundle(_write_bundle(tmp_path, extra_files={"payload.zip": b"PK\x03\x04"}))
    assert not result.ok
    assert any("Unexpected file" in e for e in result.errors)


def test_duplicate_apk_name_is_rejected(tmp_path):
    # Both apps point at the same filename.
    def _dupe(m):
        m["caleeShell"]["apk"] = "calee.apk"
        m["caleeShell"]["sha256"] = m["calee"]["sha256"]

    bundle = _write_bundle(tmp_path, manifest_overrides=_dupe)
    # Remove the now-orphaned caleeshell.apk so only the shared name exists.
    (bundle / "caleeshell.apk").unlink()
    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("Duplicate APK filename" in e for e in result.errors)


def test_missing_manifest_is_rejected(tmp_path):
    bundle = _write_bundle(tmp_path)
    (bundle / "release-manifest.json").unlink()
    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("release-manifest.json not found" in e for e in result.errors)


def test_missing_checksums_file_is_rejected(tmp_path):
    bundle = _write_bundle(tmp_path)
    (bundle / "checksums.sha256").unlink()
    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("checksums.sha256 not found" in e for e in result.errors)


def test_bundle_that_installs_nothing_is_rejected(tmp_path):
    # A manifest with no included app (both absent / not-included) would
    # install nothing -- rejected, even though each present-and-included app
    # is optional individually.
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    (bundle / "release-manifest.json").write_text(
        json.dumps({"releaseId": "2026.07.20-empty", "calee": {"included": False}})
    )
    (bundle / "checksums.sha256").write_text("")
    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("at least one app" in e for e in result.errors)


def test_calee_only_bundle_without_caleeshell_section_verifies(tmp_path):
    # The Calee-only real bundle: caleeShell section entirely absent.
    result = verify_release_bundle(_write_bundle(tmp_path, include_caleeshell=False))
    assert result.ok, result.errors
    assert result.app("calee") is not None and result.app("caleeShell") is None


def test_subdirectory_in_bundle_is_rejected(tmp_path):
    bundle = _write_bundle(tmp_path)
    (bundle / "nested").mkdir()
    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("Unexpected subdirectory" in e for e in result.errors)


# ── manifest parsing (pure) ──────────────────────────────────────────────


def test_parse_manifest_reports_all_errors_at_once():
    manifest, errors = parse_manifest(
        {"releaseId": "", "calee": {"included": True, "packageId": "x", "versionName": "latest",
                                    "versionCode": -1, "gitSha": "short", "apk": "a/b.apk", "sha256": "z"}}
    )
    # Every field problem is reported, not just the first.
    joined = "\n".join(errors)
    assert "releaseId" in joined
    assert "packageId" in joined
    assert "versionName" in joined
    assert "versionCode" in joined
    assert "gitSha" in joined


# ── install-command construction + ordering ──────────────────────────────


def _verified(tmp_path, **kw) -> BundleVerification:
    v = verify_release_bundle(_write_bundle(tmp_path, **kw))
    assert v.ok, v.errors
    return v


def test_refuses_to_build_plan_from_failed_verification(tmp_path):
    bad = verify_release_bundle(tmp_path / "does-not-exist")
    assert not bad.ok
    with pytest.raises(ReleaseInstallerError):
        build_install_plan(bad)


def test_both_app_update_order_installs_calee_first_then_caleeshell(tmp_path):
    plan = build_install_plan(_verified(tmp_path), serial="TAB123")
    labels = [s.label for s in plan.steps]
    assert labels.index("install-calee") < labels.index("install-caleeshell")
    # HOME reassertion, reboot, then verifications -- in that order.
    assert labels.index("install-caleeshell") < labels.index("set-home") < labels.index("reboot")
    assert labels.index("reboot") < labels.index("verify-calee-version")


def test_install_commands_are_data_preserving_and_never_downgrade_by_default(tmp_path):
    plan = build_install_plan(_verified(tmp_path), serial="TAB123")
    install_steps = [s for s in plan.steps if s.label.startswith("install-")]
    for step in install_steps:
        assert step.argv[:2] == ["adb", "-s"]
        assert "install" in step.argv and "-r" in step.argv
        assert "-d" not in step.argv  # no downgrade
        # never a destructive recovery command
        assert "uninstall" not in step.argv and "clear" not in step.argv


def test_allow_downgrade_adds_d_flag_and_a_note(tmp_path):
    plan = build_install_plan(_verified(tmp_path), allow_downgrade=True)
    install_steps = [s for s in plan.steps if s.label.startswith("install-")]
    assert all("-d" in s.argv for s in install_steps)
    assert any("Downgrade explicitly authorised" in n for n in plan.notes)


def test_calee_only_update_order_has_no_caleeshell_or_home_steps(tmp_path):
    plan = build_install_plan(_verified(tmp_path, include_caleeshell=False))
    labels = [s.label for s in plan.steps]
    assert "install-calee" in labels
    assert "install-caleeshell" not in labels
    assert "set-home" not in labels
    assert "verify-home" not in labels
    # still reboots and verifies Calee + its launch action
    assert "reboot" in labels and "verify-calee-version" in labels and "verify-calee-launch" in labels
    assert any("CaleeShell not included" in n for n in plan.notes)


def test_caleeshell_only_update_still_installs_and_reasserts_home(tmp_path):
    # A CaleeShell-only bundle: calee marked not-included, caleeShell included.
    def _shell_only(m):
        m["calee"] = {"included": False}

    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    shell_bytes = b"caleeshell-apk-bytes"
    (bundle / "caleeshell.apk").write_bytes(shell_bytes)
    manifest = {
        "releaseId": "2026.07.20-shellonly",
        "calee": {"included": False},
        "caleeShell": {
            "included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
            "versionCode": 212, "gitSha": SHELL_SHA, "apk": "caleeshell.apk", "sha256": _sha256(shell_bytes),
        },
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{_sha256(shell_bytes)}  caleeshell.apk\n")
    v = verify_release_bundle(bundle)
    assert v.ok, v.errors
    plan = build_install_plan(v)
    labels = [s.label for s in plan.steps]
    assert "install-caleeshell" in labels and "set-home" in labels and "verify-home" in labels
    assert "install-calee" not in labels


def test_first_time_install_uses_the_same_data_preserving_order(tmp_path):
    # First-time install is the same plan (adb install -r is create-or-update);
    # order and data-preservation are identical to an update.
    plan = build_install_plan(_verified(tmp_path))
    labels = [s.label for s in plan.steps]
    assert labels[0] == "install-calee"
    assert labels[1] == "install-caleeshell"


def test_no_serial_omits_the_s_flag(tmp_path):
    plan = build_install_plan(_verified(tmp_path), serial=None)
    for step in plan.steps:
        assert step.argv[0] == "adb"
        assert "-s" not in step.argv


# ── post-install verification parsing ────────────────────────────────────

_DUMPSYS_CALEE = """
Packages:
  Package [com.viso.calee] (abcd):
    versionName=founder-v0.3.25
    versionCode=325 minSdk=26 targetSdk=34
"""


def test_parse_installed_identity_reads_version_and_code():
    ident = parse_installed_identity("com.viso.calee", _DUMPSYS_CALEE)
    assert ident.present is True
    assert ident.version_name == "founder-v0.3.25"
    assert ident.version_code == "325"


def test_parse_installed_identity_absent_package():
    ident = parse_installed_identity("com.viso.calee", "")
    assert ident.present is False
    assert ident.version_name is None


def test_classify_version_match_ok_and_mismatch():
    expected = AppRelease(key="calee", included=True, version_name="founder-v0.3.25", version_code=325)
    good = parse_installed_identity("com.viso.calee", _DUMPSYS_CALEE)
    assert classify_version_match(expected, good) == ri.OUTCOME_OK

    wrong = parse_installed_identity("com.viso.calee", "versionName=founder-v0.3.24\nversionCode=324")
    assert classify_version_match(expected, wrong) == ri.OUTCOME_VERSION_MISMATCH

    absent = parse_installed_identity("com.viso.calee", "")
    assert classify_version_match(expected, absent) == ri.OUTCOME_VERSION_MISMATCH


def test_parse_resolved_package_from_packagename_line():
    out = "priority=0 preferredOrder=0\n  ActivityInfo:\n    packageName=com.viso.caleeshell\n    name=.ui.LauncherActivity"
    assert parse_resolved_package(out) == "com.viso.caleeshell"


def test_parse_resolved_package_from_component_name_line():
    out = "name=com.viso.caleeshell/.ui.LauncherActivity"
    assert parse_resolved_package(out) == "com.viso.caleeshell"


def test_classify_home_resolution_ok_and_mismatch():
    ok = "packageName=com.viso.caleeshell"
    assert classify_home_resolution("com.viso.caleeshell", ok) == ri.OUTCOME_OK
    # HOME still resolves to the stock launcher -> mismatch, must BLOCK.
    stock = "packageName=com.google.android.apps.nexuslauncher"
    assert classify_home_resolution("com.viso.caleeshell", stock) == ri.OUTCOME_HOME_MISMATCH


# ── adb-output classification ────────────────────────────────────────────


def test_classify_success():
    assert classify_install_output(AdbResult(returncode=0, stdout="Success\n")) == ri.OUTCOME_OK


def test_classify_signature_mismatch():
    out = AdbResult(returncode=1, stderr="adb: failed to install: INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match")
    assert classify_install_output(out) == ri.OUTCOME_SIGNATURE_MISMATCH


def test_classify_downgrade_blocked():
    out = AdbResult(returncode=1, stderr="Failure [INSTALL_FAILED_VERSION_DOWNGRADE]")
    assert classify_install_output(out) == ri.OUTCOME_DOWNGRADE_BLOCKED


def test_classify_adb_unavailable_by_returncode():
    assert classify_install_output(AdbResult(returncode=127, stderr="adb executable not found")) == ri.OUTCOME_ADB_UNAVAILABLE


def test_classify_device_unavailable():
    out = AdbResult(returncode=1, stderr="error: no devices/emulators found")
    assert classify_install_output(out) == ri.OUTCOME_DEVICE_UNAVAILABLE


def test_classify_generic_install_failure():
    out = AdbResult(returncode=1, stderr="Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]")
    assert classify_install_output(out) == ri.OUTCOME_INSTALL_FAILED


# ── downgrade decision (pure) ────────────────────────────────────────────


def test_decide_downgrade_blocks_lower_target():
    assert decide_downgrade(325, 324, allow_downgrade=False) == ri.OUTCOME_DOWNGRADE_BLOCKED


def test_decide_downgrade_allows_when_authorised():
    assert decide_downgrade(325, 324, allow_downgrade=True) == ri.OUTCOME_OK


def test_decide_downgrade_same_or_higher_is_ok():
    assert decide_downgrade(325, 325, allow_downgrade=False) == ri.OUTCOME_OK
    assert decide_downgrade(325, 326, allow_downgrade=False) == ri.OUTCOME_OK


def test_decide_downgrade_unknown_current_is_not_a_downgrade():
    assert decide_downgrade(None, 100, allow_downgrade=False) == ri.OUTCOME_OK


# ── execute_install_plan / inspect_tablet (injected fake adb runner) ──────


class FakeAdb:
    """A scriptable adb runner keyed by a substring of the argv, so tests can
    say 'when the command contains install, return this'."""

    def __init__(self, rules, default=None):
        self.rules = rules  # list of (predicate(argv)->bool, AdbResult)
        self.default = default or AdbResult(returncode=0, stdout="Success\n")
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        for pred, res in self.rules:
            if pred(argv):
                return res
        return self.default


def _contains(*tokens):
    return lambda argv: all(t in argv for t in tokens)


def _healthy_device_rules(calee_code=325, shell_code=212):
    """A fake device where every install succeeds and every verify reports the
    expected identities/HOME."""
    return [
        (_contains("install"), AdbResult(0, "Success\n")),
        (_contains("dumpsys", "package", "com.viso.calee"),
         AdbResult(0, f"versionName=founder-v0.3.25\nversionCode={calee_code}")),
        (_contains("dumpsys", "package", "com.viso.caleeshell"),
         AdbResult(0, f"versionName=founder-v0.2.12\nversionCode={shell_code}")),
        (_contains("resolve-activity", "-c", "android.intent.category.HOME"),
         AdbResult(0, "packageName=com.viso.caleeshell")),
        (_contains("resolve-activity", "-a", "com.viso.calee.action.START"),
         AdbResult(0, "packageName=com.viso.calee")),
        (_contains("wait-for-device"), AdbResult(0, "")),
    ]


def test_execute_install_plan_happy_path_is_ok(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v, serial="TAB1")
    adb = FakeAdb(_healthy_device_rules())
    execution = ri.execute_install_plan(plan, v, adb)
    assert execution.status == ri.STATUS_OK, execution.detail
    # every step ran (nothing halted early)
    assert len(execution.steps) == len(plan.steps)
    assert all(s.outcome == ri.OUTCOME_OK for s in execution.steps)
    # installed identities were parsed and recorded
    assert {i.package_id for i in execution.installed} == {"com.viso.calee", "com.viso.caleeshell"}


def test_execute_install_plan_device_unavailable_blocks_on_first_step(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v)
    adb = FakeAdb([], default=AdbResult(1, "", "error: no devices/emulators found"))
    execution = ri.execute_install_plan(plan, v, adb)
    assert execution.status == ri.STATUS_BLOCKED
    assert execution.steps[0].outcome == ri.OUTCOME_DEVICE_UNAVAILABLE
    # halted immediately -- did not attempt the rest of the plan
    assert len(execution.steps) == 1


def test_execute_install_plan_signature_mismatch_blocks_and_never_uninstalls(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v)
    rules = [(_contains("install"), AdbResult(1, "", "INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match"))]
    adb = FakeAdb(rules)
    execution = ri.execute_install_plan(plan, v, adb)
    assert execution.status == ri.STATUS_BLOCKED
    assert execution.steps[-1].outcome == ri.OUTCOME_SIGNATURE_MISMATCH
    # crucial: no uninstall/clear command was ever issued
    assert not any("uninstall" in c or "clear" in c for c in adb.calls)


def test_execute_install_plan_version_mismatch_after_install_blocks(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v)
    rules = _healthy_device_rules(calee_code=999)  # device reports wrong code
    adb = FakeAdb(rules)
    execution = ri.execute_install_plan(plan, v, adb)
    assert execution.status == ri.STATUS_BLOCKED
    assert any(s.outcome == ri.OUTCOME_VERSION_MISMATCH for s in execution.steps)


def test_execute_install_plan_home_mismatch_blocks(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v)
    rules = _healthy_device_rules()
    # override HOME resolution to the stock launcher
    rules = [(_contains("resolve-activity", "-c", "android.intent.category.HOME"),
              AdbResult(0, "packageName=com.google.android.apps.nexuslauncher"))] + rules
    adb = FakeAdb(rules)
    execution = ri.execute_install_plan(plan, v, adb)
    assert execution.status == ri.STATUS_BLOCKED
    assert any(s.outcome == ri.OUTCOME_HOME_MISMATCH for s in execution.steps)


def test_inspect_tablet_no_device_is_blocked():
    adb = FakeAdb([], default=AdbResult(1, "", "error: no devices/emulators found"))
    inspection = ri.inspect_tablet(adb, serial="TAB1")
    assert inspection.status == ri.STATUS_BLOCKED
    assert inspection.adb_available is True
    assert inspection.device_present is False


def test_inspect_tablet_adb_unavailable_is_blocked():
    adb = FakeAdb([], default=AdbResult(127, "", "adb executable not found"))
    inspection = ri.inspect_tablet(adb)
    assert inspection.status == ri.STATUS_BLOCKED
    assert inspection.adb_available is False


def test_inspect_tablet_healthy_reports_identities_and_home():
    rules = [
        (_contains("get-state"), AdbResult(0, "device\n")),
        (_contains("dumpsys", "package", "com.viso.calee"), AdbResult(0, "versionName=founder-v0.3.25\nversionCode=325")),
        (_contains("dumpsys", "package", "com.viso.caleeshell"), AdbResult(0, "versionName=founder-v0.2.12\nversionCode=212")),
        (_contains("resolve-activity", "-c", "android.intent.category.HOME"), AdbResult(0, "packageName=com.viso.caleeshell")),
    ]
    inspection = ri.inspect_tablet(FakeAdb(rules), serial="TAB1")
    assert inspection.status == ri.STATUS_OK
    assert inspection.device_present is True
    assert inspection.home_package == "com.viso.caleeshell"
    codes = {i.package_id: i.version_code for i in inspection.installed}
    assert codes["com.viso.calee"] == "325"
