"""Explicit, testable Appium session bootstrap with bounded, evidence-rich
UiAutomator2 Settings-helper recovery (Workstream 2).

This REPLACES the previous import-time monkey-patch of
``CaleeDriver.start_session`` (``appium_recovery.install_appium_settings_recovery``,
formerly installed from ``__init__.py``). Package import no longer changes
``CaleeDriver`` behaviour implicitly; the runner calls
``bootstrap_session(driver)`` explicitly and receives a structured
``SessionBootstrapReport`` describing exactly what happened.

Recovery contract (never more than this):
  1. Attempt standard session creation once.
  2. ONLY on the exact known Appium Settings startup failure:
       * preserve the full first exception;
       * inspect whether ``io.appium.settings`` is installed;
       * record its version name / version code where available;
       * record the resolved launchable activity, declared services & receivers;
       * record the helper process state (running / not);
       * record relevant device-policy / package restrictions;
       * capture a narrowly bounded, REDACTED logcat window;
       * capture the installed UiAutomator2 driver version and Appium version;
       * uninstall ONLY the stale Appium Settings helper (never an arbitrary APK);
       * let UiAutomator2 reinstall the helper on the retry;
       * retry EXACTLY once.
  3. Never loops indefinitely.
  4. Never automatically switches from standard to diagnostic mode.
  5. If the retry fails, produce a structured BLOCKED bootstrap report
     (first failure, recovery actions, command return codes, second failure,
     diagnostic paths) carried on ``SessionBootstrapError.report``.
  6. An inability to keep the Settings helper process alive is NEVER a Calee
     product failure -- it maps to a BLOCKED outcome code, not FAIL.
  7. The helper is never launched as an activity (package inspection only), and
     no arbitrary APK is ever installed.

Every device interaction goes through an injectable ``adb_runner`` (default
``subprocess.run``) and an injectable ``version_probe`` so every branch is
unit-testable with fake ADB / Appium executors and no real device.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from .appium_driver import find_adb_path
from .appium_recovery import is_settings_startup_failure, reset_settings_package

_SETTINGS_PACKAGE = "io.appium.settings"

# ── Outcome classification codes ───────────────────────────────────────────
# Every non-success outcome the framework must be able to tell apart. All of
# them map to BLOCKED (an environment/tooling problem), never to a product
# FAIL -- see docs/RELEASE_POLICY.md.
OUTCOME_SESSION_CREATED = "session_created"
OUTCOME_APPIUM_SERVER_UNAVAILABLE = "appium_server_unavailable"
OUTCOME_UIAUTOMATOR2_UNAVAILABLE = "uiautomator2_server_unavailable"
OUTCOME_SETTINGS_INSTALL_FAILED = "appium_settings_install_failed"
OUTCOME_SETTINGS_START_FAILED = "appium_settings_start_failed"
OUTCOME_SETTINGS_DEVICE_POLICY_BLOCKED = "appium_settings_device_policy_blocked"
OUTCOME_SESSION_FAILED_OTHER = "session_creation_failed_other"

# Every outcome other than a created session is BLOCKED (environment/tooling),
# never a product failure.
BLOCKED_OUTCOMES = frozenset(
    {
        OUTCOME_APPIUM_SERVER_UNAVAILABLE,
        OUTCOME_UIAUTOMATOR2_UNAVAILABLE,
        OUTCOME_SETTINGS_INSTALL_FAILED,
        OUTCOME_SETTINGS_START_FAILED,
        OUTCOME_SETTINGS_DEVICE_POLICY_BLOCKED,
        OUTCOME_SESSION_FAILED_OTHER,
    }
)

# Device-policy / user-restriction markers that explain why (re)installing the
# helper is refused by the device rather than merely failing.
_DEVICE_POLICY_MARKERS = (
    "install_failed_user_restricted",
    "no_install_apps",
    "no_control_apps",
    "disallow_install_apps",
    "blocked by administrator",
    "blocked by policy",
    "restricted by your organization",
    "restricted by device policy",
    "device owner",
    "admin has",
)

_APPIUM_SERVER_MARKERS = (
    "could not connect",
    "connection refused",
    "actively refused",
    "max retries",
    "failed to establish a new connection",
    "newconnectionerror",
    "connection aborted",
    "remote end closed connection",
    "urlopen error",
    "connection to",
    "/status",
    "no connection could be made",
)

_UIAUTOMATOR2_MARKERS = (
    "uiautomator2 server",
    "io.appium.uiautomator2.server",
    "instrumentation process",
    "could not start a new session for uiautomator2",
    "cannot start the 'io.appium.uiautomator2.server'",
    "original error: could not start uiautomator2",
)


def classify_session_exception(exc: BaseException) -> str:
    """Map a raw session-creation exception to a stable outcome code.

    Deterministic and pure so callers can classify without a device.
    """
    text = str(exc).lower()
    if "settings app is not running" in text:
        return OUTCOME_SETTINGS_START_FAILED
    if any(marker in text for marker in _UIAUTOMATOR2_MARKERS):
        return OUTCOME_UIAUTOMATOR2_UNAVAILABLE
    if any(marker in text for marker in _APPIUM_SERVER_MARKERS):
        return OUTCOME_APPIUM_SERVER_UNAVAILABLE
    return OUTCOME_SESSION_FAILED_OTHER


# ── Redaction ──────────────────────────────────────────────────────────────
_REDACT_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*)(bearer\s+)?[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}"),
    re.compile(r"(?i)(password\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(refresh[_-]?token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(access[_-]?token\s*[=:]\s*)\S+"),
    re.compile(r"(?i)(app[_-]?password\s*[=:]\s*)\S+"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
]


def redact_logcat(text: str, *, max_lines: int = 200, max_chars: int = 20000) -> str:
    """Redact obvious secrets from a bounded logcat window.

    Bounded to the LAST ``max_lines`` lines and ``max_chars`` characters so an
    unbounded device log can never bloat the report, and every recognised
    credential shape (Authorization/Bearer, password/token/app_password
    key-values, bare emails) is masked before it is ever written to disk.
    """
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    redacted = []
    for line in lines:
        masked = line
        for pattern in _REDACT_PATTERNS[:-1]:
            masked = pattern.sub(lambda m: (m.group(1) or "") + "[REDACTED]", masked)
        masked = _REDACT_PATTERNS[-1].sub("[REDACTED-EMAIL]", masked)
        redacted.append(masked)
    joined = "\n".join(redacted)
    if len(joined) > max_chars:
        joined = joined[-max_chars:]
    return joined


# ── Pure parsers (unit-testable without a device) ──────────────────────────
def parse_pm_path_installed(text: str) -> bool:
    """True when `pm path io.appium.settings` reported an installed package."""
    return bool(text) and "package:" in text


def parse_dumpsys_version(text: str) -> "tuple[str | None, str | None]":
    """(versionName, versionCode) parsed from `dumpsys package` output."""
    name = None
    code = None
    m = re.search(r"versionName=(\S+)", text or "")
    if m:
        name = m.group(1)
    m = re.search(r"versionCode=(\d+)", text or "")
    if m:
        code = m.group(1)
    return name, code


def parse_resolve_activity_brief(text: str) -> "str | None":
    """A launchable ``pkg/activity`` component from
    ``cmd package resolve-activity --brief``, or None when the package exposes
    no launchable activity. Used ONLY as evidence -- the helper is never
    launched; recording that there is (or is not) a launchable activity is what
    justifies never treating this helper as a launchable app.
    """
    if not text:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        if "/" in line and _SETTINGS_PACKAGE in line and " " not in line:
            return line
        if line.lower().startswith("no activity") or "no activity found" in line.lower():
            return None
    return None


def parse_declared_components(text: str, headers: "tuple[str, ...]") -> list:
    """Component tokens (``io.appium.settings/...``) listed under any of the
    given section ``headers`` in a `dumpsys package` dump. Best-effort: a
    format the device does not emit simply yields an empty list rather than
    raising."""
    if not text:
        return []
    found: list = []
    active = False
    header_lc = tuple(h.lower() for h in headers)
    for raw in text.splitlines():
        stripped = raw.strip().lower()
        if stripped.rstrip(":") in header_lc or any(stripped.startswith(h) for h in header_lc):
            active = True
            continue
        # A new top-level section header (a non-indented "Word:" line) ends the
        # current section.
        if active and raw and not raw[0].isspace() and raw.rstrip().endswith(":"):
            active = False
        if active:
            for token in re.findall(r"io\.appium\.settings/[\w\.$]+", raw):
                if token not in found:
                    found.append(token)
    return found


def parse_pidof(text: str) -> "list[str]":
    """PIDs from `pidof io.appium.settings` (space-separated), or []."""
    if not text:
        return []
    return [tok for tok in text.split() if tok.isdigit()]


def parse_device_policy_restrictions(text: str) -> list:
    """Lines from `dumpsys device_policy` (or an install error) that mention a
    known install/uninstall restriction, deduplicated and length-bounded."""
    if not text:
        return []
    hits: list = []
    for line in text.splitlines():
        low = line.lower()
        if any(marker in low for marker in _DEVICE_POLICY_MARKERS):
            trimmed = line.strip()[:200]
            if trimmed and trimmed not in hits:
                hits.append(trimmed)
    return hits[:20]


# ── Evidence dataclasses ───────────────────────────────────────────────────
@dataclass
class SettingsEvidence:
    installed: "bool | None" = None
    version_name: "str | None" = None
    version_code: "str | None" = None
    launchable_activity: "str | None" = None
    services: list = field(default_factory=list)
    receivers: list = field(default_factory=list)
    process_running: "bool | None" = None
    process_pids: list = field(default_factory=list)
    device_policy_restrictions: list = field(default_factory=list)
    logcat_excerpt: "str | None" = None
    uiautomator2_version: "str | None" = None
    appium_version: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "package": _SETTINGS_PACKAGE,
            "installed": self.installed,
            "versionName": self.version_name,
            "versionCode": self.version_code,
            "launchableActivity": self.launchable_activity,
            "services": list(self.services),
            "receivers": list(self.receivers),
            "processRunning": self.process_running,
            "processPids": list(self.process_pids),
            "devicePolicyRestrictions": list(self.device_policy_restrictions),
            "logcatExcerpt": self.logcat_excerpt,
            "uiautomator2Version": self.uiautomator2_version,
            "appiumVersion": self.appium_version,
        }


@dataclass
class SessionBootstrapReport:
    outcome: str = OUTCOME_SESSION_CREATED
    attempted_recovery: bool = False
    recovered: bool = False
    first_failure: "str | None" = None
    first_failure_type: "str | None" = None
    second_failure: "str | None" = None
    second_failure_type: "str | None" = None
    recovery_actions: list = field(default_factory=list)
    command_return_codes: list = field(default_factory=list)
    settings_evidence: "dict | None" = None
    diagnostic_paths: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.outcome in BLOCKED_OUTCOMES

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "blocked": self.blocked,
            "attemptedRecovery": self.attempted_recovery,
            "recovered": self.recovered,
            "firstFailure": self.first_failure,
            "firstFailureType": self.first_failure_type,
            "secondFailure": self.second_failure,
            "secondFailureType": self.second_failure_type,
            "recoveryActions": list(self.recovery_actions),
            "commandReturnCodes": list(self.command_return_codes),
            "settingsEvidence": self.settings_evidence,
            "diagnosticPaths": dict(self.diagnostic_paths),
        }


class SessionBootstrapError(Exception):
    """Raised when session bootstrap fails after its bounded recovery. Carries
    the structured ``report`` (a SessionBootstrapReport) so the runner can
    surface evidence-rich BLOCKED results without re-deriving anything."""

    def __init__(self, message: str, report: SessionBootstrapReport):
        super().__init__(message)
        self.report = report


# ── ADB / version probing (injectable) ─────────────────────────────────────
def _adb_base(config) -> list:
    cmd = [find_adb_path()]
    if getattr(config, "udid", None):
        cmd.extend(["-s", config.udid])
    return cmd


def _adb_text(config, args, adb_runner, *, timeout: int = 20) -> "str | None":
    """Run one adb command, returning its stdout text (best-effort). Evidence
    gathering must never raise and mask the real session failure, so every
    error becomes None."""
    try:
        cp = adb_runner(
            _adb_base(config) + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None
    out = getattr(cp, "stdout", None)
    if out is None:
        return None
    return out


def default_version_probe(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """Best-effort Appium + UiAutomator2 versions via the appium CLI. Returns
    ``{"appium": str|None, "uiautomator2": str|None}``; any failure yields
    None values (never raises)."""
    appium_version = None
    ua2_version = None
    try:
        cp = runner(["appium", "--version"], capture_output=True, text=True, timeout=15, check=False)
        out = (getattr(cp, "stdout", "") or "").strip()
        if out:
            appium_version = out.splitlines()[0].strip()
    except Exception:
        pass
    try:
        cp = runner(
            ["appium", "driver", "list", "--installed"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        blob = (getattr(cp, "stdout", "") or "") + (getattr(cp, "stderr", "") or "")
        m = re.search(r"uiautomator2@([0-9][\w.\-]*)", blob)
        if m:
            ua2_version = m.group(1)
    except Exception:
        pass
    return {"appium": appium_version, "uiautomator2": ua2_version}


def gather_settings_evidence(config, *, adb_runner=subprocess.run, version_probe=None, logcat_lines: int = 200) -> SettingsEvidence:
    """Gather narrowly-scoped, redacted evidence about the Appium Settings
    helper after the known startup failure. Pure inspection: it never installs,
    launches, or otherwise mutates the device (the only mutation in the whole
    recovery is the single ``uninstall io.appium.settings`` performed later by
    the caller). Best-effort throughout -- a missing/odd device response leaves
    that field unset rather than raising."""
    evidence = SettingsEvidence()

    path_text = _adb_text(config, ["shell", "pm", "path", _SETTINGS_PACKAGE], adb_runner)
    if path_text is not None:
        evidence.installed = parse_pm_path_installed(path_text)

    dump_text = _adb_text(config, ["shell", "dumpsys", "package", _SETTINGS_PACKAGE], adb_runner) or ""
    if dump_text:
        evidence.version_name, evidence.version_code = parse_dumpsys_version(dump_text)
        evidence.services = parse_declared_components(dump_text, ("Services:", "Service Resolver Table"))
        evidence.receivers = parse_declared_components(dump_text, ("Receivers:", "Receiver Resolver Table"))

    resolve_text = _adb_text(
        config, ["shell", "cmd", "package", "resolve-activity", "--brief", _SETTINGS_PACKAGE], adb_runner
    )
    if resolve_text is not None:
        evidence.launchable_activity = parse_resolve_activity_brief(resolve_text)

    pid_text = _adb_text(config, ["shell", "pidof", _SETTINGS_PACKAGE], adb_runner)
    if pid_text is not None:
        pids = parse_pidof(pid_text)
        evidence.process_pids = pids
        evidence.process_running = bool(pids)

    policy_text = _adb_text(config, ["shell", "dumpsys", "device_policy"], adb_runner)
    if policy_text:
        evidence.device_policy_restrictions = parse_device_policy_restrictions(policy_text)

    logcat_text = _adb_text(config, ["logcat", "-d", "-t", str(logcat_lines)], adb_runner, timeout=30)
    if logcat_text:
        evidence.logcat_excerpt = redact_logcat(logcat_text, max_lines=logcat_lines)

    probe = version_probe or default_version_probe
    try:
        versions = probe() or {}
    except Exception:
        versions = {}
    evidence.appium_version = versions.get("appium")
    evidence.uiautomator2_version = versions.get("uiautomator2")
    return evidence


def _classify_recovery_failure(second_exc: BaseException, evidence: SettingsEvidence) -> str:
    """Outcome code when the single retry after uninstalling the helper still
    fails. Prefers the most specific settings-* classification the evidence
    supports before falling back to the generic session classification."""
    text = str(second_exc).lower()
    if evidence.device_policy_restrictions or any(m in text for m in _DEVICE_POLICY_MARKERS):
        return OUTCOME_SETTINGS_DEVICE_POLICY_BLOCKED
    if ("install" in text and "fail" in text) or evidence.installed is False:
        # The helper could not be (re)installed after the uninstall.
        return OUTCOME_SETTINGS_INSTALL_FAILED
    if "settings app is not running" in text:
        # Reinstalled but still cannot keep the helper process alive -- never a
        # Calee product failure, a device/helper-lifecycle BLOCKED.
        return OUTCOME_SETTINGS_START_FAILED
    return classify_session_exception(second_exc)


def bootstrap_session(
    driver,
    *,
    adb_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    version_probe: "Callable[[], dict] | None" = None,
    logcat_lines: int = 200,
    is_settings_failure: "Callable[[BaseException], bool] | None" = None,
) -> SessionBootstrapReport:
    """Create an Appium session, recovering exactly once from the known Appium
    Settings startup failure. Returns a SessionBootstrapReport on success;
    raises SessionBootstrapError (with the structured report attached) on any
    failure. Never loops, never auto-switches to diagnostic mode.
    """
    is_settings_failure = is_settings_failure or is_settings_startup_failure
    report = SessionBootstrapReport()

    try:
        driver.start_session()
    except Exception as first_exc:  # noqa: BLE001 -- every failure is classified below
        report.first_failure = str(first_exc)
        report.first_failure_type = type(first_exc).__name__
    else:
        report.outcome = OUTCOME_SESSION_CREATED
        return report

    # A non-Settings failure is never "recovered" by uninstalling the helper --
    # classify it and stop (mirrors the old code's "non-settings failure is not
    # retried", but now with a structured, distinguishable outcome code).
    first_failure_text = report.first_failure or ""
    if not is_settings_failure(RuntimeError(first_failure_text)):
        report.outcome = classify_session_exception(RuntimeError(first_failure_text))
        raise SessionBootstrapError(
            f"Could not start an Appium session ({report.outcome}): {report.first_failure}",
            report,
        )

    # Exact Settings startup failure -> gather evidence, uninstall the stale
    # helper (only), and retry EXACTLY once.
    report.attempted_recovery = True
    evidence = gather_settings_evidence(
        driver.config, adb_runner=adb_runner, version_probe=version_probe, logcat_lines=logcat_lines
    )
    report.settings_evidence = evidence.to_dict()
    report.recovery_actions.append(f"inspected {_SETTINGS_PACKAGE} (installed={evidence.installed})")

    uninstall_cp = reset_settings_package(driver.config, runner=adb_runner)
    uninstall_rc = getattr(uninstall_cp, "returncode", None)
    report.recovery_actions.append(f"uninstalled stale {_SETTINGS_PACKAGE}")
    report.command_return_codes.append(
        {"command": f"adb uninstall {_SETTINGS_PACKAGE}", "returncode": uninstall_rc}
    )

    try:
        driver.start_session()
    except Exception as second_exc:  # noqa: BLE001 -- classified into a BLOCKED outcome
        report.second_failure = str(second_exc)
        report.second_failure_type = type(second_exc).__name__
        report.outcome = _classify_recovery_failure(second_exc, evidence)
        raise SessionBootstrapError(
            f"Appium session still failed after one Settings-helper recovery "
            f"({report.outcome}): {report.second_failure}",
            report,
        ) from second_exc

    report.recovered = True
    report.outcome = OUTCOME_SESSION_CREATED
    report.recovery_actions.append("retry succeeded after helper reinstall")
    return report
