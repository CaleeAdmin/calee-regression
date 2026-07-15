from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path


class AdbError(Exception):
    pass


class LaunchError(Exception):
    pass


def find_adb_path() -> str:
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.environ.get(env_var)
        if sdk_root:
            candidate = Path(sdk_root) / "platform-tools" / "adb"
            if candidate.exists():
                return str(candidate)
    return "adb"


def run_adb(config, args: list, timeout: int = 30) -> subprocess.CompletedProcess:
    adb_path = find_adb_path()
    cmd = [adb_path] + (["-s", config.udid] if getattr(config, "udid", None) else []) + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise AdbError(
            f"adb executable not found ({adb_path!r}). Set ANDROID_HOME or ANDROID_SDK_ROOT, "
            f"or add platform-tools to PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(
            f"adb command timed out after {timeout}s: {' '.join(cmd)}. "
            f"The device may be unresponsive or not connected."
        ) from exc

    if result.returncode != 0:
        raise AdbError(
            f"adb command failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def build_direct_activity_command(config) -> list:
    return [
        "shell", "am", "start", "-W",
        "-n", f"{config.app_package}/{config.app_activity}",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.DEFAULT",
    ]


def build_start_action_command(config, action=None, package=None) -> list:
    return [
        "shell", "am", "start", "-W",
        "-a", action or config.start_action,
        "-p", package or config.app_package,
    ]


def build_calee_shell_start_command(config) -> list:
    return [
        "shell", "am", "start", "-W",
        "-n", f"{config.shell_package}/{config.shell_activity}",
    ]


def resolve_launch_commands(config) -> list:
    strategy = config.launch_strategy
    if strategy == "direct_activity":
        return [build_direct_activity_command(config)]
    if strategy == "start_action":
        return [build_start_action_command(config)]
    if strategy == "calee_shell":
        return [build_calee_shell_start_command(config), build_start_action_command(config)]
    if strategy == "normal_launcher":
        return []
    raise LaunchError(f"Unknown launch_strategy: {strategy!r}")


class CaleeDriver:
    def __init__(self, config):
        self.config = config
        self.driver = None

    def start_session(self) -> None:
        from appium import webdriver
        from appium.options.android.uiautomator2.base import UiAutomator2Options

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        if self.config.udid:
            options.udid = self.config.udid
        options.device_name = self.config.device_name
        options.no_reset = self.config.no_reset
        options.new_command_timeout = self.config.new_command_timeout_seconds
        options.auto_grant_permissions = True

        if self.config.launch_strategy == "normal_launcher":
            options.app_package = self.config.app_package
            options.app_activity = self.config.app_activity

        self.driver = webdriver.Remote(self.config.appium_url, options=options)

    def quit(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None

    def launch(self) -> None:
        for cmd in resolve_launch_commands(self.config):
            run_adb(self.config, cmd)
        if self.config.launch_strategy == "normal_launcher":
            if self.driver is None:
                raise LaunchError("normal_launcher requires an active Appium session")
            self.driver.activate_app(self.config.app_package)

    def start_activity(self, package: str, activity: str) -> None:
        run_adb(self.config, ["shell", "am", "start", "-W", "-n", f"{package}/{activity}"])

    def start_action(self, action: str, package: "str | None" = None) -> None:
        run_adb(self.config, build_start_action_command(self.config, action=action, package=package))

    def shell(self, command) -> str:
        parts = shlex.split(command) if isinstance(command, str) else list(command)
        result = run_adb(self.config, ["shell"] + parts)
        return result.stdout

    def screenshot(self, path) -> None:
        self.driver.get_screenshot_as_file(str(path))

    def current_activity(self) -> str:
        return self.driver.current_activity or ""

    def page_source(self) -> str:
        return self.driver.page_source

    def _resource_id(self, raw_id: str) -> str:
        return raw_id if ":id/" in raw_id else f"{self.config.app_package}:id/{raw_id}"

    def find_by_id(self, raw_id: str):
        from appium.webdriver.common.appiumby import AppiumBy
        return self.driver.find_element(AppiumBy.ID, self._resource_id(raw_id))

    def find_by_text(self, text: str):
        from appium.webdriver.common.appiumby import AppiumBy
        quote = "'" if '"' not in text else '"'
        xpath = (
            f"//*[contains(@text,{quote}{text}{quote}) or "
            f"contains(@content-desc,{quote}{text}{quote})]"
        )
        return self.driver.find_element(AppiumBy.XPATH, xpath)

    def text_present(self, text: str) -> bool:
        return text in (self.page_source() or "")

    def any_text_present(self, texts: list) -> "str | None":
        for text in texts:
            if self.text_present(text):
                return text
        return None

    def tap_by_id(self, raw_id: str) -> None:
        self.find_by_id(raw_id).click()

    def tap_by_text(self, text: str) -> None:
        self.find_by_text(text).click()

    def tap_by_xpath(self, xpath: str) -> None:
        from appium.webdriver.common.appiumby import AppiumBy
        self.driver.find_element(AppiumBy.XPATH, xpath).click()

    def type_text(self, raw_id: str, text: str) -> None:
        self.find_by_id(raw_id).send_keys(text)

    def hide_keyboard(self) -> None:
        try:
            self.driver.hide_keyboard()
        except Exception:
            pass

    def back(self) -> None:
        self.driver.back()

    def wait_for_id(self, raw_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.find_by_id(raw_id)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def wait_for_text(self, text: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.text_present(text):
                return True
            time.sleep(0.5)
        return False
