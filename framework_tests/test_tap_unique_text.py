"""tap_unique_text / find_all_by_exact_text -- extends the row-scoped
actions' "never select an arbitrary element" guarantee (test_row_scoped_
actions.py) to flat lists with no row-container id to scope by, e.g. an
Agenda list of calendar events. See docs/TABLET_MUTATION_COVERAGE_GAPS.md's
"never select an arbitrary event row, fail on zero or multiple matching
rows" requirement.
"""

from __future__ import annotations

import re

import pytest

from calee_regression import runner
from calee_regression.appium_driver import CaleeDriver
from calee_regression.config import Config
from calee_regression.models import STATUS_PASSED


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


class El:
    def __init__(self, text):
        self.text = text
        self.clicked = False

    def click(self):
        self.clicked = True


class FlatDriver:
    """A flat (non-row) list of elements matched by exact @text or
    @content-desc -- models an Agenda list where events share no
    row-container resource id to scope by."""

    def __init__(self, texts):
        self.elements = [El(t) for t in texts]

    def find_elements(self, by, xpath):
        m = re.search(r'@text="([^"]*)"', xpath) or re.search(r"@text='([^']*)'", xpath)
        wanted = m.group(1) if m else None
        return [e for e in self.elements if e.text == wanted]


def _driver(texts):
    d = CaleeDriver(_config())
    d.driver = FlatDriver(texts)
    return d


def test_tap_unique_text_taps_the_single_match():
    d = _driver(["REG-SCRATCH-EVENT-MUTATION-ALPHA"])
    d.tap_unique_text("REG-SCRATCH-EVENT-MUTATION-ALPHA")
    assert d.driver.elements[0].clicked is True


def test_tap_unique_text_exact_match_ignores_substring_collision():
    # "...-ALPHA" must not match "...-ALPHA-EXTRA" -- a substring match here
    # would risk tapping the wrong (or an ambiguous) event.
    d = _driver(["REG-SCRATCH-EVENT-MUTATION-ALPHA", "REG-SCRATCH-EVENT-MUTATION-ALPHA-EXTRA"])
    d.tap_unique_text("REG-SCRATCH-EVENT-MUTATION-ALPHA")
    assert d.driver.elements[0].clicked is True
    assert d.driver.elements[1].clicked is False


def test_tap_unique_text_zero_matches_raises():
    d = _driver(["something else"])
    with pytest.raises(LookupError, match="No element"):
        d.tap_unique_text("REG-SCRATCH-EVENT-MUTATION-ALPHA")


def test_tap_unique_text_multiple_matches_raises_and_never_taps():
    d = _driver(["REG-DUP", "REG-DUP"])
    with pytest.raises(LookupError, match="ambiguous"):
        d.tap_unique_text("REG-DUP")
    assert all(not e.clicked for e in d.driver.elements)


def test_find_all_by_exact_text_returns_every_exact_match():
    d = _driver(["x", "x", "y"])
    assert len(d.find_all_by_exact_text("x")) == 2
    assert len(d.find_all_by_exact_text("y")) == 1
    assert len(d.find_all_by_exact_text("z")) == 0


def test_tap_unique_text_action_is_wired_in_runner():
    d = _driver(["REG-SCRATCH-EVENT-MUTATION-ALPHA"])
    step = {"name": "Open the event", "action": "tap_unique_text", "text": "REG-SCRATCH-EVENT-MUTATION-ALPHA"}

    result = runner._execute_step({"driver": d}, step)

    assert result.status == STATUS_PASSED
    assert d.driver.elements[0].clicked is True
