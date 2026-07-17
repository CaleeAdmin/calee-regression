from __future__ import annotations

import tempfile
import time
from pathlib import Path

import yaml

from .appium_driver import CaleeDriver
from .models import (
    STATE_MISMATCH_HINT,
    STATUS_BLOCKED,
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
        mandatory=bool(raw.get("mandatory", True)),
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


def _step_is_optional(step: dict) -> bool:
    """A step is optional if explicitly marked `optional: true` or
    `required: false`. The default is required -- see Workstream 1's
    "the default must be required" rule. Only the step author's explicit
    marking may downgrade an absent element from BLOCKED to SKIPPED.
    """
    if "optional" in step:
        return bool(step["optional"])
    if "required" in step:
        return not bool(step["required"])
    return False


def _step_tap_if_present(ctx, step, result: StepResult):
    try:
        _tap_target(ctx["driver"], step)
    except Exception:
        if _step_is_optional(step):
            result.status = STATUS_SKIPPED
            result.message = "element not present, skipped (step marked optional)"
        else:
            # Absence of a required element means this scenario cannot
            # complete reliably -- that is an environment/product-state
            # uncertainty, not evidence the product regressed, so it must
            # block rather than silently letting the scenario pass. Mark
            # the step `optional: true` (or `required: false`) if its
            # absence really is acceptable.
            result.status = STATUS_BLOCKED
            result.message = (
                "element not present, and this step is required (default) -- "
                "the scenario cannot reliably continue. Mark this step "
                "'optional: true' if its absence is genuinely acceptable."
            )
        return

    verify_id = step.get("then_wait_for_id")
    if verify_id:
        # The element existed and was tapped -- if it's a nav target whose
        # destination screen must then render, a present-but-broken target
        # (e.g. a nav tab that crashes/hangs when opened) must be caught as
        # a real product failure, not silently pass just because the tap
        # itself succeeded.
        timeout = step.get("then_wait_for_id_timeout_seconds", 10)
        if not ctx["driver"].wait_for_id(verify_id, timeout):
            raise AssertionError(
                f"Tapped {step.get('id') or step.get('text') or step.get('xpath')!r} but the "
                f"expected content (id={verify_id!r}) never appeared within {timeout}s -- present "
                f"but did not render."
            )
        result.message = f"tapped and verified id {verify_id!r} appeared"


def _step_tap_if_absent(ctx, step, result: StepResult):
    """Tap `id`/`text`/`xpath` only if `unless_id` is not already present.

    For idempotent toggle controls (e.g. an expand/collapse rail button)
    where blindly tapping every run would flip an already-correct,
    persisted UI state back to the wrong one instead of reliably reaching
    a known state.
    """
    unless_id = step["unless_id"]
    if ctx["driver"].id_present(unless_id):
        result.status = STATUS_SKIPPED
        result.message = f"{unless_id!r} already present, toggle not needed"
        return
    _tap_target(ctx["driver"], step)
    result.message = f"{unless_id!r} was not present, tapped toggle"


def _step_type_text(ctx, step):
    ctx["driver"].type_text(step["id"], step["text"])


def _step_clear_text(ctx, step):
    ctx["driver"].clear_text(step["id"])


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
    # _execute_step() always catches the nested step's own exceptions
    # internally and returns a StepResult rather than raising -- so this
    # must inspect the returned result's status, not wrap the call in a
    # try/except (which would never trigger and would silently leave this
    # step PASSED even when the wrapped assertion actually failed).
    nested = step["step"]
    inner = _execute_step(ctx, nested)
    result.screenshot_path = inner.screenshot_path
    result.diff_path = inner.diff_path
    if inner.status in (STATUS_FAILED, STATUS_BLOCKED):
        result.status = STATUS_WARNING
        result.message = (
            f"optional step {nested.get('name', nested.get('action'))!r} did not succeed: {inner.message}"
        )
    else:
        result.status = inner.status
        result.message = inner.message


def _step_fail_if_text(ctx, step, result: StepResult):
    texts = step.get("texts") or [step["text"]]
    found = ctx["driver"].any_text_present(texts)
    if found is not None:
        raise AssertionError(f"Unexpected text present: {found!r} (checked {texts!r})")
    result.message = f"none of {texts!r} present, as expected"


def _step_fail_if_id(ctx, step, result: StepResult):
    raw_id = step["id"]
    if ctx["driver"].id_present(raw_id):
        raise AssertionError(f"Unexpected element present: id={raw_id!r}")
    result.message = f"id {raw_id!r} not present, as expected"


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
    "tap_if_absent": _step_tap_if_absent,
    "type_text": _step_type_text,
    "clear_text": _step_clear_text,
    "hide_keyboard": _step_hide_keyboard,
    "back": _step_back,
    "wait_for_id": _step_wait_for_id,
    "wait_for_text": _step_wait_for_text,
    "optional": _step_optional,
    "fail_if_text": _step_fail_if_text,
    "fail_if_id": _step_fail_if_id,
    "assert_current_activity": _step_assert_current_activity,
}


VERIFYING_ACTIONS = {
    "assert_text", "assert_any_text", "assert_id", "wait_for_id", "wait_for_text",
    "fail_if_text", "fail_if_id", "assert_current_activity",
}


def _step_is_real_verification(step: dict) -> bool:
    """Whether a PASSED result for this step is evidence something was
    actually checked, as opposed to a no-op (launch/sleep/a screenshot
    taken with compare: false all trivially resolve PASSED without
    verifying anything)."""
    action = step.get("action")
    if action in VERIFYING_ACTIONS:
        return True
    if action == "screenshot" and step.get("compare", True):
        return True
    if action == "tap_if_present" and step.get("then_wait_for_id"):
        return True
    return False


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
                # A scenario file that fails to parse is a framework/authoring
                # problem, not evidence the product regressed — it must never
                # count as a FAIL, or a broken scenario file could silently
                # masquerade as a genuine product bug in the report.
                blocked_reason = (
                    f"Scenario definition at {path} is invalid and could not be loaded — "
                    f"this is a framework/configuration problem, not a product failure. "
                    f"Contact the technical owner. Details: {exc}"
                )
                suite_result.scenarios.append(
                    ScenarioResult(
                        name=str(path),
                        file=str(path),
                        status=STATUS_BLOCKED,
                        blocked_reason=blocked_reason,
                        steps=[
                            StepResult(
                                name="load_scenario", action="load", status=STATUS_BLOCKED,
                                message=str(exc),
                            )
                        ],
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
                        mandatory=scenario.mandatory,
                    )
                )
            else:
                runnable.append(scenario)

        if runnable:
            driver = CaleeDriver(self.config)
            try:
                driver.start_session()
            except Exception as exc:
                # Appium unreachable, device disconnected, wrong appium_url, etc.
                # are test-environment problems, never a product regression —
                # see docs/TEST_DATA_RESET_CONTRACT.md and the core design
                # requirement that a disconnected device or unavailable Appium
                # server must never be reported as a product failure.
                hint = explain_exception(exc)
                blocked_reason = (
                    f"Could not start an Appium session: {exc}. This blocks every scenario "
                    f"in this run — it is an environment/tooling problem, not a product failure."
                )
                for scenario in runnable:
                    suite_result.scenarios.append(
                        ScenarioResult(
                            name=scenario.name,
                            file=str(scenario.file),
                            status=STATUS_BLOCKED,
                            blocked_reason=blocked_reason,
                            tags=scenario.tags,
                            steps=[
                                StepResult(
                                    name="start_session", action="launch", status=STATUS_BLOCKED,
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
        stopped = False
        saw_failed = False
        saw_blocked = False
        saw_real_verification = False
        blocked_reason = None

        for raw_step in scenario.steps:
            if stopped:
                steps.append(
                    StepResult(
                        name=raw_step.get("name", raw_step.get("action", "unnamed step")),
                        action=raw_step.get("action", ""),
                        status=STATUS_SKIPPED,
                        message="not run: earlier step failed or blocked",
                    )
                )
                continue

            result = _execute_step(ctx, raw_step)
            steps.append(result)
            if result.status == STATUS_FAILED:
                saw_failed = True
                stopped = True
            elif result.status == STATUS_BLOCKED:
                saw_blocked = True
                blocked_reason = result.message
                stopped = True
            elif result.status == STATUS_PASSED and _step_is_real_verification(raw_step):
                saw_real_verification = True

        if saw_failed:
            # A real product assertion failure always wins over a block --
            # see docs/RELEASE_POLICY.md's FAIL-beats-BLOCKED precedence.
            status = STATUS_FAILED
        elif saw_blocked:
            status = STATUS_BLOCKED
        elif not saw_real_verification:
            # Nothing in this scenario actually verified anything -- e.g.
            # every tap_if_present target was absent-and-optional, or every
            # assertion was wrapped in `optional`, leaving only no-op steps
            # (launch/sleep/screenshot-without-compare all trivially
            # resolve PASSED without checking anything). That must never
            # read as a release PASS -- see Workstream 1's "all-optional
            # scenario with no actual assertions" requirement.
            status = STATUS_BLOCKED
            blocked_reason = (
                "No step in this scenario actually verified anything -- every assertion was "
                "skipped, optional, or absent. This cannot count as a release pass."
            )
        else:
            status = STATUS_PASSED

        return ScenarioResult(
            name=scenario.name,
            file=str(scenario.file),
            status=status,
            steps=steps,
            duration_seconds=time.monotonic() - started,
            tags=scenario.tags,
            mandatory=scenario.mandatory,
            blocked_reason=blocked_reason if status == STATUS_BLOCKED else None,
        )
