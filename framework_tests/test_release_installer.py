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


def test_manifest_with_no_schema_version_defaults_to_v1():
    manifest, errors = parse_manifest({
        "releaseId": "r1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": "0" * 64},
    })
    assert errors == []
    assert manifest.schema_version == 1
    assert manifest.is_schema_v2 is False
    assert manifest.profile is None and manifest.platforms is None


# ── schema version 2 (Priority 2: canonical release manifest) ────────────


def _v2_manifest(**overrides):
    base = {
        "schemaVersion": 2,
        "releaseId": "2026.07.20-rc3",
        "profile": "staging",
        "backend": "https://hub-dev.calee.com.au",
        "platforms": {"tablet": True, "mobileAndroid": False, "mobileIos": True},
        "features": {
            "synchronization": True, "meals": True, "onboarding": True,
            "googleCalendar": True, "kioskAdmin": True, "notifications": True,
        },
        "tabletSolution": {
            "calee": {
                "installArtifact": True, "apk": "calee.apk", "sha256": "0" * 64,
                "expectedInstalled": {
                    "packageId": "com.viso.calee", "versionName": "founder-v0.3.26",
                    "versionCode": 326, "gitSha": CALEE_SHA, "signerSha256": "1" * 64,
                },
            },
            "caleeShell": {
                "installArtifact": False,
                "expectedInstalled": {
                    "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                    "versionCode": 212, "gitSha": SHELL_SHA, "signerSha256": "2" * 64,
                },
            },
        },
        "caleeMobile": {
            "version": "0.0.24+24", "gitSha": "c" * 40,
            "selectorEvidenceRequired": True, "distributedBuildAcceptanceRequired": True,
        },
    }
    base.update(overrides)
    return base


def test_v2_manifest_parses_authoritative_scope_and_identity():
    manifest, errors = parse_manifest(_v2_manifest())
    assert errors == [], errors
    assert manifest.schema_version == 2
    assert manifest.is_schema_v2 is True
    assert manifest.release_id == "2026.07.20-rc3"
    assert manifest.profile == "staging"
    assert manifest.backend == "https://hub-dev.calee.com.au"
    assert manifest.platforms.tablet is True and manifest.platforms.mobile_android is False
    assert manifest.features.kiosk_admin is True and manifest.features.notifications is True
    assert manifest.calee.package_id == "com.viso.calee" and manifest.calee.version_code == 326
    assert manifest.caleeshell.install_artifact is False and manifest.caleeshell.version_name == "founder-v0.2.12"
    assert manifest.calee_mobile.version == "0.0.24+24" and manifest.calee_mobile.git_sha == "c" * 40


def test_v2_manifest_missing_tablet_solution_is_rejected():
    raw = _v2_manifest()
    del raw["tabletSolution"]
    manifest, errors = parse_manifest(raw)
    assert any("tabletSolution" in e for e in errors)


def test_v2_manifest_missing_profile_backend_platforms_features_caleemobile_is_rejected():
    raw = _v2_manifest()
    del raw["profile"]
    del raw["backend"]
    del raw["platforms"]
    del raw["features"]
    del raw["caleeMobile"]
    manifest, errors = parse_manifest(raw)
    joined = "\n".join(errors)
    assert "manifest.profile is required" in joined
    assert "manifest.backend is required" in joined
    assert "manifest.platforms is required" in joined
    assert "manifest.features is required" in joined
    assert "manifest.caleeMobile is required" in joined


def test_v2_manifest_invalid_profile_value_is_rejected():
    manifest, errors = parse_manifest(_v2_manifest(profile="prod"))
    assert any("manifest.profile" in e for e in errors)


def test_v2_manifest_abbreviated_caleemobile_sha_is_rejected():
    raw = _v2_manifest()
    raw["caleeMobile"]["gitSha"] = "short"
    manifest, errors = parse_manifest(raw)
    assert any("caleeMobile.gitSha" in e for e in errors)


def test_v2_manifest_malformed_caleemobile_version_is_rejected():
    raw = _v2_manifest()
    raw["caleeMobile"]["version"] = "latest"
    manifest, errors = parse_manifest(raw)
    assert any("caleeMobile.version" in e for e in errors)


def test_unsupported_schema_version_is_rejected_up_front():
    manifest, errors = parse_manifest({"schemaVersion": 3, "releaseId": "r1"})
    assert len(errors) == 1
    assert "schemaVersion" in errors[0] and "not supported" in errors[0]
    # No partial parse is attempted once the schema version itself is unknown.
    assert manifest.calee is None and manifest.release_id is None


def test_non_integer_schema_version_is_rejected():
    manifest, errors = parse_manifest({"schemaVersion": "2", "releaseId": "r1"})
    assert any("schemaVersion" in e for e in errors)


def test_v2_manifest_verifies_as_a_full_bundle(tmp_path):
    bundle = tmp_path / "v2-bundle"
    bundle.mkdir()
    calee_bytes = b"calee-v2-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    manifest = _v2_manifest()
    manifest["tabletSolution"]["calee"]["sha256"] = _sha256(calee_bytes)
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{_sha256(calee_bytes)}  calee.apk\n")
    result = verify_release_bundle(bundle)
    assert result.ok, result.errors
    assert result.manifest.is_schema_v2
    assert result.manifest.profile == "staging"
    assert {a.key for a in result.verified_apps} == {"calee"}


# ── schema version 2: adversarial strictness (Priority 1) ────────────────
#
# Every case below proves a defect is caught by parse_manifest() itself --
# i.e. before verify_release_bundle/install-tablet-release ever reach a real
# ADB command -- never merely by a later, post-install check.


_V2_BOOL_FIELD_CASES = [
    # (path description, mutator) -- each sets one JSON-boolean field to a
    # non-boolean value and asserts parse_manifest rejects it.
    ("tabletSolution.calee.installArtifact", lambda m: m["tabletSolution"]["calee"].__setitem__("installArtifact", "false")),
    ("tabletSolution.caleeShell.installArtifact", lambda m: m["tabletSolution"]["caleeShell"].__setitem__("installArtifact", "true")),
    ("platforms.tablet", lambda m: m["platforms"].__setitem__("tablet", "false")),
    ("platforms.mobileAndroid", lambda m: m["platforms"].__setitem__("mobileAndroid", 0)),
    ("platforms.mobileIos", lambda m: m["platforms"].__setitem__("mobileIos", None)),
    ("features.synchronization", lambda m: m["features"].__setitem__("synchronization", "false")),
    ("features.meals", lambda m: m["features"].__setitem__("meals", 1)),
    ("features.onboarding", lambda m: m["features"].__setitem__("onboarding", [])),
    ("features.googleCalendar", lambda m: m["features"].__setitem__("googleCalendar", "true")),
    ("features.kioskAdmin", lambda m: m["features"].__setitem__("kioskAdmin", None)),
    ("features.notifications", lambda m: m["features"].__setitem__("notifications", "no")),
    ("caleeMobile.selectorEvidenceRequired", lambda m: m["caleeMobile"].__setitem__("selectorEvidenceRequired", "false")),
    ("caleeMobile.distributedBuildAcceptanceRequired", lambda m: m["caleeMobile"].__setitem__("distributedBuildAcceptanceRequired", "true")),
]


@pytest.mark.parametrize("path,mutate", _V2_BOOL_FIELD_CASES, ids=[c[0] for c in _V2_BOOL_FIELD_CASES])
def test_v2_manifest_rejects_non_boolean_json_types(path, mutate):
    # A string "false" must never be truthy-coerced into True -- an explicit
    # exclusion inverted into an inclusion is exactly the defect this guards.
    raw = _v2_manifest()
    mutate(raw)
    manifest, errors = parse_manifest(raw)
    assert errors, f"{path} with a non-boolean value must be rejected"
    assert any("must be a JSON boolean" in e for e in errors), errors


@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, None, [], {}])
def test_v2_manifest_install_artifact_non_boolean_is_never_truthy_coerced(bad_value):
    # A manifest author's explicit "installArtifact": "false" (a truthy
    # Python string) must never be silently inverted into an install.
    raw = _v2_manifest()
    raw["tabletSolution"]["calee"]["installArtifact"] = bad_value
    manifest, errors = parse_manifest(raw)
    assert any("installArtifact" in e and "JSON boolean" in e for e in errors), errors


def test_v2_manifest_missing_caleeshell_section_is_rejected():
    raw = _v2_manifest()
    del raw["tabletSolution"]["caleeShell"]
    manifest, errors = parse_manifest(raw)
    assert any("tabletSolution.caleeShell is required" in e for e in errors), errors


def test_v2_manifest_missing_calee_section_is_rejected():
    raw = _v2_manifest()
    del raw["tabletSolution"]["calee"]
    manifest, errors = parse_manifest(raw)
    assert any("tabletSolution.calee is required" in e for e in errors), errors


def test_v2_manifest_calee_missing_signer_sha256_is_rejected():
    raw = _v2_manifest()
    del raw["tabletSolution"]["calee"]["expectedInstalled"]["signerSha256"]
    manifest, errors = parse_manifest(raw)
    assert any("calee expected signerSha256 is required" in e for e in errors), errors


def test_v2_manifest_caleeshell_missing_signer_sha256_is_rejected():
    raw = _v2_manifest()
    del raw["tabletSolution"]["caleeShell"]["expectedInstalled"]["signerSha256"]
    manifest, errors = parse_manifest(raw)
    assert any("caleeShell expected signerSha256 is required" in e for e in errors), errors


def test_v2_manifest_calee_missing_expected_installed_block_is_rejected():
    raw = _v2_manifest()
    del raw["tabletSolution"]["calee"]["expectedInstalled"]
    manifest, errors = parse_manifest(raw)
    assert any("calee.expectedInstalled is required" in e for e in errors), errors


def test_v2_manifest_caleeshell_missing_expected_installed_block_is_rejected():
    # Even though caleeShell is unchanged (installArtifact: false) this
    # release, its expected identity is still mandatory (Priority 1).
    raw = _v2_manifest()
    del raw["tabletSolution"]["caleeShell"]["expectedInstalled"]
    manifest, errors = parse_manifest(raw)
    assert any("caleeShell.expectedInstalled is required" in e for e in errors), errors


def test_v2_manifest_caleemobile_missing_version_only_is_rejected():
    raw = _v2_manifest()
    del raw["caleeMobile"]["version"]
    manifest, errors = parse_manifest(raw)
    assert any("caleeMobile.version is required" in e for e in errors), errors


def test_v2_manifest_caleemobile_missing_gitsha_only_is_rejected():
    raw = _v2_manifest()
    del raw["caleeMobile"]["gitSha"]
    manifest, errors = parse_manifest(raw)
    assert any("caleeMobile.gitSha is required" in e for e in errors), errors


@pytest.mark.parametrize(
    "bad_backend",
    [
        "not-a-url-at-all", "http://hub-dev.calee.com.au", "ftp://hub-dev.calee.com.au", "  ",
        # Priority 7 (this session): structured validation catches deceptive/
        # malformed URLs a bare startswith("https://") test would have missed.
        "https://user:pass@hub-dev.calee.com.au",
        "https://hub-dev.calee.com.au@evil.example",
        "https://hub-dev.calee.com.au#fragment",
        "https://hub-dev.calee.com.au:99999",
        "https:///no-host-at-all",
        " https://hub-dev.calee.com.au",
        "https://hub-dev.calee.com.au/\x00",
    ],
)
def test_v2_manifest_non_https_backend_is_rejected(bad_backend):
    raw = _v2_manifest(backend=bad_backend)
    manifest, errors = parse_manifest(raw)
    assert any("backend" in e for e in errors), errors


def test_v2_manifest_https_backend_with_port_and_path_is_accepted():
    raw = _v2_manifest(backend="https://hub-dev.calee.com.au:8443/api")
    manifest, errors = parse_manifest(raw)
    assert errors == [], errors
    assert manifest.backend == "https://hub-dev.calee.com.au:8443/api"


def test_v2_manifest_unknown_top_level_key_is_rejected():
    raw = _v2_manifest()
    raw["extraUnknownTopLevelKey"] = "surprise"
    manifest, errors = parse_manifest(raw)
    assert any("unexpected key" in e and "extraUnknownTopLevelKey" in e for e in errors), errors


def test_v2_manifest_unknown_tablet_solution_key_is_rejected():
    raw = _v2_manifest()
    raw["tabletSolution"]["extraApp"] = {}
    manifest, errors = parse_manifest(raw)
    assert any("tabletSolution has unexpected key" in e for e in errors), errors


def test_v2_manifest_misspelled_signer_sha_key_is_rejected_as_unknown():
    # A typo'd key ("signerSha246") must not silently produce
    # signerSha256=None -- it must be flagged as an unrecognised key AND the
    # real signerSha256 must be reported missing.
    raw = _v2_manifest()
    exp = raw["tabletSolution"]["calee"]["expectedInstalled"]
    exp["signerSha246"] = exp.pop("signerSha256")
    manifest, errors = parse_manifest(raw)
    joined = "\n".join(errors)
    assert "unexpected key" in joined and "signerSha246" in joined
    assert "calee expected signerSha256 is required" in joined


def test_v2_manifest_unknown_platforms_key_is_rejected():
    raw = _v2_manifest()
    raw["platforms"]["desktop"] = True
    manifest, errors = parse_manifest(raw)
    assert any("platforms has unexpected key" in e for e in errors), errors


def test_v2_manifest_unknown_features_key_is_rejected():
    raw = _v2_manifest()
    raw["features"]["darkMode"] = True
    manifest, errors = parse_manifest(raw)
    assert any("features has unexpected key" in e for e in errors), errors


def test_v2_manifest_unknown_caleemobile_key_is_rejected():
    raw = _v2_manifest()
    raw["caleeMobile"]["extraFlag"] = True
    manifest, errors = parse_manifest(raw)
    assert any("caleeMobile has unexpected key" in e for e in errors), errors


def test_v2_manifest_provenance_metadata_is_allowed():
    # Optional assembly-time provenance metadata (Priority 4) is not an
    # "unexpected key" -- confirm it doesn't trip the strict top-level check.
    raw = _v2_manifest(provenance={"repository": "CaleeAdmin/Calee", "sourceCommit": "a" * 40})
    manifest, errors = parse_manifest(raw)
    assert errors == [], errors


def test_v2_manifest_all_defects_reported_together():
    # Multiple independent schema-v2 defects at once are all surfaced in a
    # single parse -- a manifest author does not have to fix-and-resubmit
    # one error at a time.
    raw = _v2_manifest()
    raw["tabletSolution"]["calee"]["installArtifact"] = "false"
    del raw["tabletSolution"]["caleeShell"]["expectedInstalled"]["signerSha256"]
    raw["backend"] = "not-a-url"
    raw["extraUnknownTopLevelKey"] = True
    del raw["caleeMobile"]["gitSha"]
    manifest, errors = parse_manifest(raw)
    joined = "\n".join(errors)
    assert "installArtifact" in joined and "JSON boolean" in joined
    assert "caleeShell expected signerSha256 is required" in joined
    assert "backend" in joined
    assert "unexpected key" in joined and "extraUnknownTopLevelKey" in joined
    assert "caleeMobile.gitSha is required" in joined


def test_v2_manifest_defects_are_all_caught_before_bundle_verification_ok(tmp_path):
    # End-to-end proof for Priority 1 requirement 9: a schema-v2 defect makes
    # verify_release_bundle() fail -- which is what gates every downstream
    # ADB-issuing CLI command (install-tablet-release exits before ever
    # calling adb when verification fails). This is the ordering guarantee.
    bundle = tmp_path / "v2-bundle-defective"
    bundle.mkdir()
    calee_bytes = b"calee-v2-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    manifest = _v2_manifest()
    manifest["tabletSolution"]["calee"]["sha256"] = _sha256(calee_bytes)
    del manifest["tabletSolution"]["caleeShell"]["expectedInstalled"]["signerSha256"]
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{_sha256(calee_bytes)}  calee.apk\n")
    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("caleeShell expected signerSha256 is required" in e for e in result.errors)


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


# ── Priority 1: install commands use the verified ABSOLUTE APK path ───────


def test_verified_apps_carry_absolute_apk_paths_inside_the_bundle_root(tmp_path):
    v = _verified(tmp_path)
    from pathlib import Path

    root = Path(v.bundle_root)
    for app in v.verified_apps:
        assert app.apk_path is not None, f"{app.key} has no resolved apk_path"
        p = Path(app.apk_path)
        assert p.is_absolute(), f"{app.key} apk_path is not absolute: {app.apk_path}"
        assert p.is_file(), f"{app.key} apk_path does not point at a real file"
        # Stays inside the verified bundle root.
        assert p == root / app.apk or p.parent == root


def test_install_command_uses_absolute_path_not_bare_filename(tmp_path):
    plan = build_install_plan(_verified(tmp_path), serial="TAB1")
    from pathlib import Path

    for step in plan.steps:
        if step.label.startswith("install-"):
            apk_arg = step.argv[-1]
            assert Path(apk_arg).is_absolute(), f"install arg is not absolute: {apk_arg!r}"
            assert apk_arg.endswith(".apk")
            # The bare filename must never be what adb is handed.
            assert apk_arg not in ("calee.apk", "caleeshell.apk")


def test_install_works_when_cwd_differs_from_bundle_dir(tmp_path, monkeypatch):
    """The whole point of Priority 1: the bundle lives OUTSIDE the repo (e.g.
    ~/Calee-Releases/current) and the launcher's cwd is the repo, so a bare
    `adb install -r calee.apk` would fail. Verify + plan + execute from a
    completely unrelated working directory, and prove adb was handed a path
    that exists regardless of cwd."""
    bundle = _write_bundle(tmp_path)
    v = verify_release_bundle(bundle)
    assert v.ok, v.errors
    plan = build_install_plan(v, serial="TAB1")

    # Move to an unrelated cwd -- the bundle is not reachable by relative name.
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    from pathlib import Path

    for step in plan.steps:
        if step.label.startswith("install-"):
            apk_arg = step.argv[-1]
            # Absolute AND resolvable from this unrelated cwd.
            assert Path(apk_arg).is_absolute()
            assert Path(apk_arg).is_file(), f"{apk_arg} not resolvable from cwd {elsewhere}"

    # And a full execute (with a fake adb) succeeds from the foreign cwd.
    adb = FakeAdb(_healthy_device_rules())
    execution = ri.execute_install_plan(plan, v, adb)
    assert execution.status == ri.STATUS_OK, execution.detail
    # adb was literally invoked with the absolute path.
    install_calls = [c for c in adb.calls if "install" in c]
    assert install_calls, "no install command was issued"
    for call in install_calls:
        assert Path(call[-1]).is_absolute()


def test_symlink_apk_escaping_the_bundle_is_rejected(tmp_path):
    """Path traversal remains impossible even via a symlink: the manifest names
    a plain `calee.apk` (passes the filename check), but that entry is a symlink
    whose target is OUTSIDE the bundle. Resolution + containment reject it, so no
    install command can ever point outside the verified root."""
    import os
    import platform

    if platform.system() == "Windows":  # pragma: no cover - symlink perms differ
        pytest.skip("symlink semantics differ on Windows")

    # A real file outside the bundle we must never resolve to.
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = outside / "evil-real.apk"
    evil.write_bytes(b"malicious")

    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    # calee.apk is a SYMLINK to the outside file.
    link = bundle / "calee.apk"
    os.symlink(evil, link)
    calee_sha = _sha256(b"malicious")
    manifest = {
        "releaseId": "2026.07.20-evil",
        "calee": {
            "included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
            "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": calee_sha,
        },
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{calee_sha}  calee.apk\n")

    result = verify_release_bundle(bundle)
    assert not result.ok
    assert any("OUTSIDE the verified bundle root" in e for e in result.errors), result.errors


def test_build_install_command_refuses_app_without_verified_path():
    """Belt-and-suspenders: an AppRelease that never went through verification
    (so apk_path is None) can never yield an install command."""
    unverified = AppRelease(key="calee", included=True, package_id="com.viso.calee", apk="calee.apk")
    with pytest.raises(ReleaseInstallerError, match="verified absolute APK path"):
        ri.build_install_command(unverified, serial=None, allow_downgrade=False)


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



def test_execute_install_plan_reboot_zero_without_success_text_is_ok(tmp_path):
    """adb reboot normally succeeds silently; it is not an APK install."""

    v = _verified(tmp_path)
    plan = build_install_plan(v, serial="TAB1")

    rules = [
        (_contains("reboot"), AdbResult(returncode=0, stdout="", stderr="")),
    ] + _healthy_device_rules()

    execution = ri.execute_install_plan(plan, v, FakeAdb(rules))

    reboot = next(step for step in execution.steps if step.label == "reboot")

    assert reboot.returncode == 0
    assert reboot.outcome == ri.OUTCOME_OK
    assert execution.status == ri.STATUS_OK


def test_step_evidence_preserves_raw_install_diagnostics_and_stable_outcome(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v, serial="TAB1")
    message = "INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match"
    execution = ri.execute_install_plan(plan, v, FakeAdb([(_contains("install"), AdbResult(1, "", message))]))

    step = execution.steps[0]
    payload = step.to_dict()
    assert step.outcome == ri.OUTCOME_SIGNATURE_MISMATCH
    assert payload["stderr"] == message
    assert payload["detail"] != message  # generic release decision remains separate
    assert payload["commandKind"] == "apk_install"
    assert payload["serial"] == "TAB1"
    assert payload["startedAt"] and payload["completedAt"]
    assert payload["durationSeconds"] >= 0


def test_step_evidence_redacts_and_bounds_unusual_adb_output(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v)
    output = "start api_key=super-secret " + ("x" * (ri.DIAGNOSTIC_OUTPUT_LIMIT + 100)) + " end"
    execution = ri.execute_install_plan(plan, v, FakeAdb([(_contains("install"), AdbResult(1, output, ""))]))

    payload = execution.steps[0].to_dict()
    assert "super-secret" not in payload["stdout"]
    assert payload["stdout"].startswith("start api_key=[REDACTED]")
    assert payload["stdout"].endswith(" end")
    assert payload["truncation"]["stdoutTruncated"] is True
    assert len(payload["stdout"]) <= ri.DIAGNOSTIC_OUTPUT_LIMIT


def test_step_evidence_marks_timeout_and_command_specific_kinds(tmp_path):
    v = _verified(tmp_path)
    plan = build_install_plan(v)
    execution = ri.execute_install_plan(plan, v, FakeAdb([(_contains("install"), AdbResult(124, "", "timed out"))]))
    step = execution.steps[0]
    assert step.outcome == ri.OUTCOME_TIMEOUT
    assert step.timed_out is True
    assert step.command_kind == "apk_install"

    kinds = {step.label: ri.command_kind_for_step(step) for step in plan.steps}
    assert kinds["reboot"] == "reboot"
    assert kinds["wait-for-device"] == "wait_for_device"
    assert kinds["set-home"] == "home_assignment"
    assert kinds["verify-calee-version"] == "version_inspection"
    assert kinds["verify-home"] == "home_resolution"
    assert kinds["verify-calee-launch"] == "launch_resolution"
