"""Tablet release-bundle verification and installation planning.

Turns a signed release bundle (Calee + optionally CaleeShell APKs, plus a
manifest and checksum file) into an *audited, ordered ADB install plan*, and
then classifies the outcome of executing that plan.

Design constraints (see docs/RELEASE_INSTALLER.md):

  * **Everything is offline-testable.** Bundle verification, manifest schema
    checks, checksum verification, path-traversal rejection, install-command
    construction, and adb-output classification are all pure functions over
    in-memory data or a temp directory. The one place real work happens --
    running adb -- goes through an injected ``AdbRunner`` callable, so tests
    substitute a fake and no real device is ever required.
  * **A bundle we cannot fully trust never yields an install command.**
    A missing APK, a checksum mismatch, a malformed/abbreviated Git SHA, an
    unexpected executable/archive file, a duplicate APK name, or a
    path-traversal entry all make ``verify_release_bundle`` fail, and the
    installer refuses to build a plan from a failed verification.
  * **Data is preserved and downgrades/signature-mismatches BLOCK.** The
    generated plan uses a data-preserving reinstall (``adb install -r``,
    never ``-d`` unless a downgrade is explicitly authorised, and never an
    ``uninstall``/``clear`` after a signature mismatch). A signature
    mismatch, a version mismatch, a HOME-resolution mismatch, an unavailable
    adb binary, or an unavailable device are each classified as ``blocked``,
    never as a product failure and never silently "fixed" by wiping data.

This module deliberately does NOT import click or the run-workspace layer --
it is a pure library the ``cli.py`` installer commands wrap. That keeps the
whole subsystem importable and testable without the CLI.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .build_identity import parse_dumpsys_version_code, parse_dumpsys_version_name
from .identity_format import is_full_git_sha, is_wellformed_version

# Canonical package identities. A bundle may only carry these two apps; any
# other packageId is rejected so a mislabelled or substituted APK cannot be
# installed under a trusted name.
CALEE_PACKAGE_ID = "com.viso.calee"
CALEESHELL_PACKAGE_ID = "com.viso.caleeshell"

# The only file kinds a release bundle may contain. Anything else (a shell
# script, a nested zip/tar, a stray binary) is rejected -- a release bundle is
# data to install, never code to run.
ALLOWED_BUNDLE_SUFFIXES = {".apk", ".json", ".sha256"}
MANIFEST_NAME = "release-manifest.json"
CHECKSUMS_NAME = "checksums.sha256"

# Outcome vocabulary, kept aligned with the rest of the framework's
# pass/fail/blocked model (see docs/RELEASE_POLICY.md). Bundle-level problems
# are "invalid" (the input the technical owner supplied is malformed);
# install-time device/signature/version problems are "blocked".
STATUS_OK = "ok"
STATUS_INVALID = "invalid"
STATUS_BLOCKED = "blocked"

# Install-outcome classifications. Each non-OK value is a BLOCKED release
# reason -- never a product FAIL, and never a trigger for a destructive
# recovery action.
OUTCOME_OK = "ok"
OUTCOME_BUNDLE_INVALID = "bundle_invalid"
OUTCOME_ADB_UNAVAILABLE = "adb_unavailable"
OUTCOME_DEVICE_UNAVAILABLE = "device_unavailable"
OUTCOME_SIGNATURE_MISMATCH = "signature_mismatch"
OUTCOME_VERSION_MISMATCH = "version_mismatch"
OUTCOME_HOME_MISMATCH = "home_mismatch"
OUTCOME_DOWNGRADE_BLOCKED = "downgrade_blocked"
OUTCOME_INSTALL_FAILED = "install_failed"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_APK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.apk$")


class ReleaseInstallerError(Exception):
    """A programmer/usage error in this module (e.g. asked to build a plan
    from a verification that failed). Distinct from a *bundle* being invalid,
    which is reported as structured errors, never raised."""


# ── manifest / identity model ────────────────────────────────────────────


@dataclass
class AppRelease:
    """One app's identity inside a release manifest.

    Two orthogonal facts are recorded, and MUST NOT be conflated (Priority 2):

      * ``install_artifact`` -- whether THIS release ships an APK to install for
        this app. A release may replace Calee only, CaleeShell only, or both.
      * ``has_expected`` + the identity fields -- the EXPECTED INSTALLED identity
        that must be present on the tablet after the release, *whether or not*
        this release installed the app. An unchanged app is not "ignored": it
        still carries an expected identity that the post-reboot complete-solution
        check verifies.

    ``included`` is kept as a backward-compatible alias of ``install_artifact``
    (older manifests and callers use ``included``)."""

    key: str  # "calee" | "caleeShell"
    included: bool
    package_id: "str | None" = None
    version_name: "str | None" = None
    version_code: "int | None" = None
    git_sha: "str | None" = None
    apk: "str | None" = None
    sha256: "str | None" = None
    # The trusted signing-certificate SHA-256 the installed app must carry. Part
    # of the expected installed identity; used by the post-reboot signer-trust
    # check. Optional (an older manifest may omit it -> signer trust recorded as
    # not compared).
    signer_sha256: "str | None" = None
    # Whether this app ships an APK to install in this release (installArtifact).
    install_artifact: bool = True
    # Whether an EXPECTED INSTALLED identity is declared for this app (so the
    # complete-solution check verifies it). True for any installed app and for
    # an unchanged app that declares expectedInstalled; False only for a legacy
    # ``included: false`` section that declares no identity at all.
    has_expected: bool = True
    # The ABSOLUTE path of this app's APK inside the verified bundle root,
    # resolved once during verify_release_bundle and proven to stay inside that
    # root. Install commands use THIS, never the manifest-declared ``apk``
    # filename -- so an install works regardless of the process's cwd, and an
    # APK path is never reconstructed from the (untrusted) manifest after the
    # bundle has been verified. None until a bundle passes verification.
    apk_path: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "included": self.included,
            "installArtifact": self.install_artifact,
            "hasExpected": self.has_expected,
            "packageId": self.package_id,
            "versionName": self.version_name,
            "versionCode": self.version_code,
            "gitSha": self.git_sha,
            "signerSha256": self.signer_sha256,
            "apk": self.apk,
            "apkPath": self.apk_path,
            "sha256": self.sha256,
        }


@dataclass
class ReleaseManifest:
    release_id: "str | None" = None
    calee: "AppRelease | None" = None
    caleeshell: "AppRelease | None" = None

    def included_apps(self) -> "list[AppRelease]":
        """Apps whose APK this release installs (installArtifact/included)."""
        return [a for a in (self.calee, self.caleeshell) if a is not None and a.install_artifact]

    def expected_apps(self) -> "list[AppRelease]":
        """Apps that carry an EXPECTED INSTALLED identity -- installed this
        release OR unchanged-but-declared. These are what the complete-solution
        check verifies after reboot. An unchanged app is never dropped here."""
        return [a for a in (self.calee, self.caleeshell) if a is not None and a.has_expected]

    def to_dict(self) -> dict:
        return {
            "releaseId": self.release_id,
            "calee": self.calee.to_dict() if self.calee else None,
            "caleeShell": self.caleeshell.to_dict() if self.caleeshell else None,
        }


_EXPECTED_PACKAGE = {"calee": CALEE_PACKAGE_ID, "caleeShell": CALEESHELL_PACKAGE_ID}


def _parse_app(key: str, raw: Any, errors: "list[str]") -> "AppRelease | None":
    """Parse and validate one app section. ``key`` is the manifest key
    (``calee``/``caleeShell``). Returns an AppRelease or None if the section is
    missing entirely.

    Two manifest shapes are accepted (Priority 2):

      * Legacy flat: ``{"included": true, "packageId": ..., "versionName": ...,
        "versionCode": ..., "gitSha": ..., "apk": ..., "sha256": ...}``. A legacy
        ``{"included": false}`` section (no identity) means that app is simply
        absent from this release -- back-compatible.
      * Complete-solution: ``{"installArtifact": true|false, "apk"?: ...,
        "sha256"?: ..., "expectedInstalled": {"packageId", "versionName",
        "versionCode", "gitSha", "signerSha256"}}``. Here ``installArtifact``
        controls whether an APK is installed, while ``expectedInstalled`` is the
        identity the tablet must carry afterwards -- REQUIRED even when
        ``installArtifact`` is false (an unchanged app still has an expected
        identity that the post-reboot check verifies).

    The "at least one app must be installed" rule is enforced by
    ``parse_manifest`` after both sections are parsed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"manifest.{key} must be an object.")
        return None

    # installArtifact supersedes the legacy 'included'; default install.
    if "installArtifact" in raw:
        install_artifact = bool(raw.get("installArtifact"))
    else:
        install_artifact = bool(raw.get("included", True))

    # Expected installed identity: an explicit expectedInstalled block wins;
    # otherwise the flat fields carry it (legacy). ``signerSha256`` may live in
    # either the expectedInstalled block or (legacy) at the top of the section.
    exp = raw.get("expectedInstalled")
    if isinstance(exp, dict):
        package_id = exp.get("packageId")
        version_name = exp.get("versionName")
        version_code = exp.get("versionCode")
        git_sha = exp.get("gitSha")
        signer_sha256 = exp.get("signerSha256")
        has_expected = True
    else:
        if exp is not None:
            errors.append(f"manifest.{key}.expectedInstalled must be an object.")
        package_id = raw.get("packageId")
        version_name = raw.get("versionName")
        version_code = raw.get("versionCode")
        git_sha = raw.get("gitSha")
        signer_sha256 = raw.get("signerSha256")
        # Legacy: an installed app declares identity inline; an app that neither
        # installs nor declares any identity is truly absent (back-compat).
        has_expected = install_artifact or any(
            v is not None for v in (package_id, version_name, version_code, git_sha)
        )

    app = AppRelease(
        key=key,
        included=install_artifact,
        install_artifact=install_artifact,
        package_id=package_id,
        version_name=version_name,
        version_code=version_code,
        git_sha=git_sha,
        signer_sha256=signer_sha256,
        apk=raw.get("apk"),
        sha256=raw.get("sha256"),
        has_expected=has_expected,
    )

    if not has_expected:
        # A not-installed, not-declared app carries no fields; nothing to
        # validate (its identity is recorded as omitted).
        return app

    # Validate the EXPECTED INSTALLED identity (whether or not we install it --
    # an unchanged app must still declare a well-formed identity to verify).
    expected_pkg = _EXPECTED_PACKAGE[key]
    if app.package_id != expected_pkg:
        errors.append(
            f"manifest.{key} expected packageId must be {expected_pkg!r} (got {app.package_id!r})."
        )
    if not is_wellformed_version(app.version_name):
        errors.append(
            f"manifest.{key} expected versionName {app.version_name!r} is not a recognisable version "
            f"(e.g. 'founder-v0.3.25')."
        )
    if not isinstance(app.version_code, int) or isinstance(app.version_code, bool) or app.version_code <= 0:
        errors.append(f"manifest.{key} expected versionCode must be a positive integer (got {app.version_code!r}).")
    if not is_full_git_sha(app.git_sha):
        errors.append(
            f"manifest.{key} expected gitSha must be a full 40-character Git SHA (got {app.git_sha!r}); an "
            f"abbreviated SHA is ambiguous and is rejected."
        )
    if signer_sha256 is not None and not _SHA256_RE.match(str(signer_sha256)):
        errors.append(
            f"manifest.{key} expected signerSha256 {signer_sha256!r} must be a 64-character hex SHA-256."
        )

    # Install-artifact-only fields: an app we actually install must ship a valid
    # APK + checksum. An unchanged (installArtifact:false) app must NOT.
    if install_artifact:
        if not isinstance(app.apk, str) or not _APK_NAME_RE.match(app.apk or ""):
            errors.append(
                f"manifest.{key}.apk must be a plain '*.apk' filename inside the bundle root "
                f"(got {app.apk!r}); paths/traversal are rejected."
            )
        if not isinstance(app.sha256, str) or not _SHA256_RE.match(app.sha256 or ""):
            errors.append(f"manifest.{key}.sha256 must be a 64-character hex SHA-256 (got {app.sha256!r}).")
    else:
        if app.apk is not None or app.sha256 is not None:
            errors.append(
                f"manifest.{key} is not installed this release (installArtifact:false) but declares an "
                f"apk/sha256 -- an unchanged app must not carry an install artifact."
            )
    return app


def parse_manifest(raw: Any) -> "tuple[ReleaseManifest, list[str]]":
    """Parse a raw manifest dict into a ReleaseManifest plus a list of schema
    errors (empty when valid). Pure -- no filesystem access."""
    errors: "list[str]" = []
    if not isinstance(raw, dict):
        return ReleaseManifest(), ["release-manifest.json must contain a JSON object at the top level."]

    release_id = raw.get("releaseId")
    if not isinstance(release_id, str) or not release_id.strip():
        errors.append("manifest.releaseId is required and must be a non-empty string.")

    calee = _parse_app("calee", raw.get("calee"), errors)
    caleeshell = _parse_app("caleeShell", raw.get("caleeShell"), errors)

    manifest = ReleaseManifest(release_id=release_id, calee=calee, caleeshell=caleeshell)
    if not manifest.included_apps():
        errors.append(
            "A release bundle must install at least one app (calee and/or caleeShell) with "
            "installArtifact/included: true -- a bundle that would install nothing is rejected."
        )

    return manifest, errors


# ── bundle verification ──────────────────────────────────────────────────


@dataclass
class BundleVerification:
    status: str = STATUS_INVALID
    bundle_dir: "str | None" = None
    # The bundle directory RESOLVED to a single absolute path, computed once at
    # the start of verification. Every verified APK's absolute path is proven to
    # live inside this root, and install commands are built from those absolute
    # paths -- so the resolved root is the trust boundary recorded in evidence.
    bundle_root: "str | None" = None
    manifest: "ReleaseManifest | None" = None
    errors: "list[str]" = field(default_factory=list)
    # The exact APK identities the plan may install, recorded SEPARATELY per
    # app so Calee and CaleeShell identities are never conflated.
    verified_apps: "list[AppRelease]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def app(self, key: str) -> "AppRelease | None":
        for a in self.verified_apps:
            if a.key == key:
                return a
        return None

    def expected_app(self, key: str) -> "AppRelease | None":
        """The EXPECTED INSTALLED identity for an app (Calee/CaleeShell),
        whether or not this release installed it. Backs the complete-solution
        check, which verifies both apps after every update."""
        if self.manifest:
            for a in self.manifest.expected_apps():
                if a.key == key:
                    return a
        return None

    def expected_apps(self) -> "list[AppRelease]":
        return self.manifest.expected_apps() if self.manifest else []

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "bundleDir": self.bundle_dir,
            "bundleRoot": self.bundle_root,
            "releaseId": self.manifest.release_id if self.manifest else None,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "errors": list(self.errors),
            "verifiedApps": [a.to_dict() for a in self.verified_apps],
        }


def sha256_of_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksums_file(text: str) -> "dict[str, str]":
    """Parse a ``sha256sum``-style file (``<hex>  <name>`` per line) into a
    {filename: hex} map. Tolerates one or two spaces and a leading ``*`` on
    the binary-mode marker. Lines that don't parse are ignored (they surface
    as a coverage gap when a referenced APK has no entry)."""
    out: "dict[str, str]" = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*")
        if _SHA256_RE.match(digest):
            out[name] = digest.lower()
    return out


def _is_safe_bundle_name(name: str) -> bool:
    """A bundle entry name must be a single path component (no directory
    separators, no ``..``, not absolute) -- otherwise the manifest could point
    the installer at a file outside the bundle root."""
    if not name or name in (".", ".."):
        return False
    if name.startswith("/") or name.startswith("\\"):
        return False
    return "/" not in name and "\\" not in name and ".." not in Path(name).parts


def _is_within(child: Path, root: Path) -> bool:
    """True when ``child`` (already resolved) is ``root`` itself or lives inside
    it. Defense in depth on top of ``_is_safe_bundle_name``: even if a bundle
    entry is a symlink whose target escapes the bundle, the resolved path is
    proven to stay inside the verified root before any install command is built
    from it."""
    try:
        return child == root or child.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Path.is_relative_to is 3.9+
        try:
            child.relative_to(root)
            return True
        except ValueError:
            return False


def verify_release_bundle(bundle_dir: "Path | str") -> BundleVerification:
    """Fully verify a release bundle directory. Returns a BundleVerification
    whose ``ok`` is True only when every check passed. Never raises for a bad
    bundle -- all problems are collected into ``errors``."""
    bundle = Path(bundle_dir)
    result = BundleVerification(bundle_dir=str(bundle))

    if not bundle.is_dir():
        result.errors.append(f"Bundle directory not found: {bundle}")
        return result

    # Resolve the bundle root to a single absolute path ONCE. Every verified
    # APK's absolute path is derived from and proven to stay inside this root,
    # and install commands are built from those absolute paths -- so an install
    # works no matter what the current working directory is when it runs.
    bundle_root = bundle.resolve()
    result.bundle_root = str(bundle_root)

    # Enumerate the bundle root (non-recursively). A subdirectory is itself
    # unexpected -- a release bundle is flat.
    entries = sorted(p for p in bundle.iterdir())
    errors = result.errors
    for entry in entries:
        if entry.is_dir():
            errors.append(f"Unexpected subdirectory in bundle: {entry.name}/ (a release bundle must be flat).")
            continue
        if entry.suffix.lower() not in ALLOWED_BUNDLE_SUFFIXES:
            errors.append(
                f"Unexpected file in bundle: {entry.name} (only {sorted(ALLOWED_BUNDLE_SUFFIXES)} are allowed -- "
                f"a bundle carries APKs, a manifest and a checksum file, never scripts or archives)."
            )

    manifest_path = bundle / MANIFEST_NAME
    if not manifest_path.is_file():
        errors.append(f"{MANIFEST_NAME} not found in bundle root.")
        return result
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{MANIFEST_NAME} is not readable JSON: {exc}")
        return result

    manifest, schema_errors = parse_manifest(raw_manifest)
    result.manifest = manifest
    errors.extend(schema_errors)

    checksums_path = bundle / CHECKSUMS_NAME
    checksums: "dict[str, str]" = {}
    if not checksums_path.is_file():
        errors.append(f"{CHECKSUMS_NAME} not found in bundle root.")
    else:
        try:
            checksums = parse_checksums_file(checksums_path.read_text(encoding="utf-8"))
        except OSError as exc:
            errors.append(f"{CHECKSUMS_NAME} is not readable: {exc}")

    included = manifest.included_apps()

    # Duplicate APK filename across apps -- two apps must never resolve to the
    # same file (which one would actually be installed for each?).
    apk_names = [a.apk for a in included if a.apk]
    for name in set(apk_names):
        if apk_names.count(name) > 1:
            errors.append(f"Duplicate APK filename {name!r} referenced by more than one app.")

    for app in included:
        if not app.apk:
            continue  # already reported by schema validation
        if not _is_safe_bundle_name(app.apk):
            errors.append(
                f"manifest.{app.key}.apk {app.apk!r} is not a safe in-bundle filename "
                f"(path separators, '..' and absolute paths are rejected)."
            )
            continue
        apk_path = bundle / app.apk
        if not apk_path.is_file():
            errors.append(f"APK referenced by manifest.{app.key} is missing from the bundle: {app.apk}")
            continue
        # Resolve to an absolute path and PROVE it stays inside the verified
        # bundle root before it can ever back an install command (defense in
        # depth over the filename check above -- catches e.g. a symlink whose
        # target escapes the bundle). Record the absolute path on the app so the
        # installer never rebuilds it from the manifest filename afterwards.
        resolved_apk = apk_path.resolve()
        if not _is_within(resolved_apk, bundle_root):
            errors.append(
                f"manifest.{app.key}.apk {app.apk!r} resolves to {resolved_apk} which is OUTSIDE the "
                f"verified bundle root {bundle_root} -- refusing to treat it as an installable APK."
            )
            continue
        app.apk_path = str(resolved_apk)
        actual = sha256_of_file(apk_path).lower()
        if app.sha256 and actual != app.sha256.lower():
            errors.append(
                f"SHA-256 mismatch for {app.apk}: manifest says {app.sha256.lower()}, file is {actual}."
            )
        listed = checksums.get(app.apk)
        if listed is None:
            errors.append(f"{CHECKSUMS_NAME} has no entry for {app.apk}.")
        elif listed != actual:
            errors.append(
                f"{CHECKSUMS_NAME} disagrees with {app.apk}: file is {actual}, checksum file says {listed}."
            )

    if errors:
        result.status = STATUS_INVALID
        return result

    result.status = STATUS_OK
    result.verified_apps = included
    return result


# ── adb command construction ─────────────────────────────────────────────


def _adb_base(serial: "str | None") -> "list[str]":
    return ["adb"] + (["-s", serial] if serial else [])


@dataclass
class InstallStep:
    """One step of an ordered install plan: a labelled adb command plus what a
    successful outcome should look like. ``argv`` is the exact command a
    caller would run; nothing here executes it."""

    label: str
    purpose: str
    argv: "list[str]"
    expectation: str
    kind: str = "mutate"  # "mutate" | "verify"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "purpose": self.purpose,
            "argv": list(self.argv),
            "expectation": self.expectation,
            "kind": self.kind,
        }


@dataclass
class InstallPlan:
    release_id: "str | None"
    serial: "str | None"
    steps: "list[InstallStep]" = field(default_factory=list)
    notes: "list[str]" = field(default_factory=list)

    def argv_list(self) -> "list[list[str]]":
        return [s.argv for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "releaseId": self.release_id,
            "serial": self.serial,
            "steps": [s.to_dict() for s in self.steps],
            "notes": list(self.notes),
        }


def build_install_command(app: AppRelease, *, serial: "str | None", allow_downgrade: bool) -> "list[str]":
    """The data-preserving reinstall command for one app.

    ``-r`` reinstalls keeping the app's existing data (routine update). ``-d``
    (allow version downgrade) is added ONLY when a downgrade was explicitly
    authorised -- never by default, and never together with an
    uninstall/clear. A signature mismatch is left for the caller to classify
    as BLOCKED; this function never emits a destructive recovery command.

    The APK argument is the ABSOLUTE path recorded on the app during bundle
    verification (``apk_path``), never the manifest-declared filename -- so the
    command works from any working directory and is never reconstructed from
    untrusted manifest input after verification. An app with no verified
    ``apk_path`` cannot yield an install command (only a passed verification
    populates it)."""
    if not app.apk_path:
        raise ReleaseInstallerError(
            f"Refusing to build an install command for {app.key!r}: no verified absolute APK path. "
            f"Install commands must use the path resolved and containment-checked during "
            f"verify_release_bundle, never a filename reconstructed from the manifest."
        )
    cmd = _adb_base(serial) + ["install", "-r"]
    if allow_downgrade:
        cmd.append("-d")
    cmd.append(app.apk_path)
    return cmd


def build_install_plan(
    verification: BundleVerification,
    *,
    serial: "str | None" = None,
    home_component: str = f"{CALEESHELL_PACKAGE_ID}/.ui.LauncherActivity",
    calee_launch_action: str = "com.viso.calee.action.START",
    allow_downgrade: bool = False,
) -> InstallPlan:
    """Construct the ordered install plan from a *passed* verification.

    Order (see docs/RELEASE_INSTALLER.md): install Calee first, CaleeShell
    second (both data-preserving), reassert CaleeShell as HOME, reboot, then
    verify installed versions, package identities, HOME resolution, and the
    Calee launch action. Raises if handed a verification that did not pass --
    an unverified bundle must never produce install commands."""
    if not verification.ok:
        raise ReleaseInstallerError(
            "Refusing to build an install plan from a bundle that did not pass verification."
        )
    manifest = verification.manifest
    plan = InstallPlan(release_id=manifest.release_id if manifest else None, serial=serial)

    calee = verification.app("calee")
    caleeshell = verification.app("caleeShell")

    if allow_downgrade:
        plan.notes.append(
            "Downgrade explicitly authorised: 'adb install -r -d' will be used. A downgrade is "
            "normally BLOCKED -- confirm this was intended."
        )

    # 1-2: install Calee first, CaleeShell second (data-preserving).
    if calee is not None:
        plan.steps.append(
            InstallStep(
                label="install-calee",
                purpose="Install the Calee app first, preserving its existing data.",
                argv=build_install_command(calee, serial=serial, allow_downgrade=allow_downgrade),
                expectation="adb prints 'Success'; no INSTALL_FAILED_* line.",
            )
        )
    if caleeshell is not None:
        plan.steps.append(
            InstallStep(
                label="install-caleeshell",
                purpose="Install the CaleeShell launcher second, preserving its existing data.",
                argv=build_install_command(caleeshell, serial=serial, allow_downgrade=allow_downgrade),
                expectation="adb prints 'Success'; no INSTALL_FAILED_* line.",
            )
        )
    else:
        plan.notes.append("CaleeShell not included in this bundle -- its install/HOME steps are skipped.")

    # 3: reassert CaleeShell as HOME (only meaningful if CaleeShell is present).
    if caleeshell is not None:
        plan.steps.append(
            InstallStep(
                label="set-home",
                purpose="Reassert CaleeShell as the device HOME activity.",
                argv=_adb_base(serial) + ["shell", "cmd", "package", "set-home-activity", home_component],
                expectation="Command returns without error; HOME resolves to CaleeShell (verified below).",
            )
        )

    # 4: reboot.
    plan.steps.append(
        InstallStep(
            label="reboot",
            purpose="Reboot so HOME re-resolution and any first-run migration settle cleanly.",
            argv=_adb_base(serial) + ["reboot"],
            expectation="Device reboots and reconnects (adb wait-for-device below).",
        )
    )
    plan.steps.append(
        InstallStep(
            label="wait-for-device",
            purpose="Wait for the device to reconnect after reboot before verifying.",
            argv=_adb_base(serial) + ["wait-for-device"],
            expectation="adb returns once the device is back online.",
            kind="verify",
        )
    )

    # 5: verify installed versions + identities.
    if calee is not None:
        plan.steps.append(
            InstallStep(
                label="verify-calee-version",
                purpose="Verify the installed Calee versionName/versionCode and package identity.",
                argv=_adb_base(serial) + ["shell", "dumpsys", "package", CALEE_PACKAGE_ID],
                expectation=f"versionName={calee.version_name} versionCode={calee.version_code}.",
                kind="verify",
            )
        )
    if caleeshell is not None:
        plan.steps.append(
            InstallStep(
                label="verify-caleeshell-version",
                purpose="Verify the installed CaleeShell versionName/versionCode and package identity.",
                argv=_adb_base(serial) + ["shell", "dumpsys", "package", CALEESHELL_PACKAGE_ID],
                expectation=f"versionName={caleeshell.version_name} versionCode={caleeshell.version_code}.",
                kind="verify",
            )
        )
        plan.steps.append(
            InstallStep(
                label="verify-home",
                purpose="Verify HOME resolves to CaleeShell.",
                argv=_adb_base(serial)
                + ["shell", "cmd", "package", "resolve-activity", "-c", "android.intent.category.HOME", CALEESHELL_PACKAGE_ID],
                expectation=f"Resolved activity's packageName is {CALEESHELL_PACKAGE_ID}.",
                kind="verify",
            )
        )
    # 6: verify the Calee launch action resolves (the tablet framework uses
    # this action to start Calee, so a release that can't be launched is not
    # installable-complete).
    if calee is not None:
        plan.steps.append(
            InstallStep(
                label="verify-calee-launch",
                purpose="Verify the Calee launch action resolves to the Calee package.",
                argv=_adb_base(serial)
                + ["shell", "cmd", "package", "resolve-activity", "-a", calee_launch_action, CALEE_PACKAGE_ID],
                expectation=f"Resolved activity's packageName is {CALEE_PACKAGE_ID}.",
                kind="verify",
            )
        )
    return plan


# ── adb execution seam + outcome classification ──────────────────────────


@dataclass
class AdbResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# An AdbRunner takes an argv list and returns an AdbResult. The real one runs
# subprocess; tests inject a fake. Kept as a plain Callable so a test can pass
# a lambda or a small class without importing anything from here.
AdbRunner = Callable[["list[str]"], AdbResult]


def real_adb_runner(argv: "list[str]", *, timeout: float = 300.0) -> AdbResult:
    """Run adb for real. Missing binary -> returncode 127 (classified as
    adb_unavailable); timeout/other OS error -> returncode 124. Never raises,
    so the caller always gets a classifiable result."""
    import subprocess

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return AdbResult(returncode=127, stderr="adb executable not found")
    except subprocess.TimeoutExpired:
        return AdbResult(returncode=124, stderr=f"adb command timed out after {timeout}s")
    except OSError as exc:
        return AdbResult(returncode=126, stderr=str(exc))
    return AdbResult(returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


_SIGNATURE_MARKERS = ("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "signatures do not match", "INCONSISTENT_CERTIFICATES")
_DOWNGRADE_MARKERS = ("INSTALL_FAILED_VERSION_DOWNGRADE",)
_ADB_UNAVAILABLE_MARKERS = ("adb executable not found", "command not found", "No such file or directory")
_DEVICE_UNAVAILABLE_MARKERS = (
    "no devices/emulators found",
    "device offline",
    "device unauthorized",
    "device '",  # e.g. "device 'emulator-5554' not found"
    "error: no devices",
    "error: device",
)


def classify_install_output(result: AdbResult) -> str:
    """Classify a single ``adb install`` outcome. A clean success is OUTCOME_OK;
    everything else maps to a specific BLOCKED reason. Signature mismatch and
    downgrade are called out separately so the caller never responds to them
    with a destructive uninstall/clear."""
    combined = f"{result.stdout}\n{result.stderr}"
    lower = combined.lower()
    if result.returncode == 127 or any(m.lower() in lower for m in _ADB_UNAVAILABLE_MARKERS):
        return OUTCOME_ADB_UNAVAILABLE
    if any(m.lower() in lower for m in _DEVICE_UNAVAILABLE_MARKERS):
        return OUTCOME_DEVICE_UNAVAILABLE
    if any(m.lower() in lower for m in _SIGNATURE_MARKERS):
        return OUTCOME_SIGNATURE_MISMATCH
    if any(m.lower() in lower for m in _DOWNGRADE_MARKERS):
        return OUTCOME_DOWNGRADE_BLOCKED
    if result.returncode == 0 and "success" in lower and "failure" not in lower:
        return OUTCOME_OK
    return OUTCOME_INSTALL_FAILED


@dataclass
class InstalledIdentity:
    package_id: str
    present: bool = False
    version_name: "str | None" = None
    version_code: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "packageId": self.package_id,
            "present": self.present,
            "versionName": self.version_name,
            "versionCode": self.version_code,
        }


def parse_installed_identity(package_id: str, dumpsys_output: "str | None") -> InstalledIdentity:
    """Parse ``adb shell dumpsys package <pkg>`` output into an installed
    identity. Absent package (empty output / no versionName) -> present=False."""
    version_name = parse_dumpsys_version_name(dumpsys_output)
    version_code = parse_dumpsys_version_code(dumpsys_output)
    return InstalledIdentity(
        package_id=package_id,
        present=bool(version_name),
        version_name=version_name,
        version_code=version_code,
    )


def classify_version_match(expected: AppRelease, installed: InstalledIdentity) -> str:
    """OUTCOME_OK when the installed versionName+versionCode match the manifest,
    else OUTCOME_VERSION_MISMATCH. A not-present package is a mismatch (the
    install didn't take)."""
    if not installed.present:
        return OUTCOME_VERSION_MISMATCH
    if installed.version_name != expected.version_name:
        return OUTCOME_VERSION_MISMATCH
    if str(installed.version_code) != str(expected.version_code):
        return OUTCOME_VERSION_MISMATCH
    return OUTCOME_OK


def parse_resolved_package(resolve_output: "str | None") -> "str | None":
    """Extract the packageName from ``cmd package resolve-activity`` output.

    The output includes a line like ``packageName=com.viso.caleeshell`` (in
    the ActivityInfo block) or a ``name=com.viso.caleeshell/.Foo`` component
    line -- accept either form."""
    if not resolve_output:
        return None
    m = re.search(r"packageName=(\S+)", resolve_output)
    if m:
        return m.group(1)
    m = re.search(r"name=([A-Za-z0-9._]+)/", resolve_output)
    if m:
        return m.group(1)
    return None


def classify_home_resolution(expected_package: str, resolve_output: "str | None") -> str:
    """OUTCOME_OK when HOME resolves to ``expected_package``, else
    OUTCOME_HOME_MISMATCH."""
    resolved = parse_resolved_package(resolve_output)
    return OUTCOME_OK if resolved == expected_package else OUTCOME_HOME_MISMATCH


@dataclass
class StepOutcome:
    label: str
    kind: str
    argv: "list[str]"
    outcome: str
    returncode: "int | None" = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "kind": self.kind,
            "argv": list(self.argv),
            "outcome": self.outcome,
            "returncode": self.returncode,
            "detail": self.detail,
        }


@dataclass
class InstallExecution:
    status: str  # STATUS_OK | STATUS_BLOCKED
    release_id: "str | None" = None
    serial: "str | None" = None
    steps: "list[StepOutcome]" = field(default_factory=list)
    installed: "list[InstalledIdentity]" = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "releaseId": self.release_id,
            "serial": self.serial,
            "steps": [s.to_dict() for s in self.steps],
            "installed": [i.to_dict() for i in self.installed],
            "detail": self.detail,
        }


# Non-OK outcomes that must halt the plan immediately (never continue an
# install sequence once trust is broken or the device is gone).
_HALTING_OUTCOMES = {
    OUTCOME_ADB_UNAVAILABLE,
    OUTCOME_DEVICE_UNAVAILABLE,
    OUTCOME_SIGNATURE_MISMATCH,
    OUTCOME_DOWNGRADE_BLOCKED,
    OUTCOME_INSTALL_FAILED,
}

_BLOCKING_DETAIL = {
    OUTCOME_ADB_UNAVAILABLE: "adb is not available -- install/inspect cannot run. Install Android platform-tools.",
    OUTCOME_DEVICE_UNAVAILABLE: "No usable device -- connect and authorise the Calee tablet, then retry.",
    OUTCOME_SIGNATURE_MISMATCH: (
        "Installed signature does not match the release APK. This BLOCKS -- the installer never "
        "auto-uninstalls or clears data to 'fix' a signature mismatch. A technical owner must "
        "resolve the signing mismatch deliberately."
    ),
    OUTCOME_DOWNGRADE_BLOCKED: "The bundle is older than what's installed. A downgrade BLOCKS unless explicitly authorised.",
    OUTCOME_VERSION_MISMATCH: "The installed version does not match the manifest after install -- BLOCKED.",
    OUTCOME_HOME_MISMATCH: "HOME does not resolve to CaleeShell after install -- BLOCKED.",
    OUTCOME_INSTALL_FAILED: "adb reported an install failure -- BLOCKED (see step detail).",
}


def execute_install_plan(
    plan: InstallPlan,
    verification: BundleVerification,
    runner: AdbRunner,
    *,
    calee_launch_action: str = "com.viso.calee.action.START",
) -> InstallExecution:
    """Run an install plan through an injected ``runner`` and classify the
    outcome of every step. Never claims success it can't prove: a device/adb
    problem, a signature mismatch, or a post-install version/HOME mismatch each
    make the whole execution ``blocked``. This is the seam the CLI wraps; tests
    drive it with a fake runner and no device.

    With no real device attached, the very first adb command returns a
    device-unavailable result and the execution halts as ``blocked`` -- exactly
    the honest offline outcome, never a fabricated install."""
    execution = InstallExecution(status=STATUS_OK, release_id=plan.release_id, serial=plan.serial)
    calee = verification.app("calee")
    caleeshell = verification.app("caleeShell")

    for step in plan.steps:
        result = runner(step.argv)
        if step.kind == "verify":
            outcome, detail = _classify_verify_step(step, result, calee, caleeshell, execution)
        else:
            outcome = classify_install_output(result)
            detail = _BLOCKING_DETAIL.get(outcome, "") if outcome != OUTCOME_OK else ""
        execution.steps.append(
            StepOutcome(
                label=step.label, kind=step.kind, argv=step.argv, outcome=outcome,
                returncode=result.returncode, detail=detail or (result.stderr or "")[:400],
            )
        )
        if outcome != OUTCOME_OK:
            execution.status = STATUS_BLOCKED
            execution.detail = _BLOCKING_DETAIL.get(outcome, f"Step {step.label!r} did not succeed ({outcome}).")
            if outcome in _HALTING_OUTCOMES:
                break
    return execution


def _classify_verify_step(step, result, calee, caleeshell, execution) -> "tuple[str, str]":
    """Classify a verify-kind step's adb output. Records parsed installed
    identities on ``execution`` as a side effect so the report shows exactly
    what was on the device."""
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode == 127 or "adb executable not found" in combined:
        return OUTCOME_ADB_UNAVAILABLE, _BLOCKING_DETAIL[OUTCOME_ADB_UNAVAILABLE]
    if any(m.lower() in combined for m in _DEVICE_UNAVAILABLE_MARKERS):
        return OUTCOME_DEVICE_UNAVAILABLE, _BLOCKING_DETAIL[OUTCOME_DEVICE_UNAVAILABLE]

    if step.label == "wait-for-device":
        return (OUTCOME_OK, "") if result.returncode == 0 else (OUTCOME_DEVICE_UNAVAILABLE, _BLOCKING_DETAIL[OUTCOME_DEVICE_UNAVAILABLE])
    if step.label == "verify-calee-version" and calee is not None:
        ident = parse_installed_identity(CALEE_PACKAGE_ID, result.stdout)
        execution.installed.append(ident)
        outcome = classify_version_match(calee, ident)
        return outcome, _BLOCKING_DETAIL.get(outcome, "")
    if step.label == "verify-caleeshell-version" and caleeshell is not None:
        ident = parse_installed_identity(CALEESHELL_PACKAGE_ID, result.stdout)
        execution.installed.append(ident)
        outcome = classify_version_match(caleeshell, ident)
        return outcome, _BLOCKING_DETAIL.get(outcome, "")
    if step.label == "verify-home":
        outcome = classify_home_resolution(CALEESHELL_PACKAGE_ID, result.stdout)
        return outcome, _BLOCKING_DETAIL.get(outcome, "")
    if step.label == "verify-calee-launch":
        outcome = classify_home_resolution(CALEE_PACKAGE_ID, result.stdout)
        # reuse the resolved-package check; a non-Calee resolution is a launch problem
        return (OUTCOME_OK, "") if outcome == OUTCOME_OK else (OUTCOME_INSTALL_FAILED, "Calee launch action did not resolve to the Calee package -- BLOCKED.")
    return OUTCOME_OK, ""


@dataclass
class TabletInspection:
    status: str  # STATUS_OK | STATUS_BLOCKED
    serial: "str | None" = None
    adb_available: bool = False
    device_present: bool = False
    installed: "list[InstalledIdentity]" = field(default_factory=list)
    home_package: "str | None" = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "serial": self.serial,
            "adbAvailable": self.adb_available,
            "devicePresent": self.device_present,
            "installed": [i.to_dict() for i in self.installed],
            "homePackage": self.home_package,
            "detail": self.detail,
        }


def inspect_tablet(runner: AdbRunner, *, serial: "str | None" = None) -> TabletInspection:
    """Read-only inspection of the tablet: adb availability, device presence,
    installed Calee/CaleeShell identities, and the resolved HOME package. Uses
    only read-only adb commands (never installs or mutates). With no device,
    returns ``blocked`` honestly."""
    inspection = TabletInspection(status=STATUS_OK, serial=serial)
    # `adb get-state` is the cheapest presence/availability probe.
    state = runner(_adb_base(serial) + ["get-state"])
    combined = f"{state.stdout}\n{state.stderr}".lower()
    if state.returncode == 127 or "adb executable not found" in combined:
        inspection.status = STATUS_BLOCKED
        inspection.detail = _BLOCKING_DETAIL[OUTCOME_ADB_UNAVAILABLE]
        return inspection
    inspection.adb_available = True
    if state.returncode != 0 or "device" not in combined:
        inspection.status = STATUS_BLOCKED
        inspection.device_present = False
        inspection.detail = _BLOCKING_DETAIL[OUTCOME_DEVICE_UNAVAILABLE]
        return inspection
    inspection.device_present = True

    for pkg in (CALEE_PACKAGE_ID, CALEESHELL_PACKAGE_ID):
        out = runner(_adb_base(serial) + ["shell", "dumpsys", "package", pkg])
        inspection.installed.append(parse_installed_identity(pkg, out.stdout))
    home = runner(_adb_base(serial) + ["shell", "cmd", "package", "resolve-activity", "-c", "android.intent.category.HOME"])
    inspection.home_package = parse_resolved_package(home.stdout)
    return inspection


# ── complete tablet-solution verification (Priority 2) ─────────────────────
#
# After ANY release -- Calee-only, CaleeShell-only, or both -- the WHOLE
# installed solution must be verified after reboot, not just the app(s) this
# release replaced. An unchanged app is not "ignored": it still has an expected
# installed identity that must hold. For each of Calee and CaleeShell we verify:
#   * the package is installed,
#   * the installed versionName/versionCode match the expected identity,
#   * the installed signer matches the expected trusted signer,
# plus role checks: Calee's custom START action resolves to Calee, and
# CaleeShell is the HOME launcher. Any failed check on EITHER app BLOCKS.

CHECK_OK = STATUS_OK
CHECK_BLOCKED = STATUS_BLOCKED
CHECK_NOT_COMPARED = "not_compared"


@dataclass
class SolutionCheck:
    app: str      # "calee" | "caleeShell"
    check: str    # "present" | "version" | "signer" | "launch-action" | "home"
    status: str   # CHECK_OK | CHECK_BLOCKED | CHECK_NOT_COMPARED
    detail: str = ""

    def to_dict(self) -> dict:
        return {"app": self.app, "check": self.check, "status": self.status, "detail": self.detail}


@dataclass
class SolutionVerification:
    status: str  # STATUS_OK | STATUS_BLOCKED
    release_id: "str | None" = None
    serial: "str | None" = None
    checks: "list[SolutionCheck]" = field(default_factory=list)
    installed: "list[InstalledIdentity]" = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "releaseId": self.release_id,
            "serial": self.serial,
            "checks": [c.to_dict() for c in self.checks],
            "installed": [i.to_dict() for i in self.installed],
            "detail": self.detail,
        }


def _check_installed_signer(app: AppRelease, reader, *, signer_trust_required: bool = False) -> SolutionCheck:
    """Compare the app's currently-installed signer against its expected trusted
    signer.

    ``signer_trust_required`` (Priority 2) is True for a release-gating
    production or staging run: there, a missing expected digest or a missing
    reader can no longer resolve to a silently-tolerated ``not_compared`` --
    trusted signer identity is a REQUIRED part of release qualification, so
    both become BLOCKED instead. Only a non-release-gating development run
    (``signer_trust_required=False``, the default) may still record
    ``not_compared`` for these two cases. An unreadable installed signer or an
    actual mismatch is ALWAYS BLOCKED, regardless of this flag -- that is a
    real trust failure, never something a profile can downgrade."""
    if not app.signer_sha256:
        if signer_trust_required:
            return SolutionCheck(app.key, "signer", CHECK_BLOCKED,
                                 "No expected signerSha256 declared -- trusted signer identity is REQUIRED for "
                                 "this release qualification and cannot be established. BLOCKED.")
        return SolutionCheck(app.key, "signer", CHECK_NOT_COMPARED,
                             "No expected signerSha256 declared -- installed-signer trust not verified.")
    if reader is None:
        if signer_trust_required:
            return SolutionCheck(app.key, "signer", CHECK_BLOCKED,
                                 "No installed-signer reader available -- trusted signer identity is REQUIRED for "
                                 "this release qualification and cannot be established. BLOCKED.")
        return SolutionCheck(app.key, "signer", CHECK_NOT_COMPARED,
                             "No installed-signer reader supplied -- installed-signer trust not verified.")
    read = reader(app.package_id)
    digest = getattr(read, "digest", None)
    detail = getattr(read, "detail", "") or ""
    if digest and digest.lower() == app.signer_sha256.lower():
        return SolutionCheck(app.key, "signer", CHECK_OK,
                             "Installed signer matches the expected trusted signer.")
    if not digest:
        return SolutionCheck(app.key, "signer", CHECK_BLOCKED,
                             f"Could not read the installed {app.key} signer to verify trust -- BLOCKED. {detail}".strip())
    return SolutionCheck(app.key, "signer", CHECK_BLOCKED,
                         f"Installed {app.key} signer {digest} != expected trusted signer "
                         f"{app.signer_sha256} -- BLOCKED. The installer never wipes data to work around it.")


def verify_tablet_solution(
    calee: "AppRelease | None",
    caleeshell: "AppRelease | None",
    runner: AdbRunner,
    *,
    serial: "str | None" = None,
    release_id: "str | None" = None,
    installed_signer_reader=None,
    calee_launch_action: str = "com.viso.calee.action.START",
    signer_trust_required: bool = False,
) -> SolutionVerification:
    """Verify the COMPLETE installed Calee tablet solution after a release.

    ``calee`` and ``caleeshell`` are the EXPECTED INSTALLED identities of both
    apps (from ``verification.expected_app(...)``) -- present whether or not this
    release installed each one. BOTH are always checked. Uses only read-only adb
    commands and an optional injected signer reader, so it is fully offline-
    testable. With no device, returns ``blocked`` honestly.

    A missing expected identity for either app is itself a BLOCK: a release must
    declare what the tablet should carry for both Calee and CaleeShell.

    ``signer_trust_required`` (Priority 2) is the release-gating signer policy:
    True for a production release, or any release-gating staging run -- there, a
    missing/unreadable/mismatching signer for EITHER app BLOCKS, closing the gap
    where an absent ``signerSha256`` let a release qualify with signer trust
    merely ``not_compared``. False (the default) is for non-release-gating
    development/diagnostic use, where ``not_compared`` may still be recorded."""
    result = SolutionVerification(status=STATUS_OK, release_id=release_id, serial=serial)

    if calee is None or not calee.has_expected:
        result.checks.append(SolutionCheck("calee", "expected-identity", CHECK_BLOCKED,
                                            "No expected Calee identity declared -- cannot verify the solution."))
    if caleeshell is None or not caleeshell.has_expected:
        result.checks.append(SolutionCheck("caleeShell", "expected-identity", CHECK_BLOCKED,
                                            "No expected CaleeShell identity declared -- cannot verify the solution."))

    # Device presence gate: without a device nothing can be verified.
    state = runner(_adb_base(serial) + ["get-state"])
    combined = f"{state.stdout}\n{state.stderr}".lower()
    if state.returncode == 127 or "adb executable not found" in combined:
        result.status = STATUS_BLOCKED
        result.detail = _BLOCKING_DETAIL[OUTCOME_ADB_UNAVAILABLE]
        return result
    if state.returncode != 0 or "device" not in combined:
        result.status = STATUS_BLOCKED
        result.detail = _BLOCKING_DETAIL[OUTCOME_DEVICE_UNAVAILABLE]
        return result

    def _verify_app(app: "AppRelease | None", pkg: str):
        if app is None or not app.has_expected:
            return  # already recorded as a blocking missing-identity check above
        out = runner(_adb_base(serial) + ["shell", "dumpsys", "package", pkg])
        ident = parse_installed_identity(pkg, out.stdout)
        result.installed.append(ident)
        if not ident.present:
            result.checks.append(SolutionCheck(app.key, "present", CHECK_BLOCKED,
                                                f"{pkg} is NOT installed on the tablet -- the complete solution is "
                                                f"broken even though this release may not have touched it."))
        else:
            result.checks.append(SolutionCheck(app.key, "present", CHECK_OK, f"{pkg} is installed."))
            vm = classify_version_match(app, ident)
            if vm == OUTCOME_OK:
                result.checks.append(SolutionCheck(app.key, "version", CHECK_OK,
                                                    f"Installed {app.version_name}/{app.version_code} matches expected."))
            else:
                result.checks.append(SolutionCheck(app.key, "version", CHECK_BLOCKED,
                                                    f"Installed {ident.version_name}/{ident.version_code} != expected "
                                                    f"{app.version_name}/{app.version_code} -- BLOCKED."))
        result.checks.append(_check_installed_signer(
            app, installed_signer_reader, signer_trust_required=signer_trust_required,
        ))

    _verify_app(calee, CALEE_PACKAGE_ID)
    _verify_app(caleeshell, CALEESHELL_PACKAGE_ID)

    # Role checks. Calee's custom START action must resolve to Calee.
    if calee is not None and calee.has_expected:
        launch = runner(_adb_base(serial) + ["shell", "cmd", "package", "resolve-activity", "-a", calee_launch_action, CALEE_PACKAGE_ID])
        if parse_resolved_package(launch.stdout) == CALEE_PACKAGE_ID:
            result.checks.append(SolutionCheck("calee", "launch-action", CHECK_OK,
                                               f"Calee START action {calee_launch_action} resolves to Calee."))
        else:
            result.checks.append(SolutionCheck("calee", "launch-action", CHECK_BLOCKED,
                                               f"Calee START action {calee_launch_action} does NOT resolve to "
                                               f"{CALEE_PACKAGE_ID} -- BLOCKED."))
    # CaleeShell must be the HOME launcher.
    if caleeshell is not None and caleeshell.has_expected:
        home = runner(_adb_base(serial) + ["shell", "cmd", "package", "resolve-activity", "-c", "android.intent.category.HOME", CALEESHELL_PACKAGE_ID])
        if parse_resolved_package(home.stdout) == CALEESHELL_PACKAGE_ID:
            result.checks.append(SolutionCheck("caleeShell", "home", CHECK_OK, "CaleeShell is the HOME launcher."))
        else:
            result.checks.append(SolutionCheck("caleeShell", "home", CHECK_BLOCKED,
                                               "CaleeShell is NOT the HOME launcher -- BLOCKED."))

    blocking = [c for c in result.checks if c.status == CHECK_BLOCKED]
    if blocking:
        result.status = STATUS_BLOCKED
        result.detail = "; ".join(f"{c.app}/{c.check}: {c.detail}" for c in blocking)
    return result


def decide_downgrade(current_version_code: "int | str | None", target_version_code: "int | str | None", *, allow_downgrade: bool) -> str:
    """Compare a target install against what's currently installed. A strictly
    lower target versionCode is a downgrade -- OUTCOME_DOWNGRADE_BLOCKED unless
    explicitly authorised. Unknown current version (nothing installed) is never
    a downgrade. Non-numeric codes are treated as unknown (not a downgrade),
    since a real device always reports a numeric versionCode."""
    try:
        target = int(str(target_version_code))
    except (TypeError, ValueError):
        return OUTCOME_OK
    try:
        current = int(str(current_version_code))
    except (TypeError, ValueError):
        return OUTCOME_OK
    if target < current and not allow_downgrade:
        return OUTCOME_DOWNGRADE_BLOCKED
    return OUTCOME_OK
