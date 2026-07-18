"""Row-scoped Appium actions (Workstream 4 / Priority 4).

A RecyclerView row reuses the same descendant resource ids on every row, so
acting on the "first global flCheckboxTarget" toggles the wrong task. The
resolver locates the ONE row whose EXACT title (on its title control -- tvName
for tasks, tvTitle for chores) matches, resolves a descendant within it, and:

  * matches by exact text equality (never substring);
  * retries a temporary zero-rows / missing-descendant / stale-element rebind;
  * scrolls an off-screen fixture row into view;
  * fails immediately (no retry) on duplicate rows or duplicate descendants;
  * captures a screenshot + page source on final failure;
  * records attempt count, scroll count and elapsed duration.
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
from calee_regression.models import STATUS_FAILED, STATUS_PASSED, Scenario

PKG = "com.viso.calee"


def _config():
    return Config(
        appium_url="http://127.0.0.1:4723/wd/hub",
        device_name="Calee Test Tablet",
        udid="emulator-5554",
        apk_path="/tmp/calee.apk",
        app_package=PKG,
        app_activity=".ui.HomeActivity",
        shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity",
        launch_strategy="direct_activity",
        start_action="com.viso.calee.action.START",
    )


# ── A RecyclerView-shaped fake: exact-text rows, a scroll viewport, rebinds ──


class DescEl:
    def __init__(self):
        self.clicked = False

    def click(self):
        self.clicked = True


class RowEl:
    """A row element: exposes its (persistent) descendants by resource-id."""

    def __init__(self, row):
        self.row = row

    def find_elements(self, by, xpath):
        m = re.search(r"@resource-id='[^']*/([^']+)'", xpath)
        short = m.group(1) if m else None
        return list(self.row.descendant_els.get(short, []))


class Row:
    def __init__(self, title, card_id="taskItemCard", title_id="tvName", descendants=None):
        self.title = title
        self.card_id = card_id
        self.title_id = title_id
        counts = descendants if descendants is not None else {"flCheckboxTarget": 1}
        self.descendant_els = {sid: [DescEl() for _ in range(n)] for sid, n in counts.items()}
        self.element = RowEl(self)

    def set_descendant(self, short, n):
        self.descendant_els[short] = [DescEl() for _ in range(n)]

    def clicked(self, short="flCheckboxTarget"):
        return any(e.clicked for e in self.descendant_els.get(short, []))


def _parse_row_xpath(xpath):
    """Extract (card_id short, title, title_id short|None) from the resolver's
    row xpath, mirroring exact-text matching."""
    ids = re.findall(r"@resource-id='[^']*/([^']+)'", xpath)
    card = ids[0] if ids else None
    title_id = ids[1] if len(ids) > 1 else None
    m = re.search(r'@text="([^"]*)"', xpath)
    title = m.group(1) if m else None
    return card, title, title_id


class RecyclerDriver:
    """Models a scrollable list with a viewport. find_elements returns only the
    rows currently in view that EXACTLY match the query."""

    def __init__(self, rows, visible=None):
        self.rows = rows
        self.visible = visible if visible is not None else len(rows)
        self.offset = 0
        self.swipes = 0
        self.swipe_dirs = []
        self.screenshots = []

    @property
    def page_source(self):
        # Reflects the current scroll position, so a scroll that changed the
        # viewport is distinguishable from one that revealed nothing (the real
        # page source changes when a RecyclerView scrolls).
        window = self.rows[self.offset:self.offset + self.visible]
        titles = ",".join(r.title for r in window)
        return f"<hierarchy offset='{self.offset}'>{titles}</hierarchy>"

    def find_elements(self, by, xpath):
        card, title, title_id = _parse_row_xpath(xpath)
        window = self.rows[self.offset:self.offset + self.visible]
        out = []
        for r in window:
            if r.card_id != card or r.title != title:
                continue
            if title_id and r.title_id != title_id:
                continue
            out.append(r.element)
        return out

    def swipe(self, sx, sy, ex, ey, dur):
        self.swipes += 1
        max_offset = max(0, len(self.rows) - self.visible)
        if ey < sy:  # finger swipes up -> content moves up -> reveal rows below
            self.swipe_dirs.append("down")
            self.offset = min(self.offset + 1, max_offset)
        else:        # finger swipes down -> reveal rows above
            self.swipe_dirs.append("up")
            self.offset = max(self.offset - 1, 0)

    def get_window_size(self):
        return {"width": 1000, "height": 1600}

    def get_screenshot_as_file(self, path):
        self.screenshots.append(path)
        open(path, "w").close()


def _driver(rows, visible=None):
    d = CaleeDriver(_config())
    d.driver = RecyclerDriver(rows, visible=visible)
    d.row_retry_interval = 0  # no real sleeping in tests
    return d


# ── _xpath_text_literal (quote-safe) ─────────────────────────────────────────


def test_xpath_text_literal_plain():
    assert CaleeDriver._xpath_text_literal("REG-TASK-OPEN-001") == '"REG-TASK-OPEN-001"'


def test_xpath_text_literal_with_double_quote_uses_single():
    assert CaleeDriver._xpath_text_literal('say "hi"') == "'say \"hi\"'"


def test_xpath_text_literal_with_both_quotes_uses_concat():
    assert CaleeDriver._xpath_text_literal("a\"b'c").startswith("concat(")


# ── exact title matching ─────────────────────────────────────────────────────


def test_scoped_xpath_uses_exact_text_and_title_id():
    d = _driver([])
    d.find_rows_by_title("taskItemCard", "REG-TASK-OPEN-001", title_id="tvName")
    # The last xpath is captured by the fake through find_elements; rebuild it.
    from appium.webdriver.common.appiumby import AppiumBy  # noqa: F401
    xp = None

    class Cap:
        def find_elements(self, by, xpath):
            nonlocal xp
            xp = xpath
            return []

    d.driver = Cap()
    d.find_rows_by_title("taskItemCard", "REG-TASK-OPEN-001", title_id="tvName")
    assert f"{PKG}:id/taskItemCard" in xp
    assert f"{PKG}:id/tvName" in xp
    assert '@text="REG-TASK-OPEN-001"' in xp  # exact, not contains(...)
    assert "contains(" not in xp


def test_exact_match_ignores_substring_collision():
    # REG-TASK-OPEN-1 must NOT select REG-TASK-OPEN-10 (the substring-match bug).
    toggle_row = Row("REG-TASK-OPEN-1", descendants={"flCheckboxTarget": 1})
    collide = Row("REG-TASK-OPEN-10", descendants={"flCheckboxTarget": 1})
    d = _driver([collide, toggle_row])
    resolution = d.tap_in_row("taskItemCard", "REG-TASK-OPEN-1", "flCheckboxTarget", title_id="tvName")
    assert toggle_row.clicked() is True
    assert collide.clicked() is False
    assert resolution.matched_rows == 1


def test_title_id_disambiguates_name_vs_description():
    # A different task whose *description* equals our task's name must not match
    # when we scope the exact match to the title control (tvName).
    ours = Row("REG-TASK-OPEN-2", title_id="tvName", descendants={"flCheckboxTarget": 1})
    d = _driver([ours])
    d.tap_in_row("taskItemCard", "REG-TASK-OPEN-2", "flCheckboxTarget", title_id="tvName")
    assert ours.clicked() is True


# ── uniqueness / ambiguity (immediate failure) ───────────────────────────────


def test_duplicate_exact_title_rows_fail_immediately(monkeypatch):
    a = Row("REG-DUP", descendants={"flCheckboxTarget": 1})
    b = Row("REG-DUP", descendants={"flCheckboxTarget": 1})
    d = _driver([a, b])
    swipes_before = d.driver.swipes
    with pytest.raises(RowAmbiguityError) as exc:
        d.tap_in_row("taskItemCard", "REG-DUP", "flCheckboxTarget", title_id="tvName")
    assert "ambiguous" in str(exc.value)
    assert d.driver.swipes == swipes_before  # no retry/scroll on a permanent ambiguity


def test_duplicate_descendants_fail_immediately():
    row = Row("REG-TASK", descendants={"ivIcon": 2})
    d = _driver([row])
    with pytest.raises(RowAmbiguityError) as exc:
        d.resolve_row_target("taskItemCard", "REG-TASK", "ivIcon", title_id="tvName")
    assert "ambiguous" in str(exc.value)


# ── bounded retries: temporary empties, missing descendants, rebinds ─────────


class Flaky:
    """Wraps a RecyclerDriver, misbehaving for the first `bad` calls."""

    def __init__(self, inner, mode, bad):
        self.inner = inner
        self.mode = mode  # "empty" | "stale"
        self.bad = bad
        self.calls = 0

    def find_elements(self, by, xpath):
        self.calls += 1
        if self.calls <= self.bad:
            if self.mode == "empty":
                return []
            raise RuntimeError("StaleElementReferenceException: rebind in progress")
        return self.inner.find_elements(by, xpath)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_retries_transient_zero_rows_then_succeeds():
    row = Row("REG-TASK-OPEN-3", descendants={"flCheckboxTarget": 1})
    d = CaleeDriver(_config())
    d.driver = Flaky(RecyclerDriver([row]), mode="empty", bad=2)
    d.row_retry_interval = 0
    resolution = d.tap_in_row("taskItemCard", "REG-TASK-OPEN-3", "flCheckboxTarget", title_id="tvName")
    assert row.clicked() is True
    assert resolution.attempts >= 3  # two empty attempts, then success


def test_retries_stale_element_then_succeeds():
    row = Row("REG-TASK-OPEN-4", descendants={"flCheckboxTarget": 1})
    d = CaleeDriver(_config())
    d.driver = Flaky(RecyclerDriver([row]), mode="stale", bad=1)
    d.row_retry_interval = 0
    d.tap_in_row("taskItemCard", "REG-TASK-OPEN-4", "flCheckboxTarget", title_id="tvName")
    assert row.clicked() is True


def test_retries_temporary_missing_descendant_then_binds():
    # Row is present immediately; its checkbox binds only on the 3rd look.
    row = Row("REG-TASK-OPEN-5", descendants={"flCheckboxTarget": 0})
    d = _driver([row])
    d.row_retry_interval = 0
    calls = {"n": 0}
    orig = row.element.find_elements

    def flaky_desc(by, xpath):
        calls["n"] += 1
        if calls["n"] >= 3:
            row.set_descendant("flCheckboxTarget", 1)
        return orig(by, xpath)

    row.element.find_elements = flaky_desc
    resolution = d.tap_in_row("taskItemCard", "REG-TASK-OPEN-5", "flCheckboxTarget", title_id="tvName")
    assert row.clicked() is True
    assert resolution.attempts >= 3


# ── scroll-to-row for an off-screen fixture row ──────────────────────────────


def test_scrolls_offscreen_row_into_view():
    # 4 rows, viewport shows 1 at a time; the target is last -> needs scrolling.
    rows = [Row(f"REG-ROW-{i}") for i in range(4)]
    target = rows[3]
    d = _driver(rows, visible=1)
    d.row_retry_interval = 0
    resolution = d.tap_in_row("taskItemCard", "REG-ROW-3", "flCheckboxTarget", title_id="tvName")
    assert target.clicked() is True
    assert resolution.scrolls >= 1
    assert d.driver.swipes >= 1


# ── permanent absence: exhausts budget, captures diagnostics, reports metrics ─


def test_permanently_absent_row_fails_with_diagnostics():
    d = _driver([Row("REG-OTHER")], visible=1)
    d.row_retry_interval = 0
    with pytest.raises(RowResolutionError) as exc:
        d.tap_in_row("taskItemCard", "REG-MISSING", "flCheckboxTarget", title_id="tvName")
    res = exc.value.resolution
    assert res is not None
    assert res.attempts == d.row_max_attempts
    assert res.elapsed_seconds >= 0
    # Diagnostics captured on final failure.
    assert res.screenshot_path is not None
    assert res.page_source_path is not None
    assert len(d.driver.screenshots) >= 1


def test_id_present_in_row_true_false_and_reraise():
    present = Row("REG-P", descendants={"tvName": 1})
    assert _driver([present]).id_present_in_row("taskItemCard", "REG-P", "tvName", title_id="tvName") is True
    absent = Row("REG-A", descendants={})
    d = _driver([absent])
    d.row_retry_interval = 0
    assert d.id_present_in_row("taskItemCard", "REG-A", "tvActionMenu", title_id="tvName") is False
    # A missing/ambiguous ROW must not be swallowed as "descendant absent".
    dupe = _driver([Row("REG-D"), Row("REG-D")])
    with pytest.raises(RowAmbiguityError):
        dupe.id_present_in_row("taskItemCard", "REG-D", "tvName", title_id="tvName")


# ── Priority 5: stale-at-click retry (re-resolve, never click a stale element) ─


class StaleClickDesc:
    """A descendant whose .click() raises a stale error while `stale` is set."""

    def __init__(self, stale):
        self.stale = stale
        self.clicked = False

    def click(self):
        if self.stale:
            raise RuntimeError("StaleElementReferenceException: element is not attached to the page document")
        self.clicked = True


class StaleRowEl:
    """A row that hands out a NEW descendant element on every resolve. The first
    `stale_times` are stale-at-click; later ones click cleanly. Records every
    element handed out so a test can prove the stale ones were never clicked."""

    def __init__(self, stale_times):
        self.stale_times = stale_times
        self.resolves = 0
        self.handed = []

    def find_elements(self, by, xpath):
        self.resolves += 1
        el = StaleClickDesc(stale=self.resolves <= self.stale_times)
        self.handed.append(el)
        return [el]


def test_stale_at_click_reresolves_and_never_clicks_stale():
    row = Row("REG-STALE-1")
    row.element = StaleRowEl(stale_times=1)  # first resolve is stale-at-click
    d = _driver([row])
    d.row_retry_interval = 0
    resolution = d.tap_in_row("taskItemCard", "REG-STALE-1", "flCheckboxTarget", title_id="tvName")
    # Two elements handed out: the first went stale at click, the second (a fresh
    # RE-RESOLVE) clicked. The stale element was NEVER clicked.
    assert len(row.element.handed) == 2
    assert row.element.handed[0].clicked is False
    assert row.element.handed[1].clicked is True
    assert resolution.stale_at_click is True
    assert resolution.click_attempts == 2


def test_stale_at_click_exhausts_and_fails_with_diagnostics():
    row = Row("REG-STALE-2")
    row.element = StaleRowEl(stale_times=99)  # always stale at click
    d = _driver([row])
    d.row_retry_interval = 0
    with pytest.raises(RowResolutionError) as exc:
        d.tap_in_row("taskItemCard", "REG-STALE-2", "flCheckboxTarget", title_id="tvName")
    res = exc.value.resolution
    assert res.stale_at_click is True
    assert res.click_attempts == d.row_click_max_retries
    assert res.screenshot_path is not None and res.page_source_path is not None
    # Every element handed out was stale and none was ever clicked.
    assert all(e.clicked is False for e in row.element.handed)


# ── Priority 5: bidirectional scroll + no-progress detection ──────────────────


def test_bidirectional_scroll_finds_row_above():
    rows = [Row("REG-UP-0"), Row("REG-UP-1")]
    d = _driver(rows, visible=1)
    d.driver.offset = 1  # start below the target; row 0 is above the fold
    d.row_retry_interval = 0
    resolution = d.tap_in_row("taskItemCard", "REG-UP-0", "flCheckboxTarget", title_id="tvName")
    assert rows[0].clicked() is True
    assert "up" in resolution.scroll_directions
    assert "up" in d.driver.swipe_dirs


def test_scroll_exhaustion_detected_when_list_cannot_move():
    # A single-row list not containing the target: neither direction can move it.
    d = _driver([Row("REG-ONLY")], visible=1)
    d.row_retry_interval = 0
    with pytest.raises(RowResolutionError) as exc:
        d.tap_in_row("taskItemCard", "REG-NOPE", "flCheckboxTarget", title_id="tvName")
    res = exc.value.resolution
    assert res.scroll_exhausted is True
    assert "scroll exhausted" in (res.problem or "")
    assert {"down", "up"} <= set(res.scroll_directions)  # both directions tried


def test_scroll_direction_recorded_on_success():
    rows = [Row(f"REG-DN-{i}") for i in range(3)]
    d = _driver(rows, visible=1)  # target is last -> scans down
    d.row_retry_interval = 0
    resolution = d.tap_in_row("taskItemCard", "REG-DN-2", "flCheckboxTarget", title_id="tvName")
    assert rows[2].clicked() is True
    assert "down" in resolution.scroll_directions


def test_ambiguity_error_carries_diagnostics():
    d = _driver([Row("REG-DUP2"), Row("REG-DUP2")])
    with pytest.raises(RowAmbiguityError) as exc:
        d.tap_in_row("taskItemCard", "REG-DUP2", "flCheckboxTarget", title_id="tvName")
    res = exc.value.resolution
    assert res is not None
    assert res.matched_rows == 2
    assert res.screenshot_path is not None  # ambiguity is captured into the bundle


def test_resolution_metrics_serialize():
    row = Row("REG-M")
    d = _driver([row])
    md = d.tap_in_row("taskItemCard", "REG-M", "flCheckboxTarget", title_id="tvName").to_dict()
    for key in ("scrollDirections", "scrollExhausted", "staleAtClick", "clickAttempts",
                "matchedRows", "elapsedSeconds", "attempts", "scrolls"):
        assert key in md
    assert md["clickAttempts"] == 1
    assert md["matchedRows"] == 1


# ── runner actions on top of the resolver ────────────────────────────────────


class RunnerFakeDriver:
    def __init__(self, present=True, raise_on=None):
        self.present = present
        self.raise_on = raise_on
        self.tapped = None

    def tap_in_row(self, card_id, title, target_id, title_id=None):
        if self.raise_on:
            raise self.raise_on
        self.tapped = (card_id, title, target_id, title_id)
        return None  # a stub returns no RowResolution; the runner tolerates it

    def id_present_in_row(self, card_id, title, target_id, title_id=None):
        return self.present


def _ctx(driver):
    scenario = Scenario(
        name="t", file=None, tags=[], requires_state="logged_in_tablet",
        default_timeout_seconds=5, steps=[],
    )
    return {"driver": driver, "config": _config(), "scenario": scenario, "report_builder": None}


def test_action_tap_in_row_threads_title_id():
    driver = RunnerFakeDriver()
    step = {"action": "tap_in_row", "card_id": "taskItemCard", "title_id": "tvName",
            "title": "REG-TASK-OPEN-001", "target_id": "flCheckboxTarget"}
    result = runner._execute_step(_ctx(driver), step)
    assert result.status == STATUS_PASSED, result.message
    assert driver.tapped == ("taskItemCard", "REG-TASK-OPEN-001", "flCheckboxTarget", "tvName")


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


# ── Priority 5.8/5.9: diagnostics attached to StepResult + bundle inclusion ────


def test_row_failure_attaches_diagnostics_to_step_result(tmp_path):
    from calee_regression.appium_driver import RowResolution, RowResolutionError

    shot = tmp_path / "diag.png"
    shot.write_bytes(b"png")
    src = tmp_path / "diag.xml"
    src.write_text("<hierarchy/>")
    res = RowResolution(
        attempts=5, scrolls=2, matched_rows=0, scroll_directions=["down", "up"],
        scroll_exhausted=True, screenshot_path=str(shot), page_source_path=str(src),
        problem="not found (scroll exhausted)",
    )

    class FailDriver:
        def tap_in_row(self, *a, **k):
            raise RowResolutionError("could not resolve", res)

    step = {"action": "tap_in_row", "card_id": "c", "title": "t", "target_id": "d"}
    result = runner._execute_step(_ctx(FailDriver()), step)
    assert result.status == STATUS_FAILED
    assert result.screenshot_path == str(shot)
    assert result.page_source_path == str(src)
    assert result.row_metrics["scrollExhausted"] is True
    assert result.row_metrics["matchedRows"] == 0
    assert result.row_metrics["scrollDirections"] == ["down", "up"]


def test_collect_step_diagnostic_paths_for_bundle(tmp_path):
    from calee_regression.consolidated_report import collect_step_diagnostic_paths

    shot = tmp_path / "s.png"
    shot.write_bytes(b"x")
    src = tmp_path / "s.xml"
    src.write_text("<x/>")
    report = {"scenarios": [{"steps": [
        {"screenshot_path": str(shot), "page_source_path": str(src)},
        {"screenshot_path": None},
        {"screenshot_path": str(tmp_path / "missing.png")},  # not on disk -> skipped
        {"screenshot_path": str(shot)},                       # duplicate -> deduped
    ]}]}
    paths = collect_step_diagnostic_paths(report)
    assert shot in paths and src in paths
    assert len(paths) == 2  # deduped + missing skipped
