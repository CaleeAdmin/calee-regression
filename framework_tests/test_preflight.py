from calee_regression import preflight
from calee_regression.appium_driver import AdbError
from calee_regression.config import Config


def _make_config(**overrides):
    kwargs = dict(
        appium_url="http://127.0.0.1:9/wd/hub",
        device_name="Calee Test Tablet",
        udid="emulator-5554",
        apk_path="/definitely/does/not/exist.apk",
        app_package="com.viso.calee",
        app_activity=".ui.HomeActivity",
        shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity",
        launch_strategy="direct_activity",
        start_action="com.viso.calee.action.START",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def test_explain_exception_appium_not_running():
    hint = preflight.explain_exception(Exception("Connection refused: failed to establish a new connection"))
    assert "appium" in hint.lower()
    assert "--base-path" in hint


def test_explain_exception_adb_not_found():
    hint = preflight.explain_exception(AdbError("adb executable not found ('adb')."))
    assert "android_home" in hint.lower() or "android_sdk_root" in hint.lower() or "path" in hint.lower()


def test_explain_exception_no_devices():
    hint = preflight.explain_exception(Exception("No devices/emulators found"))
    assert "device" in hint.lower()


def test_explain_exception_generic_fallback_differs_from_others():
    generic = preflight.explain_exception(Exception("some totally unrelated message xyz123"))
    appium_hint = preflight.explain_exception(Exception("Connection refused"))
    adb_hint = preflight.explain_exception(AdbError("adb executable not found"))

    hints = {generic, appium_hint, adb_hint}
    assert len(hints) == 3, "expected distinct hints for distinct failure modes"
    assert "doctor" in generic.lower()


def test_run_doctor_never_raises_and_reports_errors():
    cfg = _make_config()
    checks = preflight.run_doctor(cfg)

    assert isinstance(checks, list)
    assert len(checks) > 0
    assert preflight.has_errors(checks) is True


def test_has_errors_false_for_all_ok_checks():
    from calee_regression.models import DoctorCheck

    checks = [DoctorCheck("a", "ok", "fine"), DoctorCheck("b", "warning", "meh")]
    assert preflight.has_errors(checks) is False
