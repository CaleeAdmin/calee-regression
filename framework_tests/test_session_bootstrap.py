"""Offline tests for the explicit Appium session bootstrap (Workstream 2).

Every branch is exercised with a fake driver + fake ADB/Appium executors -- no
real Appium server, device, or adb binary is touched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from calee_regression import session_bootstrap as sb


class FakeDriver:
    """A driver whose start_session() raises a scripted sequence of exceptions
    (or succeeds when the scripted value is None)."""

    def __init__(self, sequence, udid="tablet-1"):
        self._sequence = list(sequence)
        self.config = SimpleNamespace(udid=udid)
        self.starts = 0

    def start_session(self):
        self.starts += 1
        outcome = self._sequence.pop(0) if self._sequence else None
        if outcome is not None:
            raise outcome


SETTINGS_EXC = RuntimeError("Appium Settings app is not running after 5000ms")


def _adb_ok(*records):
    """A fake adb runner returning canned stdout keyed loosely by argv."""

    def runner(cmd, **kwargs):
        text = " ".join(cmd)
        for needle, stdout in records:
            if needle in text:
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return runner


def _fake_probe():
    return {"appium": "2.11.1", "uiautomator2": "3.7.6"}


# ── Success paths ──────────────────────────────────────────────────────────
def test_session_created_on_first_attempt_no_recovery():
    driver = FakeDriver([None])
    report = sb.bootstrap_session(driver, adb_runner=_adb_ok(), version_probe=_fake_probe)
    assert report.outcome == sb.OUTCOME_SESSION_CREATED
    assert report.attempted_recovery is False
    assert report.recovered is False
    assert driver.starts == 1
    assert report.blocked is False


def test_settings_failure_then_recovery_succeeds():
    driver = FakeDriver([SETTINGS_EXC, None])
    uninstalls = []

    def adb_runner(cmd, **kwargs):
        text = " ".join(cmd)
        if "uninstall" in text:
            uninstalls.append(text)
            return SimpleNamespace(returncode=0, stdout="Success", stderr="")
        if "pm path" in text:
            return SimpleNamespace(returncode=0, stdout="package:/data/app/io.appium.settings.apk", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    report = sb.bootstrap_session(driver, adb_runner=adb_runner, version_probe=_fake_probe)
    assert report.outcome == sb.OUTCOME_SESSION_CREATED
    assert report.attempted_recovery is True
    assert report.recovered is True
    assert driver.starts == 2
    assert len(uninstalls) == 1  # exactly one uninstall, one retry
    assert any("io.appium.settings" in rc["command"] for rc in report.command_return_codes)
    assert report.settings_evidence["installed"] is True


# ── Failure classification ─────────────────────────────────────────────────
def test_appium_server_unavailable_is_blocked_and_not_recovered():
    driver = FakeDriver([RuntimeError("Could not connect to Appium server /status: connection refused")])
    with pytest.raises(sb.SessionBootstrapError) as exc:
        sb.bootstrap_session(driver, adb_runner=_adb_ok(), version_probe=_fake_probe)
    report = exc.value.report
    assert report.outcome == sb.OUTCOME_APPIUM_SERVER_UNAVAILABLE
    assert report.blocked is True
    assert report.attempted_recovery is False
    # No product scenario/recovery: only one start attempt, no uninstall.
    assert driver.starts == 1


def test_uiautomator2_unavailable_is_classified():
    driver = FakeDriver([RuntimeError("Could not start a new session for UiAutomator2 server")])
    with pytest.raises(sb.SessionBootstrapError) as exc:
        sb.bootstrap_session(driver, adb_runner=_adb_ok(), version_probe=_fake_probe)
    assert exc.value.report.outcome == sb.OUTCOME_UIAUTOMATOR2_UNAVAILABLE


def test_generic_failure_is_session_failed_other():
    driver = FakeDriver([RuntimeError("some totally unrelated boom")])
    with pytest.raises(sb.SessionBootstrapError) as exc:
        sb.bootstrap_session(driver, adb_runner=_adb_ok(), version_probe=_fake_probe)
    assert exc.value.report.outcome == sb.OUTCOME_SESSION_FAILED_OTHER


def test_settings_failure_twice_maps_to_settings_start_failed():
    driver = FakeDriver([SETTINGS_EXC, SETTINGS_EXC])
    with pytest.raises(sb.SessionBootstrapError) as exc:
        sb.bootstrap_session(
            driver,
            adb_runner=_adb_ok(("pm path", "package:/data/app/io.appium.settings.apk")),
            version_probe=_fake_probe,
        )
    report = exc.value.report
    assert report.outcome == sb.OUTCOME_SETTINGS_START_FAILED
    assert report.attempted_recovery is True
    assert report.recovered is False
    assert driver.starts == 2  # one retry only -- never loops
    assert report.first_failure and report.second_failure


def test_settings_then_install_failure_maps_to_install_failed():
    driver = FakeDriver([SETTINGS_EXC, RuntimeError("Could not install io.appium.settings: install failed")])
    with pytest.raises(sb.SessionBootstrapError) as exc:
        sb.bootstrap_session(driver, adb_runner=_adb_ok(), version_probe=_fake_probe)
    assert exc.value.report.outcome == sb.OUTCOME_SETTINGS_INSTALL_FAILED


def test_settings_then_device_policy_maps_to_policy_blocked():
    # Evidence gathering surfaces a device-policy restriction.
    driver = FakeDriver([SETTINGS_EXC, SETTINGS_EXC])

    def adb_runner(cmd, **kwargs):
        text = " ".join(cmd)
        if "device_policy" in text:
            return SimpleNamespace(
                returncode=0,
                stdout="Enabled Device Policy:\n  no_install_apps=true (blocked by administrator)",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(sb.SessionBootstrapError) as exc:
        sb.bootstrap_session(driver, adb_runner=adb_runner, version_probe=_fake_probe)
    report = exc.value.report
    assert report.outcome == sb.OUTCOME_SETTINGS_DEVICE_POLICY_BLOCKED
    assert report.settings_evidence["devicePolicyRestrictions"]


def test_no_automatic_switch_to_diagnostic_mode():
    # The bootstrap must never flip device_initialization_mode; it only ever
    # calls start_session, never mutates config.
    driver = FakeDriver([SETTINGS_EXC, SETTINGS_EXC])
    driver.config.device_initialization_mode = "standard"
    with pytest.raises(sb.SessionBootstrapError):
        sb.bootstrap_session(driver, adb_runner=_adb_ok(), version_probe=_fake_probe)
    assert driver.config.device_initialization_mode == "standard"


# ── Evidence gathering never masks the real failure ────────────────────────
def test_evidence_gathering_survives_adb_errors():
    driver = FakeDriver([SETTINGS_EXC, SETTINGS_EXC])

    def adb_runner(cmd, **kwargs):
        raise OSError("adb exploded")

    with pytest.raises(sb.SessionBootstrapError) as exc:
        sb.bootstrap_session(driver, adb_runner=adb_runner, version_probe=_fake_probe)
    # Still classified, still BLOCKED, evidence simply sparse -- never a crash.
    assert exc.value.report.outcome in sb.BLOCKED_OUTCOMES
    assert exc.value.report.settings_evidence is not None


# ── Pure parser + redaction coverage ───────────────────────────────────────
def test_parse_dumpsys_version():
    text = "  versionCode=44 minSdk=21\n  versionName=5.12.2\n"
    assert sb.parse_dumpsys_version(text) == ("5.12.2", "44")


def test_parse_pm_path_installed():
    assert sb.parse_pm_path_installed("package:/data/app/io.appium.settings-1/base.apk") is True
    assert sb.parse_pm_path_installed("") is False


def test_parse_pidof():
    assert sb.parse_pidof("2201 2202") == ["2201", "2202"]
    assert sb.parse_pidof("") == []


def test_parse_resolve_activity_brief_none_when_no_launchable():
    assert sb.parse_resolve_activity_brief("No Activity found to handle") is None
    assert (
        sb.parse_resolve_activity_brief("priority=0\nio.appium.settings/.Settings")
        == "io.appium.settings/.Settings"
    )


def test_redact_logcat_masks_secrets_and_bounds_length():
    raw = "\n".join(
        [
            "07-23 line Authorization: Bearer abc.def.ghi",
            "07-23 password=supersecret here",
            "07-23 user user@example.com logged in",
            "07-23 harmless line",
        ]
    )
    out = sb.redact_logcat(raw, max_lines=10)
    assert "supersecret" not in out
    assert "abc.def.ghi" not in out
    assert "user@example.com" not in out
    assert "harmless line" in out


def test_parse_device_policy_restrictions():
    text = "no_install_apps=true\nsomething benign\nblocked by administrator: install"
    hits = sb.parse_device_policy_restrictions(text)
    assert len(hits) == 2
