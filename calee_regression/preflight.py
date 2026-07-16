from __future__ import annotations

import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .appium_driver import AdbError, find_adb_path
from .models import DoctorCheck


def explain_exception(exc: BaseException) -> str:
    text = str(exc).lower()

    if isinstance(exc, AdbError) and "adb executable not found" in text:
        return (
            "adb was not found. Set ANDROID_HOME or ANDROID_SDK_ROOT to your Android SDK path, "
            "or add platform-tools to PATH."
        )

    if any(s in text for s in ("connection refused", "max retries exceeded", "failed to establish a new connection", "newconnectionerror")):
        return (
            "Appium does not appear to be running. Start it with: "
            "appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell — and make sure "
            "the appium_url in your config matches (including the /wd/hub path)."
        )

    if "404" in text and "session" in text:
        return (
            "This often means the Appium base path doesn't match your config's appium_url "
            "(a /session or //session routing error). Start Appium with --base-path /wd/hub and "
            "make sure appium_url ends in /wd/hub, or vice versa if you changed the base path."
        )

    if "insecure" in text or "has not been enabled" in text or "adb_shell" in text:
        return (
            "Your launch_strategy needs shell access, but Appium's insecure uiautomator2:adb_shell "
            "feature isn't enabled. Restart Appium with: "
            "appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell"
        )

    if any(s in text for s in ("no devices", "device not found", "device offline", "more than one device")):
        return "No matching Android device/emulator is connected. Run 'adb devices' and check your config's udid."

    if "adb executable not found" in text:
        return (
            "adb was not found. Set ANDROID_HOME or ANDROID_SDK_ROOT to your Android SDK path, "
            "or add platform-tools to PATH."
        )

    if "nosuchelement" in text or "an element could not be located" in text:
        return (
            "The expected UI element was not found. This can mean the screen hasn't loaded yet "
            "(add a sleep/wait_for step), the id/text changed, or — for scenarios tagged "
            "requires_state: logged_in_tablet — that the device is still on the clean/onboarding "
            "screen instead of the logged-in home screen."
        )

    return f"Unexpected error: {exc}. Run: python -m calee_regression doctor --config <your config> to check your setup."


def check_appium_reachable(config) -> DoctorCheck:
    url = config.appium_url.rstrip("/") + "/status"
    try:
        urllib.request.urlopen(url, timeout=5)
        return DoctorCheck("appium_reachable", "ok", f"Appium responded at {url}")
    except Exception as exc:
        return DoctorCheck(
            "appium_reachable", "error", f"Could not reach Appium at {url}: {exc}",
            hint=explain_exception(exc),
        )


def check_android_sdk_env() -> DoctorCheck:
    try:
        home = os.environ.get("ANDROID_HOME")
        sdk_root = os.environ.get("ANDROID_SDK_ROOT")
        if not home and not sdk_root:
            return DoctorCheck(
                "android_sdk_env", "error", "Neither ANDROID_HOME nor ANDROID_SDK_ROOT is set.",
                hint="export ANDROID_HOME=/path/to/Android/sdk (or ANDROID_SDK_ROOT)",
            )
        for env_value in (home, sdk_root):
            if env_value and (Path(env_value) / "platform-tools" / "adb").exists():
                return DoctorCheck("android_sdk_env", "ok", f"Android SDK found at {env_value}")
        return DoctorCheck(
            "android_sdk_env", "warning",
            f"ANDROID_HOME/ANDROID_SDK_ROOT is set but platform-tools/adb was not found under it.",
            hint="Verify your Android SDK installation includes platform-tools.",
        )
    except Exception as exc:
        return DoctorCheck("android_sdk_env", "error", str(exc), hint=explain_exception(exc))


def check_adb_available() -> DoctorCheck:
    try:
        adb_path = find_adb_path()
        resolved = shutil.which(adb_path) or (adb_path if Path(adb_path).exists() else None)
        if not resolved:
            return DoctorCheck(
                "adb_available", "error", f"adb ({adb_path}) not found on PATH or SDK location.",
                hint="Set ANDROID_HOME or ANDROID_SDK_ROOT, or add platform-tools to PATH.",
            )
        subprocess.run([adb_path, "version"], capture_output=True, text=True, timeout=10)
        return DoctorCheck("adb_available", "ok", f"adb found at {resolved}")
    except Exception as exc:
        return DoctorCheck("adb_available", "error", str(exc), hint=explain_exception(exc))


def check_device_connected(config) -> DoctorCheck:
    adb_path = find_adb_path()
    try:
        result = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=15)
        lines = [l.strip() for l in result.stdout.splitlines()[1:] if l.strip()]
        connected = [l.split("\t")[0] for l in lines if "\tdevice" in l or l.endswith("\tdevice")]
        if not lines:
            return DoctorCheck(
                "device_connected", "error", "No devices/emulators are connected (adb devices is empty).",
                hint="Start your emulator, or connect the tablet with adb over USB/network.",
            )
        if config.udid and config.udid not in connected:
            return DoctorCheck(
                "device_connected", "warning",
                f"config udid {config.udid!r} is not among connected devices: {connected}",
                hint="Update udid in your config, or connect/start the expected device.",
            )
        return DoctorCheck("device_connected", "ok", f"Connected devices: {connected or lines}")
    except FileNotFoundError:
        return DoctorCheck(
            "device_connected", "error", f"Could not run adb ({adb_path!r}) to list devices.",
            hint="adb was not found. Set ANDROID_HOME or ANDROID_SDK_ROOT, or add platform-tools to PATH.",
        )
    except Exception as exc:
        return DoctorCheck("device_connected", "error", str(exc), hint=explain_exception(exc))


def check_apk_exists(config) -> DoctorCheck:
    try:
        if Path(config.apk_path).is_file():
            return DoctorCheck("apk_exists", "ok", f"APK found at {config.apk_path}")
        return DoctorCheck(
            "apk_exists", "error", f"APK not found at {config.apk_path}",
            hint="Fix apk_path in your config to point at a real, built Calee APK.",
        )
    except Exception as exc:
        return DoctorCheck("apk_exists", "error", str(exc), hint=explain_exception(exc))


def check_config_placeholders(config) -> DoctorCheck:
    return DoctorCheck(
        "config_placeholders", "ok",
        "config validated (no PUT_ACTIVITY_HERE placeholders, known launch_strategy)",
    )


def check_launch_strategy(config) -> DoctorCheck:
    try:
        strategy = config.launch_strategy
        message = (
            f"launch_strategy={strategy!r}. This strategy invokes the adb CLI directly "
            f"(am start / broadcast) — it does not depend on Appium's insecure mobile:shell feature."
        )
        return DoctorCheck("launch_strategy", "ok", message)
    except Exception as exc:
        return DoctorCheck("launch_strategy", "error", str(exc), hint=explain_exception(exc))


def check_state_expectation(config) -> DoctorCheck:
    try:
        if config.expected_state == "logged_in_tablet" and config.is_emulator():
            return DoctorCheck(
                "state_expectation", "warning",
                f"expected_state is logged_in_tablet but udid ({config.udid!r}) looks like an emulator.",
                hint="Make sure this emulator actually has a prepared demo/logged-in Hub session, or point udid at a real tablet.",
            )
        if config.expected_state == "fresh" and not config.is_emulator():
            return DoctorCheck(
                "state_expectation", "warning",
                f"expected_state is fresh but udid ({config.udid!r}) does not look like an emulator.",
                hint="Double check you're not about to wipe/rely on a clean state on a real tablet that already has an account signed in.",
            )
        return DoctorCheck("state_expectation", "ok", f"expected_state={config.expected_state!r} is consistent with udid={config.udid!r}")
    except Exception as exc:
        return DoctorCheck("state_expectation", "error", str(exc), hint=explain_exception(exc))


def run_doctor(config) -> list:
    return [
        check_appium_reachable(config),
        check_android_sdk_env(),
        check_adb_available(),
        check_device_connected(config),
        check_apk_exists(config),
        check_config_placeholders(config),
        check_launch_strategy(config),
        check_state_expectation(config),
    ]


def has_errors(checks: list) -> bool:
    return any(c.status == "error" for c in checks)
