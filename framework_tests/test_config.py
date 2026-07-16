import pytest
import yaml

from calee_regression import config


def _base_config_dict(**overrides):
    d = {
        "appium_url": "http://127.0.0.1:4723/wd/hub",
        "device_name": "Calee Test Tablet",
        "udid": "emulator-5554",
        "apk_path": "/tmp/calee.apk",
        "app_package": "com.viso.calee",
        "app_activity": ".ui.HomeActivity",
        "shell_package": "com.viso.caleeshell",
        "shell_activity": ".ui.LauncherActivity",
        "launch_strategy": "direct_activity",
        "start_action": "com.viso.calee.action.START",
        "default_timeout_seconds": 20,
        "report_dir": "reports",
        "baseline_dir": "baselines",
        "screenshot_stabilize_seconds": 0.5,
        "max_diff_ratio": 0.01,
        "pixel_threshold": 12,
        "expected_state": "fresh",
    }
    d.update(overrides)
    return d


def _write_config(tmp_path, data, filename="tester.local.yaml"):
    path = tmp_path / filename
    with path.open("w") as f:
        yaml.safe_dump(data, f)
    return path


def test_load_valid_config(tmp_path):
    path = _write_config(tmp_path, _base_config_dict())
    cfg = config.load_config(path)

    assert cfg.appium_url == "http://127.0.0.1:4723/wd/hub"
    assert cfg.app_package == "com.viso.calee"
    assert cfg.app_activity == ".ui.HomeActivity"
    assert cfg.shell_package == "com.viso.caleeshell"
    assert cfg.launch_strategy == "direct_activity"
    assert cfg.expected_state == "fresh"
    assert cfg.default_timeout_seconds == 20


def test_load_config_applies_defaults_for_omitted_optional_fields(tmp_path):
    data = _base_config_dict()
    for optional_field in ("default_timeout_seconds", "report_dir", "baseline_dir", "expected_state"):
        data.pop(optional_field, None)
    path = _write_config(tmp_path, data)

    cfg = config.load_config(path)

    assert cfg.default_timeout_seconds == 20
    assert cfg.report_dir == "reports"
    assert cfg.baseline_dir == "baselines"
    assert cfg.expected_state == "fresh"


def test_load_config_missing_required_field_raises(tmp_path):
    data = _base_config_dict()
    del data["app_activity"]
    path = _write_config(tmp_path, data)

    with pytest.raises(config.ConfigError):
        config.load_config(path)


def test_rejects_put_activity_here_placeholder(tmp_path):
    data = _base_config_dict(app_activity="PUT_ACTIVITY_HERE")
    path = _write_config(tmp_path, data)

    with pytest.raises(config.ConfigError, match="PUT_ACTIVITY_HERE"):
        config.load_config(path)


def test_rejects_put_activity_here_placeholder_in_other_fields(tmp_path):
    data = _base_config_dict(shell_activity="PUT_ACTIVITY_HERE")
    path = _write_config(tmp_path, data)

    with pytest.raises(config.ConfigError, match="PUT_ACTIVITY_HERE"):
        config.load_config(path)


def test_load_config_invalid_launch_strategy_raises(tmp_path):
    data = _base_config_dict(launch_strategy="not_a_real_strategy")
    path = _write_config(tmp_path, data)

    with pytest.raises(config.ConfigError):
        config.load_config(path)


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load_config(tmp_path / "does_not_exist.yaml")


@pytest.mark.parametrize(
    "udid,is_physical,expected",
    [
        ("emulator-5554", None, True),
        ("R58N12ABCDE", None, False),
        ("emulator-5554", True, False),
        ("R58N12ABCDE", False, True),
    ],
)
def test_is_emulator(tmp_path, udid, is_physical, expected):
    data = _base_config_dict(udid=udid)
    if is_physical is not None:
        data["is_physical_device"] = is_physical
    path = _write_config(tmp_path, data)

    cfg = config.load_config(path)

    assert cfg.is_emulator() is expected
