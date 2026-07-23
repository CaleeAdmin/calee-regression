"""WS3: non-mutating, bounded-scrolling unique-text assertions
(wait_for_unique_text / assert_unique_text). All fake-driver, no Appium/device.

Proves: a visible event passes without scrolling; a below-fold event passes
after bounded scrolling; a delayed event passes; an absent event FAILs with
evidence; duplicate titles FAIL as ambiguity; a stale query is re-resolved; the
assertion NEVER taps; and the non-scrolling wait_for_text remains available.
"""

from __future__ import annotations

import re

import pytest

from calee_regression import runner
from calee_regression.appium_driver import CaleeDriver, RowAmbiguityError, RowResolutionError
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


def _scenario():
    return type("S", (), {"requires_state": None, "default_timeout_seconds": 20})()


class StaleQuery(Exception):
    """Named so CaleeDriver treats it like a stale-element condition."""


def _wanted(xpath):
    m = re.search(r'@text="([^"]*)"', xpath) or re.search(r"@text='([^']*)'", xpath)
    return m.group(1) if m else None


class El:
    def __init__(self, text):
        self.text = text
        self.clicked = False

    def click(self):
        self.clicked = True


class FlatDriver:
    def __init__(self, texts):
        self.elements = [El(t) for t in texts]

    def find_elements(self, by, xpath):
        wanted = _wanted(xpath)
        return [e for e in self.elements if e.text == wanted]


class ScrollDriver:
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

    def swipe(self, *a, **k):
        self.swipes += 1
        self._page += 1

    def find_elements(self, by, xpath):
        if _wanted(xpath) != self.text:
            return []
        return [self.el] if self.swipes >= self.appear_after_swipes else []


class DelayedDriver:
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


class StaleThenPresentDriver:
    def __init__(self, text):
        self.text = text
        self.calls = 0
        self.el = El(text)

    def find_elements(self, by, xpath):
        if _wanted(xpath) != self.text:
            return []
        self.calls += 1
        if self.calls == 1:
            raise StaleQuery("stale element reference during query")
        return [self.el]


class EmptyDiagnosticDriver:
    @property
    def page_source(self):
        return "<hierarchy/>"

    def get_window_size(self):
        return {"width": 1000, "height": 1600}

    def swipe(self, *a, **k):
        pass

    def find_elements(self, by, xpath):
        return []

    def get_screenshot_as_file(self, path):
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG\r\n")
        return True


def _driver(fake, *, tmp_path=None):
    d = CaleeDriver(_config())
    d.driver = fake
    d.unique_text_timeout = 0.0
    d.unique_text_retry_interval = 0.0
    if tmp_path is not None:
        d.diagnostics_dir = str(tmp_path)
    return d


def _run(d, action, text, **extra):
    step = {"name": f"{action} step", "action": action, "text": text, **extra}
    return runner._execute_step({"driver": d, "scenario": _scenario()}, step)


# ── visible / below-fold / delayed ─────────────────────────────────────────
def test_visible_event_passes_without_scrolling():
    d = _driver(FlatDriver(["REG-EVENT-RECURRING-001"]))
    result = _run(d, "assert_unique_text", "REG-EVENT-RECURRING-001")
    assert result.status == STATUS_PASSED
    assert d.driver.elements[0].clicked is False  # never taps
    assert result.row_metrics["matchedRows"] == 1


def test_below_fold_event_passes_after_scrolling():
    d = _driver(ScrollDriver("REG-EVENT-RECURRING-001", appear_after_swipes=1))
    d.unique_text_timeout = 5.0
    result = _run(d, "wait_for_unique_text", "REG-EVENT-RECURRING-001", scroll=True, max_swipes=5, timeout_seconds=5)
    assert result.status == STATUS_PASSED
    assert d.driver.swipes >= 1
    assert d.driver.el.clicked is False
    assert result.row_metrics["scrolls"] >= 1


def test_delayed_event_passes():
    d = _driver(DelayedDriver("REG-LATE", appear_after=2))
    result = _run(d, "wait_for_unique_text", "REG-LATE", timeout_seconds=5)
    assert result.status == STATUS_PASSED
    assert result.row_metrics["attempts"] >= 3


# ── absent / ambiguous / stale ─────────────────────────────────────────────
def test_absent_event_fails_with_evidence(tmp_path):
    d = _driver(EmptyDiagnosticDriver(), tmp_path=tmp_path)
    d.unique_text_timeout = 0.1
    d.unique_text_retry_interval = 0.02
    result = _run(d, "wait_for_unique_text", "REG-NEVER")
    assert result.status == STATUS_FAILED
    assert result.screenshot_path is not None
    assert result.page_source_path is not None
    assert result.row_metrics["matchedRows"] == 0


def test_duplicate_titles_fail_as_ambiguous(tmp_path):
    d = _driver(FlatDriver(["REG-DUP", "REG-DUP"]), tmp_path=tmp_path)
    result = _run(d, "assert_unique_text", "REG-DUP")
    assert result.status == STATUS_FAILED
    assert "ambiguous" in result.message.lower()
    assert all(not e.clicked for e in d.driver.elements)
    assert result.row_metrics["matchedRows"] == 2


def test_stale_query_is_re_resolved():
    d = _driver(StaleThenPresentDriver("REG-STALE"))
    d.unique_text_timeout = 5.0
    result = _run(d, "wait_for_unique_text", "REG-STALE", timeout_seconds=5)
    assert result.status == STATUS_PASSED
    assert d.driver.calls >= 2  # first stale query retried
    assert d.driver.el.clicked is False


# ── driver-level: resolve_unique_text raises the right exceptions ──────────
def test_resolve_unique_text_zero_raises(tmp_path):
    d = _driver(FlatDriver(["other"]), tmp_path=tmp_path)
    with pytest.raises(RowResolutionError):
        d.resolve_unique_text("REG-NEVER", timeout=0.0)


def test_resolve_unique_text_many_raises_ambiguity(tmp_path):
    d = _driver(FlatDriver(["REG-DUP", "REG-DUP"]), tmp_path=tmp_path)
    with pytest.raises(RowAmbiguityError):
        d.resolve_unique_text("REG-DUP", timeout=0.0)


def test_missing_text_is_authoring_error():
    d = _driver(FlatDriver([]))
    result = runner._execute_step(
        {"driver": d, "scenario": _scenario()},
        {"name": "bad", "action": "assert_unique_text"},
    )
    assert result.status == STATUS_FAILED
    assert "requires a 'text'" in result.message


# ── the non-scrolling wait_for_text remains available for fixed-screen labels
def test_non_scrolling_wait_for_text_still_works():
    class FixedScreen:
        page_source = "<hierarchy>tvEventDetailTitle REG-EVENT-RECURRING-001</hierarchy>"

    d = CaleeDriver(_config())
    d.driver = FixedScreen()
    assert d.wait_for_text("REG-EVENT-RECURRING-001", timeout=0.5) is True
    assert "wait_for_text" in runner.ACTIONS
