"""Release-candidate fingerprint + immutable snapshot (Priority 4).

Closes the time-of-check/time-of-use (TOCTOU) gap between ``release-config``
composing its evidence from an already-verified release bundle and
``install-tablet-release``'s first mutating ADB command. Both commands used
to independently call ``release_installer.verify_release_bundle()`` against
the SAME mutable drop folder, with nothing carried between them -- a bundle
whose bytes changed after approval (a different manifest, a corrupted
checksum, a re-signed APK, a re-pointed symlink) would simply be re-verified
against its NEW content and installed, with no proof the tester saw the same
bytes.

The fix: once release-config verifies a bundle, its manifest, checksums
file, and every verified app's ACTUAL apk bytes (the resolved, contained-in-
root path -- never the manifest-declared filename) are copied into a run-
scoped, immutable snapshot (``reports/runs/<run-id>/release-candidate/``),
and a content-addressed fingerprint of that snapshot is recorded.
``install-tablet-release`` then installs ONLY from that snapshot -- refusing
outright to fall back to the original, still-mutable drop folder once a
same-run snapshot exists -- and re-verifies the snapshot's CURRENT bytes
against the recorded fingerprint immediately before building the install
plan, so tampering after the snapshot was taken is caught too.

Mirrors ``selector_provenance.py``'s raw-byte digest + envelope-digest
pattern, already proven and tamper-tested in this codebase for a different
(selector-contract) evidence stream, and ``release_bundle_assembly.py``'s
temp-dir + atomic-replace pattern for the snapshot write itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FINGERPRINT_FILENAME = "release-candidate-fingerprint.json"
MANIFEST_NAME = "release-manifest.json"
CHECKSUMS_NAME = "checksums.sha256"


class CandidateFingerprintError(Exception):
    """The release-candidate snapshot or its fingerprint is missing,
    unreadable, or malformed -- a framework/pipeline fault, never silently
    treated as "nothing to check"."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


@dataclass
class CandidateFingerprint:
    """A content-addressed fingerprint of one run's approved release
    candidate: the resolved snapshot root, the release ID + schema version it
    was approved for, and a raw-byte SHA-256 for the manifest, the checksums
    file, and every included app's APK."""

    bundle_root: "str | None" = None
    release_id: "str | None" = None
    schema_version: "int | None" = None
    manifest_sha256: "str | None" = None
    checksums_sha256: "str | None" = None
    # app key ("calee"/"caleeShell") -> {"filename": ..., "sha256": ...}
    apk_sha256: "dict[str, dict[str, str]]" = field(default_factory=dict)
    envelope_digest: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "bundleRoot": self.bundle_root,
            "releaseId": self.release_id,
            "schemaVersion": self.schema_version,
            "manifestSha256": self.manifest_sha256,
            "checksumsSha256": self.checksums_sha256,
            "apkSha256": {k: dict(v) for k, v in self.apk_sha256.items()},
            "envelopeDigest": self.envelope_digest,
        }


def _envelope_digest(fp: CandidateFingerprint) -> str:
    """A digest over every OTHER field, so the fingerprint record itself
    cannot be edited (e.g. a problem file's digest silently replaced) without
    also invalidating this envelope -- mirrors selector_provenance.py's
    envelope_digest construction."""
    payload = json.dumps(
        {
            "releaseId": fp.release_id,
            "schemaVersion": fp.schema_version,
            "manifestSha256": fp.manifest_sha256,
            "checksumsSha256": fp.checksums_sha256,
            "apkSha256": {k: dict(sorted(v.items())) for k, v in sorted(fp.apk_sha256.items())},
        },
        sort_keys=True,
    )
    return _sha256_bytes(payload.encode("utf-8"))


def snapshot_release_candidate(
    verification: "Any",
    snapshot_dir: "Path | str",
    *,
    release_id: "str | None",
    schema_version: "int | None",
) -> CandidateFingerprint:
    """Copy a verified release bundle's manifest, checksums file, and every
    verified app's ACTUAL apk bytes into ``snapshot_dir``, and return a
    fingerprint of the copy.

    ``verification`` is a ``release_installer.BundleVerification`` that
    already passed (``verification.ok``); its ``verified_apps`` carry the
    RESOLVED absolute ``apk_path`` for each included app -- the same trusted
    path the installer itself uses, never the manifest-declared filename.

    Every source file's bytes are hashed both immediately BEFORE and AFTER
    the copy; a mismatch (the source changed mid-copy) raises rather than
    silently snapshotting a torn read. The whole snapshot is built into a
    fresh temp sibling directory and atomically swapped into place on
    success, so a failed/partial snapshot never leaves a stale or half-
    written candidate at ``snapshot_dir`` (mirrors release_bundle_assembly.
    write_release_bundle's atomic-replace pattern) -- and a pre-existing
    (e.g. run-workspace-initialised empty) directory at that path is safely
    replaced, never merged into.
    """
    bundle_root = Path(verification.bundle_root)
    manifest_src = bundle_root / MANIFEST_NAME
    checksums_src = bundle_root / CHECKSUMS_NAME
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(dir=snapshot_dir.parent, prefix=f".{snapshot_dir.name}.tmp-"))
    try:
        def _copy_hashed(src: Path, dst: Path) -> str:
            if not src.is_file():
                raise CandidateFingerprintError(f"{src} is missing -- cannot snapshot the release candidate.")
            before = _sha256_file(src)
            data = src.read_bytes()
            dst.write_bytes(data)
            after = _sha256_bytes(data)
            reread = _sha256_file(dst)
            if before != after or before != reread:
                raise CandidateFingerprintError(
                    f"{src} changed while being snapshotted -- refusing to trust a torn read."
                )
            return after

        manifest_sha256 = _copy_hashed(manifest_src, tmp_dir / MANIFEST_NAME)
        checksums_sha256 = _copy_hashed(checksums_src, tmp_dir / CHECKSUMS_NAME)

        apk_sha256: "dict[str, dict[str, str]]" = {}
        for app in verification.verified_apps:
            if not app.apk_path:
                continue
            apk_src = Path(app.apk_path)
            apk_dst = tmp_dir / apk_src.name
            apk_sha256[app.key] = {"filename": apk_src.name, "sha256": _copy_hashed(apk_src, apk_dst)}

        fingerprint = CandidateFingerprint(
            bundle_root=str(snapshot_dir),
            release_id=release_id,
            schema_version=schema_version,
            manifest_sha256=manifest_sha256,
            checksums_sha256=checksums_sha256,
            apk_sha256=apk_sha256,
        )
        fingerprint.envelope_digest = _envelope_digest(fingerprint)
        (tmp_dir / FINGERPRINT_FILENAME).write_text(
            json.dumps(fingerprint.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

        _atomic_replace_dir(tmp_dir, snapshot_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return fingerprint


def _atomic_replace_dir(tmp_dir: Path, dest_dir: Path) -> None:
    """Atomically make ``tmp_dir`` become ``dest_dir``. POSIX ``rename``
    cannot replace a non-empty existing directory in one call, so an existing
    ``dest_dir`` is first renamed aside, ``tmp_dir`` is renamed into place,
    and only then is the old directory removed -- a failure partway through
    the swap still leaves a valid directory at ``dest_dir`` (either the old
    one, restored, or the new one)."""
    backup_dir = None
    if dest_dir.exists():
        backup_dir = dest_dir.with_name(f".{dest_dir.name}.bak-{os.getpid()}")
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        dest_dir.rename(backup_dir)
    try:
        tmp_dir.rename(dest_dir)
    except Exception:
        if backup_dir is not None:
            backup_dir.rename(dest_dir)
        raise
    if backup_dir is not None:
        shutil.rmtree(backup_dir, ignore_errors=True)


def load_candidate_fingerprint(path: "Path | str") -> CandidateFingerprint:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CandidateFingerprintError(
            f"could not read release-candidate fingerprint at {path}: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CandidateFingerprintError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CandidateFingerprintError(f"{path} must contain a JSON object.")
    apk_sha256_raw = data.get("apkSha256")
    apk_sha256: "dict[str, dict[str, str]]" = {}
    if isinstance(apk_sha256_raw, dict):
        for key, value in apk_sha256_raw.items():
            if isinstance(value, dict):
                apk_sha256[key] = {"filename": value.get("filename"), "sha256": value.get("sha256")}
    return CandidateFingerprint(
        bundle_root=data.get("bundleRoot"),
        release_id=data.get("releaseId"),
        schema_version=data.get("schemaVersion"),
        manifest_sha256=data.get("manifestSha256"),
        checksums_sha256=data.get("checksumsSha256"),
        apk_sha256=apk_sha256,
        envelope_digest=data.get("envelopeDigest"),
    )


def verify_candidate_fingerprint(snapshot_dir: "Path | str", fingerprint: CandidateFingerprint) -> "list[str]":
    """Recompute every digest from the snapshot directory's CURRENT bytes and
    compare against the recorded fingerprint. Returns a list of problems
    (empty means the snapshot is byte-for-byte unchanged since it was
    recorded). Any changed manifest, checksums file, or APK -- including one
    substituted via a re-pointed symlink, since the bytes actually present at
    the path are what gets hashed -- is caught here."""
    problems: "list[str]" = []
    snapshot_dir = Path(snapshot_dir)

    if fingerprint.envelope_digest != _envelope_digest(fingerprint):
        problems.append(
            "release-candidate fingerprint record itself is inconsistent (envelope digest mismatch) -- "
            "the fingerprint file was edited after it was written."
        )

    def _check(label: str, expected: "str | None", path: Path) -> None:
        if expected is None:
            problems.append(f"fingerprint has no recorded digest for {label}.")
            return
        if not path.is_file():
            problems.append(f"{label} is missing from the release-candidate snapshot ({path}).")
            return
        try:
            actual = _sha256_file(path)
        except OSError as exc:
            problems.append(f"{label} could not be read from the snapshot: {exc}")
            return
        if actual != expected:
            problems.append(
                f"{label} changed since release-config approved this candidate "
                f"(expected sha256 {expected}, got {actual})."
            )

    _check(MANIFEST_NAME, fingerprint.manifest_sha256, snapshot_dir / MANIFEST_NAME)
    _check(CHECKSUMS_NAME, fingerprint.checksums_sha256, snapshot_dir / CHECKSUMS_NAME)
    for app_key, info in sorted(fingerprint.apk_sha256.items()):
        filename = info.get("filename")
        expected_sha = info.get("sha256")
        if not filename:
            problems.append(f"fingerprint has no recorded APK filename for app {app_key!r}.")
            continue
        _check(f"{app_key} APK ({filename})", expected_sha, snapshot_dir / filename)

    return problems
