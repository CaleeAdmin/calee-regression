"""Deterministic release-bundle assembly (Priority 4).

A technical-owner operation that turns already-signed, locally-available APKs
(plus explicitly-stated expected identities for any unchanged application)
into a release bundle directory that ``verify-release-bundle``/
``install-tablet-release`` (release_installer.py) can consume directly:
inspected (never signed) APK identities, generated SHA-256 checksums, and a
schema-version-2 ``release-manifest.json`` (release_installer.py's
``RELEASE_MANIFEST_SCHEMA_V2``).

Design constraints mirror release_installer.py's own (see its module
docstring):

  * **Never signs anything.** Every APK is inspected via ``apk_inspect.
    inspect_apk`` (``apksigner verify --print-certs``, never ``sign``); an
    unsigned/unreadable signer BLOCKS assembly.
  * **Never downloads anything.** Every input is an already-local path or an
    explicitly-stated value -- no URL fetching, no "latest" resolution.
    Optional ``provenance`` fields are pure metadata (repository, workflow run
    id, artifact name, source commit, artifact digest), recorded verbatim,
    never used to fetch anything.
  * **No credential concept exists here at all.** Nothing in this module
    authenticates to anything, so there is nothing to leak into command
    arguments, the assembly report, or the generated manifest.
  * **Ambiguous references are rejected.** Every Git SHA must be the full
    40-character form (identity_format.is_full_git_sha); every version must
    be a recognisable version, never ``"latest"`` (identity_format.
    is_wellformed_version).
  * **An unchanged application still needs a stated identity.** Omitting
    ``--calee-apk``/``--caleeshell-apk`` requires the matching
    ``--calee-expected``/``--caleeshell-expected`` JSON file -- Calee-only,
    CaleeShell-only and both-app releases are all supported, but a release
    that would leave an app with NO stated identity at all is rejected (the
    post-install complete-solution check always verifies both apps).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import apk_inspect
from .identity_format import is_full_git_sha, is_wellformed_version
from .release_installer import (
    CALEE_PACKAGE_ID,
    CALEESHELL_PACKAGE_ID,
    CHECKSUMS_NAME,
    MANIFEST_NAME,
    RELEASE_MANIFEST_SCHEMA_V2,
    VALID_RELEASE_PROFILES,
)

STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_BLOCKED = "blocked"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CANONICAL_PACKAGE = {"calee": CALEE_PACKAGE_ID, "caleeShell": CALEESHELL_PACKAGE_ID}


class AssemblyError(Exception):
    """A programmer/usage error (e.g. asked to write a bundle from a failed
    assembly). Distinct from an APK/expected-identity problem, which is
    reported as structured errors on the returned BundleAssembly, never
    raised."""


@dataclass
class ExpectedIdentity:
    """An explicitly-stated expected installed identity for an app this
    release does NOT ship an APK for (it is unchanged this release) -- read
    from a ``--calee-expected``/``--caleeshell-expected`` JSON file."""

    package_id: "str | None" = None
    version_name: "str | None" = None
    version_code: "int | None" = None
    git_sha: "str | None" = None
    signer_sha256: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "packageId": self.package_id,
            "versionName": self.version_name,
            "versionCode": self.version_code,
            "gitSha": self.git_sha,
            "signerSha256": self.signer_sha256,
        }


def parse_expected_identity(raw: Any) -> "tuple[ExpectedIdentity, list[str]]":
    """Parse+validate an unchanged-application expected-identity JSON object.
    Pure -- no filesystem access."""
    errors: "list[str]" = []
    if not isinstance(raw, dict):
        return ExpectedIdentity(), ["must contain a JSON object."]
    version_code = raw.get("versionCode")
    identity = ExpectedIdentity(
        package_id=raw.get("packageId"),
        version_name=raw.get("versionName"),
        version_code=version_code,
        git_sha=raw.get("gitSha"),
        signer_sha256=raw.get("signerSha256"),
    )
    if not identity.package_id or not isinstance(identity.package_id, str):
        errors.append("packageId is required.")
    if not is_wellformed_version(identity.version_name):
        errors.append(f"versionName {identity.version_name!r} is not a recognisable version.")
    if not isinstance(version_code, int) or isinstance(version_code, bool) or version_code <= 0:
        errors.append(f"versionCode must be a positive integer (got {version_code!r}).")
    if not is_full_git_sha(identity.git_sha):
        errors.append(
            f"gitSha must be a full 40-character Git SHA (got {identity.git_sha!r}); an abbreviated or "
            f"ambiguous reference (e.g. 'latest', a short SHA) is rejected."
        )
    if identity.signer_sha256 is not None and not _SHA256_RE.match(str(identity.signer_sha256)):
        errors.append(f"signerSha256 must be a 64-character hex SHA-256 (got {identity.signer_sha256!r}).")
    return identity, errors


@dataclass
class AppAssemblyResult:
    key: str  # "calee" | "caleeShell"
    install_artifact: bool
    apk_source: "Path | None" = None
    apk_filename: "str | None" = None
    apk_sha256: "str | None" = None
    inspection: "apk_inspect.ApkInspection | None" = None
    expected_identity: "ExpectedIdentity | None" = None
    errors: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def manifest_section(self) -> dict:
        if self.install_artifact:
            insp = self.inspection
            version_code = None
            if insp is not None and insp.version_code is not None:
                try:
                    version_code = int(insp.version_code)
                except (TypeError, ValueError):
                    version_code = None
            return {
                "installArtifact": True,
                "apk": self.apk_filename,
                "sha256": self.apk_sha256,
                "expectedInstalled": {
                    "packageId": insp.application_id if insp else None,
                    "versionName": insp.version_name if insp else None,
                    "versionCode": version_code,
                    "gitSha": insp.manifest_git_sha if insp else None,
                    "signerSha256": insp.signer_sha256 if insp else None,
                },
            }
        exp = self.expected_identity or ExpectedIdentity()
        return {"installArtifact": False, "expectedInstalled": exp.to_dict()}


def _assemble_app(
    key: str,
    *,
    apk_path: "Path | str | None",
    git_sha: "str | None",
    expected_identity_raw: "dict | None",
    which: "Callable[[str], Optional[str]]",
    runner,
) -> AppAssemblyResult:
    canonical = _CANONICAL_PACKAGE[key]
    flag_prefix = "calee" if key == "calee" else "caleeshell"

    if apk_path is not None:
        if not is_full_git_sha(git_sha):
            return AppAssemblyResult(key=key, install_artifact=True, errors=[
                f"{key}: --{flag_prefix}-git-sha must be a full 40-character Git SHA when an APK is "
                f"supplied (got {git_sha!r}); an abbreviated or ambiguous reference is rejected."
            ])
        insp = apk_inspect.inspect_apk(apk_path, key, manifest_git_sha=git_sha, which=which, runner=runner)
        if not insp.ok:
            return AppAssemblyResult(
                key=key, install_artifact=True, apk_source=Path(apk_path), inspection=insp,
                errors=[f"{key}: {d}" for d in insp.detail],
            )
        errors: "list[str]" = []
        if insp.application_id != canonical:
            errors.append(
                f"{key}: the APK's actual applicationId {insp.application_id!r} does not match the "
                f"canonical package {canonical!r} -- refusing to assemble a mislabelled/substituted APK."
            )
        if not is_wellformed_version(insp.version_name):
            errors.append(f"{key}: the APK's actual versionName {insp.version_name!r} is not a recognisable version.")
        try:
            version_code_int = int(insp.version_code) if insp.version_code is not None else None
        except (TypeError, ValueError):
            version_code_int = None
        if not version_code_int or version_code_int <= 0:
            errors.append(f"{key}: the APK's actual versionCode {insp.version_code!r} must be a positive integer.")
        if not insp.signer_sha256:
            errors.append(
                f"{key}: the APK's signing certificate could not be read -- refusing to accept an "
                f"unsigned or unreadable APK (this command never signs an APK itself)."
            )
        return AppAssemblyResult(
            key=key, install_artifact=True, apk_source=Path(apk_path), apk_filename=Path(apk_path).name,
            apk_sha256=insp.apk_sha256, inspection=insp, errors=errors,
        )

    # Not included this release -- an EXPLICIT expected identity is required;
    # an unchanged application is never silently dropped from the manifest.
    if expected_identity_raw is None:
        return AppAssemblyResult(key=key, install_artifact=False, errors=[
            f"{key}: not included in this release (no --{flag_prefix}-apk given), but no expected "
            f"identity was supplied either -- pass --{flag_prefix}-expected with its explicit expected "
            f"installed identity (an unchanged application still requires a stated identity)."
        ])
    identity, id_errors = parse_expected_identity(expected_identity_raw)
    errors = [f"{key}: expected-identity.{e}" for e in id_errors]
    if identity.package_id and identity.package_id != canonical:
        errors.append(
            f"{key}: expected-identity.packageId {identity.package_id!r} must be the canonical package "
            f"{canonical!r}."
        )
    return AppAssemblyResult(key=key, install_artifact=False, expected_identity=identity, errors=errors)


@dataclass
class BundleAssembly:
    status: str = STATUS_INVALID
    release_id: "str | None" = None
    out_dir: "str | None" = None
    calee: "AppAssemblyResult | None" = None
    caleeshell: "AppAssemblyResult | None" = None
    manifest: "dict | None" = None
    errors: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "releaseId": self.release_id,
            "outDir": self.out_dir,
            "errors": list(self.errors),
            "calee": self.calee.manifest_section() if (self.calee and self.calee.install_artifact is not None) else None,
            "caleeShell": self.caleeshell.manifest_section() if (self.caleeshell and self.caleeshell.install_artifact is not None) else None,
            "manifest": self.manifest,
        }


def assemble_release_bundle(
    *,
    release_id: "str | None",
    profile: "str | None",
    backend: "str | None",
    caleemobile_sha: "str | None",
    caleemobile_version: "str | None",
    calee_apk: "Path | str | None" = None,
    calee_git_sha: "str | None" = None,
    calee_expected: "dict | None" = None,
    caleeshell_apk: "Path | str | None" = None,
    caleeshell_git_sha: "str | None" = None,
    caleeshell_expected: "dict | None" = None,
    selector_evidence_required: bool = True,
    distributed_build_acceptance_required: bool = True,
    platforms: "dict | None" = None,
    features: "dict | None" = None,
    provenance: "dict | None" = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    runner=apk_inspect.real_tool_runner,
) -> BundleAssembly:
    """Validate every input and inspect every supplied APK; never writes any
    file (see ``write_release_bundle`` for that). Pure/offline except for the
    injected APK-inspection tool runner. Returns a BundleAssembly whose ``ok``
    is True only when every check passed -- including the "at least one app
    installed" rule and a fully-populated schema-v2 manifest dict on success.
    """
    errors: "list[str]" = []

    if not release_id or not isinstance(release_id, str) or not release_id.strip():
        errors.append("--release-id is required and must be a non-empty string.")
    if profile not in VALID_RELEASE_PROFILES:
        errors.append(f"--profile must be one of {sorted(VALID_RELEASE_PROFILES)} (got {profile!r}).")
    if not backend or not isinstance(backend, str) or not backend.strip():
        errors.append("--backend is required and must be a non-empty string.")
    if not is_full_git_sha(caleemobile_sha):
        errors.append(
            f"--caleemobile-sha must be a full 40-character Git SHA (got {caleemobile_sha!r}); an "
            f"abbreviated or ambiguous reference (e.g. 'latest') is rejected."
        )
    if not is_wellformed_version(caleemobile_version):
        errors.append(f"--caleemobile-version {caleemobile_version!r} is not a recognisable version.")

    if calee_apk is None and caleeshell_apk is None:
        errors.append(
            "A release bundle must install at least one app (--calee-apk and/or --caleeshell-apk) -- a "
            "bundle that would install nothing is rejected."
        )

    calee_result = _assemble_app(
        "calee", apk_path=calee_apk, git_sha=calee_git_sha, expected_identity_raw=calee_expected,
        which=which, runner=runner,
    )
    caleeshell_result = _assemble_app(
        "caleeShell", apk_path=caleeshell_apk, git_sha=caleeshell_git_sha,
        expected_identity_raw=caleeshell_expected, which=which, runner=runner,
    )
    errors.extend(calee_result.errors)
    errors.extend(caleeshell_result.errors)

    assembly = BundleAssembly(release_id=release_id, calee=calee_result, caleeshell=caleeshell_result)
    if errors:
        assembly.status = STATUS_INVALID
        assembly.errors = errors
        return assembly

    platforms = platforms or {"tablet": True, "mobileAndroid": True, "mobileIos": True}
    features = features or {
        "synchronization": True, "meals": True, "onboarding": True,
        "googleCalendar": True, "kioskAdmin": True, "notifications": True,
    }
    manifest = {
        "schemaVersion": RELEASE_MANIFEST_SCHEMA_V2,
        "releaseId": release_id,
        "profile": profile,
        "backend": backend,
        "platforms": dict(platforms),
        "features": dict(features),
        "tabletSolution": {
            "calee": calee_result.manifest_section(),
            "caleeShell": caleeshell_result.manifest_section(),
        },
        "caleeMobile": {
            "version": caleemobile_version,
            "gitSha": caleemobile_sha,
            "selectorEvidenceRequired": bool(selector_evidence_required),
            "distributedBuildAcceptanceRequired": bool(distributed_build_acceptance_required),
        },
    }
    if provenance:
        manifest["provenance"] = dict(provenance)

    assembly.status = STATUS_OK
    assembly.manifest = manifest
    return assembly


def _atomic_replace_dir(tmp_dir: Path, dest_dir: Path) -> None:
    """Atomically make ``tmp_dir`` become ``dest_dir``. POSIX ``rename``
    cannot replace a non-empty existing directory in one call, so an existing
    ``dest_dir`` is first renamed aside, ``tmp_dir`` is renamed into place,
    and only then is the old directory removed -- a failure partway through
    the swap still leaves a valid directory at ``dest_dir`` (either the old
    one, restored, or the new one). Mirrors release_candidate.py's identical
    helper (Priority 4/9) -- kept as its own copy here rather than a cross-
    import, matching this module's existing style of not depending on
    release_candidate.py."""
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


def write_release_bundle(assembly: BundleAssembly, out_dir: "Path | str") -> Path:
    """Writes the assembled, schema-v2 bundle to ``out_dir``: copies each
    included APK, generates ``checksums.sha256`` and ``release-manifest.json``.
    Raises AssemblyError if ``assembly`` did not succeed -- never writes a
    partial/invalid bundle.

    Writes ATOMICALLY (Priority 9): everything is built in a fresh temporary
    sibling directory first, then swapped into place with one directory
    rename (``_atomic_replace_dir``, mirroring ``release_candidate.py``'s
    identical snapshot-swap pattern). A process killed/crashing partway
    through a write leaves ``out_dir`` exactly as it was before this call --
    never a torn state where, say, a freshly-copied APK sits alongside a
    STALE ``release-manifest.json`` still describing a previous release's
    identity (or vice versa). That specific torn state is dangerous, not
    just untidy: if ``--out`` is reused across releases with a filename that
    happens to coincide, a half-written bundle could otherwise still
    successfully verify -- just as the WRONG release. Because the swap
    replaces the whole directory, ``out_dir`` is always exactly the
    single bundle this call produced -- an ``--out`` reused across releases
    never accumulates stale APKs/manifests left over from an earlier,
    differently-named release. A destination used for anything besides a
    release bundle should not be pointed at by ``--out``."""
    if not assembly.ok or assembly.manifest is None:
        raise AssemblyError("Cannot write a bundle from a failed/incomplete assembly.")
    out = Path(out_dir)
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{out.name}.tmp-", dir=str(out.parent)))
    try:
        checksum_lines: "list[str]" = []
        for result in (assembly.calee, assembly.caleeshell):
            if result is None or not result.install_artifact:
                continue
            dest = tmp_dir / result.apk_filename
            shutil.copyfile(result.apk_source, dest)
            checksum_lines.append(f"{result.apk_sha256}  {result.apk_filename}")

        (tmp_dir / CHECKSUMS_NAME).write_text(
            "\n".join(checksum_lines) + ("\n" if checksum_lines else ""), encoding="utf-8",
        )
        (tmp_dir / MANIFEST_NAME).write_text(json.dumps(assembly.manifest, indent=2) + "\n", encoding="utf-8")
        _atomic_replace_dir(tmp_dir, out)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return out
