"""Actual APK content + signer inspection before install (Priority 5).

Bundle verification (release_installer.verify_release_bundle) trusts what the
release *manifest declares* plus the file hash. That is not enough for a
release: a manifest can declare ``com.viso.calee`` / ``founder-v0.3.25`` next to
an APK that actually contains a different application id, a different version,
or is signed by a different key. This module reads what the APK *actually*
contains, using the real Android SDK tools that ship with a macOS Android
setup, and records the answers so the installer can refuse a mismatch BEFORE
any ``adb install`` runs.

What it records per APK (see ApkInspection):

  * actual Android application id      (aapt2 / apkanalyzer)
  * actual version name                (aapt2 / apkanalyzer)
  * actual version code                (aapt2 / apkanalyzer)
  * signing certificate SHA-256 digest (apksigner)
  * APK file SHA-256                    (hashlib -- what was actually read)
  * the release-manifest-declared Git SHA (recorded for traceability; the
    Calee/CaleeShell APKs do not embed a Git SHA in their manifest, so this is
    NOT invented from the APK -- it is the declared identity carried alongside
    the actual inspection).

Design constraints:

  * **Offline-testable.** Every external tool goes through an injected runner
    (``ToolRunner``) and every filesystem discovery through an injected
    ``which``; tests pass fixture outputs and no real signed APK is required.
  * **Tool absence BLOCKS with guidance.** A missing apkanalyzer/aapt2/apksigner
    is a setup problem (BLOCKED), never a silent skip and never a product FAIL.
  * **A mismatch never triggers a destructive recovery.** This module only
    inspects and classifies; it never uninstalls or clears data. The installer
    consumes its verdict.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .release_installer import sha256_of_file

# Inspection status vocabulary, aligned with release_installer's model.
STATUS_OK = "ok"
STATUS_INVALID = "invalid"   # the APK's actual contents contradict the manifest
STATUS_BLOCKED = "blocked"   # a tool/device problem prevented inspection

# Signer-comparison classifications.
SIGNER_OK = "ok"
SIGNER_MISMATCH = "mismatch"
SIGNER_UNKNOWN = "unknown"           # could not read the installed signer
SIGNER_NOT_INSTALLED = "not_installed"  # nothing installed to compare against

_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")

# Markers that mean ``pm path`` could not authoritatively answer (a package
# manager crash/unavailability), as opposed to a clean "no such package". When
# any of these appear, an empty ``pm path`` result must be read as
# SIGNER_UNKNOWN (may be installed, unreadable) -- never as "not installed".
_PM_FAILURE_RE = re.compile(
    r"error|exception|could not access|package manager|failure|killed|not running|"
    r"can't find service|securityexception|permission den" ,
    re.IGNORECASE,
)


@dataclass
class ToolResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# A ToolRunner takes an argv list and returns a ToolResult. The real one runs
# subprocess; tests inject a fake.
ToolRunner = Callable[["list[str]"], ToolResult]


def real_tool_runner(argv: "list[str]", *, timeout: float = 120.0) -> ToolResult:
    """Run an SDK tool for real. A missing binary -> returncode 127; a
    timeout -> 124; any other OS error -> 126. Never raises, so the caller
    always gets a classifiable result."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return ToolResult(returncode=127, stderr=f"{argv[0]} not found")
    except subprocess.TimeoutExpired:
        return ToolResult(returncode=124, stderr=f"{argv[0]} timed out after {timeout}s")
    except OSError as exc:
        return ToolResult(returncode=126, stderr=str(exc))
    return ToolResult(returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


_SETUP_GUIDANCE = (
    "Install the Android SDK build-tools/command-line-tools and put apkanalyzer (or aapt2) and "
    "apksigner on PATH (e.g. via Android Studio's SDK Manager, or `sdkmanager \"build-tools;34.0.0\" "
    "\"cmdline-tools;latest\"`), then re-run. APK content/signer inspection is a hard release gate: "
    "it is BLOCKED, never skipped, when the tools are absent."
)


# ── parsers (pure) ────────────────────────────────────────────────────────


def parse_aapt2_badging(stdout: str) -> "tuple[str | None, str | None, str | None]":
    """Parse ``aapt2 dump badging`` output's ``package:`` line into
    ``(applicationId, versionCode, versionName)`` -- any of which may be None if
    absent."""
    m = re.search(r"^package:\s*(.*)$", stdout, re.MULTILINE)
    if not m:
        return None, None, None
    line = m.group(1)

    def _field(key: str) -> "str | None":
        fm = re.search(rf"{key}='([^']*)'", line)
        return fm.group(1) if fm else None

    return _field("name"), _field("versionCode"), _field("versionName")


def parse_apksigner_certs(stdout: str) -> "str | None":
    """Extract the FIRST signer's certificate SHA-256 digest from
    ``apksigner verify --print-certs`` output. Accepts the canonical
    ``Signer #1 certificate SHA-256 digest: <hex>`` line (case/spacing
    tolerant)."""
    m = re.search(
        r"certificate SHA-?256 digest:\s*([0-9a-fA-F]{64})",
        stdout,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).lower()
    return None


def parse_pm_path(stdout: str) -> "str | None":
    """Parse ``adb shell pm path <pkg>`` output (``package:/data/app/.../base.apk``)
    into the on-device APK path (the first ``package:`` line)."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("package:"):
            path = line[len("package:"):].strip()
            if path:
                return path
    return None


# ── APK inspection ────────────────────────────────────────────────────────


@dataclass
class ApkInspection:
    key: str                       # "calee" | "caleeShell"
    apk_path: str
    status: str = STATUS_BLOCKED
    application_id: "str | None" = None
    version_name: "str | None" = None
    version_code: "str | None" = None
    signer_sha256: "str | None" = None
    apk_sha256: "str | None" = None
    manifest_git_sha: "str | None" = None
    tool_used: "str | None" = None
    detail: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "apkPath": self.apk_path,
            "status": self.status,
            "applicationId": self.application_id,
            "versionName": self.version_name,
            "versionCode": self.version_code,
            "signerSha256": self.signer_sha256,
            "apkSha256": self.apk_sha256,
            "manifestGitSha": self.manifest_git_sha,
            "toolUsed": self.tool_used,
            "detail": list(self.detail),
        }


def _read_identity(
    apk_path: Path, *, which: "Callable[[str], Optional[str]]", runner: ToolRunner,
) -> "tuple[str | None, tuple[str | None, str | None, str | None], list[str]]":
    """Return ``(tool_used, (appId, versionCode, versionName), problems)`` using
    apkanalyzer first, then aapt2. A missing tool is reported as a problem, not
    a crash."""
    problems: "list[str]" = []

    apkanalyzer = which("apkanalyzer")
    if apkanalyzer:
        app_id = _one_line(runner([apkanalyzer, "manifest", "application-id", str(apk_path)]))
        version_code = _one_line(runner([apkanalyzer, "manifest", "version-code", str(apk_path)]))
        version_name = _one_line(runner([apkanalyzer, "manifest", "version-name", str(apk_path)]))
        if app_id or version_code or version_name:
            return "apkanalyzer", (app_id, version_code, version_name), problems
        problems.append("apkanalyzer produced no manifest identity output.")

    aapt2 = which("aapt2")
    if aapt2:
        res = runner([aapt2, "dump", "badging", str(apk_path)])
        if res.returncode == 0:
            app_id, version_code, version_name = parse_aapt2_badging(res.stdout)
            if app_id or version_code or version_name:
                return "aapt2", (app_id, version_code, version_name), problems
            problems.append("aapt2 dump badging produced no package identity.")
        else:
            problems.append(f"aapt2 dump badging exited {res.returncode}: {(res.stderr or '').strip()[:200]}")

    if not apkanalyzer and not aapt2:
        problems.append("Neither apkanalyzer nor aapt2 is on PATH -- cannot read the APK's actual identity.")
    return None, (None, None, None), problems


def _one_line(res: ToolResult) -> "str | None":
    if res.returncode != 0:
        return None
    text = (res.stdout or "").strip()
    return text.splitlines()[0].strip() if text else None


def inspect_apk(
    apk_path: "Path | str",
    key: str,
    *,
    manifest_git_sha: "str | None" = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    runner: ToolRunner = real_tool_runner,
) -> ApkInspection:
    """Inspect ONE APK's actual identity + signer. Never raises; a missing tool
    or unreadable APK is reported as BLOCKED with setup guidance. The APK file
    SHA-256 is always computed (that is what was actually read)."""
    apk = Path(apk_path)
    inspection = ApkInspection(key=key, apk_path=str(apk), manifest_git_sha=manifest_git_sha)

    if not apk.is_file():
        inspection.status = STATUS_BLOCKED
        inspection.detail.append(f"APK not found: {apk}")
        return inspection

    inspection.apk_sha256 = sha256_of_file(apk).lower()

    tool_used, (app_id, version_code, version_name), id_problems = _read_identity(
        apk, which=which, runner=runner
    )
    inspection.tool_used = tool_used
    inspection.application_id = app_id
    inspection.version_code = version_code
    inspection.version_name = version_name

    apksigner = which("apksigner")
    signer_problems: "list[str]" = []
    if apksigner:
        res = runner([apksigner, "verify", "--print-certs", str(apk)])
        digest = parse_apksigner_certs(res.stdout)
        if digest is None and res.returncode != 0:
            signer_problems.append(
                f"apksigner verify exited {res.returncode}: {(res.stderr or '').strip()[:200]}"
            )
        elif digest is None:
            signer_problems.append("apksigner did not report a certificate SHA-256 digest.")
        inspection.signer_sha256 = digest
    else:
        signer_problems.append("apksigner is not on PATH -- cannot read the APK's signing certificate.")

    problems = id_problems + signer_problems
    if problems:
        inspection.status = STATUS_BLOCKED
        inspection.detail.extend(problems)
        inspection.detail.append(_SETUP_GUIDANCE)
        return inspection

    inspection.status = STATUS_OK
    return inspection


def verify_identity_matches(
    inspection: ApkInspection, *, expected_package: str, manifest_version_name: "str | None",
    manifest_version_code: "int | str | None",
) -> "list[str]":
    """Cross-check an OK inspection's ACTUAL identity against the manifest and
    the expected canonical package. Returns problems (empty == matches). A
    package/version mismatch is a hard INVALID (a substituted or mislabelled
    APK), never a product FAIL."""
    problems: "list[str]" = []
    if inspection.application_id != expected_package:
        problems.append(
            f"{inspection.key}: actual APK applicationId {inspection.application_id!r} != expected "
            f"{expected_package!r} -- refusing to install a mislabelled/substituted APK."
        )
    if manifest_version_name is not None and inspection.version_name != manifest_version_name:
        problems.append(
            f"{inspection.key}: actual APK versionName {inspection.version_name!r} != manifest "
            f"{manifest_version_name!r}."
        )
    if manifest_version_code is not None and str(inspection.version_code) != str(manifest_version_code):
        problems.append(
            f"{inspection.key}: actual APK versionCode {inspection.version_code!r} != manifest "
            f"{manifest_version_code!r}."
        )
    return problems


# ── installed-signer comparison ───────────────────────────────────────────


@dataclass
class SignerReadResult:
    status: str  # SIGNER_OK reader states are only ok/unknown/not_installed here
    digest: "str | None" = None
    detail: str = ""


def classify_signer(release_digest: "str | None", installed: SignerReadResult) -> "tuple[str, str]":
    """Compare the release APK signer with the installed app's signer.

    Returns ``(classification, detail)``. When nothing is installed, this is
    SIGNER_NOT_INSTALLED (a first-time install, nothing to compare). When the
    installed signer could not be read, SIGNER_UNKNOWN (the installer treats
    that as BLOCKED -- it cannot prove the same signer). A read digest that
    differs from the release digest is SIGNER_MISMATCH, which BLOCKS
    installation and must NEVER be 'fixed' by uninstalling/clearing data."""
    if installed.status == SIGNER_NOT_INSTALLED:
        return SIGNER_NOT_INSTALLED, "App not currently installed -- first-time install, no signer to compare."
    if installed.status == SIGNER_UNKNOWN or not installed.digest:
        return SIGNER_UNKNOWN, installed.detail or "Could not read the installed app's signing certificate."
    if not release_digest:
        return SIGNER_UNKNOWN, "Release APK signer digest is unknown -- cannot compare."
    if installed.digest.lower() == release_digest.lower():
        return SIGNER_OK, "Installed signer matches the release APK signer."
    return (
        SIGNER_MISMATCH,
        f"Installed signer {installed.digest} != release signer {release_digest} -- BLOCKED. "
        f"The installer never auto-uninstalls or clears data to work around a signature mismatch; "
        f"a technical owner must resolve the signing difference deliberately.",
    )


# A callable that pulls a device path to a local file. Default runs `adb pull`;
# injectable so tests avoid a device/filesystem.
PullFn = Callable[[str, str], ToolResult]


def read_installed_signer(
    package_id: str,
    *,
    serial: "str | None" = None,
    adb_runner: "Callable[[list[str]], ToolResult]",
    apksigner_runner: ToolRunner,
    pull: "PullFn | None" = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    work_dir: "Path | str | None" = None,
    retain_diagnostics: bool = False,
) -> SignerReadResult:
    """Best-effort read of the CURRENTLY INSTALLED app's signing certificate,
    for comparison with the release APK signer.

    Steps (all through injected seams, so this is offline-testable):
      1. ``adb shell pm path <pkg>`` -> the on-device APK path. A *clean* empty
         answer means the app is not installed -> SIGNER_NOT_INSTALLED; a device
         error (offline/unauthorized/no device) or a package-manager failure
         means we cannot tell -> SIGNER_UNKNOWN (the installer BLOCKS: the app
         may be installed with a conflicting signer we simply could not read);
      2. ``adb pull`` that path to a local temp file (pull failure -> UNKNOWN);
      3. ``apksigner verify --print-certs`` on the pulled APK -> the digest
         (missing apksigner / unreadable APK / no digest -> UNKNOWN).

    The pulled APK and any temporary workspace are deleted after the read unless
    ``retain_diagnostics`` is set (a caller-supplied ``work_dir`` is always left
    to its owner). Never mutates the device (read-only pm path + pull). Never
    raises."""
    base = ["adb"] + (["-s", serial] if serial else [])
    path_res = adb_runner(base + ["shell", "pm", "path", package_id])
    combined = f"{path_res.stdout}\n{path_res.stderr}".lower()
    if path_res.returncode == 127 or "not found" in combined and "adb" in combined:
        return SignerReadResult(SIGNER_UNKNOWN, detail="adb is not available -- cannot read the installed signer.")
    if any(m in combined for m in ("no devices", "device offline", "device unauthorized", "error: device")):
        return SignerReadResult(SIGNER_UNKNOWN, detail="No usable device -- cannot read the installed signer.")

    device_path = parse_pm_path(path_res.stdout)
    if not device_path:
        # No 'package:' path came back. Distinguish a GENUINE not-installed
        # (pm answered cleanly, nothing to report) from a package-manager
        # FAILURE (pm crashed / was unreachable / denied). The latter must
        # NEVER be read as "not installed" -- the package may already be
        # installed and its signer would then go unverified. A pm failure is
        # SIGNER_UNKNOWN, which BLOCKS installation.
        combined_out = f"{path_res.stdout}\n{path_res.stderr}"
        pm_failed = bool(
            (path_res.returncode != 0 and (path_res.stderr or "").strip())
            or _PM_FAILURE_RE.search(combined_out)
        )
        if pm_failed:
            return SignerReadResult(
                SIGNER_UNKNOWN,
                detail=(
                    f"pm path could not authoritatively report whether {package_id} is "
                    f"installed -- refusing to assume it is absent: "
                    f"{((path_res.stderr or path_res.stdout) or '').strip()[:200]}"
                ),
            )
        return SignerReadResult(SIGNER_NOT_INSTALLED, detail=f"{package_id} is not installed on the device.")

    apksigner = which("apksigner")
    if not apksigner:
        return SignerReadResult(SIGNER_UNKNOWN, detail="apksigner is not on PATH -- cannot read the installed signer.")

    import tempfile

    created_temp = work_dir is None
    out_dir = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="installed-apk-"))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SignerReadResult(SIGNER_UNKNOWN, detail=f"Could not create a workspace to pull the installed APK: {exc}")
    local = out_dir / f"{package_id}.installed.apk"

    try:
        pull_fn = pull or (lambda dev, dest: adb_runner(base + ["pull", dev, dest]))
        pull_res = pull_fn(device_path, str(local))
        if pull_res.returncode != 0:
            return SignerReadResult(
                SIGNER_UNKNOWN,
                detail=f"Could not pull the installed APK ({device_path}) to compare its signer: "
                       f"{(pull_res.stderr or '').strip()[:200]}",
            )

        certs = apksigner_runner([apksigner, "verify", "--print-certs", str(local)])
        digest = parse_apksigner_certs(certs.stdout)
        if not digest:
            return SignerReadResult(
                SIGNER_UNKNOWN,
                detail="apksigner did not report a SHA-256 digest for the installed APK.",
            )
        return SignerReadResult(SIGNER_OK, digest=digest, detail="Read the installed signer certificate.")
    finally:
        # Remove the pulled APK and our temp workspace after the read, unless a
        # diagnostic retention was explicitly requested. A caller-supplied
        # work_dir is left to its owner. Cleanup never changes the read result.
        if created_temp and not retain_diagnostics:
            shutil.rmtree(out_dir, ignore_errors=True)


# ── whole-bundle pre-install inspection (the release gate) ─────────────────


# Canonical expected package per manifest key (mirrors release_installer).
from .release_installer import CALEE_PACKAGE_ID, CALEESHELL_PACKAGE_ID  # noqa: E402

_EXPECTED_PACKAGE = {"calee": CALEE_PACKAGE_ID, "caleeShell": CALEESHELL_PACKAGE_ID}

# A reader that returns the installed signer for a package id. None => the
# installed-signer comparison is skipped (recorded as not compared); the
# install-time signature-mismatch classification remains the backstop.
InstalledSignerReader = Callable[[str], SignerReadResult]


@dataclass
class PreinstallInspection:
    status: str  # STATUS_OK | STATUS_INVALID | STATUS_BLOCKED
    apps: "list[ApkInspection]" = field(default_factory=list)
    signers: "dict" = field(default_factory=dict)  # key -> {classification, detail, releaseSigner, installedSigner}
    detail: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "apps": [a.to_dict() for a in self.apps],
            "signers": self.signers,
            "detail": list(self.detail),
        }


def preinstall_inspect_bundle(
    verification,
    *,
    installed_signer_reader: "InstalledSignerReader | None" = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    runner: ToolRunner = real_tool_runner,
) -> PreinstallInspection:
    """Inspect every verified APK's ACTUAL identity + signer before install.

    For each included app: read its actual applicationId/versionName/versionCode
    and signer digest; check they match the manifest AND the canonical Calee/
    CaleeShell package; and (when a signer reader is supplied) compare the
    release signer against the currently installed signer.

    Overall status:
      * BLOCKED -- a required SDK tool is missing (never a silent skip), or an
        already-installed app's signer MISMATCHES the release APK (install must
        not proceed; data is never wiped to work around it);
      * INVALID -- an APK's actual package/version contradicts the manifest;
      * OK      -- every APK's actual identity matches and no signer conflict.

    Raises nothing. A verification that did not pass yields INVALID (an
    unverified bundle must never reach content inspection)."""
    result = PreinstallInspection(status=STATUS_OK)
    if not getattr(verification, "ok", False):
        result.status = STATUS_INVALID
        result.detail.append("Refusing to inspect APK contents for a bundle that did not pass verification.")
        return result

    saw_blocked = False
    saw_invalid = False

    for app in verification.verified_apps:
        insp = inspect_apk(
            app.apk_path, app.key, manifest_git_sha=app.git_sha, which=which, runner=runner
        )
        result.apps.append(insp)

        if insp.status == STATUS_BLOCKED:
            saw_blocked = True
            result.detail.extend(insp.detail)
            continue

        expected_pkg = _EXPECTED_PACKAGE.get(app.key)
        problems = verify_identity_matches(
            insp,
            expected_package=expected_pkg,
            manifest_version_name=app.version_name,
            manifest_version_code=app.version_code,
        )
        if problems:
            insp.status = STATUS_INVALID
            insp.detail.extend(problems)
            result.detail.extend(problems)
            saw_invalid = True
            continue

        # Signer comparison vs the currently installed app (when a reader exists).
        if installed_signer_reader is not None and app.package_id:
            read = installed_signer_reader(app.package_id)
            classification, detail = classify_signer(insp.signer_sha256, read)
            result.signers[app.key] = {
                "classification": classification,
                "detail": detail,
                "releaseSigner": insp.signer_sha256,
                "installedSigner": read.digest,
            }
            # Both a MISMATCH and an UNKNOWN installed signer BLOCK. UNKNOWN
            # means the package may already be installed but its signer could
            # not be authoritatively read; proceeding could silently install
            # over a differently-signed app, so no install command may run.
            if classification in (SIGNER_MISMATCH, SIGNER_UNKNOWN):
                saw_blocked = True
                result.detail.append(detail)
        else:
            result.signers[app.key] = {
                "classification": "not_compared",
                "detail": "No installed-signer reader supplied (e.g. no device) -- the install-time "
                          "signature check remains the backstop.",
                "releaseSigner": insp.signer_sha256,
                "installedSigner": None,
            }

    if saw_blocked:
        result.status = STATUS_BLOCKED
    elif saw_invalid:
        result.status = STATUS_INVALID
    else:
        result.status = STATUS_OK
    return result


def device_installed_signer_reader(
    *, serial: "str | None" = None,
    adb_runner: "Callable[[list[str]], ToolResult] | None" = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    runner: ToolRunner = real_tool_runner,
    retain_diagnostics: bool = False,
) -> InstalledSignerReader:
    """Build an InstalledSignerReader backed by real adb + apksigner. Used by
    the installer CLI. When the installed signer cannot be authoritatively read
    (no/offline/unauthorized device, package-manager failure, missing apksigner,
    unreadable APK), each read resolves to SIGNER_UNKNOWN, which the pre-install
    gate treats as BLOCKED -- an install must never proceed over a possibly
    conflicting, unverifiable installed signer. ``retain_diagnostics`` keeps the
    pulled-APK workspace for inspection instead of deleting it."""
    def _adb(argv: "list[str]") -> ToolResult:
        return runner(argv)

    adb = adb_runner or _adb

    def _read(package_id: str) -> SignerReadResult:
        return read_installed_signer(
            package_id, serial=serial, adb_runner=adb, apksigner_runner=runner, which=which,
            retain_diagnostics=retain_diagnostics,
        )

    return _read
