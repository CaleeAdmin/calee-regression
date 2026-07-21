"""Release-candidate fingerprint + immutable snapshot (Priority 4 legacy;
Priorities 4/5 this session).

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

Publication itself (this session's Priority 4) goes through
``atomic_publish.publish_version`` -- an immutable, content-addressed version
directory plus a symlink pointer swapped only after verification -- so a
process killed mid-publish can never leave ``snapshot_dir`` pointing at a
partial/missing directory; a previously valid candidate stays discoverable
and installable.

The fingerprint (this session's Priority 5) additionally binds:

  * ``candidateId`` -- a content-addressed identifier for the ENTIRE
    published directory tree (``atomic_publish.directory_content_id``),
    independent of any machine-local absolute path;
  * ``runId`` -- the run this candidate was frozen for, so a candidate
    directory copied wholesale into a different run's workspace is rejected
    (its embedded ``runId`` will disagree with the run trying to install it);
  * ``releaseConfigDigest`` -- a digest of the release-config selections
    (profile/scope/expected identities) that approved this exact candidate,
    so ``install-tablet-release`` can independently recompute the same
    digest from the same-run release-config report and refuse to install if
    they disagree;
  * ``createdAt`` -- when the candidate was frozen.

``bundleRoot`` remains a machine-local, mutable diagnostic path and is
deliberately EXCLUDED from the envelope digest -- see the module docstring
in ``atomic_publish.py``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import atomic_publish

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


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class CandidateFingerprint:
    """A content-addressed fingerprint of one run's approved release
    candidate: the resolved snapshot root, the release ID + schema version it
    was approved for, a raw-byte SHA-256 for the manifest/checksums/every
    included app's APK, and (Priority 5) the run it belongs to, the digest of
    the release-config that approved it, its whole-directory content id, and
    its creation time."""

    bundle_root: "str | None" = None
    release_id: "str | None" = None
    schema_version: "int | None" = None
    manifest_sha256: "str | None" = None
    checksums_sha256: "str | None" = None
    # app key ("calee"/"caleeShell") -> {"filename": ..., "sha256": ...}
    apk_sha256: "dict[str, dict[str, str]]" = field(default_factory=dict)
    # Priority 5 fields -- all bound into the envelope digest below.
    run_id: "str | None" = None
    candidate_id: "str | None" = None
    release_config_digest: "str | None" = None
    created_at: "str | None" = None
    envelope_digest: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "bundleRoot": self.bundle_root,
            "releaseId": self.release_id,
            "schemaVersion": self.schema_version,
            "manifestSha256": self.manifest_sha256,
            "checksumsSha256": self.checksums_sha256,
            "apkSha256": {k: dict(v) for k, v in self.apk_sha256.items()},
            "runId": self.run_id,
            "candidateId": self.candidate_id,
            "releaseConfigDigest": self.release_config_digest,
            "createdAt": self.created_at,
            "envelopeDigest": self.envelope_digest,
        }


def _envelope_digest(fp: CandidateFingerprint) -> str:
    """A digest over every OTHER cryptographically-bound field, so the
    fingerprint record itself cannot be edited (e.g. a problem file's digest
    silently replaced, or the candidate re-pointed at another run/root)
    without also invalidating this envelope -- mirrors
    ``selector_provenance.py``'s ``envelope_digest`` construction.

    ``bundle_root`` is deliberately NOT included -- it is a machine-local,
    mutable diagnostic path, not part of the candidate's cryptographic
    identity (see module docstring)."""
    payload = json.dumps(
        {
            "releaseId": fp.release_id,
            "schemaVersion": fp.schema_version,
            "manifestSha256": fp.manifest_sha256,
            "checksumsSha256": fp.checksums_sha256,
            "apkSha256": {k: dict(sorted(v.items())) for k, v in sorted(fp.apk_sha256.items())},
            "runId": fp.run_id,
            "candidateId": fp.candidate_id,
            "releaseConfigDigest": fp.release_config_digest,
            "createdAt": fp.created_at,
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
    run_id: "str | None" = None,
    release_config_digest: "str | None" = None,
    created_at: "str | None" = None,
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
    silently snapshotting a torn read.

    Publication goes through ``atomic_publish.publish_version`` (Priority 4):
    the new content is built in a fresh, content-addressed version
    directory, verified, and only then atomically pointed to by
    ``snapshot_dir`` (a symlink) -- a process killed at any point during this
    leaves either the previous valid candidate (if any) or nothing, never a
    partially-written one, discoverable at ``snapshot_dir``.
    """
    bundle_root = Path(verification.bundle_root)
    manifest_src = bundle_root / MANIFEST_NAME
    checksums_src = bundle_root / CHECKSUMS_NAME
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)

    fingerprint_holder: "dict[str, CandidateFingerprint]" = {}

    def _build(tmp_dir: Path) -> None:
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
            run_id=run_id,
            release_config_digest=release_config_digest,
            created_at=created_at or _utc_now_iso(),
        )
        # The whole-directory content id is computed from what's ACTUALLY in
        # tmp_dir (the same bytes that will be published), excluding the
        # fingerprint file itself (which doesn't exist yet at this point, and
        # must never depend on its own id) -- so it always agrees with a
        # later `directory_content_id(snapshot_dir, exclude=...)` recompute.
        fingerprint.candidate_id = atomic_publish.directory_content_id(tmp_dir, exclude={FINGERPRINT_FILENAME})
        fingerprint.envelope_digest = _envelope_digest(fingerprint)
        (tmp_dir / FINGERPRINT_FILENAME).write_text(
            json.dumps(fingerprint.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        fingerprint_holder["fingerprint"] = fingerprint

    def _verify(tmp_dir: Path) -> "list[str]":
        # Re-read what was just written and prove it round-trips before it is
        # ever published (Priority 4 requirement: verify before the pointer
        # moves).
        try:
            fp = load_candidate_fingerprint(tmp_dir / FINGERPRINT_FILENAME)
        except CandidateFingerprintError as exc:
            return [f"newly-built candidate fingerprint did not round-trip: {exc}"]
        return verify_candidate_fingerprint(tmp_dir, fp)

    atomic_publish.publish_version(snapshot_dir, _build, verify_fn=_verify)
    return fingerprint_holder["fingerprint"]


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
        run_id=data.get("runId"),
        candidate_id=data.get("candidateId"),
        release_config_digest=data.get("releaseConfigDigest"),
        created_at=data.get("createdAt"),
        envelope_digest=data.get("envelopeDigest"),
    )


def verify_candidate_fingerprint(
    snapshot_dir: "Path | str",
    fingerprint: CandidateFingerprint,
    *,
    expected_run_id: "str | None" = None,
    expected_release_id: "str | None" = None,
    expected_schema_version: "int | None" = None,
    expected_release_config_digest: "str | None" = None,
) -> "list[str]":
    """Recompute every digest from the snapshot directory's CURRENT bytes and
    compare against the recorded fingerprint. Returns a list of problems
    (empty means the snapshot is byte-for-byte unchanged since it was
    recorded, and matches every ``expected_*`` binding supplied). Any changed
    manifest, checksums file, or APK -- including one substituted via a
    re-pointed symlink, since the bytes actually present at the path are what
    gets hashed -- is caught here.

    Priority 5: ``expected_run_id``/``expected_release_id``/
    ``expected_schema_version``/``expected_release_config_digest``, when
    given, bind this verification to a SPECIFIC run/release/config -- a
    candidate directory copied wholesale from another run, or a fingerprint
    silently approved under a different release-config, is rejected here
    even though its own internal envelope digest is self-consistent."""
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

    # Priority 5: the immutable whole-directory candidate id must match what
    # is ACTUALLY on disk right now -- catches a fingerprint that names a
    # candidateId belonging to a different (e.g. substituted-in-full)
    # directory tree, even one whose individual per-file digests were also
    # consistently forged.
    if snapshot_dir.is_dir():
        if not fingerprint.candidate_id:
            problems.append("fingerprint has no recorded candidateId -- the candidate is not identity-bound.")
        else:
            try:
                actual_candidate_id = atomic_publish.directory_content_id(
                    snapshot_dir, exclude={FINGERPRINT_FILENAME}
                )
            except OSError as exc:
                problems.append(f"could not recompute the candidate directory's content id: {exc}")
            else:
                if actual_candidate_id != fingerprint.candidate_id:
                    problems.append(
                        f"candidateId mismatch: fingerprint says {fingerprint.candidate_id}, the snapshot "
                        f"directory's current content id is {actual_candidate_id} -- this is not the exact "
                        f"candidate directory the fingerprint was recorded for."
                    )

    if expected_run_id is not None and fingerprint.run_id != expected_run_id:
        problems.append(
            f"candidate fingerprint runId {fingerprint.run_id!r} != this run {expected_run_id!r} -- this "
            f"candidate belongs to a DIFFERENT run and must not be installed here."
        )
    if expected_release_id is not None and fingerprint.release_id != expected_release_id:
        problems.append(
            f"candidate fingerprint releaseId {fingerprint.release_id!r} != expected {expected_release_id!r}."
        )
    if expected_schema_version is not None and fingerprint.schema_version != expected_schema_version:
        problems.append(
            f"candidate fingerprint schemaVersion {fingerprint.schema_version!r} != expected "
            f"{expected_schema_version!r}."
        )
    if expected_release_config_digest is not None and fingerprint.release_config_digest != expected_release_config_digest:
        problems.append(
            f"candidate fingerprint releaseConfigDigest {fingerprint.release_config_digest!r} != the same-run "
            f"release-config's digest {expected_release_config_digest!r} -- this candidate was not approved by "
            f"the release-config evidence for this run."
        )

    return problems
