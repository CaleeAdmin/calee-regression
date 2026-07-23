"""Tests for the pure Appium Settings helper functions.

The old import-time monkey-patch (`install_appium_settings_recovery`) has been
removed; its behaviour now lives in the explicit `session_bootstrap` component
(see test_session_bootstrap.py). Only the reusable, side-effect-free helpers are
tested here.
"""

from types import SimpleNamespace

from calee_regression import appium_recovery


def test_exact_settings_failure_is_detected():
    assert appium_recovery.is_settings_startup_failure(
        RuntimeError("Appium Settings app is not running after 5000ms")
    )
    assert not appium_recovery.is_settings_startup_failure(RuntimeError("connection refused"))


def test_reset_uninstalls_only_settings_package():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    config = SimpleNamespace(udid="tablet-1")
    result = appium_recovery.reset_settings_package(config, runner=runner)

    command, kwargs = calls[0]
    assert command[-2:] == ["uninstall", "io.appium.settings"]
    assert command[1:3] == ["-s", "tablet-1"]
    assert kwargs["check"] is False
    # The uninstall CompletedProcess is now returned so session_bootstrap can
    # record its return code.
    assert result.returncode == 0


def test_reset_is_best_effort_when_runner_raises():
    def runner(command, **kwargs):
        raise OSError("adb not found")

    # A failed uninstall must never raise (an absent package is already the
    # desired recovery state); it returns None.
    assert appium_recovery.reset_settings_package(SimpleNamespace(udid=None), runner=runner) is None
