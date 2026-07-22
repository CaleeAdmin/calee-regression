"""Bounded recovery for a known Appium UiAutomator2 bootstrap failure."""

from __future__ import annotations

import subprocess
from typing import Callable

from .appium_driver import CaleeDriver, find_adb_path

_SETTINGS_PACKAGE = "io.appium.settings"
_SETTINGS_FAILURE = "appium settings app is not running"
_PATCH_MARKER = "_calee_appium_settings_recovery_installed"


def is_settings_startup_failure(exc: BaseException) -> bool:
    """Return True only for the known Appium Settings bootstrap failure."""
    return _SETTINGS_FAILURE in str(exc).lower()


def reset_settings_package(config, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    """Remove the stale helper package so UiAutomator2 can reinstall it.

    Uninstall is intentionally best-effort: an absent package is already the
    desired recovery state. The subsequent Appium session retry is the actual
    readiness check and remains fail-closed.
    """
    adb = find_adb_path()
    command = [adb]
    if getattr(config, "udid", None):
        command.extend(["-s", config.udid])
    command.extend(["uninstall", _SETTINGS_PACKAGE])
    runner(command, capture_output=True, text=True, timeout=30, check=False)


def install_appium_settings_recovery(driver_class=CaleeDriver) -> None:
    """Patch CaleeDriver with one exact-error recovery attempt, idempotently."""
    if getattr(driver_class, _PATCH_MARKER, False):
        return

    original = driver_class.start_session

    def start_session_with_recovery(self) -> None:
        try:
            original(self)
            return
        except Exception as exc:
            if not is_settings_startup_failure(exc):
                raise
            reset_settings_package(self.config)

        # Exactly one retry. A second failure propagates to ScenarioRunner,
        # which correctly classifies session/tooling failures as BLOCKED.
        original(self)

    driver_class.start_session = start_session_with_recovery
    setattr(driver_class, _PATCH_MARKER, True)
