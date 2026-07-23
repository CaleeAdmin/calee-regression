from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .models import DEVICE_INIT_STANDARD, LAUNCH_STRATEGIES, VALID_DEVICE_INIT_MODES

REQUIRED_STRING_FIELDS = [
    "appium_url",
    "device_name",
    "udid",
    "apk_path",
    "app_package",
    "app_activity",
    "shell_package",
    "shell_activity",
    "launch_strategy",
    "start_action",
]

VALID_EXPECTED_STATES = {"fresh", "logged_in_tablet"}

PLACEHOLDER = "PUT_ACTIVITY_HERE"


class ConfigError(Exception):
    pass


@dataclass
class Config:
    appium_url: str
    device_name: str
    udid: str
    apk_path: str
    app_package: str
    app_activity: str
    shell_package: str
    shell_activity: str
    launch_strategy: str
    start_action: str
    default_timeout_seconds: int = 20
    report_dir: str = "reports"
    baseline_dir: str = "baselines"
    screenshot_stabilize_seconds: float = 0.5
    max_diff_ratio: float = 0.01
    pixel_threshold: int = 12
    expected_state: str = "fresh"
    allow_release_technical: bool = False
    is_physical_device: "bool | None" = None
    no_reset: bool = True
    new_command_timeout_seconds: int = 120
    # Tablet device-initialization mode (Workstream 6): "standard" (default,
    # certification-eligible) or "skip" (diagnostic-only). Never falls back
    # from standard to skip automatically.
    device_initialization_mode: str = DEVICE_INIT_STANDARD
    config_path: "Path | None" = None

    def is_emulator(self) -> bool:
        if self.is_physical_device is not None:
            return not self.is_physical_device
        return self.udid.startswith("emulator-")


def default_config_path() -> "Path | None":
    value = os.environ.get("CALEE_TEST_CONFIG")
    if value:
        return Path(value)
    return None


def _collect_placeholder_errors(raw: dict) -> list:
    errors = []
    for key, value in raw.items():
        if isinstance(value, str) and PLACEHOLDER.lower() in value.lower():
            errors.append(
                f"{key} still contains the placeholder {PLACEHOLDER} — replace it with the "
                f"real value (see docs/CALEE_LAUNCH_MODEL.md)."
            )
    return errors


def load_config(path) -> Config:
    path = Path(path)
    errors = []

    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file at {path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config file at {path} must contain a YAML mapping at the top level.")

    for field_name in REQUIRED_STRING_FIELDS:
        value = raw.get(field_name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"Missing or invalid required field: {field_name}")

    errors.extend(_collect_placeholder_errors(raw))

    launch_strategy = raw.get("launch_strategy")
    if isinstance(launch_strategy, str) and launch_strategy not in LAUNCH_STRATEGIES:
        errors.append(
            f"Invalid launch_strategy: {launch_strategy!r}. Must be one of: "
            f"{', '.join(sorted(LAUNCH_STRATEGIES))}."
        )

    expected_state = raw.get("expected_state", "fresh")
    if expected_state not in VALID_EXPECTED_STATES:
        errors.append(
            f"Invalid expected_state: {expected_state!r}. Must be one of: "
            f"{', '.join(sorted(VALID_EXPECTED_STATES))}."
        )

    device_initialization_mode = raw.get("device_initialization_mode", DEVICE_INIT_STANDARD)
    if device_initialization_mode not in VALID_DEVICE_INIT_MODES:
        errors.append(
            f"Invalid device_initialization_mode: {device_initialization_mode!r}. Must be one of: "
            f"{', '.join(sorted(VALID_DEVICE_INIT_MODES))}."
        )

    if errors:
        raise ConfigError(
            f"Config file at {path} has {len(errors)} problem(s):\n" + "\n".join(f"  - {e}" for e in errors)
        )

    return Config(
        appium_url=raw["appium_url"],
        device_name=raw["device_name"],
        udid=raw["udid"],
        apk_path=raw["apk_path"],
        app_package=raw["app_package"],
        app_activity=raw["app_activity"],
        shell_package=raw["shell_package"],
        shell_activity=raw["shell_activity"],
        launch_strategy=raw["launch_strategy"],
        start_action=raw["start_action"],
        default_timeout_seconds=int(raw.get("default_timeout_seconds", 20)),
        report_dir=raw.get("report_dir", "reports"),
        baseline_dir=raw.get("baseline_dir", "baselines"),
        screenshot_stabilize_seconds=float(raw.get("screenshot_stabilize_seconds", 0.5)),
        max_diff_ratio=float(raw.get("max_diff_ratio", 0.01)),
        pixel_threshold=int(raw.get("pixel_threshold", 12)),
        expected_state=expected_state,
        allow_release_technical=bool(raw.get("allow_release_technical", False)),
        is_physical_device=raw.get("is_physical_device"),
        no_reset=bool(raw.get("no_reset", True)),
        new_command_timeout_seconds=int(raw.get("new_command_timeout_seconds", 120)),
        device_initialization_mode=device_initialization_mode,
        config_path=path.resolve(),
    )
