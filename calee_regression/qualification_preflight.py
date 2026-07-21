"""Technical-owner qualification preflight (Priority 9).

A single, read-only, non-mutating command that tells a technical owner
whether THIS MacBook is actually ready for a real physical qualification run
-- before they spend an afternoon discovering, one broken step at a time,
that Appium isn't running, the tablet isn't the one the config expects, or
the CaleeMobile sibling checkout doesn't exist.

Every check here is deliberately read-only:

  * no APK is installed;
  * no subscribed-fixture publication/ingestion is attempted (only
    configuration + reachability of the public URL is probed with a GET, and
    only when a URL is already configured);
  * no product API is mutated (credential PRESENCE is checked, never a
    value printed, and never used to call a mutating endpoint);
  * a check that cannot be verified is reported BLOCKED or WARNING, never
    a fabricated PASS/READY.

Everything is injectable (adb runner, ``which``, an HTTP opener, a
subprocess runner) so this is fully offline-testable without a real Mac,
Android SDK, Appium install, or network access.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"
STATUS_WARNING = "warning"

WhichFn = Callable[[str], "Optional[str]"]
RunnerFn = Callable[..., "subprocess.CompletedProcess"]


@dataclass
class PreflightCheck:
    name: str
    status: str
    detail: str
    hint: "str | None" = None

    def to_dict(self) -> dict:
        data = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.hint:
            data["hint"] = self.hint
        return data


def _check(name: str, status: str, detail: str, hint: "str | None" = None) -> PreflightCheck:
    return PreflightCheck(name=name, status=status, detail=detail, hint=hint)


# ── individual checks (each pure/injectable, no hidden global state) ────


def check_machine_config(config_path: "Path | None") -> PreflightCheck:
    from . import machine_config as mc

    path = config_path or (Path("config") / "machine.local.yaml")
    if not Path(path).is_file():
        return _check(
            "machine_config", STATUS_BLOCKED, f"No machine config at {path}.",
            hint="Copy config/machine.local.example.yaml to config/machine.local.yaml and fill it in.",
        )
    try:
        cfg = mc.load_machine_config(path)
    except mc.MachineConfigError as exc:
        return _check("machine_config", STATUS_BLOCKED, f"Machine config at {path} is invalid: {exc}")
    return _check("machine_config", STATUS_READY, f"Machine config loaded from {path} (serial {cfg.tablet_serial!r}).")


def check_report_root(machine_report_dir: "str | None", env: "dict | None" = None) -> PreflightCheck:
    """Read-only equivalent of report_root.resolve_report_root -- inspects
    without creating anything."""
    environ = env if env is not None else os.environ
    candidate = environ.get("CALEE_REPORT_ROOT") or machine_report_dir or "."
    path = Path(candidate).expanduser()
    if path.is_absolute() and str(path) == path.root:
        return _check("report_root", STATUS_BLOCKED, f"Report root resolves to the filesystem root ({path}) -- refusing.")
    if not path.exists():
        return _check(
            "report_root", STATUS_WARNING, f"Report root {path} does not exist yet (it will be created on first run).",
        )
    if not os.access(path, os.W_OK):
        return _check("report_root", STATUS_BLOCKED, f"Report root {path} exists but is not writable.")
    return _check("report_root", STATUS_READY, f"Report root {path} exists and is writable.")


def check_android_sdk(which: WhichFn = shutil.which) -> PreflightCheck:
    home = os.environ.get("ANDROID_HOME")
    sdk_root = os.environ.get("ANDROID_SDK_ROOT")
    for env_value in (home, sdk_root):
        if env_value and (Path(env_value) / "platform-tools" / "adb").exists():
            return _check("android_sdk_tools", STATUS_READY, f"Android SDK found at {env_value}.")
    if which("adb"):
        return _check("android_sdk_tools", STATUS_READY, "adb found on PATH.")
    return _check(
        "android_sdk_tools", STATUS_BLOCKED, "Neither ANDROID_HOME/ANDROID_SDK_ROOT nor a PATH adb was found.",
        hint="export ANDROID_HOME=/path/to/Android/sdk, or add platform-tools to PATH.",
    )


def check_adb_devices(
    expected_serial: "str | None", *, adb_runner: "Callable[[list], subprocess.CompletedProcess] | None" = None,
) -> "tuple[PreflightCheck, PreflightCheck, list[str]]":
    """Returns (device_availability, expected_serial_check, connected_serials)
    -- read-only ``adb devices``, no install/mutation."""
    runner = adb_runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))
    try:
        result = runner(["adb", "devices"])
    except (OSError, subprocess.SubprocessError) as exc:
        blocked = _check("adb_device_availability", STATUS_BLOCKED, f"Could not run 'adb devices': {exc}")
        return blocked, _check("expected_tablet_serial", STATUS_BLOCKED, "Could not determine connected devices."), []

    lines = [ln.strip() for ln in (result.stdout or "").splitlines()[1:] if ln.strip()]
    connected = [ln.split("\t")[0] for ln in lines if "\tdevice" in ln]
    if not connected:
        availability = _check(
            "adb_device_availability", STATUS_BLOCKED, "No Android devices/emulators are connected.",
            hint="Connect the tablet (and any Android phone in scope) over USB/network.",
        )
    else:
        availability = _check("adb_device_availability", STATUS_READY, f"Connected devices: {connected}.")

    if not expected_serial:
        serial_check = _check("expected_tablet_serial", STATUS_WARNING, "No tablet_serial configured in machine config.")
    elif expected_serial in connected:
        serial_check = _check("expected_tablet_serial", STATUS_READY, f"Expected tablet serial {expected_serial!r} is connected.")
    else:
        serial_check = _check(
            "expected_tablet_serial", STATUS_BLOCKED,
            f"Expected tablet serial {expected_serial!r} is not among connected devices {connected}.",
        )
    return availability, serial_check, connected


def check_iphone_available(*, runner: "Optional[RunnerFn]" = None, which: WhichFn = shutil.which) -> "bool | None":
    """Best-effort, read-only iOS device detection via idevice_id (libimobiledevice).
    Returns None (unknown) when the tool isn't installed -- never guesses."""
    tool = which("idevice_id")
    if not tool:
        return None
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    try:
        result = runner([tool, "-l"])
    except (OSError, subprocess.SubprocessError):
        return None
    return bool((result.stdout or "").strip())


def _load_appium_url(repo_root: Path) -> "str | None":
    """appium_url lives in the legacy tester config (config/tester.local.yaml),
    not machine.local.yaml -- read-only best-effort lookup, mirroring how
    cli.py's own machine-config-snapshot reconciles the two."""
    import yaml as _yaml

    for name in ("tester.local.yaml", "tester.local.example.yaml"):
        path = repo_root / "config" / name
        if not path.is_file():
            continue
        try:
            raw = _yaml.safe_load(path.read_text(encoding="utf-8"))
        except _yaml.YAMLError:
            continue
        if isinstance(raw, dict) and raw.get("appium_url"):
            return raw["appium_url"]
    return None


def check_appium(appium_url: "str | None", *, opener: "Callable[[str], Any] | None" = None) -> PreflightCheck:
    if not appium_url:
        return _check("appium", STATUS_WARNING, "No appium_url configured.")
    url = appium_url.rstrip("/") + "/status"
    opener = opener or (lambda u: urllib.request.urlopen(u, timeout=5))
    try:
        opener(url)
    except Exception as exc:  # noqa: BLE001 - reachability probe, any failure is just "not reachable"
        return _check(
            "appium", STATUS_BLOCKED, f"Could not reach Appium at {url}: {exc}",
            hint="Start Appium: appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell",
        )
    return _check("appium", STATUS_READY, f"Appium responded at {url}.")


def check_flutter(which: WhichFn = shutil.which) -> PreflightCheck:
    path = which("flutter")
    if not path:
        return _check("flutter", STATUS_BLOCKED, "flutter is not on PATH.", hint="Install Flutter and add it to PATH.")
    return _check("flutter", STATUS_READY, f"flutter found at {path}.")


def check_mobile_devices_for_scope(
    mobile_platforms: "list[str]", *, android_connected: "list[str]", iphone_available: "bool | None" = None,
) -> "list[PreflightCheck]":
    checks: "list[PreflightCheck]" = []
    if "android" in mobile_platforms:
        if android_connected:
            checks.append(_check("android_device_for_scope", STATUS_READY, f"Android device available: {android_connected}."))
        else:
            checks.append(_check("android_device_for_scope", STATUS_BLOCKED, "Release scope includes Android, but no Android device is connected."))
    if "ios" in mobile_platforms:
        if iphone_available:
            checks.append(_check("iphone_device_for_scope", STATUS_READY, "iPhone device available."))
        elif iphone_available is False:
            checks.append(_check("iphone_device_for_scope", STATUS_BLOCKED, "Release scope includes iOS, but no iPhone device was detected."))
        else:
            checks.append(_check("iphone_device_for_scope", STATUS_WARNING, "Release scope includes iOS; iPhone availability could not be determined on this machine."))
    return checks


def check_sibling_checkout(name: str, path: Path) -> PreflightCheck:
    if not path.is_dir():
        return _check(name, STATUS_BLOCKED, f"{name} checkout not found at {path}.")
    if not (path / ".git").exists():
        return _check(name, STATUS_WARNING, f"{path} exists but does not look like a Git checkout.")
    return _check(name, STATUS_READY, f"{name} checkout found at {path}.")


def check_keychain_credentials(*, resolver=None) -> PreflightCheck:
    """Checks PRESENCE only -- never logs a resolved secret value."""
    from . import credentials as credentials_mod

    resolver = resolver or credentials_mod.default_resolver()
    missing = []
    for req in credentials_mod.REQUIRED_SECRETS:
        if resolver.get(req) is None:
            missing.append(req.name)
    if missing:
        return _check(
            "keychain_credentials", STATUS_BLOCKED,
            f"Required credential(s) not resolvable from environment or Keychain: {missing}.",
            hint="Set the matching env var, or store it in the macOS Keychain (see calee_regression/credentials.py).",
        )
    return _check("keychain_credentials", STATUS_READY, "All required credentials resolve (values not shown).")


def check_ics_publisher_config(section: "dict | None") -> PreflightCheck:
    from . import url_validation

    if not section:
        return _check("external_ics_publisher", STATUS_WARNING, "No subscribed_fixture section configured (offline-only mode will be used).")
    publisher = section.get("publisher")
    public_url = section.get("public_url")
    if section.get("mode") != "published":
        return _check("external_ics_publisher", STATUS_WARNING, f"subscribed_fixture.mode is {section.get('mode')!r}, not 'published' -- publisher config not required.")
    if not publisher:
        return _check("external_ics_publisher", STATUS_BLOCKED, "published mode configured but no publisher adapter is set.")
    if not public_url:
        return _check("external_ics_publisher", STATUS_BLOCKED, "published mode configured but no public_url is set.")
    problems = url_validation.validate_backend_url(public_url)
    if problems:
        return _check("external_ics_publisher", STATUS_BLOCKED, f"public_url is invalid: {'; '.join(problems)}")
    return _check("external_ics_publisher", STATUS_READY, f"Publisher {publisher!r} configured with a valid public_url.")


def check_public_ics_url(public_url: "str | None", *, opener: "Callable[[str], Any] | None" = None) -> PreflightCheck:
    if not public_url:
        return _check("public_ics_url", STATUS_WARNING, "No public_url configured to check.")
    opener = opener or (lambda u: urllib.request.urlopen(u, timeout=10))
    try:
        opener(public_url)
    except Exception as exc:  # noqa: BLE001 - reachability probe
        return _check("public_ics_url", STATUS_BLOCKED, f"public_url {public_url} is not reachable: {exc}")
    return _check("public_ics_url", STATUS_READY, f"public_url {public_url} is reachable.")


def check_ingestion_bridge(repo_root: Path) -> PreflightCheck:
    from . import sync_smoke_bridge as ssb_mod

    if ssb_mod.is_ingestion_bridge_available(repo_root):
        return _check("ingestion_api_bridge", STATUS_READY, "CaleeMobile-Regression ingestion bridge is available.")
    return _check(
        "ingestion_api_bridge", STATUS_WARNING,
        "CaleeMobile-Regression ingestion bridge is not available (sibling checkout/bridge action missing).",
    )


def check_selector_ci_evidence_availability(*, env: "dict | None" = None) -> PreflightCheck:
    from . import github_artifact as ga_mod

    token = ga_mod.resolve_token(env=env)
    if token is None:
        return _check(
            "selector_ci_evidence_availability", STATUS_WARNING,
            f"No GitHub API credential resolvable from {list(ga_mod.TOKEN_ENV_VARS)}.",
            hint="A production release requires a CI-authenticated selector artifact -- set REGRESSION_API_TOKEN/GITHUB_TOKEN/GH_TOKEN.",
        )
    return _check("selector_ci_evidence_availability", STATUS_READY, "A GitHub API credential is resolvable (value not shown).")


def check_distributed_build_evidence_availability(source_path: "Path | None") -> PreflightCheck:
    from . import distributed_build_provenance as dbp

    if not source_path:
        return _check("distributed_build_evidence_availability", STATUS_WARNING, "No --distributed-build-evidence path given to check.")
    if not Path(source_path).is_file():
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path} does not exist.")
    try:
        evidence = json.loads(Path(source_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path} is not valid JSON: {exc}")
    problems = dbp.validate_distributed_evidence(evidence) if isinstance(evidence, dict) else ["not a JSON object"]
    if problems:
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"Evidence at {source_path} has problem(s): {'; '.join(problems)}")
    return _check("distributed_build_evidence_availability", STATUS_READY, f"Distributed-build evidence at {source_path} is well-formed.")


def check_frozen_candidate_ability(bundle_path: "Path | None") -> PreflightCheck:
    from . import release_installer

    if not bundle_path:
        return _check("frozen_candidate_ability", STATUS_WARNING, "No --bundle given to check.")
    verification = release_installer.verify_release_bundle(bundle_path)
    if not verification.ok:
        return _check(
            "frozen_candidate_ability", STATUS_BLOCKED,
            f"Bundle at {bundle_path} would fail verification: {'; '.join(verification.errors)}",
        )
    return _check("frozen_candidate_ability", STATUS_READY, f"Bundle at {bundle_path} verifies cleanly (release {verification.manifest.release_id}).")


def check_manual_check_definitions(path: "Path | None") -> PreflightCheck:
    from . import manual_checks as mc_mod

    if path is not None:
        candidate = path
    else:
        candidate = Path("config") / "manual-checks.json"
        if not candidate.is_file():
            candidate = Path("config") / "manual-checks.example.json"
    if not candidate.is_file():
        return _check("manual_check_definitions", STATUS_BLOCKED, f"{candidate}: not found.")
    try:
        defs = mc_mod.load_check_definitions(candidate)
    except mc_mod.ManualChecksDefinitionError as exc:
        return _check("manual_check_definitions", STATUS_BLOCKED, f"{candidate}: {exc}")
    return _check("manual_check_definitions", STATUS_READY, f"{len(defs)} manual check definition(s) loaded from {candidate}.")


@dataclass
class PreflightReport:
    checks: "list[PreflightCheck]" = field(default_factory=list)

    @property
    def overall(self) -> str:
        if any(c.status == STATUS_BLOCKED for c in self.checks):
            return STATUS_BLOCKED
        return STATUS_READY

    def to_dict(self) -> dict:
        return {
            "overall": self.overall.upper(),
            "checks": [c.to_dict() for c in self.checks],
        }


def run_qualification_preflight(
    *,
    config_path: "Path | None" = None,
    bundle_path: "Path | None" = None,
    distributed_build_evidence_path: "Path | None" = None,
    repo_root: "Path | None" = None,
    manual_checks_path: "Path | None" = None,
    adb_runner=None,
    which: WhichFn = shutil.which,
    http_opener=None,
    env: "dict | None" = None,
) -> PreflightReport:
    """Run every Priority-9 check and return one report. Read-only throughout
    -- see the module docstring. ``adb_runner``/``which``/``http_opener``/
    ``env`` are injectable so this runs fully offline in tests."""
    from . import machine_config as mc

    repo_root = repo_root or Path(".")
    environ = env if env is not None else os.environ

    checks: "list[PreflightCheck]" = []
    checks.append(check_machine_config(config_path))

    cfg = None
    resolved_config_path = config_path or (Path("config") / "machine.local.yaml")
    if Path(resolved_config_path).is_file():
        try:
            cfg = mc.load_machine_config(resolved_config_path)
        except mc.MachineConfigError:
            cfg = None

    checks.append(check_report_root(cfg.report_dir if cfg else None, env=environ))
    checks.append(check_android_sdk(which=which))

    availability, serial_check, connected_serials = check_adb_devices(
        cfg.tablet_serial if cfg else None, adb_runner=adb_runner,
    )
    checks.append(availability)
    checks.append(serial_check)

    checks.append(check_appium(_load_appium_url(repo_root), opener=http_opener))
    checks.append(check_flutter(which=which))

    mobile_platforms = cfg.mobile_platforms if cfg else []
    iphone_available = check_iphone_available(which=which) if "ios" in mobile_platforms else None
    checks.extend(check_mobile_devices_for_scope(
        mobile_platforms, android_connected=connected_serials, iphone_available=iphone_available,
    ))

    checks.append(check_sibling_checkout("caleemobile_checkout", repo_root.parent / "CaleeMobile"))
    checks.append(check_sibling_checkout("caleemobile_regression_checkout", repo_root.parent / "CaleeMobile-Regression"))

    checks.append(check_keychain_credentials())

    section = {}
    if cfg is not None:
        try:
            import yaml as _yaml

            raw = _yaml.safe_load(Path(resolved_config_path).read_text(encoding="utf-8"))
            section = (raw or {}).get("subscribed_fixture") or {}
        except Exception:  # noqa: BLE001 - best-effort, never crash the preflight
            section = {}
    checks.append(check_ics_publisher_config(section))
    checks.append(check_public_ics_url(section.get("public_url"), opener=http_opener))
    checks.append(check_ingestion_bridge(repo_root))
    checks.append(check_selector_ci_evidence_availability(env=environ))
    checks.append(check_distributed_build_evidence_availability(distributed_build_evidence_path))
    checks.append(check_frozen_candidate_ability(bundle_path))
    checks.append(check_manual_check_definitions(manual_checks_path))

    return PreflightReport(checks=checks)
