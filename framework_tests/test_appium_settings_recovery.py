from types import SimpleNamespace

import pytest

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
    appium_recovery.reset_settings_package(config, runner=runner)

    command, kwargs = calls[0]
    assert command[-2:] == ["uninstall", "io.appium.settings"]
    assert command[1:3] == ["-s", "tablet-1"]
    assert kwargs["check"] is False


def test_install_retries_once_for_settings_failure(monkeypatch):
    attempts = []
    recoveries = []

    class FakeDriver:
        config = SimpleNamespace(udid="tablet-1")

        def start_session(self):
            attempts.append("start")
            if len(attempts) == 1:
                raise RuntimeError("Appium Settings app is not running after 5000ms")

    monkeypatch.setattr(
        appium_recovery,
        "reset_settings_package",
        lambda config: recoveries.append(config.udid),
    )

    appium_recovery.install_appium_settings_recovery(FakeDriver)
    FakeDriver().start_session()

    assert attempts == ["start", "start"]
    assert recoveries == ["tablet-1"]


def test_non_settings_failure_is_not_retried(monkeypatch):
    attempts = []

    class FakeDriver:
        config = SimpleNamespace(udid="tablet-1")

        def start_session(self):
            attempts.append("start")
            raise RuntimeError("connection refused")

    monkeypatch.setattr(
        appium_recovery,
        "reset_settings_package",
        lambda config: pytest.fail("recovery must not run"),
    )

    appium_recovery.install_appium_settings_recovery(FakeDriver)
    with pytest.raises(RuntimeError, match="connection refused"):
        FakeDriver().start_session()

    assert attempts == ["start"]


def test_second_settings_failure_propagates_after_one_retry(monkeypatch):
    attempts = []

    class FakeDriver:
        config = SimpleNamespace(udid="tablet-1")

        def start_session(self):
            attempts.append("start")
            raise RuntimeError("Appium Settings app is not running after 5000ms")

    monkeypatch.setattr(appium_recovery, "reset_settings_package", lambda config: None)
    appium_recovery.install_appium_settings_recovery(FakeDriver)

    with pytest.raises(RuntimeError, match="Appium Settings"):
        FakeDriver().start_session()

    assert attempts == ["start", "start"]
