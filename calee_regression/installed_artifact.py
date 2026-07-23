"""Installed-artifact identity attestation (tablet + iPhone observed identity).

The focused/tablet suites exercise WHATEVER app is currently installed on the
device -- which is not necessarily the configured APK. This module reconciles
the EXPECTED identity (read from the configured APK via apk_inspect: sha256,
applicationId, versionName/versionCode, signer digest) against the INSTALLED
identity on the connected device (``adb shell dumpsys package`` + ``pm path``,
parsed defensively), producing a typed result:

  * ``verified``  -- every comparable field matches;
  * ``mismatch``  -- one or more fields differ (the differing fields are
    named). Policy: a mismatch BLOCKS standard/certifying tablet execution;
  * ``unproven``  -- adb/device/tooling/parsing unavailable, with the reason.
    Policy: unproven blocks a standard CERTIFICATION, but a diagnostic-purpose
    run may proceed while remaining non-certifying.

Design constraints (matching apk_inspect):

  * pure logic + injectable command runners -- fully offline-testable;
  * NO broad filesystem search and NO installation/mutation ever: only the
    configured apk_path is read, and only read-only adb queries run;
  * never raises -- every failure is classified into the result;
  * never records certificates/provisioning secrets -- only public digests.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import apk_inspect
from . import build_identity as build_identity_mod
from .apk_inspect import ToolResult, real_tool_runner

STATUS_VERIFIED = "verified"
STATUS_MISMATCH = "mismatch"
STATUS_UNPROVEN = "unproven"

_DUMPSYS_LAST_UPDATE_RE = re.compile(r"lastUpdateTime=([^\r\n]+)")

# The identity fields reconcile() compares when both sides have a value.
_COMPARED_FIELDS = ("applicationId", "versionName", "versionCode", "signerSha256")


def parse_dumpsys_identity(dumpsys_output: "str | None") -> dict:
    """Defensively parse ``adb shell dumpsys package <pkg>`` output into
    ``{versionName, versionCode, lastUpdateTime}`` (any value may be None).
    Reuses build_identity's proven versionName/versionCode parsers."""
    return {
        "versionName": build_identity_mod.parse_dumpsys_version_name(dumpsys_output),
        "versionCode": build_identity_mod.parse_dumpsys_version_code(dumpsys_output),
        "lastUpdateTime": (
            m.group(1).strip() if (m := _DUMPSYS_LAST_UPDATE_RE.search(dumpsys_output or "")) else None
        ),
    }


@dataclass
class ReconcileResult:
    """The typed outcome of expected-vs-installed identity reconciliation.
    ``expected``/``installed`` are plain evidence dicts (no secrets);
    ``mismatched_fields`` names exactly what differed."""

    status: str
    expected: dict = field(default_factory=dict)
    installed: dict = field(default_factory=dict)
    mismatched_fields: "list[str]" = field(default_factory=list)
    reason: str = ""
    detail: "list[str]" = field(default_factory=list)
    iphone_observed: "dict | None" = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "expected": dict(self.expected),
            "installed": dict(self.installed),
            "mismatchedFields": list(self.mismatched_fields),
            "reason": self.reason,
            "detail": list(self.detail),
            "iphoneObserved": dict(self.iphone_observed) if self.iphone_observed else None,
        }


def blocks_tablet_execution(result: ReconcileResult, *, certifying: bool) -> "tuple[bool, str]":
    """The tablet-execution policy for a reconciliation result.

    * ``mismatch``: BLOCKS every standard/certifying tablet execution -- the
      device would exercise a different build than the one under test.
    * ``unproven``: blocks a standard CERTIFICATION (identity could not be
      established), but a diagnostic-purpose run may proceed while remaining
      explicitly non-certifying.
    * ``verified``: never blocks.
    """
    if result.status == STATUS_MISMATCH:
        return True, (
            "installed app identity MISMATCHES the configured APK "
            f"(fields: {', '.join(result.mismatched_fields)}) -- tablet execution is blocked."
        )
    if result.status == STATUS_UNPROVEN and certifying:
        return True, (
            f"installed app identity is unproven ({result.reason}) -- a certifying tablet "
            f"execution cannot proceed; a diagnostic run may, but stays non-certifying."
        )
    return False, ""


def expected_identity_from_apk(
    apk_path: "Path | str",
    app_package: str,
    *,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    tool_runner: "apk_inspect.ToolRunner" = real_tool_runner,
) -> "tuple[dict, list[str]]":
    """The EXPECTED identity of the configured APK, via apk_inspect (which
    already reads applicationId/version/signer and computes the file sha256).
    Returns ``(identity_dict, problems)``; problems are non-fatal (partial
    identity is still recorded)."""
    inspection = apk_inspect.inspect_apk(
        apk_path, "configured", which=which, runner=tool_runner
    )
    identity = {
        "source": "configured-apk",
        "apkPath": str(apk_path),
        "applicationId": inspection.application_id or app_package,
        "versionName": inspection.version_name,
        "versionCode": inspection.version_code,
        "apkSha256": inspection.apk_sha256,
        "signerSha256": inspection.signer_sha256,
    }
    return identity, list(inspection.detail)


def _default_adb_runner(argv: "list[str]") -> ToolResult:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return ToolResult(returncode=127, stderr="adb not found")
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolResult(returncode=126, stderr=str(exc))
    return ToolResult(returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or "")


def installed_identity_from_device(
    app_package: str,
    *,
    serial: "str | None" = None,
    adb_runner: "Callable[[list[str]], ToolResult]" = _default_adb_runner,
) -> "tuple[dict | None, str]":
    """The INSTALLED identity on the connected device, read via read-only adb
    queries (``dumpsys package`` + ``pm path``). Returns ``(identity, reason)``
    -- identity is None (with the reason) when adb/the device/the package
    manager could not authoritatively answer. Never installs, never searches
    the filesystem, never raises."""
    base = ["adb"] + (["-s", serial] if serial else [])
    res = adb_runner(base + ["shell", "dumpsys", "package", app_package])
    combined = f"{res.stdout}\n{res.stderr}".lower()
    if res.returncode == 127 or ("not found" in combined and "adb" in combined):
        return None, "adb is not available -- cannot read the installed identity."
    if any(m in combined for m in ("no devices", "device offline", "device unauthorized", "error: device")):
        return None, "no usable device -- cannot read the installed identity."
    if res.returncode != 0:
        return None, f"dumpsys package exited {res.returncode}: {(res.stderr or '').strip()[:200]}"

    parsed = parse_dumpsys_identity(res.stdout)
    path_res = adb_runner(base + ["shell", "pm", "path", app_package])
    device_apk = apk_inspect.parse_pm_path(path_res.stdout)
    if not device_apk and parsed["versionName"] is None and parsed["versionCode"] is None:
        return None, f"{app_package} does not appear to be installed on the device."
    if parsed["versionName"] is None and parsed["versionCode"] is None:
        return None, "dumpsys package output could not be parsed (no versionName/versionCode)."
    identity = {
        "source": "device",
        "applicationId": app_package if device_apk or parsed["versionName"] else None,
        "versionName": parsed["versionName"],
        "versionCode": parsed["versionCode"],
        "lastUpdateTime": parsed["lastUpdateTime"],
        "devicePath": device_apk,
    }
    return identity, ""


def reconcile(
    *,
    apk_path: "Path | str | None",
    app_package: "str | None",
    serial: "str | None" = None,
    adb_runner: "Callable[[list[str]], ToolResult]" = _default_adb_runner,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    tool_runner: "apk_inspect.ToolRunner" = real_tool_runner,
    installed_signer_reader: "apk_inspect.InstalledSignerReader | None" = None,
) -> ReconcileResult:
    """Reconcile the configured APK's expected identity against the installed
    app on the device. Best-effort and read-only: any gap in configuration,
    tooling, or device access yields ``unproven`` with the reason -- never an
    exception, never an install, never a filesystem search."""
    if not app_package:
        return ReconcileResult(
            status=STATUS_UNPROVEN, reason="no app_package configured -- nothing to reconcile."
        )
    if not apk_path:
        return ReconcileResult(
            status=STATUS_UNPROVEN, reason="no apk_path configured -- expected identity unavailable."
        )
    if not Path(apk_path).is_file():
        return ReconcileResult(
            status=STATUS_UNPROVEN,
            reason=f"configured APK not found at {apk_path} (no filesystem search is performed).",
        )

    try:
        expected, expected_problems = expected_identity_from_apk(
            apk_path, app_package, which=which, tool_runner=tool_runner
        )
        installed, install_reason = installed_identity_from_device(
            app_package, serial=serial, adb_runner=adb_runner
        )
    except Exception as exc:  # noqa: BLE001 -- attestation must never crash a run
        return ReconcileResult(status=STATUS_UNPROVEN, reason=f"identity read raised: {exc}")

    if installed is None:
        return ReconcileResult(
            status=STATUS_UNPROVEN, expected=expected, reason=install_reason,
            detail=expected_problems,
        )

    # Optional pull-less installed-signer comparison via apk_inspect's reader.
    if installed_signer_reader is not None:
        read = installed_signer_reader(app_package)
        if read.status == apk_inspect.SIGNER_OK and read.digest:
            installed["signerSha256"] = read.digest

    mismatched = []
    for name in _COMPARED_FIELDS:
        exp, got = expected.get(name), installed.get(name)
        if exp is not None and got is not None and str(exp) != str(got):
            mismatched.append(name)
    if mismatched:
        return ReconcileResult(
            status=STATUS_MISMATCH, expected=expected, installed=installed,
            mismatched_fields=mismatched,
            reason="installed app identity differs from the configured APK.",
            detail=expected_problems + [
                f"{name}: expected {expected.get(name)!r}, installed {installed.get(name)!r}"
                for name in mismatched
            ],
        )
    comparable = [
        n for n in _COMPARED_FIELDS
        if expected.get(n) is not None and installed.get(n) is not None
    ]
    if not comparable:
        return ReconcileResult(
            status=STATUS_UNPROVEN, expected=expected, installed=installed,
            reason="no identity field was readable on both sides -- nothing could be compared.",
            detail=expected_problems,
        )
    return ReconcileResult(
        status=STATUS_VERIFIED, expected=expected, installed=installed,
        reason=f"matched on: {', '.join(comparable)}.", detail=expected_problems,
    )


def iphone_observed_identity(
    caleemobile_repo: "Path | str",
    *,
    head_sha: "Callable[[Path], str | None] | None" = None,
    dirty: "Callable[[Path], bool | None] | None" = None,
) -> "dict | None":
    """Cheaply observable iPhone-side identity fields from the read-only
    CaleeMobile checkout (git SHA + dirty state, pubspec version). Read-only
    file reads only; None when the checkout is absent. Never touches
    certificates/provisioning material."""
    repo = Path(caleemobile_repo)
    if not repo.is_dir():
        return None
    pubspec_version = None
    try:
        pubspec = repo / "pubspec.yaml"
        if pubspec.is_file():
            pubspec_version = build_identity_mod.parse_pubspec_version(
                pubspec.read_text(encoding="utf-8")
            )
    except OSError:
        pass
    return {
        "source": "caleemobile-checkout",
        "repoPath": str(repo),
        "gitSha": head_sha(repo) if head_sha else None,
        "gitDirty": dirty(repo) if dirty else None,
        "pubspecVersion": pubspec_version,
    }
