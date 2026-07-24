"""Host execution-capability report (Workstream 7).

One read-only, typed probe of what THIS host can actually do, so a cloud
session no longer has to infer manually that it is not on Yiwen's qualification
Mac. It answers, deterministically and without side effects:

  * what OS/arch/host this is, and which interpreter/venv is running;
  * whether ADB / Appium / Flutter / Xcode are available;
  * whether any Android/iOS devices are visible;
  * whether the macOS Keychain, a backend and credential SOURCES are available
    (their PRESENCE only -- never a secret value);
  * whether the tester config, a release bundle and a writable report root
    exist;
  * a single ``executionCapability`` classification -- e.g.
    ``OFFLINE_FRAMEWORK_ONLY`` in a cloud container, or a physical-capability
    class on a Mac with the expected devices.

It performs NO fixture reset, NO app launch and reveals NO secret. Everything
(the ``which`` lookup, the adb/idevice runners, the platform facts and the
environment) is injectable so the same logic is exercised offline for both a
cloud host and a fully-equipped Mac. It is the single reusable environment
probe behind ``qualification-plan`` and is safe for ``--preflight-only`` to
reuse.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from . import credentials as credentials_mod
from .bootstrap_provenance import interpreter_provenance

SCHEMA_VERSION = 1

# ── deterministic capability reason codes ───────────────────────────────────
AVAILABLE = "available"                 # present and usable on this host
UNAVAILABLE = "unavailable"             # could exist on this host, but was not found
NOT_CONFIGURED = "not-configured"       # exists but needs configuration to use
UNSUPPORTED_ON_HOST = "unsupported-on-host"  # cannot exist on this OS (e.g. Xcode on Linux)
PRESENT = "present"
ABSENT = "absent"
WRITABLE = "writable"
NOT_WRITABLE = "not-writable"

# ── execution-capability classifications ────────────────────────────────────
OFFLINE_FRAMEWORK_ONLY = "OFFLINE_FRAMEWORK_ONLY"
PHYSICAL_QUALIFICATION_CAPABLE = "PHYSICAL_QUALIFICATION_CAPABLE"


def _cap(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _tool_capability(name: str, binary: str, which, *, darwin_only: bool, is_darwin: bool) -> dict:
    if darwin_only and not is_darwin:
        return _cap(name, UNSUPPORTED_ON_HOST, f"{binary} is only available on macOS; this host is not macOS.")
    path = which(binary)
    if path:
        return _cap(name, AVAILABLE, f"{binary} found at {path}.")
    return _cap(name, UNAVAILABLE, f"{binary} was not found on PATH.")


def _android_devices(adb_available: bool, adb_runner) -> dict:
    if not adb_available:
        return {"status": UNAVAILABLE, "count": None, "detail": "adb is not available; cannot enumerate Android devices."}
    runner = adb_runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))
    try:
        result = runner(["adb", "devices"])
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"status": UNAVAILABLE, "count": None, "detail": f"Could not run 'adb devices': {exc}"}
    serials = []
    for line in (getattr(result, "stdout", "") or "").splitlines()[1:]:
        line = line.strip()
        if line and "\tdevice" in line:
            serials.append(line.split("\t", 1)[0])
    if serials:
        return {"status": AVAILABLE, "count": len(serials), "detail": f"{len(serials)} Android device(s)/emulator(s) connected."}
    return {"status": UNAVAILABLE, "count": 0, "detail": "No Android devices/emulators are connected."}


def _ios_devices(which, idevice_runner, *, is_darwin: bool) -> dict:
    tool = which("idevice_id")
    if not tool:
        detail = "idevice_id (libimobiledevice) not found; cannot enumerate iOS devices."
        return {"status": UNAVAILABLE if is_darwin else UNSUPPORTED_ON_HOST, "count": None, "detail": detail}
    runner = idevice_runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))
    try:
        result = runner(["idevice_id", "-l"])
    except Exception as exc:  # noqa: BLE001
        return {"status": UNAVAILABLE, "count": None, "detail": f"Could not run 'idevice_id -l': {exc}"}
    udids = [ln.strip() for ln in (getattr(result, "stdout", "") or "").splitlines() if ln.strip()]
    if udids:
        return {"status": AVAILABLE, "count": len(udids), "detail": f"{len(udids)} iOS device(s) visible."}
    return {"status": UNAVAILABLE, "count": 0, "detail": "No iOS devices are visible."}


def _keychain(which, *, is_darwin: bool) -> dict:
    if not is_darwin:
        return _cap("macos_keychain", UNSUPPORTED_ON_HOST, "The macOS Keychain does not exist on a non-macOS host.")
    if which("security"):
        return _cap("macos_keychain", AVAILABLE, "The macOS 'security' tool is available for Keychain access.")
    return _cap("macos_keychain", UNAVAILABLE, "The macOS 'security' tool was not found.")


def _credential_sources(env: dict, which, *, is_darwin: bool) -> dict:
    """Report which credential SOURCES are available -- PRESENCE only, never a
    value. A required credential is 'available' if its env var is set, or (on
    macOS) if a Keychain is present to hold it."""
    keychain_available = is_darwin and bool(which("security"))
    out = {"macosKeychain": AVAILABLE if keychain_available else (UNAVAILABLE if is_darwin else UNSUPPORTED_ON_HOST),
           "credentials": []}
    any_env = False
    for req in credentials_mod.ALL_REQUESTS if hasattr(credentials_mod, "ALL_REQUESTS") else _default_requests():
        env_present = bool(env.get(req.env_var))
        any_env = any_env or env_present
        # We never read the value -- only whether a source could supply it.
        if env_present:
            status = AVAILABLE
            source = "environment"
        elif keychain_available and req.keychain_service and req.keychain_account:
            status = AVAILABLE
            source = "keychain-capable"
        else:
            status = UNAVAILABLE
            source = "none"
        out["credentials"].append({
            "name": req.name,
            "envVar": req.env_var,
            "required": getattr(req, "required", True),
            "status": status,
            "source": source,
        })
    out["environmentAny"] = any_env
    return out


def _default_requests():
    # Fall back to the two mandatory regression credentials if the module does
    # not expose an aggregate list.
    reqs = []
    for attr in ("REGRESSION_USERNAME", "REGRESSION_PASSWORD"):
        r = getattr(credentials_mod, attr, None)
        if r is not None:
            reqs.append(r)
    return reqs


def _host_category(system: str) -> str:
    return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(system, "other")


def gather_host_capabilities(
    *,
    which=shutil.which,
    env: "dict | None" = None,
    repo_root: "Path | None" = None,
    adb_runner=None,
    idevice_runner=None,
    system: "str | None" = None,
    machine: "str | None" = None,
    release: "str | None" = None,
    hostname: "str | None" = None,
) -> dict:
    env = dict(os.environ) if env is None else env
    repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    system = system or platform.system()
    machine = machine or platform.machine()
    release = release or platform.release()
    is_darwin = system == "Darwin"

    adb = _tool_capability("adb", "adb", which, darwin_only=False, is_darwin=is_darwin)
    appium = _tool_capability("appium", "appium", which, darwin_only=False, is_darwin=is_darwin)
    flutter = _tool_capability("flutter", "flutter", which, darwin_only=False, is_darwin=is_darwin)
    xcode = _tool_capability("xcode", "xcrun", which, darwin_only=True, is_darwin=is_darwin)

    adb_available = adb["status"] == AVAILABLE
    android_devices = _android_devices(adb_available, adb_runner)
    ios_devices = _ios_devices(which, idevice_runner, is_darwin=is_darwin)

    # repository virtualenv
    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    prov = interpreter_provenance()
    active_venv = prov.get("virtualEnvironment")
    repo_venv = {
        "present": venv_python.exists(),
        "path": str(venv_dir),
        "isActiveInterpreter": bool(active_venv) and Path(active_venv).resolve() == venv_dir.resolve()
        if active_venv else False,
    }

    # backend presence (never the value)
    backend_url = env.get("CALEE_API_BASE")
    backend = {
        "status": AVAILABLE if backend_url else NOT_CONFIGURED,
        "source": "CALEE_API_BASE",
        "detail": "A backend is configured (value withheld)." if backend_url
        else "No backend configured (CALEE_API_BASE is unset); it never defaults to production.",
    }

    # tester config + release bundle + report root
    tester_config = env.get("CALEE_TEST_CONFIG") or str(repo_root / "config" / "tester.local.yaml")
    tester_config_present = Path(tester_config).is_file()
    machine_config_present = (repo_root / "config" / "machine.local.yaml").is_file()

    report_root = _report_root_capability(repo_root, env)
    credential_sources = _credential_sources(env, which, is_darwin=is_darwin)

    # ── execution-capability classification ─────────────────────────────────
    appium_available = appium["status"] == AVAILABLE
    flutter_available = flutter["status"] == AVAILABLE
    xcode_available = xcode["status"] == AVAILABLE
    tablet_capable = adb_available and appium_available
    android_capable = adb_available and flutter_available
    ios_capable = xcode_available and flutter_available
    physical = {
        "tablet": {"capable": tablet_capable, "requires": "adb + appium"},
        "android": {"capable": android_capable, "requires": "adb + flutter"},
        "ios": {"capable": ios_capable, "requires": "xcode (xcrun) + flutter"},
    }
    any_device_tooling = adb_available or appium_available or flutter_available or xcode_available
    execution_capability = PHYSICAL_QUALIFICATION_CAPABLE if any_device_tooling else OFFLINE_FRAMEWORK_ONLY

    reasons = []
    if execution_capability == OFFLINE_FRAMEWORK_ONLY:
        reasons.append("No device tooling (adb/appium/flutter/xcrun) is available; only offline framework work is possible on this host.")
    else:
        for plat, info in physical.items():
            reasons.append(f"{plat} qualification {'capable' if info['capable'] else 'NOT capable'} (needs {info['requires']}).")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "report": "host-capabilities",
        "operatingSystem": system,
        "osRelease": release,
        "architecture": machine,
        "hostCategory": _host_category(system),
        "hostname": hostname if hostname is not None else platform.node(),
        "python": prov,
        "repositoryVirtualEnvironment": repo_venv,
        "toolchains": {"adb": adb, "appium": appium, "flutter": flutter, "xcode": xcode},
        "devices": {"android": android_devices, "ios": ios_devices},
        "macosKeychain": _keychain(which, is_darwin=is_darwin),
        "backend": backend,
        "credentialSources": credential_sources,
        "testerConfig": {"status": PRESENT if tester_config_present else ABSENT, "path": tester_config},
        "machineConfig": {"status": PRESENT if machine_config_present else ABSENT},
        "releaseBundle": _release_bundle_capability(repo_root, env),
        "reportRoot": report_root,
        "physicalQualification": physical,
        "executionCapability": execution_capability,
        "capabilityReasons": reasons,
    }


def _report_root_capability(repo_root: Path, env: dict) -> dict:
    root = env.get("CALEE_REPORT_ROOT")
    base = Path(root) if root else repo_root
    reports = base / "reports"
    if reports.is_dir():
        writable = os.access(reports, os.W_OK)
        return {"status": WRITABLE if writable else NOT_WRITABLE, "path": str(reports)}
    # A missing reports/ is fine: it is created on first write, as long as its
    # parent is writable.
    if base.is_dir() and os.access(base, os.W_OK):
        return {"status": WRITABLE, "path": str(reports), "detail": "reports/ does not exist yet but will be created on first write."}
    return {"status": NOT_WRITABLE, "path": str(reports)}


def _release_bundle_capability(repo_root: Path, env: dict) -> dict:
    bundle = env.get("CALEE_RELEASE_BUNDLE") or env.get("MACHINE_RELEASE_BUNDLE_DIR")
    if bundle and Path(bundle).is_dir():
        return {"status": PRESENT, "path": bundle}
    return {"status": ABSENT, "detail": "No release bundle configured (CALEE_RELEASE_BUNDLE unset or missing)."}


def render_text(report: dict) -> str:
    """A compact, human-readable rendering (never prints a secret)."""
    lines = []
    lines.append("Calee host capabilities")
    lines.append(f"  execution capability : {report['executionCapability']}")
    lines.append(f"  host                 : {report['hostCategory']} ({report['operatingSystem']} {report['osRelease']}, {report['architecture']})")
    py = report["python"]
    lines.append(f"  python               : {py['pythonVersion']} @ {py['pythonExecutable']}")
    lines.append(f"  repo .venv           : {'present' if report['repositoryVirtualEnvironment']['present'] else 'absent'}"
                 f" (active: {report['repositoryVirtualEnvironment']['isActiveInterpreter']})")
    tc = report["toolchains"]
    lines.append("  toolchains           : " + ", ".join(f"{k}={v['status']}" for k, v in tc.items()))
    dev = report["devices"]
    lines.append(f"  devices              : android={dev['android']['status']}({dev['android']['count']}), ios={dev['ios']['status']}({dev['ios']['count']})")
    lines.append(f"  macOS keychain       : {report['macosKeychain']['status']}")
    lines.append(f"  backend              : {report['backend']['status']} ({report['backend']['source']})")
    lines.append(f"  tester config        : {report['testerConfig']['status']}")
    lines.append(f"  report root          : {report['reportRoot']['status']} ({report['reportRoot']['path']})")
    lines.append("  reasons:")
    for r in report["capabilityReasons"]:
        lines.append(f"    - {r}")
    return "\n".join(lines) + "\n"
