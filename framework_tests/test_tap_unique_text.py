"""tap_unique_text / resolve_unique_text -- extends the row-scoped actions'
"never select an arbitrary element" guarantee (test_row_scoped_actions.py) to
flat lists with no row-container id to scope by, e.g. an Agenda list of calendar
events. See docs/TABLET_MUTATION_COVERAGE_GAPS.md's "never select an arbitrary
event row, fail on zero or multiple matching rows" requirement.

Priority 8 hardens the flat-list resolution with bounded retries, optional
bounded scrolling, stale-element recovery and on-failure diagnostics -- all
exercised here with fake drivers (no Appium/device).
"""

from __future__ import annotations

import re

import pytest

from calee_regression import runner
from calee_regression.appium_driver import (
    CaleeDriver,
    RowAmbiguityError,
    RowResolutionError,
)
from calee_regression.config import Config
from calee_regression.models import STATUS_FAILED, STATUS_PASSED


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


class StaleElementReferenceException(Exception):
    """Named so CaleeDriver._is_stale_exception recognises it by type name."""


def _wanted(xpath):
    m = re.search(r'@text="([^"]*)"', xpath) or re.search(r"@text='([^']*)'", xpath)
    return m.group(1) if m else None


class El:
    def __init__(self, text, *, stale_clicks=0):
        self.text = text
        self.clicked = False
        self.click_calls = 0
        self._stale_clicks = stale_clicks

    def click(self):
        self.click_calls += 1
        if self.click_calls <= self._stale_clicks:
            raise StaleElementReferenceException("stale element reference")
        self.clicked = True


class FlatDriver:
    """A flat (non-row) list of elements matched by exact @text or
    @content-desc -- models an Agenda list where events share no row-container
    resource id to scope by."""

    def __init__(self, texts):
        self.elements = [El(t) for t in texts]

    def find_elements(self, by, xpath):
        wanted = _wanted(xpath)
        return [e for e in self.elements if e.text == wanted]


def _driver(texts, *, tmp_path=None):
    d = CaleeDriver(_config())
    d.driver = FlatDriver(texts)
    # Fast, deterministic budgets for the offline tests.
    d.unique_text_timeout = 0.0
    d.unique_text_retry_interval = 0.0
    if tmp_path is not None:
        d.diagnostics_dir = str(tmp_path)
    return d


# ── one / zero / multiple exact match ─────────────────────────────────────


def test_tap_unique_text_taps_the_single_match():
    d = _driver(["REG-SCRATCH-EVENT-MUTATION-ALPHA"])
    res = d.tap_unique_text("REG-SCRATCH-EVENT-MUTATION-ALPHA")
    assert d.driver.elements[0].clicked is True
    assert res.matched_rows == 1
    assert res.attempts == 1


def test_tap_unique_text_exact_match_ignores_substring_collision():
    # "...-ALPHA" must not match "...-ALPHA-EXTRA" -- a substring match here
    # would risk tapping the wrong (or an ambiguous) event.
    d = _driver(["REG-SCRATCH-EVENT-MUTATION-ALPHA", "REG-SCRATCH-EVENT-MUTATION-ALPHA-EXTRA"])
    d.tap_unique_text("REG-SCRATCH-EVENT-MUTATION-ALPHA")
    assert d.driver.elements[0].clicked is True
    assert d.driver.elements[1].clicked is False


def test_tap_unique_text_zero_matches_raises(tmp_path):
    d = _driver(["something else"], tmp_path=tmp_path)
    with pytest.raises(RowResolutionError, match="No element"):
        d.tap_unique_text("REG-SCRATCH-EVENT-MUTATION-ALPHA")


def test_tap_unique_text_multiple_matches_raises_and_never_taps(tmp_path):
    d = _driver(["REG-DUP", "REG-DUP"], tmp_path=tmp_path)
    with pytest.raises(RowAmbiguityError, match="ambiguous") as exc_info:
        d.tap_unique_text("REG-DUP")
    assert all(not e.clicked for e in d.driver.elements)
    # The ambiguity carries the match count in its resolution evidence.
    assert exc_info.value.resolution.matched_rows == 2


def test_find_all_by_exact_text_returns_every_exact_match():
    d = _driver(["x", "x", "y"])
    assert len(d.find_all_by_exact_text("x")) == 2
    assert len(d.find_all_by_exact_text("y")) == 1
    assert len(d.find_all_by_exact_text("z")) == 0


# ── delayed appearance (bounded wait, re-query each attempt) ───────────────


class DelayedDriver:
    """The match renders only after `appear_after` queries -- models a list
    that populates a beat after the screen settles."""

    def __init__(self, text, appear_after):
        self.text = text
        self.appear_after = appear_after
        self.calls = 0
        self.el = El(text)

    def find_elements(self, by, xpath):
        if _wanted(xpath) != self.text:
            return []
        self.calls += 1
        return [self.el] if self.calls > self.appear_after else []


def test_tap_unique_text_waits_for_delayed_appearance():
    d = CaleeDriver(_config())
    d.driver = DelayedDriver("REG-LATE", appear_after=2)
    d.unique_text_timeout = 5.0
    d.unique_text_retry_interval = 0.0  # spin fast; deadline is generous
    res = d.tap_unique_text("REG-LATE")
    assert d.driver.el.clicked is True
    # It re-queried until the item appeared (3rd query), not just once.
    assert res.attempts >= 3


# ── stale element followed by success (re-resolve, never re-click stale) ───


class StaleThenSuccessDriver:
    """The first resolved element goes stale at click; a re-resolve returns a
    FRESH element that clicks cleanly -- the stale element is never re-clicked."""

    def __init__(self, text):
        self.text = text
        self.first = El(text, stale_clicks=1)
        self.second = El(text)
        self.resolve_calls = 0

    def find_elements(self, by, xpath):
        if _wanted(xpath) != self.text:
            return []
        self.resolve_calls += 1
        return [self.first] if self.resolve_calls == 1 else [self.second]


def test_tap_unique_text_recovers_from_stale_at_click():
    d = CaleeDriver(_config())
    d.driver = StaleThenSuccessDriver("REG-STALE")
    d.unique_text_timeout = 0.0
    d.unique_text_retry_interval = 0.0
    res = d.tap_unique_text("REG-STALE")
    # The FRESH element was clicked; the stale one never successfully clicked.
    assert d.driver.second.clicked is True
    assert d.driver.first.clicked is False
    assert res.stale_at_click is True
    assert res.click_attempts == 2


# ── bounded scrolling brings an off-screen match into view ─────────────────


class ScrollDriver:
    """The match becomes visible only after a scroll -- models an off-screen
    Agenda item. page_source changes per swipe so no-progress detection sees
    progress."""

    def __init__(self, text, appear_after_swipes=1):
        self.text = text
        self.appear_after_swipes = appear_after_swipes
        self.swipes = 0
        self._page = 0
        self.el = El(text)

    @property
    def page_source(self):
        return f"<hierarchy page={self._page}/>"

    def get_window_size(self):
        return {"width": 1000, "height": 1600}

    def swipe(self, *args, **kwargs):
        self.swipes += 1
        self._page += 1

    def find_elements(self, by, xpath):
        if _wanted(xpath) != self.text:
            return []
        return [self.el] if self.swipes >= self.appear_after_swipes else []


def test_tap_unique_text_bounded_scroll_reveals_match():
    d = CaleeDriver(_config())
    d.driver = ScrollDriver("REG-OFFSCREEN", appear_after_swipes=1)
    d.unique_text_timeout = 5.0
    d.unique_text_retry_interval = 0.0
    d.unique_text_max_swipes = 3
    res = d.tap_unique_text("REG-OFFSCREEN", scroll=True)
    assert d.driver.el.clicked is True
    assert res.scrolls >= 1
    assert "down" in res.scroll_directions


def test_tap_unique_text_does_not_scroll_when_not_opted_in():
    # Without scroll=True, an off-screen item is never revealed -> it times out
    # rather than silently scrolling.
    d = CaleeDriver(_config())
    d.driver = ScrollDriver("REG-OFFSCREEN", appear_after_swipes=1)
    d.unique_text_timeout = 0.0
    d.unique_text_retry_interval = 0.0
    with pytest.raises(RowResolutionError):
        d.tap_unique_text("REG-OFFSCREEN")  # scroll not requested
    assert d.driver.swipes == 0


# ── timeout produces diagnostics + metrics ────────────────────────────────


class EmptyDiagnosticDriver:
    """Never matches; supports the diagnostics capture (screenshot + source)."""

    def __init__(self):
        self.screenshots = []

    @property
    def page_source(self):
        return "<hierarchy/>"

    def get_window_size(self):
        return {"width": 1000, "height": 1600}

    def swipe(self, *args, **kwargs):
        pass

    def find_elements(self, by, xpath):
        return []

    def get_screenshot_as_file(self, path):
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG\r\n")
        self.screenshots.append(path)
        return True


def test_tap_unique_text_timeout_captures_diagnostics_and_metrics(tmp_path):
    d = CaleeDriver(_config())
    d.driver = EmptyDiagnosticDriver()
    d.diagnostics_dir = str(tmp_path)
    d.unique_text_timeout = 0.2
    d.unique_text_retry_interval = 0.05
    with pytest.raises(RowResolutionError) as exc_info:
        d.tap_unique_text("REG-NEVER")
    res = exc_info.value.resolution
    assert res is not None
    assert res.matched_rows == 0
    assert res.attempts >= 1
    assert res.elapsed_seconds >= 0.0
    # A screenshot AND page source were captured for the evidence bundle.
    assert res.screenshot_path is not None
    assert res.page_source_path is not None
    from pathlib import Path

    assert Path(res.screenshot_path).is_file()
    assert Path(res.page_source_path).is_file()


# ── runner wiring ─────────────────────────────────────────────────────────


def test_tap_unique_text_action_is_wired_in_runner():
    d = _driver(["REG-SCRATCH-EVENT-MUTATION-ALPHA"])
    step = {"name": "Open the event", "action": "tap_unique_text", "text": "REG-SCRATCH-EVENT-MUTATION-ALPHA"}

    result = runner._execute_step({"driver": d}, step)

    assert result.status == STATUS_PASSED
    assert d.driver.elements[0].clicked is True
    # The step evidence records how hard the resolve worked.
    assert "attempt(s)" in result.message


def test_tap_unique_text_runner_reports_metrics_and_diagnostics_on_failure(tmp_path):
    d = CaleeDriver(_config())
    d.driver = EmptyDiagnosticDriver()
    d.diagnostics_dir = str(tmp_path)
    d.unique_text_timeout = 0.1
    d.unique_text_retry_interval = 0.05
    step = {"name": "Open the event", "action": "tap_unique_text", "text": "REG-NEVER"}

    result = runner._execute_step(
        {"driver": d, "scenario": type("S", (), {"requires_state": None})()}, step
    )

    assert result.status == STATUS_FAILED
    # Row/text resolution diagnostics flow into the step evidence.
    assert result.screenshot_path is not None
    assert result.page_source_path is not None
    assert getattr(result, "row_metrics", None) is not None
    assert result.row_metrics["matchedRows"] == 0


def test_tap_unique_text_runner_forwards_scroll_and_timeout(tmp_path):
    d = CaleeDriver(_config())
    d.driver = ScrollDriver("REG-OFFSCREEN", appear_after_swipes=1)
    d.diagnostics_dir = str(tmp_path)
    d.unique_text_retry_interval = 0.0
    step = {
        "name": "Open the off-screen event",
        "action": "tap_unique_text",
        "text": "REG-OFFSCREEN",
        "scroll": True,
        "timeout": 5.0,
        "max_swipes": 3,
    }

    result = runner._execute_step({"driver": d}, step)

    assert result.status == STATUS_PASSED
    assert d.driver.el.clicked is True
    assert d.driver.swipes >= 1
