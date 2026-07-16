"""Tests for Workstream 1: correct mandatory-skip handling.

Locks in the distinction between an optional skip (a step/scenario whose
absence is genuinely acceptable) and a mandatory one (whose absence must
block, never silently pass). See docs/RELEASE_POLICY.md and
docs/SCENARIO_REFERENCE.md's `tap_if_present`/`optional`/`mandatory` entries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from calee_regression import cli, runner
from calee_regression.config import Config
from calee_regression.models import (
    EXIT_BLOCKED,
    EXIT_REGRESSION,
    EXIT_SUCCESS,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    Scenario,
    ScenarioResult,
    SuiteResult,
)


def _make_config(**overrides):
    kwargs = dict(
        appium_url="http://127.0.0.1:4723/wd/hub",
        device_name="Calee Test Tablet",
        udid="emulator-5554",
        apk_path="/tmp/calee.apk",
        app_package="com.viso.calee",
        app_activity=".ui.HomeActivity",
        shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity",
        launch_strategy="direct_activity",
        start_action="com.viso.calee.action.START",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_scenario(steps, *, mandatory=True, requires_state="any", file="synthetic.yaml"):
    return Scenario(
        name="synthetic",
        file=Path(file),
        tags=[],
        requires_state=requires_state,
        default_timeout_seconds=5,
        steps=steps,
        mandatory=mandatory,
    )


class _StubDriver:
    """A minimal in-memory stand-in for CaleeDriver: `present_ids` decides
    what `tap_by_id`/`wait_for_id`/`id_present` see as existing."""

    def __init__(self, present_ids=None):
        self.present_ids = set(present_ids or [])

    def tap_by_id(self, raw_id):
        if raw_id not in self.present_ids:
            raise RuntimeError(f"element not found: {raw_id}")

    def tap_by_text(self, text):
        raise RuntimeError(f"element not found: {text}")

    def id_present(self, raw_id):
        return raw_id in self.present_ids

    def wait_for_id(self, raw_id, timeout):
        return raw_id in self.present_ids

    def wait_for_text(self, text, timeout):
        return False

    def find_by_id(self, raw_id):
        if raw_id not in self.present_ids:
            raise RuntimeError(f"element not found: {raw_id}")
        return object()

    def text_present(self, text):
        return False

    def any_text_present(self, texts):
        return None

    def screenshot(self, path):
        Path(path).write_bytes(b"")


def test_required_tap_if_present_missing_element_blocks_scenario():
    driver = _StubDriver(present_ids=set())
    scenario = _make_scenario(
        [
            {"name": "wait home", "action": "wait_for_id", "id": "llHome", "timeout_seconds": 1},
        ]
    )
    # llHome IS present, so the wait passes; the tap target below is not.
    driver.present_ids = {"llHome"}
    scenario.steps.append({"name": "Tap maybe", "action": "tap_if_present", "id": "llMissing"})

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    assert result.status == STATUS_BLOCKED
    assert result.steps[-1].status == STATUS_BLOCKED
    assert "required" in result.blocked_reason.lower()


def test_optional_tap_if_present_missing_element_allows_scenario_to_pass():
    driver = _StubDriver(present_ids={"llHome"})
    scenario = _make_scenario(
        [
            {"name": "wait home", "action": "wait_for_id", "id": "llHome", "timeout_seconds": 1},
            {"name": "Tap maybe", "action": "tap_if_present", "id": "llMissing", "optional": True},
        ]
    )

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    assert result.status == STATUS_PASSED
    assert result.steps[-1].status == STATUS_SKIPPED


def test_required_false_is_equivalent_to_optional_true():
    driver = _StubDriver(present_ids={"llHome"})
    scenario = _make_scenario(
        [
            {"name": "wait home", "action": "wait_for_id", "id": "llHome", "timeout_seconds": 1},
            {"name": "Tap maybe", "action": "tap_if_present", "id": "llMissing", "required": False},
        ]
    )

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    assert result.status == STATUS_PASSED
    assert result.steps[-1].status == STATUS_SKIPPED


def test_tap_if_present_defaults_to_required():
    """No optional/required key at all -- the default must be required."""
    driver = _StubDriver(present_ids=set())
    scenario = _make_scenario([{"name": "Tap maybe", "action": "tap_if_present", "id": "llMissing"}])

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    assert result.status == STATUS_BLOCKED


def test_tap_if_present_then_wait_for_id_catches_present_but_broken_target():
    """The element exists and is tapped, but the destination content never
    renders -- a real product failure, not a soft skip."""
    driver = _StubDriver(present_ids={"llChores"})  # choresRecyclerView never appears
    scenario = _make_scenario(
        [
            {
                "name": "Open chores",
                "action": "tap_if_present",
                "id": "llChores",
                "optional": True,
                "then_wait_for_id": "choresRecyclerView",
                "then_wait_for_id_timeout_seconds": 1,
            },
        ]
    )

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    assert result.status == STATUS_FAILED
    assert result.steps[0].status == STATUS_FAILED


def test_mandatory_skipped_scenario_blocks_suite():
    suite_result = SuiteResult(
        name="mixed",
        scenarios=[
            ScenarioResult(name="p", file="p.yaml", status=STATUS_PASSED, mandatory=True),
            ScenarioResult(name="s", file="s.yaml", status=STATUS_SKIPPED, mandatory=True, skip_reason="state mismatch"),
        ],
    )
    assert suite_result.mandatory_skipped_count == 1
    assert cli._exit_code_for(suite_result) == EXIT_BLOCKED


def test_optional_skipped_scenario_does_not_block_suite():
    suite_result = SuiteResult(
        name="mixed",
        scenarios=[
            ScenarioResult(name="p", file="p.yaml", status=STATUS_PASSED, mandatory=True),
            ScenarioResult(name="s", file="s.yaml", status=STATUS_SKIPPED, mandatory=False, skip_reason="chore-less account"),
        ],
    )
    assert suite_result.mandatory_skipped_count == 0
    assert cli._exit_code_for(suite_result) == EXIT_SUCCESS


def test_state_mismatch_skip_carries_scenario_mandatory_flag_into_suite(monkeypatch):
    class _AlwaysStartsDriver(_StubDriver):
        def start_session(self):
            pass

        def quit(self):
            pass

    monkeypatch.setattr(runner, "CaleeDriver", lambda config: _AlwaysStartsDriver(present_ids={"llHome"}))

    mandatory_but_incompatible = _make_scenario(
        [{"name": "wait home", "action": "wait_for_id", "id": "llHome", "timeout_seconds": 1}],
        requires_state="physical_tablet",
        file="mandatory_incompatible.yaml",
    )
    passing = _make_scenario(
        [{"name": "wait home", "action": "wait_for_id", "id": "llHome", "timeout_seconds": 1}],
        file="passing.yaml",
    )

    scenario_runner = runner.ScenarioRunner(_make_config(udid="emulator-5554"))
    monkeypatch.setattr(scenario_runner, "check_state_compatibility", lambda s: (
        "requires a physical tablet" if s.requires_state == "physical_tablet" else None
    ))

    class _Loader:
        def __call__(self, path):
            return {mandatory_but_incompatible.file: mandatory_but_incompatible, passing.file: passing}[path]

    monkeypatch.setattr(runner, "load_scenario", _Loader())

    suite_result = scenario_runner.run_scenarios([mandatory_but_incompatible.file, passing.file])

    skipped = next(s for s in suite_result.scenarios if s.status == STATUS_SKIPPED)
    assert skipped.mandatory is True
    assert suite_result.mandatory_skipped_count == 1
    assert cli._exit_code_for(suite_result) == EXIT_BLOCKED


def test_all_optional_scenario_with_no_real_assertions_cannot_pass():
    """Every step resolves SKIPPED/no-op -- nothing was actually verified."""
    driver = _StubDriver(present_ids=set())
    scenario = _make_scenario(
        [
            {"name": "maybe launch banner", "action": "tap_if_present", "id": "llMissing", "optional": True},
            {"name": "settle", "action": "sleep", "seconds": 0},
            {"name": "capture", "action": "screenshot", "screenshot_name": "x", "compare": False},
        ]
    )

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    assert result.status == STATUS_BLOCKED
    assert "nothing was actually verified" in result.blocked_reason.lower() or "no step" in result.blocked_reason.lower()


def test_optional_wrapper_reflects_inner_failure_as_warning_not_silent_pass():
    """Regression test for the discovered bug where `action: optional`
    always reported PASSED regardless of the wrapped step's real outcome
    (because _execute_step swallows exceptions internally and the old
    _step_optional never inspected the returned result)."""
    driver = _StubDriver(present_ids={"llHome"})
    scenario = _make_scenario(
        [
            {"name": "wait home", "action": "wait_for_id", "id": "llHome", "timeout_seconds": 1},
            {
                "name": "maybe assert weather",
                "action": "optional",
                "step": {"name": "weather text", "action": "assert_text", "text": "never present"},
            },
        ]
    )

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    from calee_regression.models import STATUS_WARNING

    assert result.steps[-1].status == STATUS_WARNING
    assert "did not succeed" in result.steps[-1].message
    # The scenario itself still passes -- optional means non-blocking --
    # but only because the earlier wait_for_id was a real verification.
    assert result.status == STATUS_PASSED


def test_product_assertion_failure_still_produces_fail():
    driver = _StubDriver(present_ids={"llHome"})
    scenario = _make_scenario(
        [
            {"name": "wait home", "action": "wait_for_id", "id": "llHome", "timeout_seconds": 1},
            {"name": "hard assert", "action": "assert_id", "id": "llMissing"},
        ]
    )

    result = runner.ScenarioRunner(_make_config()).run_scenario(driver, scenario)

    assert result.status == STATUS_FAILED


@pytest.mark.parametrize(
    "statuses,expected",
    [
        # A real failure anywhere must win over a simultaneous block, even
        # when the block comes from a mandatory-skipped scenario.
        ([(STATUS_FAILED, True), (STATUS_SKIPPED, True)], EXIT_REGRESSION),
        ([(STATUS_PASSED, True), (STATUS_SKIPPED, True)], EXIT_BLOCKED),
        ([(STATUS_PASSED, True), (STATUS_SKIPPED, False)], EXIT_SUCCESS),
    ],
)
def test_fail_wins_over_mandatory_skip_blocked_in_consolidation(statuses, expected):
    suite_result = SuiteResult(
        name="s",
        scenarios=[
            ScenarioResult(name=f"n{i}", file="f.yaml", status=status, mandatory=mandatory)
            for i, (status, mandatory) in enumerate(statuses)
        ],
    )
    assert cli._exit_code_for(suite_result) == expected
