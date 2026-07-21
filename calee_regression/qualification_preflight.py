"""Technical-owner qualification preflight (Priority 9 prior session;
Priority 7 THIS session: release-authoritative derivation).

A single, read-only, non-mutating command that tells a technical owner
whether THIS MacBook is actually ready for a real physical qualification run
-- before they spend an afternoon discovering, one broken step at a time,
that Appium isn't running, the tablet isn't the one the config expects, or
the CaleeMobile sibling checkout doesn't exist.

Priority 7 (this session) closes the defect where required-check status was
derived from the MACHINE's own declared capability scope
(``machine.mobile_platforms``) rather than from what the actual release
candidate REQUIRES. When ``--bundle`` is given, this composes the SAME
effective release configuration ``release-config``/the real launcher would
(:func:`release_config.compose_effective_release_config`) and derives every
required check from IT: a platform/feature the release doesn't need is never
required here just because the machine happens to support it, and a
platform/feature the release DOES need is required even if the machine's own
``machine.local.yaml`` under-declares it (the composition's own blocking
conflicts, e.g. a missing kiosk-admin authorisation, are surfaced directly).

Every check here is deliberately read-only:

  * no APK is installed;
  * no subscribed-fixture publication/ingestion is attempted (only
    configuration + reachability of the public URL is probed with a GET, and
    only when a URL is already configured);
  * no product API is mutated (credential PRESENCE is checked, never a
    value printed, and never used to call a mutating endpoint);
  * a check that cannot be verified is reported BLOCKED (when the release
    candidate actually requires that capability) or WARNING (when it does
    not), never a fabricated PASS/READY -- an UNDETERMINABLE required check
    BLOCKS, it is never merely a warning (Priority 7 requirement 7).

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

# Pinned to match selector_evidence.EXPECTED_FLUTTER_VERSION /
# toolchain_verify.DEFAULT_EXPECTED_FLUTTER_VERSION -- one toolchain version
# across every consumer of "is Flutter the right version".
EXPECTED_FLUTTER_VERSION = "3.44.1"


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


def _required_or_warning(*, required: bool, ok: bool, name: str, ok_detail: str, missing_detail: str, hint: "str | None" = None) -> PreflightCheck:
    """Shared shape for "this capability is READY, or its absence is BLOCKED
    (required) / WARNING (not required)" -- never silently PASS."""
    if ok:
        return _check(name, STATUS_READY, ok_detail)
    return _check(name, STATUS_BLOCKED if required else STATUS_WARNING, missing_detail, hint=hint)


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


def check_android_sdk(which: WhichFn = shutil.which, *, env: "dict | None" = None) -> PreflightCheck:
    environ = env if env is not None else os.environ
    home = environ.get("ANDROID_HOME")
    sdk_root = environ.get("ANDROID_SDK_ROOT")
    for env_value in (home, sdk_root):
        if env_value and (Path(env_value) / "platform-tools" / "adb").exists():
            return _check("android_sdk_tools", STATUS_READY, f"Android SDK found at {env_value}.")
    if which("adb"):
        return _check("android_sdk_tools", STATUS_READY, "adb found on PATH.")
    return _check(
        "android_sdk_tools", STATUS_BLOCKED, "Neither ANDROID_HOME/ANDROID_SDK_ROOT nor a PATH adb was found.",
        hint="export ANDROID_HOME=/path/to/Android/sdk, or add platform-tools to PATH.",
    )


def check_android_build_tools(*, which: WhichFn = shutil.which, required: bool = True) -> PreflightCheck:
    """Priority 7 requirement 9: adb, aapt/aapt2 (or apkanalyzer), apksigner
    -- the tools apk_inspect.py actually shells out to for APK content/signer
    inspection before any install. Distinct from ``android_sdk_tools`` (bare
    adb presence) so a technical owner sees exactly which tool is missing."""
    missing = []
    if not which("adb"):
        missing.append("adb")
    if not (which("aapt2") or which("aapt") or which("apkanalyzer")):
        missing.append("aapt2 (or aapt/apkanalyzer)")
    if not which("apksigner"):
        missing.append("apksigner")
    return _required_or_warning(
        required=required, ok=not missing, name="android_build_tools",
        ok_detail="adb, aapt2 (or aapt/apkanalyzer), and apksigner are all on PATH.",
        missing_detail=f"Missing Android build tool(s) on PATH: {missing}.",
        hint="Install Android SDK build-tools/command-line-tools (e.g. via sdkmanager) and add them to PATH.",
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


def check_iphone_connected_udids(*, runner: "Optional[RunnerFn]" = None, which: WhichFn = shutil.which) -> "list[str] | None":
    """Best-effort, read-only iOS device enumeration via idevice_id
    (libimobiledevice). Returns the list of connected UDIDs, or None
    (state cannot be determined) when the tool isn't installed -- never
    guesses, and never collapses "no tool" and "no device" into the same
    answer, since Priority 7 requires treating them differently (BLOCKED vs
    a real absence check)."""
    tool = which("idevice_id")
    if not tool:
        return None
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    try:
        result = runner([tool, "-l"])
    except (OSError, subprocess.SubprocessError):
        return None
    return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]


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


def check_appium_drivers(
    *, required_drivers: "list[str]", which: WhichFn = shutil.which, runner: "Optional[RunnerFn]" = None,
) -> PreflightCheck:
    """Priority 7 requirement 10: Appium responding is necessary but not
    sufficient -- the actual platform driver(s) the release scope needs
    (uiautomator2 for tablet/Android, xcuitest for iOS) must be installed,
    or every session request for that platform fails regardless of Appium's
    own health."""
    if not required_drivers:
        return _check("appium_drivers", STATUS_WARNING, "No platform in scope requires a specific Appium driver.")
    tool = which("appium")
    if not tool:
        return _check(
            "appium_drivers", STATUS_BLOCKED, "appium is not on PATH -- cannot list installed drivers.",
            hint="npm install -g appium, then: appium driver install uiautomator2 (and/or xcuitest).",
        )
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))
    try:
        result = runner([tool, "driver", "list", "--installed", "--json"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _check("appium_drivers", STATUS_BLOCKED, f"Could not run 'appium driver list --installed': {exc}")
    try:
        installed = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return _check("appium_drivers", STATUS_BLOCKED, "Could not parse 'appium driver list --installed --json' output.")
    installed_names = set(installed.keys()) if isinstance(installed, dict) else set()
    missing = [d for d in required_drivers if d not in installed_names]
    if missing:
        return _check(
            "appium_drivers", STATUS_BLOCKED, f"Required Appium driver(s) not installed: {missing}.",
            hint=f"appium driver install {' '.join(missing)}",
        )
    return _check("appium_drivers", STATUS_READY, f"Required Appium driver(s) installed: {required_drivers}.")


def check_flutter(
    which: WhichFn = shutil.which, *, runner: "Optional[RunnerFn]" = None,
    expected_version: str = EXPECTED_FLUTTER_VERSION,
) -> PreflightCheck:
    """Priority 7 requirement 8: the EXACT pinned Flutter version, not just
    executable presence -- selectors/the UI suite verified on a different
    toolchain are not evidence for what will actually run."""
    path = which("flutter")
    if not path:
        return _check("flutter", STATUS_BLOCKED, "flutter is not on PATH.", hint="Install Flutter and add it to PATH.")
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30))
    try:
        result = runner([path, "--version", "--machine"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _check("flutter", STATUS_BLOCKED, f"Could not run 'flutter --version --machine': {exc}")
    from . import toolchain_verify as tv_mod

    version, _dart = tv_mod.parse_flutter_version_machine(result.stdout or "")
    if not version:
        return _check("flutter", STATUS_BLOCKED, f"Could not parse 'flutter --version --machine' output from {path}.")
    if expected_version is not None and version != expected_version:
        return _check(
            "flutter", STATUS_BLOCKED,
            f"flutter version {version!r} (at {path}) != pinned {expected_version!r} -- selectors/the UI "
            f"suite must run on the same toolchain product CI uses.",
        )
    return _check("flutter", STATUS_READY, f"flutter {version} (pinned) found at {path}.")


def check_mobile_device_scope(
    *,
    enabled_platforms: "list[str]",
    tablet_serial: "str | None",
    android_device: "str | None",
    android_connected: "list[str]",
    iphone_device: "str | None",
    iphone_connected_udids: "list[str] | None",
) -> "list[PreflightCheck]":
    """Priority 7 requirements 3-7: derives Android-mobile/iOS readiness from
    the RELEASE-AUTHORITATIVE ``enabled_platforms`` (composed from the
    release candidate, not the machine's own declared scope), and requires
    the CONFIGURED device identifier specifically:

      * the tablet's own serial never satisfies Android-mobile readiness --
        ``android_device`` must be configured AND distinct from
        ``tablet_serial`` AND actually connected;
      * "some iPhone is connected" never satisfies iOS readiness -- the
        CONFIGURED ``iphone_device`` UDID specifically must be present;
      * a required device whose state cannot be determined (no
        idevice_id -> ``iphone_connected_udids is None``) BLOCKS, it is
        never merely a WARNING.
    """
    checks: "list[PreflightCheck]" = []
    if "android" in enabled_platforms:
        if not android_device:
            checks.append(_check(
                "android_device_for_scope", STATUS_BLOCKED,
                "Release scope requires Android-mobile, but no android_device is configured in "
                "machine.local.yaml (a device identifier distinct from tablet_serial is required -- the "
                "tablet does not satisfy Android-mobile readiness).",
            ))
        elif tablet_serial is not None and android_device == tablet_serial:
            checks.append(_check(
                "android_device_for_scope", STATUS_BLOCKED,
                f"android_device ({android_device!r}) is the SAME serial as tablet_serial -- the tablet "
                f"alone does not satisfy Android-mobile readiness; a distinct Android phone/emulator is "
                f"required.",
            ))
        elif android_device in android_connected:
            checks.append(_check(
                "android_device_for_scope", STATUS_READY, f"Configured android_device {android_device!r} is connected.",
            ))
        else:
            checks.append(_check(
                "android_device_for_scope", STATUS_BLOCKED,
                f"Configured android_device {android_device!r} is not among connected devices {android_connected}.",
            ))
    if "ios" in enabled_platforms:
        if not iphone_device:
            checks.append(_check(
                "iphone_device_for_scope", STATUS_BLOCKED,
                "Release scope requires iOS, but no iphone_device is configured in machine.local.yaml.",
            ))
        elif iphone_connected_udids is None:
            checks.append(_check(
                "iphone_device_for_scope", STATUS_BLOCKED,
                "Release scope requires iOS, but iPhone connectivity could not be determined on this "
                "machine (idevice_id not on PATH) -- a required device whose state is unknown BLOCKS.",
                hint="Install libimobiledevice (idevice_id) to detect the configured iPhone.",
            ))
        elif iphone_device in iphone_connected_udids:
            checks.append(_check(
                "iphone_device_for_scope", STATUS_READY, f"Configured iphone_device {iphone_device!r} is connected.",
            ))
        else:
            checks.append(_check(
                "iphone_device_for_scope", STATUS_BLOCKED,
                f"Configured iphone_device {iphone_device!r} is not among connected iPhone UDIDs "
                f"{iphone_connected_udids}.",
            ))
    return checks


def check_mobile_devices_for_scope(
    mobile_platforms: "list[str]", *, android_connected: "list[str]", iphone_available: "bool | None" = None,
) -> "list[PreflightCheck]":
    """DEPRECATED (kept for any external/legacy caller): the coarse,
    machine-scope-only precursor to :func:`check_mobile_device_scope`.
    ``run_qualification_preflight`` no longer calls this -- it derives
    device-scope requirements from the composed release configuration via
    :func:`check_mobile_device_scope` instead (Priority 7)."""
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


def check_sibling_checkout_sha(
    name: str, path: Path, *, expected_sha: "str | None", required: bool = False, runner: "Optional[RunnerFn]" = None,
) -> PreflightCheck:
    """Priority 7 requirement 11: verify the ACTUAL checkout SHA, not merely
    that a Git checkout exists, when the caller supplied an expected SHA to
    check against (e.g. from the composed release config's expected
    identities)."""
    check_name = f"{name}_sha"
    if not expected_sha:
        return _check(check_name, STATUS_WARNING, f"No expected SHA supplied to check {name} against.")
    if not (path / ".git").exists():
        return _check(
            check_name, STATUS_BLOCKED if required else STATUS_WARNING,
            f"{path} is not a Git checkout -- cannot verify its SHA.",
        )
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    try:
        result = runner(["git", "-C", str(path), "rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(check_name, STATUS_BLOCKED if required else STATUS_WARNING, f"Could not read {name}'s HEAD SHA: {exc}")
    actual = (result.stdout or "").strip()
    if not actual:
        return _check(check_name, STATUS_BLOCKED if required else STATUS_WARNING, f"Could not determine {name}'s HEAD SHA.")
    if actual.lower() != expected_sha.strip().lower():
        return _check(
            check_name, STATUS_BLOCKED,
            f"{name} HEAD is {actual!r}, expected {expected_sha!r} -- checkout is not at the release-config-expected commit.",
        )
    return _check(check_name, STATUS_READY, f"{name} HEAD matches expected SHA {expected_sha!r}.")


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


def check_ics_publisher_config(section: "dict | None", *, required: bool = False) -> PreflightCheck:
    from . import url_validation

    if not section:
        return _check(
            "external_ics_publisher", STATUS_BLOCKED if required else STATUS_WARNING,
            "No subscribed_fixture section configured (offline-only mode will be used).",
        )
    publisher = section.get("publisher")
    public_url = section.get("public_url")
    if section.get("mode") != "published":
        status = STATUS_BLOCKED if required else STATUS_WARNING
        return _check("external_ics_publisher", status, f"subscribed_fixture.mode is {section.get('mode')!r}, not 'published' -- publisher config not required.")
    if not publisher:
        return _check("external_ics_publisher", STATUS_BLOCKED, "published mode configured but no publisher adapter is set.")
    if not public_url:
        return _check("external_ics_publisher", STATUS_BLOCKED, "published mode configured but no public_url is set.")
    problems = url_validation.validate_backend_url(public_url)
    if problems:
        return _check("external_ics_publisher", STATUS_BLOCKED, f"public_url is invalid: {'; '.join(problems)}")
    return _check("external_ics_publisher", STATUS_READY, f"Publisher {publisher!r} configured with a valid public_url.")


def check_public_ics_url(public_url: "str | None", *, opener: "Callable[[str], Any] | None" = None, required: bool = False) -> PreflightCheck:
    if not public_url:
        return _check("public_ics_url", STATUS_BLOCKED if required else STATUS_WARNING, "No public_url configured to check.")
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


def check_selector_ci_evidence_availability(*, env: "dict | None" = None, required: bool = False) -> PreflightCheck:
    from . import github_artifact as ga_mod

    token = ga_mod.resolve_token(env=env)
    if token is None:
        return _check(
            "selector_ci_evidence_availability", STATUS_BLOCKED if required else STATUS_WARNING,
            f"No GitHub API credential resolvable from {list(ga_mod.TOKEN_ENV_VARS)}.",
            hint="A production release requires a CI-authenticated selector artifact -- set REGRESSION_API_TOKEN/GITHUB_TOKEN/GH_TOKEN.",
        )
    return _check("selector_ci_evidence_availability", STATUS_READY, "A GitHub API credential is resolvable (value not shown).")


def check_distributed_build_evidence_availability(
    source_path: "Path | None", *, required: bool = False,
    expected_release_id: "str | None" = None, expected_git_sha: "str | None" = None,
    expected_version: "str | None" = None,
) -> PreflightCheck:
    from . import distributed_build_provenance as dbp

    if not source_path:
        return _check(
            "distributed_build_evidence_availability", STATUS_BLOCKED if required else STATUS_WARNING,
            "No --distributed-build-evidence path given to check.",
        )
    if not Path(source_path).is_file():
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path} does not exist.")
    try:
        evidence = json.loads(Path(source_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path} is not valid JSON: {exc}")
    if not isinstance(evidence, dict):
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path}: not a JSON object.")
    # Priority 7 requirement 13: validated against the RELEASE's own
    # id/SHA/version when supplied, not merely "is this file well-formed".
    problems = dbp.validate_distributed_evidence(
        evidence, expected_release_id=expected_release_id, expected_git_sha=expected_git_sha,
        expected_version=expected_version,
    )
    if problems:
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"Evidence at {source_path} has problem(s): {'; '.join(problems)}")
    return _check("distributed_build_evidence_availability", STATUS_READY, f"Distributed-build evidence at {source_path} is well-formed and matches the release identity.")


def check_frozen_candidate_ability(bundle_path: "Path | None", *, verification: "Any | None" = None) -> PreflightCheck:
    from . import release_installer

    if not bundle_path:
        return _check("frozen_candidate_ability", STATUS_WARNING, "No --bundle given to check.")
    if verification is None:
        verification = release_installer.verify_release_bundle(bundle_path)
    if not verification.ok:
        return _check(
            "frozen_candidate_ability", STATUS_BLOCKED,
            f"Bundle at {bundle_path} would fail verification: {'; '.join(verification.errors)}",
        )
    return _check("frozen_candidate_ability", STATUS_READY, f"Bundle at {bundle_path} verifies cleanly (release {verification.manifest.release_id}).")


def check_main_ci_evidence(
    summary_path: "Path | None", *, expected_sha: "str | None" = None, expected_repository: "str | None" = None,
    required: bool = False,
) -> PreflightCheck:
    """Priority 7 requirement 15 (offline half): structural verification of
    a downloaded main-CI evidence summary via the SAME canonical verifier
    verify-main-ci-evidence uses. STRUCTURAL VALIDATION ONLY -- see
    main_ci_evidence.py; this does not authenticate the file's origin (use
    ``--main-ci-workflow-run-id``/``--main-ci-artifact-id`` for that)."""
    from . import main_ci_evidence as mce_mod

    if not summary_path:
        return _check("main_ci_evidence", STATUS_BLOCKED if required else STATUS_WARNING, "No --main-ci-evidence path given to check.")
    try:
        summary, _raw = mce_mod.load_summary(summary_path)
    except mce_mod.MainCiEvidenceError as exc:
        return _check("main_ci_evidence", STATUS_BLOCKED, f"{summary_path}: {exc}")
    canonical_gates = None
    if expected_repository == mce_mod.CALEEMOBILE_REGRESSION_REPOSITORY:
        canonical_gates = mce_mod.CALEEMOBILE_REGRESSION_REQUIRED_GATES
    problems = mce_mod.verify_main_ci_evidence(
        summary, expected_sha=expected_sha or summary.get("commitSha") or "",
        expected_repository=expected_repository, canonical_required_gates=canonical_gates,
    )
    if problems:
        return _check("main_ci_evidence", STATUS_BLOCKED, f"{summary_path} has problem(s): {'; '.join(problems)}")
    return _check(
        "main_ci_evidence", STATUS_READY,
        f"Main-CI evidence at {summary_path} verified (STRUCTURAL VALIDATION ONLY -- origin not authenticated).",
    )


def check_main_ci_artifact_authenticated(
    *, repository: "str | None", workflow_run_id: "str | None", artifact_id: "str | None",
    expected_merge_sha: "str | None", required: bool = False, env: "dict | None" = None,
) -> PreflightCheck:
    """Priority 7 requirement 15 (authenticated half): when a workflow run
    id + artifact id are supplied, verify main-CI evidence via the
    AUTHENTICATED GitHub-API chain (Priority 6) instead of the offline,
    structural-only check. Read-only: performs GET requests only, never a
    write."""
    from . import main_ci_artifact as mca_mod
    from . import main_ci_evidence as mce_mod

    if not (workflow_run_id and artifact_id):
        return _check(
            "main_ci_artifact_authenticated", STATUS_WARNING,
            "No --main-ci-workflow-run-id/--main-ci-artifact-id given -- authenticated main-CI verification skipped "
            "(offline structural check only, see main_ci_evidence).",
        )
    if not (repository and expected_merge_sha):
        return _check(
            "main_ci_artifact_authenticated", STATUS_BLOCKED,
            "--main-ci-workflow-run-id/--main-ci-artifact-id were given, but --main-ci-repository/an expected "
            "merge SHA are required to authenticate against.",
        )
    profile = mca_mod.KNOWN_PROFILES.get(repository)
    if profile is None:
        return _check(
            "main_ci_artifact_authenticated", STATUS_BLOCKED,
            f"--main-ci-repository {repository!r} is not a recognised profile ({sorted(mca_mod.KNOWN_PROFILES)}).",
        )
    canonical_gates = mce_mod.CALEEMOBILE_REGRESSION_REQUIRED_GATES if repository == mce_mod.CALEEMOBILE_REGRESSION_REPOSITORY else None
    try:
        chain = mca_mod.acquire_main_ci_artifact(
            repository=repository, workflow_path=profile["workflow_path"], run_id=workflow_run_id,
            artifact_id=artifact_id, expected_merge_sha=expected_merge_sha,
            expected_artifact_name=f"{profile['artifact_prefix']}{expected_merge_sha}",
            expected_result_filename=profile["result_filename"], canonical_required_gates=canonical_gates,
            env=env,
        )
    except mca_mod.MainCiArtifactError as exc:
        return _check("main_ci_artifact_authenticated", STATUS_BLOCKED, str(exc))
    if not chain.ok:
        return _check(
            "main_ci_artifact_authenticated", STATUS_BLOCKED,
            f"Authenticated main-CI artifact rejected: {'; '.join(chain.problems)}",
        )
    return _check(
        "main_ci_artifact_authenticated", STATUS_READY,
        f"Authenticated merged-main CI artifact verified for {repository} (run {workflow_run_id}, commit {expected_merge_sha}).",
    )


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
        # Priority 7 requirements 16-17: BLOCKED beats WARNING beats READY --
        # any warning prevents an unqualified READY. The previous behaviour
        # (any number of WARNINGs still reported READY) was exactly the
        # fail-open defect this closes.
        if any(c.status == STATUS_BLOCKED for c in self.checks):
            return STATUS_BLOCKED
        if any(c.status == STATUS_WARNING for c in self.checks):
            return STATUS_WARNING
        return STATUS_READY

    def to_dict(self) -> dict:
        # Priority 7 requirement 18: explain exactly which required
        # capability is missing, not just an aggregate status.
        blocked = [c.name for c in self.checks if c.status == STATUS_BLOCKED]
        warned = [c.name for c in self.checks if c.status == STATUS_WARNING]
        return {
            "overall": self.overall.upper(),
            "blockedCapabilities": blocked,
            "warnedCapabilities": warned,
            "checks": [c.to_dict() for c in self.checks],
        }


def _compose_effective_release_config(
    *, cfg, repo_root: Path, bundle_path: "Path | None", verification: "Any | None",
):
    """Priority 7 requirements 2-3: compose the SAME effective release
    configuration the real launcher (release-config) would, so every
    required check below is derived from the RELEASE, not merely from the
    machine's own declared capability scope. Returns None when there is no
    machine config or no bundle to compose from (nothing release-authoritative
    to derive -- callers fall back to machine-scope-only checks in that case,
    exactly like before this priority existed)."""
    if cfg is None or verification is None or not verification.ok:
        return None
    from . import release_config as rc_mod
    from . import release_platforms as rp_mod

    bundle_manifest = verification.manifest
    is_v2 = bundle_manifest is not None and bundle_manifest.is_schema_v2
    if is_v2:
        platforms = rp_mod.ReleasePlatforms()
        features = rp_mod.ReleaseFeatures()
        expected = rp_mod.ExpectedBuildIdentity()
        expected_backend = None
        distributed_build_required = False
        release_id = bundle_manifest.release_id
    else:
        try:
            platforms = rp_mod.load_release_platforms()
            features = rp_mod.load_release_features()
            expected = rp_mod.load_expected_build_identity()
        except rp_mod.ReleasePlatformsError:
            return None
        expected_backend = None
        distributed_build_required = False
        release_id = bundle_manifest.release_id if bundle_manifest is not None else None
        resolved_platforms_path = rp_mod.DEFAULT_CONFIG_PATH
        if resolved_platforms_path.is_file():
            try:
                import yaml as _yaml

                raw = _yaml.safe_load(resolved_platforms_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    expected_backend = raw.get("backend") or raw.get("expected_backend") or None
                    distributed_build_required = bool(raw.get("distributed_build_required", False))
                    release_id = release_id or raw.get("release_id")
            except Exception:  # noqa: BLE001 - best-effort, never crash the preflight
                pass

    return rc_mod.compose_effective_release_config(
        cfg, platforms, features, expected,
        run_id="qualification-preflight", release_id=release_id,
        expected_backend=expected_backend, distributed_build_required=distributed_build_required,
        bundle_manifest=bundle_manifest,
    )


def run_qualification_preflight(
    *,
    config_path: "Path | None" = None,
    bundle_path: "Path | None" = None,
    distributed_build_evidence_path: "Path | None" = None,
    repo_root: "Path | None" = None,
    manual_checks_path: "Path | None" = None,
    main_ci_evidence_path: "Path | None" = None,
    main_ci_repository: "str | None" = None,
    main_ci_workflow_run_id: "str | None" = None,
    main_ci_artifact_id: "str | None" = None,
    expected_caleemobile_sha: "str | None" = None,
    expected_caleemobile_regression_sha: "str | None" = None,
    adb_runner=None,
    which: WhichFn = shutil.which,
    http_opener=None,
    subprocess_runner: "Optional[RunnerFn]" = None,
    env: "dict | None" = None,
) -> PreflightReport:
    """Run every qualification check and return one report. Read-only
    throughout -- see the module docstring. Every I/O seam is injectable so
    this runs fully offline in tests.

    Priority 7: the release bundle (when given) is verified FIRST, its
    effective release configuration is composed exactly like the real
    launcher would, and every platform/feature/evidence requirement below is
    derived from THAT -- not from the machine's own declared capability
    scope. A machine.local.yaml under-declaring what the release actually
    needs no longer silently narrows what gets checked.
    """
    from . import machine_config as mc
    from . import release_installer as ri_mod

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
    checks.append(check_android_sdk(which=which, env=environ))

    availability, serial_check, connected_serials = check_adb_devices(
        cfg.tablet_serial if cfg else None, adb_runner=adb_runner,
    )
    checks.append(availability)
    checks.append(serial_check)

    # Priority 7 requirement 1: the release bundle is verified FIRST, before
    # any requirement derivation below depends on it.
    verification = None
    if bundle_path:
        verification = ri_mod.verify_release_bundle(bundle_path)
    checks.append(check_frozen_candidate_ability(bundle_path, verification=verification))

    effective = _compose_effective_release_config(
        cfg=cfg, repo_root=repo_root, bundle_path=bundle_path, verification=verification,
    )
    if effective is not None:
        # Surface every BLOCKING conflict the composition itself already
        # derived (platform capability gaps, kiosk_admin authorisation,
        # profile/backend disagreement, identity mismatches) as its own
        # preflight check -- release-authoritative for free, since this is
        # the exact same reconciliation release-config performs.
        for conflict in effective.conflicts:
            if conflict.blocking:
                checks.append(_check(
                    f"release_scope_conflict:{conflict.axis}", STATUS_BLOCKED, conflict.explanation,
                ))
        enabled_platforms = list(effective.enabled_platforms)
        enabled_features = list(effective.enabled_features)
        profile = effective.profile
        expected_identities = effective.expected_identities or {}
    else:
        # No bundle / no machine config / bundle verification failed --
        # nothing release-authoritative to derive from. Fall back to the
        # machine's OWN declared scope (the pre-Priority-7 behaviour) so a
        # bare diagnostic/no-bundle invocation still says something useful,
        # but nothing here is escalated to a release REQUIREMENT.
        enabled_platforms = list(cfg.mobile_platforms) if cfg else []
        if cfg and cfg.tablet_serial:
            enabled_platforms = enabled_platforms + ["tablet"]
        enabled_features = []
        profile = None
        expected_identities = {}

    checks.append(check_android_build_tools(which=which, required=bool({"tablet", "android"} & set(enabled_platforms))))

    checks.append(check_appium(_load_appium_url(repo_root), opener=http_opener))
    required_drivers = []
    if {"tablet", "android"} & set(enabled_platforms):
        required_drivers.append("uiautomator2")
    if "ios" in enabled_platforms:
        required_drivers.append("xcuitest")
    checks.append(check_appium_drivers(required_drivers=required_drivers, which=which, runner=subprocess_runner))

    checks.append(check_flutter(which=which, runner=subprocess_runner))

    iphone_connected_udids = (
        check_iphone_connected_udids(which=which, runner=subprocess_runner) if "ios" in enabled_platforms else None
    )
    checks.extend(check_mobile_device_scope(
        enabled_platforms=enabled_platforms,
        tablet_serial=cfg.tablet_serial if cfg else None,
        android_device=cfg.android_device if cfg else None,
        android_connected=connected_serials,
        iphone_device=cfg.iphone_device if cfg else None,
        iphone_connected_udids=iphone_connected_udids,
    ))

    checks.append(check_sibling_checkout("caleemobile_checkout", repo_root.parent / "CaleeMobile"))
    checks.append(check_sibling_checkout("caleemobile_regression_checkout", repo_root.parent / "CaleeMobile-Regression"))
    caleemobile_expected_sha = expected_caleemobile_sha or expected_identities.get("caleeMobile", {}).get("gitSha")
    checks.append(check_sibling_checkout_sha(
        "caleemobile_checkout", repo_root.parent / "CaleeMobile", expected_sha=caleemobile_expected_sha,
        required=bool(caleemobile_expected_sha), runner=subprocess_runner,
    ))
    checks.append(check_sibling_checkout_sha(
        "caleemobile_regression_checkout", repo_root.parent / "CaleeMobile-Regression",
        expected_sha=expected_caleemobile_regression_sha, required=bool(expected_caleemobile_regression_sha),
        runner=subprocess_runner,
    ))

    checks.append(check_keychain_credentials())

    section = {}
    if cfg is not None:
        try:
            import yaml as _yaml

            raw = _yaml.safe_load(Path(resolved_config_path).read_text(encoding="utf-8"))
            section = (raw or {}).get("subscribed_fixture") or {}
        except Exception:  # noqa: BLE001 - best-effort, never crash the preflight
            section = {}
    calendar_required = "google_calendar" in enabled_features
    checks.append(check_ics_publisher_config(section, required=calendar_required))
    checks.append(check_public_ics_url(section.get("public_url"), opener=http_opener, required=calendar_required))
    checks.append(check_ingestion_bridge(repo_root))

    from . import release_config as rc_mod

    selector_required = bool(rc_mod.resolve_selector_evidence_required(
        profile=profile, enabled_platforms=enabled_platforms,
        schema_version=effective.schema_version if effective is not None else None,
        manifest_required=expected_identities.get("caleeMobile", {}).get("selectorEvidenceRequired"),
    ))
    checks.append(check_selector_ci_evidence_availability(env=environ, required=selector_required))

    distributed_required = bool(expected_identities.get("caleeMobile", {}).get("distributedBuildAcceptanceRequired"))
    caleemobile_identity = expected_identities.get("caleeMobile", {}) or {}
    checks.append(check_distributed_build_evidence_availability(
        distributed_build_evidence_path, required=distributed_required,
        expected_release_id=effective.release_id if effective is not None else None,
        expected_git_sha=caleemobile_identity.get("gitSha"),
        expected_version=caleemobile_identity.get("buildVersion"),
    ))

    checks.append(check_main_ci_evidence(
        main_ci_evidence_path, expected_repository=main_ci_repository, required=bool(main_ci_evidence_path),
    ))
    checks.append(check_main_ci_artifact_authenticated(
        repository=main_ci_repository, workflow_run_id=main_ci_workflow_run_id, artifact_id=main_ci_artifact_id,
        expected_merge_sha=caleemobile_identity.get("gitSha"), env=environ,
    ))

    checks.append(check_manual_check_definitions(manual_checks_path))

    return PreflightReport(checks=checks)
