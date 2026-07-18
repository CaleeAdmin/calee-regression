"""Row-scoped Appium actions (Workstream 4).

A RecyclerView row reuses the same descendant resource ids on every row, so
acting on the "first global flCheckboxTarget" toggles the wrong task. These
tests cover the generic row-scoped resolver: locate the ONE row whose visible
title matches, resolve a descendant within it, retry safely on a rebind, and
fail loudly on zero or multiple matching rows -- plus the runner actions that
sit on top of it.
"""

from __future__ import annotations

import re

import pytest

from calee_regression import runner
from calee_regression.appium_driver import CaleeDriver
from calee_regression.config import Config
from calee_regression.models import STATUS_FAILED, STATUS_PASSED, Scenario


def _config():
    return Config(
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


class FakeEl:
    """A fake Appium element: a subtree keyed by full resource id."""

    def __init__(self, descendants=None):
        self._descendants = descendants or {}  # full 'pkg:id/<id>' -> list[FakeEl]
        self.clicked = False

    def find_elements(self, by, xpath):
        m = re.search(r"@resource-id='([^']+)'", xpath)
        rid = m.group(1) if m else None
        return list(self._descendants.get(rid, []))

    def click(self):
        self.clicked = True


class FakeWebDriver:
    def __init__(self, rows):
        self._rows = rows
        self.last_xpath = None

    def find_elements(self, by, xpath):
        self.last_xpath = xpath
        return list(self._rows)


def _driver_with_rows(rows):
    d = CaleeDriver(_config())
    d.driver = FakeWebDriver(rows)
    return d


# ── _xpath_text_literal (quote-safe) ─────────────────────────────────────────


def test_xpath_text_literal_plain():
    assert CaleeDriver._xpath_text_literal("REG-TASK-OPEN-001") == '"REG-TASK-OPEN-001"'


def test_xpath_text_literal_with_double_quote_uses_single():
    assert CaleeDriver._xpath_text_literal('say "hi"') == "'say \"hi\"'"


def test_xpath_text_literal_with_both_quotes_uses_concat():
    out = CaleeDriver._xpath_text_literal("a\"b'c")
    assert out.startswith("concat(")


# ── row uniqueness contract ──────────────────────────────────────────────────


def test_find_rows_builds_scoped_xpath():
    d = _driver_with_rows([])
    d.find_rows_by_title("taskItemCard", "REG-TASK-OPEN-001")
    xp = d.driver.last_xpath
    assert "com.viso.calee:id/taskItemCard" in xp
    assert "REG-TASK-OPEN-001" in xp
    assert "content-desc" in xp  # matches text OR content-desc


def test_zero_matching_rows_raises():
    d = _driver_with_rows([])
    with pytest.raises(LookupError) as exc:
        d.tap_in_row("taskItemCard", "REG-TASK-OPEN-001", "flCheckboxTarget")
    assert "No 'taskItemCard' row" in str(exc.value)


def test_multiple_matching_rows_raises():
    d = _driver_with_rows([FakeEl(), FakeEl()])
    with pytest.raises(LookupError) as exc:
        d.tap_in_row("taskItemCard", "REG-TASK-OPEN-001", "flCheckboxTarget")
    assert "ambiguous" in str(exc.value)


def test_unique_row_taps_scoped_descendant():
    toggle = FakeEl()
    row = FakeEl(descendants={"com.viso.calee:id/flCheckboxTarget": [toggle]})
    d = _driver_with_rows([row])
    d.tap_in_row("taskItemCard", "REG-TASK-OPEN-001", "flCheckboxTarget")
    assert toggle.clicked is True


def test_descendant_missing_in_row_raises():
    row = FakeEl(descendants={})
    d = _driver_with_rows([row])
    with pytest.raises(LookupError) as exc:
        d.tap_in_row("taskItemCard", "REG-TASK-OPEN-001", "flCheckboxTarget")
    assert "no descendant" in str(exc.value)


def test_multiple_descendants_in_row_raises():
    row = FakeEl(descendants={"com.viso.calee:id/ivIcon": [FakeEl(), FakeEl()]})
    d = _driver_with_rows([row])
    with pytest.raises(LookupError) as exc:
        d._resolve_row_descendant("taskItemCard", "REG-TASK-OPEN-001", "ivIcon")
    assert "ambiguous" in str(exc.value)


def test_id_present_in_row_true_and_false():
    present_row = FakeEl(descendants={"com.viso.calee:id/tvName": [FakeEl()]})
    assert _driver_with_rows([present_row]).id_present_in_row("taskItemCard", "T", "tvName") is True
    absent_row = FakeEl(descendants={})
    assert _driver_with_rows([absent_row]).id_present_in_row("taskItemCard", "T", "tvName") is False


def test_id_present_in_row_reraises_on_ambiguous_row():
    # A missing/ambiguous ROW must not be swallowed as "descendant absent".
    with pytest.raises(LookupError):
        _driver_with_rows([]).id_present_in_row("taskItemCard", "T", "tvName")


def test_row_scoped_retries_on_transient_rebind_then_succeeds():
    toggle = FakeEl()
    good_row = FakeEl(descendants={"com.viso.calee:id/flCheckboxTarget": [toggle]})

    class FlakyDriver:
        def __init__(self):
            self.calls = 0

        def find_elements(self, by, xpath):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("StaleElementReferenceException: rebind in progress")
            return [good_row]

    d = CaleeDriver(_config())
    d.driver = FlakyDriver()
    d.tap_in_row("taskItemCard", "REG-TASK-OPEN-001", "flCheckboxTarget")
    assert toggle.clicked is True
    assert d.driver.calls >= 2  # retried past the transient failure


# ── runner actions on top of the resolver ────────────────────────────────────


class RunnerFakeDriver:
    def __init__(self, present=True, raise_on=None):
        self.present = present
        self.raise_on = raise_on
        self.tapped = None

    def tap_in_row(self, card_id, title, target_id):
        if self.raise_on:
            raise self.raise_on
        self.tapped = (card_id, title, target_id)

    def id_present_in_row(self, card_id, title, target_id):
        return self.present


def _ctx(driver):
    scenario = Scenario(
        name="t", file=None, tags=[], requires_state="logged_in_tablet",
        default_timeout_seconds=5, steps=[],
    )
    return {"driver": driver, "config": _config(), "scenario": scenario, "report_builder": None}


def test_action_tap_in_row_calls_driver():
    driver = RunnerFakeDriver()
    step = {"action": "tap_in_row", "card_id": "taskItemCard", "title": "REG-TASK-OPEN-001", "target_id": "flCheckboxTarget"}
    result = runner._execute_step(_ctx(driver), step)
    assert result.status == STATUS_PASSED, result.message
    assert driver.tapped == ("taskItemCard", "REG-TASK-OPEN-001", "flCheckboxTarget")


def test_action_assert_in_row_passes_when_present():
    result = runner._execute_step(_ctx(RunnerFakeDriver(present=True)),
                                  {"action": "assert_in_row", "card_id": "c", "title": "t", "target_id": "d"})
    assert result.status == STATUS_PASSED


def test_action_assert_in_row_fails_when_absent():
    result = runner._execute_step(_ctx(RunnerFakeDriver(present=False)),
                                  {"action": "assert_in_row", "card_id": "c", "title": "t", "target_id": "d"})
    assert result.status == STATUS_FAILED


def test_action_fail_if_in_row_fails_when_present():
    result = runner._execute_step(_ctx(RunnerFakeDriver(present=True)),
                                  {"action": "fail_if_in_row", "card_id": "c", "title": "t", "target_id": "d"})
    assert result.status == STATUS_FAILED


def test_action_fail_if_in_row_passes_when_absent():
    result = runner._execute_step(_ctx(RunnerFakeDriver(present=False)),
                                  {"action": "fail_if_in_row", "card_id": "c", "title": "t", "target_id": "d"})
    assert result.status == STATUS_PASSED


@pytest.mark.parametrize("missing", ["card_id", "title", "target_id"])
def test_action_row_scoped_missing_arg_is_authoring_error(missing):
    step = {"action": "tap_in_row", "card_id": "c", "title": "t", "target_id": "d"}
    del step[missing]
    result = runner._execute_step(_ctx(RunnerFakeDriver()), step)
    assert result.status == STATUS_FAILED
    assert "row-scoped action requires" in result.message
