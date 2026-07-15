import pytest
import yaml

from calee_regression import appium_driver, runner
from calee_regression.config import Config


def _make_config(launch_strategy="direct_activity", **overrides):
    kwargs = dict(
        appium_url="http://127.0.0.1:4723/wd/hub",
        device_name="Calee Test Tablet",
        udid="emulator-5554",
        apk_path="/tmp/calee.apk",
        app_package="com.viso.calee",
        app_activity=".ui.HomeActivity",
        shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity",
        launch_strategy=launch_strategy,
        start_action="com.viso.calee.action.START",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def test_build_direct_activity_command():
    cfg = _make_config()
    assert appium_driver.build_direct_activity_command(cfg) == [
        "shell", "am", "start", "-W",
        "-n", "com.viso.calee/.ui.HomeActivity",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.DEFAULT",
    ]


def test_build_start_action_command():
    cfg = _make_config()
    assert appium_driver.build_start_action_command(cfg) == [
        "shell", "am", "start", "-W",
        "-a", "com.viso.calee.action.START",
        "-p", "com.viso.calee",
    ]


def test_build_calee_shell_start_command():
    cfg = _make_config()
    assert appium_driver.build_calee_shell_start_command(cfg) == [
        "shell", "am", "start", "-W",
        "-n", "com.viso.caleeshell/.ui.LauncherActivity",
    ]


@pytest.mark.parametrize(
    "strategy,expected_len",
    [
        ("direct_activity", 1),
        ("start_action", 1),
        ("calee_shell", 2),
        ("normal_launcher", 0),
    ],
)
def test_resolve_launch_commands_resolves_correct_strategy(strategy, expected_len):
    cfg = _make_config(launch_strategy=strategy)
    commands = appium_driver.resolve_launch_commands(cfg)

    assert len(commands) == expected_len

    if strategy == "direct_activity":
        assert commands[0] == appium_driver.build_direct_activity_command(cfg)
    elif strategy == "start_action":
        assert commands[0] == appium_driver.build_start_action_command(cfg)
    elif strategy == "calee_shell":
        assert commands[0] == appium_driver.build_calee_shell_start_command(cfg)
        assert commands[1] == appium_driver.build_start_action_command(cfg)
    elif strategy == "normal_launcher":
        assert commands == []


def test_resolve_launch_commands_unknown_strategy_raises():
    cfg = _make_config(launch_strategy="totally_unknown")
    with pytest.raises(appium_driver.LaunchError):
        appium_driver.resolve_launch_commands(cfg)


def _write_scenario(tmp_path, data, filename="scenario.yaml"):
    path = tmp_path / filename
    with path.open("w") as f:
        yaml.safe_dump(data, f)
    return path


def test_load_scenario_valid(tmp_path):
    data = {
        "name": "example",
        "tags": ["smoke"],
        "requires_state": "fresh",
        "default_timeout_seconds": 15,
        "steps": [{"name": "Launch", "action": "launch"}],
    }
    path = _write_scenario(tmp_path, data)

    scenario = runner.load_scenario(path)

    assert scenario.name == "example"
    assert scenario.tags == ["smoke"]
    assert scenario.requires_state == "fresh"
    assert scenario.default_timeout_seconds == 15
    assert scenario.steps == data["steps"]


def test_load_scenario_missing_steps_raises(tmp_path):
    data = {"name": "example", "requires_state": "fresh"}
    path = _write_scenario(tmp_path, data)

    with pytest.raises(runner.ScenarioError):
        runner.load_scenario(path)


def test_load_scenario_invalid_requires_state_raises(tmp_path):
    data = {
        "name": "example",
        "requires_state": "not_a_real_state",
        "steps": [{"name": "Launch", "action": "launch"}],
    }
    path = _write_scenario(tmp_path, data)

    with pytest.raises(runner.ScenarioError):
        runner.load_scenario(path)


def test_load_scenario_defaults_requires_state_to_any(tmp_path):
    data = {"name": "example", "steps": [{"name": "Launch", "action": "launch"}]}
    path = _write_scenario(tmp_path, data)

    scenario = runner.load_scenario(path)

    assert scenario.requires_state == "any"
