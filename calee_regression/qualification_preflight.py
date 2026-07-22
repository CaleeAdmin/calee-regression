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
# Priority 11 (this session): a SECTION-level status (see PreflightReport.
# sections) meaning "nothing in this release scope needs this capability at
# all" -- distinct from STATUS_WARNING, which still means "this might matter
# and its state is unresolved/ambiguous". Never returned as an individual
# PreflightCheck.status (existing callers/tests keep seeing WARNING there
# unchanged); a check instead sets PreflightCheck.not_applicable=True
# alongside its existing WARNING status, and section rollup (below) is what
# turns "every check in this section is READY or not_applicable" into an
# overall NOT_APPLICABLE for that section.
STATUS_NOT_APPLICABLE = "not_applicable"

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
    # Priority 11 (this session): set alongside status=STATUS_WARNING when
    # this check's absence/ambiguity is WARNING only because it isn't
    # required for the current release scope -- never changes .status
    # itself (existing callers keep seeing WARNING there), only feeds
    # section-level NOT_APPLICABLE rollup (see PreflightReport.sections).
    not_applicable: bool = False

    def to_dict(self) -> dict:
        data = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.hint:
            data["hint"] = self.hint
        if self.not_applicable:
            data["notApplicableToReleaseScope"] = True
        return data


def _check(name: str, status: str, detail: str, hint: "str | None" = None, not_applicable: bool = False) -> PreflightCheck:
    return PreflightCheck(name=name, status=status, detail=detail, hint=hint, not_applicable=not_applicable)


def _required_or_warning(*, required: bool, ok: bool, name: str, ok_detail: str, missing_detail: str, hint: "str | None" = None) -> PreflightCheck:
    """Shared shape for "this capability is READY, or its absence is BLOCKED
    (required) / WARNING (not required)" -- never silently PASS."""
    if ok:
        return _check(name, STATUS_READY, ok_detail)
    return _check(name, STATUS_BLOCKED if required else STATUS_WARNING, missing_detail, hint=hint, not_applicable=not required)


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


def check_appium(
    appium_url: "str | None", *, opener: "Callable[[str], Any] | None" = None, required: bool = False,
) -> PreflightCheck:
    """Priority 9 (this session): ``required`` reflects whether the release
    scope actually needs Appium-driven mobile UI testing at all (any of
    tablet/android/ios) -- a technical owner running a scope with no mobile
    platform in it is never blocked merely because Appium isn't configured.
    When it IS required, an unconfigured Appium is a real gap and BLOCKS,
    not a mere warning; an appium_url that IS configured but unreachable
    always BLOCKS regardless, since a stale/wrong URL is worth surfacing
    hard either way."""
    if not appium_url:
        return _check("appium", STATUS_BLOCKED if required else STATUS_WARNING, "No appium_url configured.", not_applicable=not required)
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
        return _check("appium_drivers", STATUS_WARNING, "No platform in scope requires a specific Appium driver.", not_applicable=True)
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
    expected_version: str = EXPECTED_FLUTTER_VERSION, required: bool = True,
) -> PreflightCheck:
    """Priority 7 requirement 8: the EXACT pinned Flutter version, not just
    executable presence -- selectors/the UI suite verified on a different
    toolchain are not evidence for what will actually run.

    Priority 9 (this session): ``required`` reflects whether the release
    scope actually needs the CaleeMobile Flutter toolchain at all (any of
    tablet/android/ios in scope -- CaleeMobile's Flutter codebase underlies
    all three) -- a release scope with no mobile platform enabled is never
    blocked merely because Flutter isn't installed on this machine.
    Defaults to ``True`` (the previous, unconditional behaviour) so a
    direct/legacy caller sees no change; :func:`run_qualification_preflight`
    passes the release-derived value."""
    path = which("flutter")
    if not path:
        return _check(
            "flutter", STATUS_BLOCKED if required else STATUS_WARNING, "flutter is not on PATH.",
            hint="Install Flutter and add it to PATH.", not_applicable=not required,
        )
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=30))
    try:
        result = runner([path, "--version", "--machine"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(
            "flutter", STATUS_BLOCKED if required else STATUS_WARNING, f"Could not run 'flutter --version --machine': {exc}",
            not_applicable=not required,
        )
    from . import toolchain_verify as tv_mod

    version, _dart = tv_mod.parse_flutter_version_machine(result.stdout or "")
    if not version:
        return _check(
            "flutter", STATUS_BLOCKED if required else STATUS_WARNING,
            f"Could not parse 'flutter --version --machine' output from {path}.", not_applicable=not required,
        )
    if expected_version is not None and version != expected_version:
        return _check(
            "flutter", STATUS_BLOCKED if required else STATUS_WARNING,
            f"flutter version {version!r} (at {path}) != pinned {expected_version!r} -- selectors/the UI "
            f"suite must run on the same toolchain product CI uses.", not_applicable=not required,
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
        return _check(check_name, STATUS_WARNING, f"No expected SHA supplied to check {name} against.", not_applicable=True)
    if not (path / ".git").exists():
        return _check(
            check_name, STATUS_BLOCKED if required else STATUS_WARNING,
            f"{path} is not a Git checkout -- cannot verify its SHA.", not_applicable=not required,
        )
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    try:
        result = runner(["git", "-C", str(path), "rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(
            check_name, STATUS_BLOCKED if required else STATUS_WARNING, f"Could not read {name}'s HEAD SHA: {exc}",
            not_applicable=not required,
        )
    actual = (result.stdout or "").strip()
    if not actual:
        return _check(
            check_name, STATUS_BLOCKED if required else STATUS_WARNING, f"Could not determine {name}'s HEAD SHA.",
            not_applicable=not required,
        )
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
            "No subscribed_fixture section configured (offline-only mode will be used).", not_applicable=not required,
        )
    publisher = section.get("publisher")
    public_url = section.get("public_url")
    if section.get("mode") != "published":
        status = STATUS_BLOCKED if required else STATUS_WARNING
        return _check(
            "external_ics_publisher", status,
            f"subscribed_fixture.mode is {section.get('mode')!r}, not 'published' -- publisher config not required.",
            not_applicable=not required,
        )
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
        return _check(
            "public_ics_url", STATUS_BLOCKED if required else STATUS_WARNING, "No public_url configured to check.",
            not_applicable=not required,
        )
    opener = opener or (lambda u: urllib.request.urlopen(u, timeout=10))
    try:
        opener(public_url)
    except Exception as exc:  # noqa: BLE001 - reachability probe
        return _check("public_ics_url", STATUS_BLOCKED, f"public_url {public_url} is not reachable: {exc}")
    return _check("public_ics_url", STATUS_READY, f"public_url {public_url} is reachable.")


def check_ingestion_bridge(repo_root: Path, *, required: bool = False) -> PreflightCheck:
    """Priority 9 (this session): ``required`` reflects whether the release
    scope actually enables the ``google_calendar`` (subscribed-calendar)
    feature -- the ingestion bridge only matters for that sync path. A
    release with no subscribed-calendar feature enabled is never blocked
    merely because the bridge is unavailable; one that DOES enable it BLOCKS
    on a missing bridge instead of merely warning, mirroring
    check_ics_publisher_config/check_public_ics_url's existing scoping."""
    from . import sync_smoke_bridge as ssb_mod

    if ssb_mod.is_ingestion_bridge_available(repo_root):
        return _check("ingestion_api_bridge", STATUS_READY, "CaleeMobile-Regression ingestion bridge is available.")
    return _check(
        "ingestion_api_bridge", STATUS_BLOCKED if required else STATUS_WARNING,
        "CaleeMobile-Regression ingestion bridge is not available (sibling checkout/bridge action missing).",
        not_applicable=not required,
    )


def check_selector_ci_evidence_availability(
    *, env: "dict | None" = None, required: bool = False,
    workflow_run_id: "str | None" = None, artifact_id: "str | None" = None,
    selector_artifact_zip: "str | Path | None" = None,
    expected_regression_sha: "str | None" = None, expected_tested_sha: "str | None" = None,
    expected_version: "str | None" = None, expected_release_id: "str | None" = None,
) -> PreflightCheck:
    """Priority 6 (this session): a resolvable GitHub API credential alone is
    NOT evidence -- it only proves a request COULD be made, not that a real
    selector-contract artifact for this release was ever produced or
    verified. When a workflow run id + artifact id are supplied, this runs
    the SAME authenticated artifact chain the release-gating
    ``selector-contract`` command itself uses
    (:func:`github_artifact.acquire_github_artifact`) -- repository/workflow-
    path/run-success/artifact-ownership/digest verification, extracted
    identity checked against the expected CaleeMobile-Regression SHA and
    CaleeMobile tested SHA/version -- so a preflight READY here means a real
    artifact was independently authenticated, never merely that credentials
    exist. ``selector_artifact_zip`` is the same already-downloaded ZIP input
    accepted by the release launcher. It replaces only the redirected archive
    download: GitHub API authentication of the run, jobs, artifact ownership,
    and recorded digest is always retained."""
    from . import github_artifact as ga_mod

    token = ga_mod.resolve_token(env=env)
    if token is None:
        return _check(
            "selector_ci_evidence_availability", STATUS_BLOCKED if required else STATUS_WARNING,
            f"No GitHub API credential resolvable from {list(ga_mod.TOKEN_ENV_VARS)}.",
            hint="A production release requires a CI-authenticated selector artifact -- set REGRESSION_API_TOKEN/GITHUB_TOKEN/GH_TOKEN.",
            not_applicable=not required,
        )
    if not (workflow_run_id and artifact_id):
        return _check(
            "selector_ci_evidence_availability", STATUS_BLOCKED if required else STATUS_WARNING,
            "A GitHub API credential is resolvable, but no --selector-workflow-run-id/--selector-artifact-id "
            "was given -- credential PRESENCE is never treated as evidence; authenticated selector-artifact "
            "verification was skipped.",
            hint="Supply --selector-workflow-run-id/--selector-artifact-id to authenticate a real "
                 "selector-contract-result artifact via the GitHub API.",
            not_applicable=not required,
        )
    try:
        chain = ga_mod.acquire_github_artifact(
            run_id=workflow_run_id, artifact_id=artifact_id,
            local_zip_path=str(selector_artifact_zip) if selector_artifact_zip else None,
            expected_regression_sha=expected_regression_sha, expected_tested_sha=expected_tested_sha,
            expected_version=expected_version, expected_release_id=expected_release_id, env=env,
        )
    except ga_mod.GithubArtifactError as exc:
        return _check("selector_ci_evidence_availability", STATUS_BLOCKED, str(exc))
    if not chain.ok:
        return _check(
            "selector_ci_evidence_availability", STATUS_BLOCKED,
            f"Authenticated selector-contract artifact rejected: {'; '.join(chain.problems)}",
        )
    return _check(
        "selector_ci_evidence_availability", STATUS_READY,
        f"Authenticated selector-contract artifact verified (run {workflow_run_id}, artifact {artifact_id}"
        f"{' using the supplied local ZIP' if selector_artifact_zip else ''}).",
    )


def check_distributed_build_evidence_availability(
    source_path: "Path | None", *, required: bool = False,
    expected_release_id: "str | None" = None, expected_git_sha: "str | None" = None,
    expected_version: "str | None" = None,
) -> PreflightCheck:
    """Priority 7 (this session): re-verify the run-scoped distributed-build-
    acceptance REPORT -- the exact ``results.json``
    ``record-distributed-build-acceptance`` writes -- through the SAME
    independent re-verification consolidation itself relies on
    (:func:`consolidated_report.component_from_distributed_build_acceptance_
    report`). The previous version of this check ran ``validate_distributed_
    evidence`` -- a FORMAT-only check -- directly over whatever JSON was at
    ``source_path``; a hand-typed file with plausible-looking fields (a
    fabricated provider/channel/build id, no authenticated provenance at
    all) passed every one of those rules. Re-using the consolidation
    function means a report missing its authenticated ``provenance`` block,
    whose envelope/raw-byte digest doesn't match the adjacent evidence
    bundle, or whose ``evidenceTier`` isn't one of ``provider_evidence.
    AUTHENTICATED_TIERS``, BLOCKS here exactly as it would at consolidation
    -- closing the "arbitrary --source JSON passes every offline format
    check" hole for the preflight path too."""
    from . import consolidated_report as cr_mod

    if not source_path:
        return _check(
            "distributed_build_evidence_availability", STATUS_BLOCKED if required else STATUS_WARNING,
            "No --distributed-build-evidence path given to check.", not_applicable=not required,
        )
    source_path = Path(source_path)
    if not source_path.is_file():
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path} does not exist.")
    try:
        report = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path} is not valid JSON: {exc}")
    if not isinstance(report, dict):
        return _check("distributed_build_evidence_availability", STATUS_BLOCKED, f"{source_path}: not a JSON object.")

    result = cr_mod.component_from_distributed_build_acceptance_report(
        "distributed_build_evidence_availability", report, mandatory=required,
        expected_git_sha=expected_git_sha, expected_version=expected_version,
        expected_release_id=expected_release_id, component_dir=source_path.parent,
    )
    if result.status == cr_mod.STATUS_PASS:
        return _check(
            "distributed_build_evidence_availability", STATUS_READY,
            "; ".join(result.detail) or f"Distributed-build acceptance evidence at {source_path} is authenticated and matches the release identity.",
        )
    if result.status == cr_mod.STATUS_NOT_RUN:
        return _check(
            "distributed_build_evidence_availability", STATUS_BLOCKED if required else STATUS_WARNING,
            "; ".join(result.detail) or f"No distributed-build acceptance evidence at {source_path}.",
            not_applicable=not required,
        )
    return _check(
        "distributed_build_evidence_availability", STATUS_BLOCKED,
        f"{source_path}: " + ("; ".join(result.detail) or "distributed-build acceptance evidence rejected."),
    )


def check_frozen_candidate_ability(bundle_path: "Path | None", *, verification: "Any | None" = None) -> PreflightCheck:
    from . import release_installer

    if not bundle_path:
        return _check("frozen_candidate_ability", STATUS_WARNING, "No --bundle given to check.", not_applicable=True)
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
        return _check(
            "main_ci_evidence", STATUS_BLOCKED if required else STATUS_WARNING, "No --main-ci-evidence path given to check.",
            not_applicable=not required,
        )
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
    *, check_name: str = "main_ci_artifact_authenticated", repository: "str | None",
    workflow_run_id: "str | None", artifact_id: "str | None",
    expected_merge_sha: "str | None", required: bool = False, env: "dict | None" = None,
    run_id_flag: str = "--main-ci-workflow-run-id", artifact_id_flag: str = "--main-ci-artifact-id",
    sha_flag: str = "--expected-merge-sha",
) -> PreflightCheck:
    """Priority 8 (this session): verify ONE repository's merged-main CI via
    the AUTHENTICATED GitHub-API chain instead of the offline, structural-
    only check. Read-only: performs GET requests only, never a write.

    ``repository``/``expected_merge_sha`` identify exactly which repository
    and commit this call is authenticating -- callers MUST pass the
    repository's OWN expected SHA (see :func:`run_qualification_preflight`,
    which calls this once per regression repository with its own distinct
    ``--calee-regression-main-sha`` / ``--caleemobile-regression-main-sha``
    input; reusing the CaleeMobile product SHA for a regression repository's
    own main-CI check would silently compare against the wrong commit
    entirely). ``check_name`` lets multiple independent calls (one per
    repository) each surface as their own named check in the report rather
    than colliding under one shared name.
    """
    from . import main_ci_artifact as mca_mod
    from . import main_ci_evidence as mce_mod

    if not (workflow_run_id and artifact_id):
        return _check(
            check_name, STATUS_BLOCKED if required else STATUS_WARNING,
            f"No {run_id_flag}/{artifact_id_flag} given -- authenticated main-CI verification skipped "
            "(offline structural check only, see main_ci_evidence).",
            not_applicable=not required,
        )
    if not (repository and expected_merge_sha):
        return _check(
            check_name, STATUS_BLOCKED,
            f"{run_id_flag}/{artifact_id_flag} were given, but a repository and {sha_flag} are required to "
            "authenticate against.",
        )
    profile = mca_mod.KNOWN_PROFILES.get(repository)
    if profile is None:
        return _check(
            check_name, STATUS_BLOCKED,
            f"repository {repository!r} is not a recognised profile ({sorted(mca_mod.KNOWN_PROFILES)}).",
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
        return _check(check_name, STATUS_BLOCKED, str(exc))
    if not chain.ok:
        return _check(
            check_name, STATUS_BLOCKED,
            f"Authenticated main-CI artifact rejected: {'; '.join(chain.problems)}",
        )
    return _check(
        check_name, STATUS_READY,
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


# Priority 11 (this session): qualification output must separate these
# named identity/evidence/infrastructure buckets, each independently
# reporting READY/WARNING/BLOCKED/NOT_APPLICABLE with remediation guidance
# -- never one flat, undifferentiated check list a technical owner has to
# mentally partition themselves. Order here is the order sections are
# reported in.
SECTION_RELEASE_CANDIDATE_IDENTITY = "release_candidate_identity"
SECTION_PRODUCT_BUILD_IDENTITY = "product_build_identity"
SECTION_REGRESSION_FRAMEWORK_IDENTITY = "regression_framework_identity"
SECTION_SELECTOR_EVIDENCE = "selector_evidence"
SECTION_DISTRIBUTED_BUILD_PROVIDER_EVIDENCE = "distributed_build_provider_evidence"
SECTION_DISTRIBUTED_BUILD_PROVENANCE = "distributed_build_provenance"
SECTION_PHYSICAL_DEVICES = "physical_devices"
SECTION_TOOLCHAINS = "toolchains"
SECTION_SUBSCRIBED_CALENDAR_INFRASTRUCTURE = "subscribed_calendar_infrastructure"
SECTION_CREDENTIALS = "credentials"

SECTION_TITLES = {
    SECTION_RELEASE_CANDIDATE_IDENTITY: "Release Candidate Identity",
    SECTION_PRODUCT_BUILD_IDENTITY: "Product Build Identity (CaleeMobile)",
    SECTION_REGRESSION_FRAMEWORK_IDENTITY: "Regression Framework Identity (calee-regression + CaleeMobile-Regression)",
    SECTION_SELECTOR_EVIDENCE: "Selector Evidence",
    SECTION_DISTRIBUTED_BUILD_PROVIDER_EVIDENCE: "Distributed Build Provider Evidence",
    SECTION_DISTRIBUTED_BUILD_PROVENANCE: "Distributed Build Provenance",
    SECTION_PHYSICAL_DEVICES: "Physical Devices",
    SECTION_TOOLCHAINS: "Toolchains",
    SECTION_SUBSCRIBED_CALENDAR_INFRASTRUCTURE: "Subscribed-Calendar Infrastructure",
    SECTION_CREDENTIALS: "Credentials",
}

# Every check name this module ever produces is mapped to exactly one
# section, EXCEPT distributed_build_evidence_availability, which maps to
# BOTH distributed-build sections: this codebase verifies the provider-
# observation and build-provenance halves of the identity chain together,
# as one joined, authenticated record (see build_provenance.
# join_provider_and_build_provenance) -- there is no separate standalone
# check for either half alone. release_scope_conflict:<axis> (a dynamic
# per-conflict name) is matched by prefix in _sections_for_check below, not
# listed here.
_SECTION_BY_CHECK_NAME = {
    "machine_config": SECTION_RELEASE_CANDIDATE_IDENTITY,
    "report_root": SECTION_RELEASE_CANDIDATE_IDENTITY,
    "frozen_candidate_ability": SECTION_RELEASE_CANDIDATE_IDENTITY,
    "manual_check_definitions": SECTION_RELEASE_CANDIDATE_IDENTITY,
    "caleemobile_checkout": SECTION_PRODUCT_BUILD_IDENTITY,
    "caleemobile_checkout_sha": SECTION_PRODUCT_BUILD_IDENTITY,
    "caleemobile_regression_checkout": SECTION_REGRESSION_FRAMEWORK_IDENTITY,
    "caleemobile_regression_checkout_sha": SECTION_REGRESSION_FRAMEWORK_IDENTITY,
    "main_ci_evidence": SECTION_REGRESSION_FRAMEWORK_IDENTITY,
    "calee_regression_main_ci_authenticated": SECTION_REGRESSION_FRAMEWORK_IDENTITY,
    "caleemobile_regression_main_ci_authenticated": SECTION_REGRESSION_FRAMEWORK_IDENTITY,
    "selector_ci_evidence_availability": SECTION_SELECTOR_EVIDENCE,
    "distributed_build_evidence_availability": (
        SECTION_DISTRIBUTED_BUILD_PROVIDER_EVIDENCE, SECTION_DISTRIBUTED_BUILD_PROVENANCE,
    ),
    "adb_device_availability": SECTION_PHYSICAL_DEVICES,
    "expected_tablet_serial": SECTION_PHYSICAL_DEVICES,
    "android_device_for_scope": SECTION_PHYSICAL_DEVICES,
    "iphone_device_for_scope": SECTION_PHYSICAL_DEVICES,
    "android_sdk_tools": SECTION_TOOLCHAINS,
    "android_build_tools": SECTION_TOOLCHAINS,
    "appium": SECTION_TOOLCHAINS,
    "appium_drivers": SECTION_TOOLCHAINS,
    "flutter": SECTION_TOOLCHAINS,
    "external_ics_publisher": SECTION_SUBSCRIBED_CALENDAR_INFRASTRUCTURE,
    "public_ics_url": SECTION_SUBSCRIBED_CALENDAR_INFRASTRUCTURE,
    "ingestion_api_bridge": SECTION_SUBSCRIBED_CALENDAR_INFRASTRUCTURE,
    "keychain_credentials": SECTION_CREDENTIALS,
}


def _sections_for_check(check: "PreflightCheck") -> "tuple[str, ...]":
    if check.name.startswith("release_scope_conflict:"):
        return (SECTION_RELEASE_CANDIDATE_IDENTITY,)
    mapped = _SECTION_BY_CHECK_NAME.get(check.name)
    if mapped is None:
        return ()
    return mapped if isinstance(mapped, tuple) else (mapped,)


def _section_status(checks: "list[PreflightCheck]") -> str:
    """BLOCKED beats a genuine WARNING beats READY/NOT_APPLICABLE -- mirrors
    PreflightReport.overall's own precedence. A check tagged not_applicable
    never counts as a genuine warning here; a section where EVERY
    constituent check is not_applicable rolls up to NOT_APPLICABLE as a
    whole (nothing in it pertains to this release scope); an empty section
    (no check in this run ever mapped to it) is likewise NOT_APPLICABLE."""
    if not checks:
        return STATUS_NOT_APPLICABLE
    if any(c.status == STATUS_BLOCKED for c in checks):
        return STATUS_BLOCKED
    if any(c.status == STATUS_WARNING and not c.not_applicable for c in checks):
        return STATUS_WARNING
    if all(c.not_applicable for c in checks):
        return STATUS_NOT_APPLICABLE
    return STATUS_READY


@dataclass
class PreflightReport:
    checks: "list[PreflightCheck]" = field(default_factory=list)

    @property
    def overall(self) -> str:
        # Priority 7 requirements 16-17: BLOCKED beats WARNING beats READY --
        # any warning prevents an unqualified READY. The previous behaviour
        # (any number of WARNINGs still reported READY) was exactly the
        # fail-open defect this closes. Deliberately NOT relaxed for
        # not_applicable-tagged warnings (see Priority 11's section-level
        # rollup for that): a release-wide READY still requires every
        # individual check to be genuinely READY, never merely "not
        # applicable" -- see test_overall_never_ready_while_any_warning_
        # present_in_full_orchestration, which pins this exact invariant.
        if any(c.status == STATUS_BLOCKED for c in self.checks):
            return STATUS_BLOCKED
        if any(c.status == STATUS_WARNING for c in self.checks):
            return STATUS_WARNING
        return STATUS_READY

    def sections(self) -> "list[dict]":
        """Priority 11: the SAME checks as .checks, grouped into the named
        buckets qualification output must separate, each independently
        rolled up to READY/WARNING/BLOCKED/NOT_APPLICABLE with its own
        remediation guidance (collected from the constituent checks' own
        hints -- never a secret value, see PreflightCheck.to_dict)."""
        by_section: "dict[str, list[PreflightCheck]]" = {key: [] for key in SECTION_TITLES}
        for check in self.checks:
            for section in _sections_for_check(check):
                by_section[section].append(check)
        result = []
        for key, title in SECTION_TITLES.items():
            section_checks = by_section[key]
            remediation = [c.hint for c in section_checks if c.hint and c.status != STATUS_READY]
            result.append({
                "section": key,
                "title": title,
                "status": _section_status(section_checks).upper(),
                "checks": [c.name for c in section_checks],
                "remediation": remediation,
            })
        return result

    def to_dict(self) -> dict:
        # Priority 7 requirement 18: explain exactly which required
        # capability is missing, not just an aggregate status.
        blocked = [c.name for c in self.checks if c.status == STATUS_BLOCKED]
        warned = [c.name for c in self.checks if c.status == STATUS_WARNING]
        return {
            "overall": self.overall.upper(),
            "blockedCapabilities": blocked,
            "warnedCapabilities": warned,
            "sections": self.sections(),
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
    expected_caleemobile_sha: "str | None" = None,
    expected_caleemobile_regression_sha: "str | None" = None,
    selector_workflow_run_id: "str | None" = None,
    selector_artifact_id: "str | None" = None,
    selector_artifact_zip: "Path | None" = None,
    calee_regression_main_sha: "str | None" = None,
    calee_regression_main_workflow_run_id: "str | None" = None,
    calee_regression_main_artifact_id: "str | None" = None,
    caleemobile_regression_main_sha: "str | None" = None,
    caleemobile_regression_main_workflow_run_id: "str | None" = None,
    caleemobile_regression_main_artifact_id: "str | None" = None,
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

    Priority 8 (this session): calee-regression's and CaleeMobile-
    Regression's own merged-main CI are each authenticated SEPARATELY, with
    their OWN distinct workflow-run/artifact/expected-SHA inputs -- never
    the CaleeMobile product SHA (``expected_caleemobile_sha``, which is used
    ONLY for the CaleeMobile sibling-checkout-SHA check below). The previous
    single generic ``--main-ci-workflow-run-id``/``--main-ci-artifact-id``
    pair could only ever target one of these two regression repositories at
    a time yet was verified against the CaleeMobile product's own expected
    SHA regardless of which repository was named -- a meaningless
    comparison for a regression-framework repository's own main-CI run.
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

    # Priority 9 (this session): any mobile platform in scope at all -- the
    # one condition shared by Appium (reachability + drivers) and the
    # Flutter toolchain, since CaleeMobile's Flutter codebase and its
    # Appium-driven UI automation underlie tablet/android/ios alike.
    any_mobile_platform_in_scope = bool({"tablet", "android", "ios"} & set(enabled_platforms))

    checks.append(check_android_build_tools(which=which, required=bool({"tablet", "android"} & set(enabled_platforms))))

    checks.append(check_appium(_load_appium_url(repo_root), opener=http_opener, required=any_mobile_platform_in_scope))
    required_drivers = []
    if {"tablet", "android"} & set(enabled_platforms):
        required_drivers.append("uiautomator2")
    if "ios" in enabled_platforms:
        required_drivers.append("xcuitest")
    checks.append(check_appium_drivers(required_drivers=required_drivers, which=which, runner=subprocess_runner))

    checks.append(check_flutter(which=which, runner=subprocess_runner, required=any_mobile_platform_in_scope))

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
    checks.append(check_ingestion_bridge(repo_root, required=calendar_required))

    from . import release_config as rc_mod

    caleemobile_identity = expected_identities.get("caleeMobile", {}) or {}

    selector_required = bool(rc_mod.resolve_selector_evidence_required(
        profile=profile, enabled_platforms=enabled_platforms,
        schema_version=effective.schema_version if effective is not None else None,
        manifest_required=caleemobile_identity.get("selectorEvidenceRequired"),
    ))
    checks.append(check_selector_ci_evidence_availability(
        env=environ, required=selector_required,
        workflow_run_id=selector_workflow_run_id, artifact_id=selector_artifact_id,
        selector_artifact_zip=selector_artifact_zip,
        expected_regression_sha=expected_caleemobile_regression_sha,
        expected_tested_sha=caleemobile_identity.get("gitSha"),
        expected_version=caleemobile_identity.get("buildVersion"),
        expected_release_id=effective.release_id if effective is not None else None,
    ))

    distributed_required = bool(caleemobile_identity.get("distributedBuildAcceptanceRequired"))
    checks.append(check_distributed_build_evidence_availability(
        distributed_build_evidence_path, required=distributed_required,
        expected_release_id=effective.release_id if effective is not None else None,
        expected_git_sha=caleemobile_identity.get("gitSha"),
        expected_version=caleemobile_identity.get("buildVersion"),
    ))

    checks.append(check_main_ci_evidence(
        main_ci_evidence_path, expected_repository=main_ci_repository, required=bool(main_ci_evidence_path),
    ))
    # Priority 8: calee-regression's and CaleeMobile-Regression's own
    # merged-main CI, each authenticated SEPARATELY against its OWN expected
    # SHA -- never the CaleeMobile product SHA (caleemobile_identity above).
    from . import main_ci_evidence as mce_mod_for_profiles

    # required=bool(...-main-sha) mirrors check_main_ci_evidence's own
    # required=bool(main_ci_evidence_path) convention: supplying the SHA
    # signals the operator wants this repository's main CI authenticated,
    # so a run-id/artifact-id left out at that point is an incomplete
    # configuration that BLOCKS rather than merely warns -- offline test 22
    # ("missing either regression main-CI artifact blocks") pins this.
    checks.append(check_main_ci_artifact_authenticated(
        check_name="calee_regression_main_ci_authenticated",
        repository="CaleeAdmin/calee-regression",
        workflow_run_id=calee_regression_main_workflow_run_id, artifact_id=calee_regression_main_artifact_id,
        expected_merge_sha=calee_regression_main_sha, required=bool(calee_regression_main_sha), env=environ,
        run_id_flag="--calee-regression-main-workflow-run-id", artifact_id_flag="--calee-regression-main-artifact-id",
        sha_flag="--calee-regression-main-sha",
    ))
    checks.append(check_main_ci_artifact_authenticated(
        check_name="caleemobile_regression_main_ci_authenticated",
        repository=mce_mod_for_profiles.CALEEMOBILE_REGRESSION_REPOSITORY,
        workflow_run_id=caleemobile_regression_main_workflow_run_id, artifact_id=caleemobile_regression_main_artifact_id,
        expected_merge_sha=caleemobile_regression_main_sha, required=bool(caleemobile_regression_main_sha), env=environ,
        run_id_flag="--caleemobile-regression-main-workflow-run-id",
        artifact_id_flag="--caleemobile-regression-main-artifact-id",
        sha_flag="--caleemobile-regression-main-sha",
    ))

    checks.append(check_manual_check_definitions(manual_checks_path))

    return PreflightReport(checks=checks)
