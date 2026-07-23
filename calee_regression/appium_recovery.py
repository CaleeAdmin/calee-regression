"""Pure, reusable helpers for the Appium UiAutomator2 Settings-helper failure.

Historical note: this module used to install an import-time monkey-patch of
``CaleeDriver.start_session`` (``install_appium_settings_recovery``), applied
from ``calee_regression/__init__.py``. That implicit, package-import-time change
of class behaviour has been REMOVED in favour of an explicit, testable
session-bootstrap component (see ``session_bootstrap.bootstrap_session``), which
the runner calls directly. Only the narrowly-scoped, side-effect-free helpers
below remain here; ``session_bootstrap`` reuses them.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from .appium_driver import find_adb_path

_SETTINGS_PACKAGE = "io.appium.settings"
_SETTINGS_FAILURE = "appium settings app is not running"


def is_settings_startup_failure(exc: BaseException) -> bool:
    """Return True only for the known Appium Settings bootstrap failure."""
    return _SETTINGS_FAILURE in str(exc).lower()


def reset_settings_package(
    config, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> "subprocess.CompletedProcess | None":
    """Remove the stale helper package so UiAutomator2 can reinstall it.

    Uninstall is intentionally best-effort: an absent package is already the
    desired recovery state, and the subsequent Appium session retry is the
    actual readiness check (which remains fail-closed). Returns the uninstall's
    CompletedProcess so a caller (session_bootstrap) can record its return code,
    or None if the uninstall could not even be spawned.

    Only ``io.appium.settings`` is ever uninstalled -- never an arbitrary
    package chosen by a broad search.
    """
    adb = find_adb_path()
    command = [adb]
    if getattr(config, "udid", None):
        command.extend(["-s", config.udid])
    command.extend(["uninstall", _SETTINGS_PACKAGE])
    try:
        return runner(command, capture_output=True, text=True, timeout=30, check=False)
    except Exception:
        return None
