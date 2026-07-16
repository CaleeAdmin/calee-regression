from __future__ import annotations

import tempfile
import time
from pathlib import Path

import yaml

from .appium_driver import CaleeDriver
from .models import (
    STATE_MISMATCH_HINT,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    STATUS_WARNING,
    VALID_REQUIRES_STATES,
    Scenario,
    ScenarioResult,
    StepResult,
    SuiteResult,
)
from .preflight import explain_exception

STATE_SENSITIVE_ACTIONS = {"assert_text", "assert_any_text", "wait_for_text", "wait_for_id", "assert_current_activity"}


class ScenarioError(Exception):
    pass


def load_scenario(path) -> Scenario:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"Scenario file at {path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ScenarioError(f"Scenario file at {path} must contain a YAML mapping at the top level.")

    if not raw.get("name"):
        raise ScenarioError(f"Scenario file at {path} is missing required field 'name'.")

    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ScenarioError(f"Scenario file at {path} is missing a non-empty 'steps' list.")

    requires_state = raw.get("requires_state", "any")
    if requires_state not in VALID_REQUIRES_STATES:
        raise ScenarioError(
            f"Scenario file at {path} has invalid requires_state {requires_state!r}. "
            f"Must be one of: {', '.join(sorted(VALID_REQUIRES_STATES))}."
        )

    return Scenario(
        name=raw["name"],
        file=path,
        tags=raw.get("tags", []),
        requires_state=requires_state,
        default_timeout_seconds=int(raw.get("default_timeout_seconds", 20)),
        steps=steps,
    )


def _screenshot_target(ctx, name: str) -> Path:
    if ctx["report_builder"] is not None:
        return ctx["report_builder"].screenshot_path(name)
    tmp_dir = Path(tempfile.gettempdir()) / "calee_regression_screenshots"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir / f"{name}.png"


def _diff_dir(ctx) -> Path:
    if ctx["report_builder"] is not None:
        return ctx["report_builder"].diff_dir()
    return Path(tempfile.gettempdir()) / "calee_regression_screenshots"


def _step_launch(ctx, step):
    ctx["driver"].launch()


def _step_start_activity(ctx, step):
    config = ctx["config"]
    ctx["driver"].start_activity(step.get("package", config.app_package), step.get("activity", config.app_activity))


def _step_start_action(ctx, step):
    config = ctx["config"]
    ctx["driver"].start_action(step.get("action", config.start_action), step.get("package", config.app_package))


def _step_shell(ctx, step, result: StepResult):
    output = ctx["driver"].shell(step["command"])
    result.message = (output or "")[:500]


def _step_sleep(ctx, step):
    time.sleep(step["seconds"])


def _step_screenshot(ctx, step, result: StepResult):
    config = ctx["config"]
    time.sleep(config.screenshot_stabilize_seconds)
    target = _screenshot_target(ctx, step["screenshot_name"])
    ctx["driver"].screenshot(target)
    result.screenshot_path = str(target)

    if step.get("compare", True):
        from .visual import compare_screenshot

        diff_result = compare_screenshot(
            target,
            Path(config.baseline_dir),
            step["screenshot_name"],
            step.get("max_diff_ratio", config.max_diff_ratio),
            step.get("pixel_threshold", config.pixel_threshold),
            _diff_dir(ctx),
        )
        result.diff_path = diff_result.diff_path
        if not diff_result.match:
            raise AssertionError(diff_result.message)
        result.message = diff_result.message


def _step_assert_text(ctx, step):
    text = step["text"]
    if not ctx["driver"].text_present(text):
        raise AssertionError(f"Expected text not found: {text!r}")


def _step_assert_any_text(ctx, step, result: StepResult):
    texts = step["texts"]
    found = ctx["driver"].any_text_present(texts)
    if found is None:
        raise AssertionError(f"None of the expected texts were found: {texts!r}")
    result.message = f"found {found!r} (checked {texts!r})"


def _step_assert_id(ctx, step):
    ctx["driver"].find_by_id(step["id"])


def _tap_target(driver, step):
    if "id" in step:
        driver.tap_by_id(step["id"])
    elif "text" in step:
        driver.tap_by_text(step["text"])
    elif "xpath" in step:
        driver.tap_by_xpath(step["xpath"])
    else:
        raise ScenarioError("tap/tap_if_present step requires one of: id, text, xpath")


def _step_tap(ctx, step):
    _tap_target(ctx["driver"], step)


def _step_tap_if_present(ctx, step, result: StepResult):
    try:
        _tap_target(ctx["driver"], step)
    except Exception:
        result.status = STATUS_SKIPPED
        result.message = "element not present, skipped"


def _step_type_text(ctx, step):
    ctx["driver"].type_text(step["id"], step["text"])


def _step_hide_keyboard(ctx, step):
    ctx["driver"].hide_keyboard()


def _step_back(ctx, step):
    ctx["driver"].back()


def _step_wait_for_id(ctx, step, result: StepResult):
    scenario = ctx["scenario"]
    timeout = step.get("timeout_seconds", scenario.default_timeout_seconds)
    if not ctx["driver"].wait_for_id(step["id"], timeout):
        raise AssertionError(f"Timed out waiting for id {step['id']!r} after {timeout}s")


def _step_wait_for_text(ctx, step, result: StepResult):
    scenario = ctx["scenario"]
    timeout = step.get("timeout_seconds", scenario.default_timeout_seconds)
    if not ctx["driver"].wait_for_text(step["text"], timeout):
        raise AssertionError(f"Timed out waiting for text {step['text']!r} after {timeout}s")


def _step_optional(ctx, step, result: StepResult):
    nested = step["step"]
    try:
        _execute_step(ctx, nested)
    except Exception as exc:
        result.status = STATUS_WARNING
        result.message = f"optional step {nested.get('name', nested.get('action'))!r} did not succeed: {exc}"


def _step_fail_if_text(ctx, step, result: StepResult):
    texts = step.get("texts") or [step["text"]]
    found = ctx["driver"].any_text_present(texts)
    if found is not None:
        raise AssertionError(f"Unexpected text present: {found!r} (checked {texts!r})")
    result.message = f"none of {texts!r} present, as expected"


def _step_assert_current_activity(ctx, step):
    expected = step["activity"]
    actual = ctx["driver"].current_activity()
    expected_bare = expected.lstrip(".")
    actual_bare = actual.lstrip(".")
    if expected_bare not in actual_bare and actual_bare not in expected_bare:
        raise AssertionError(f"Expected current activity to contain {expected!r}, got {actual!r}")


ACTIONS = {
    "launch": _step_launch,
    "start_activity": _step_start_activity,
    "start_action": _step_start_action,
    "shell": _step_shell,
    "sleep": _step_sleep,
    "screenshot": _step_screenshot,
    "assert_text": _step_assert_text,
    "assert_any_text": _step_assert_any_text,
    "assert_id": _step_assert_id,
    "tap": _step_tap,
    "tap_if_present": _step_tap_if_present,
    "type_text": _step_type_text,
    "hide_keyboard": _step_hide_keyboard,
    "back": _step_back,
    "wait_for_id": _step_wait_for_id,
    "wait_for_text": _step_wait_for_text,
    "optional": _step_optional,
    "fail_if_text": _step_fail_if_text,
    "assert_current_activity": _step_assert_current_activity,
}


def _execute_step(ctx, step: dict) -> StepResult:
    action = step.get("action")
    handler = ACTIONS.get(action)
    name = step.get("name", action or "unnamed step")
    result = StepResult(name=name, action=action or "", status=STATUS_PASSED)

    if handler is None:
        result.status = STATUS_FAILED
        result.message = f"Unknown action: {action!r}"
        return result

    started = time.monotonic()
    try:
        import inspect

        if "result" in inspect.signature(handler).parameters:
            handler(ctx, step, result)
        else:
            handler(ctx, step)
        if result.status == STATUS_PASSED:
            result.message = result.message or "ok"
    except Exception as exc:
        result.status = STATUS_FAILED
        result.message = str(exc)
        result.hint = explain_exception(exc)
        if ctx["scenario"].requires_state == "logged_in_tablet" and action in STATE_SENSITIVE_ACTIONS:
            result.hint = STATE_MISMATCH_HINT
    finally:
        result.duration_seconds = time.monotonic() - started

    return result


class ScenarioRunner:
    def __init__(self, config, report_builder=None):
        self.config = config
        self.report_builder = report_builder

    def check_state_compatibility(self, scenario: Scenario) -> "str | None":
        if scenario.requires_state == "logged_in_tablet" and self.config.expected_state == "fresh":
            return (
                "This scenario requires a prepared, logged-in tablet (requires_state=logged_in_tablet), "
                "but the config's expected_state is 'fresh'. Skipping to avoid a false failure — set "
                "expected_state: logged_in_tablet in your config once the tablet/emulator has a signed-in "
                "demo account, then re-run."
            )
        if scenario.requires_state == "physical_tablet" and self.config.is_emulator():
            return (
                f"This scenario requires a real physical tablet (requires_state=physical_tablet) and "
                f"cannot run on an emulator (udid={self.config.udid!r}). Run it against a real device."
            )
        return None

    def run_scenarios(self, scenario_paths: list, suite_name: str = "") -> SuiteResult:
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        suite_result = SuiteResult(name=suite_name or "adhoc", started_at=started_at)

        loaded = []
        for path in scenario_paths:
            try:
                loaded.append(load_scenario(path))
            except ScenarioError as exc:
                suite_result.scenarios.append(
                    ScenarioResult(
                        name=str(path),
                        file=str(path),
                        status=STATUS_FAILED,
                        steps=[StepResult(name="load_scenario", action="load", status=STATUS_FAILED, message=str(exc))],
                    )
                )

        runnable = []
        for scenario in loaded:
            skip_reason = self.check_state_compatibility(scenario)
            if skip_reason:
                suite_result.scenarios.append(
                    ScenarioResult(
                        name=scenario.name,
                        file=str(scenario.file),
                        status=STATUS_SKIPPED,
                        skip_reason=skip_reason,
                        tags=scenario.tags,
                    )
                )
            else:
                runnable.append(scenario)

        if runnable:
            driver = CaleeDriver(self.config)
            try:
                driver.start_session()
            except Exception as exc:
                hint = explain_exception(exc)
                for scenario in runnable:
                    suite_result.scenarios.append(
                        ScenarioResult(
                            name=scenario.name,
                            file=str(scenario.file),
                            status=STATUS_FAILED,
                            tags=scenario.tags,
                            steps=[
                                StepResult(
                                    name="start_session", action="launch", status=STATUS_FAILED,
                                    message=str(exc), hint=hint,
                                )
                            ],
                        )
                    )
                runnable = []
            else:
                try:
                    for scenario in runnable:
                        suite_result.scenarios.append(self.run_scenario(driver, scenario))
                finally:
                    driver.quit()

        order = {str(p): i for i, p in enumerate(scenario_paths)}
        suite_result.scenarios.sort(key=lambda r: order.get(r.file, len(order)))
        suite_result.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return suite_result

    def run_scenario(self, driver, scenario: Scenario) -> ScenarioResult:
        ctx = {
            "driver": driver,
            "config": self.config,
            "scenario": scenario,
            "report_builder": self.report_builder,
        }
        steps: list = []
        started = time.monotonic()
        failed = False

        for raw_step in scenario.steps:
            if failed:
                steps.append(
                    StepResult(
                        name=raw_step.get("name", raw_step.get("action", "unnamed step")),
                        action=raw_step.get("action", ""),
                        status=STATUS_SKIPPED,
                        message="not run: earlier step failed",
                    )
                )
                continue

            result = _execute_step(ctx, raw_step)
            steps.append(result)
            if result.status == STATUS_FAILED:
                failed = True

        status = STATUS_FAILED if failed else STATUS_PASSED
        return ScenarioResult(
            name=scenario.name,
            file=str(scenario.file),
            status=status,
            steps=steps,
            duration_seconds=time.monotonic() - started,
            tags=scenario.tags,
        )
