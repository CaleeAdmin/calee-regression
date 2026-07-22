from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


class AdbError(Exception):
    pass


class LaunchError(Exception):
    pass


class RowAmbiguityError(LookupError):
    """A PERMANENT row-scoping ambiguity: more than one row matches the exact
    title, or a resolved row has more than one matching descendant. Never
    retried -- acting on any one of them could mutate the wrong fixture. Carries
    the diagnostic capture so the ambiguity is visible in the evidence bundle."""

    def __init__(self, message: str, resolution: "RowResolution | None" = None):
        super().__init__(message)
        self.resolution = resolution


class RowResolutionError(LookupError):
    """The row (or its descendant) could not be resolved within the bounded
    retry/scroll budget: a temporary zero-row / missing-descendant / stale-element
    condition that never settled, a stale-at-click that never bound, or the row
    is simply not present. Carries the diagnostic capture (screenshot + page
    source) and attempt/scroll metrics."""

    def __init__(self, message: str, resolution: "RowResolution | None" = None):
        super().__init__(message)
        self.resolution = resolution


@dataclass
class RowResolution:
    """The outcome of resolving a descendant within the one row whose exact
    title matches -- returned on success and attached to RowResolutionError on
    failure, so a caller/report can record how hard the resolve worked."""

    element: "object | None" = None
    attempts: int = 0
    scrolls: int = 0
    elapsed_seconds: float = 0.0
    matched_rows: int = 0
    screenshot_path: "str | None" = None
    page_source_path: "str | None" = None
    problem: "str | None" = None
    # Priority 5 runtime-safety metrics.
    scroll_directions: "list[str]" = field(default_factory=list)  # e.g. ["down","up"]
    scroll_exhausted: bool = False   # both directions made no further progress
    stale_at_click: bool = False     # element went stale between resolve and click
    click_attempts: int = 0          # how many re-resolve+click cycles were needed

    def to_dict(self) -> dict:
        return {
            "attempts": self.attempts,
            "scrolls": self.scrolls,
            "scrollDirections": list(self.scroll_directions),
            "scrollExhausted": self.scroll_exhausted,
            "staleAtClick": self.stale_at_click,
            "clickAttempts": self.click_attempts,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "matchedRows": self.matched_rows,
            "screenshotPath": self.screenshot_path,
            "pageSourcePath": self.page_source_path,
            "problem": self.problem,
        }


def find_adb_path() -> str:
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.environ.get(env_var)
        if sdk_root:
            candidate = Path(sdk_root) / "platform-tools" / "adb"
            if candidate.exists():
                return str(candidate)
    return "adb"


def run_adb(config, args: list, timeout: int = 30) -> subprocess.CompletedProcess:
    adb_path = find_adb_path()
    cmd = [adb_path] + (["-s", config.udid] if getattr(config, "udid", None) else []) + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise AdbError(
            f"adb executable not found ({adb_path!r}). Set ANDROID_HOME or ANDROID_SDK_ROOT, "
            f"or add platform-tools to PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(
            f"adb command timed out after {timeout}s: {' '.join(cmd)}. "
            f"The device may be unresponsive or not connected."
        ) from exc

    if result.returncode != 0:
        raise AdbError(
            f"adb command failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def build_direct_activity_command(config) -> list:
    return [
        "shell", "am", "start", "-W",
        "-n", f"{config.app_package}/{config.app_activity}",
        "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.DEFAULT",
    ]


def build_start_action_command(config, action=None, package=None) -> list:
    return [
        "shell", "am", "start", "-W",
        "-a", action or config.start_action,
        "-p", package or config.app_package,
    ]


def build_calee_shell_start_command(config) -> list:
    return [
        "shell", "am", "start", "-W",
        "-n", f"{config.shell_package}/{config.shell_activity}",
    ]


def resolve_launch_commands(config) -> list:
    strategy = config.launch_strategy
    if strategy == "direct_activity":
        return [build_direct_activity_command(config)]
    if strategy == "start_action":
        return [build_start_action_command(config)]
    if strategy == "calee_shell":
        return [build_calee_shell_start_command(config), build_start_action_command(config)]
    if strategy == "normal_launcher":
        return []
    raise LaunchError(f"Unknown launch_strategy: {strategy!r}")


class CaleeDriver:
    def __init__(self, config):
        self.config = config
        self.driver = None
        # Row-scoped resolution budget (Priority 4). Overridable per instance
        # (tests set row_retry_interval=0 for speed) or per call.
        self.row_max_attempts = 5      # bounded retries for transient conditions
        self.row_retry_interval = 0.4  # seconds between attempts
        self.row_max_swipes = 3        # RecyclerView scroll-to-row attempts
        # Bounded re-resolve+click cycles when the element goes stale between
        # resolution and .click() (Priority 5). Each cycle RE-RESOLVES the exact
        # row -- a previously resolved stale element is never clicked.
        self.row_click_max_retries = 3
        # Unique-exact-text resolution budget (Priority 8). tap_unique_text waits
        # up to unique_text_timeout for the ONE exact match to appear, re-querying
        # every unique_text_retry_interval, and (when a step opts in) scrolls up
        # to unique_text_max_swipes to bring an off-screen item into view.
        self.unique_text_timeout = 5.0
        self.unique_text_retry_interval = 0.4
        self.unique_text_max_swipes = 3
        # Where final-failure diagnostics (screenshot + page source) are written.
        # The runner points this at the run workspace; None -> a temp dir.
        self.diagnostics_dir = None

    def start_session(self) -> None:
        from appium import webdriver
        from appium.options.android.uiautomator2.base import UiAutomator2Options

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        if self.config.udid:
            options.udid = self.config.udid
        options.device_name = self.config.device_name
        options.no_reset = self.config.no_reset
        options.new_command_timeout = self.config.new_command_timeout_seconds
        options.auto_grant_permissions = True

        if self.config.launch_strategy == "normal_launcher":
            options.app_package = self.config.app_package
            options.app_activity = self.config.app_activity

        self.driver = webdriver.Remote(self.config.appium_url, options=options)

    def quit(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                self.driver = None

    def launch(self) -> None:
        for cmd in resolve_launch_commands(self.config):
            run_adb(self.config, cmd)
        if self.config.launch_strategy == "normal_launcher":
            if self.driver is None:
                raise LaunchError("normal_launcher requires an active Appium session")
            self.driver.activate_app(self.config.app_package)

    def start_activity(self, package: str, activity: str) -> None:
        run_adb(self.config, ["shell", "am", "start", "-W", "-n", f"{package}/{activity}"])

    def start_action(self, action: str, package: "str | None" = None) -> None:
        run_adb(self.config, build_start_action_command(self.config, action=action, package=package))

    def shell(self, command) -> str:
        parts = shlex.split(command) if isinstance(command, str) else list(command)
        result = run_adb(self.config, ["shell"] + parts)
        return result.stdout

    def screenshot(self, path) -> None:
        self.driver.get_screenshot_as_file(str(path))

    def current_activity(self) -> str:
        return self.driver.current_activity or ""

    def current_package(self) -> str:
        return self.driver.current_package or ""

    def page_source(self) -> str:
        return self.driver.page_source

    def _resource_id(self, raw_id: str) -> str:
        return raw_id if ":id/" in raw_id else f"{self.config.app_package}:id/{raw_id}"

    def find_by_id(self, raw_id: str):
        from appium.webdriver.common.appiumby import AppiumBy
        return self.driver.find_element(AppiumBy.ID, self._resource_id(raw_id))

    def find_by_text(self, text: str):
        from appium.webdriver.common.appiumby import AppiumBy
        quote = "'" if '"' not in text else '"'
        xpath = (
            f"//*[contains(@text,{quote}{text}{quote}) or "
            f"contains(@content-desc,{quote}{text}{quote})]"
        )
        return self.driver.find_element(AppiumBy.XPATH, xpath)

    def text_present(self, text: str) -> bool:
        return text in (self.page_source() or "")

    def any_text_present(self, texts: list) -> "str | None":
        for text in texts:
            if self.text_present(text):
                return text
        return None

    def tap_by_id(self, raw_id: str) -> None:
        self.find_by_id(raw_id).click()

    def tap_by_text(self, text: str) -> None:
        self.find_by_text(text).click()

    def tap_by_xpath(self, xpath: str) -> None:
        from appium.webdriver.common.appiumby import AppiumBy
        self.driver.find_element(AppiumBy.XPATH, xpath).click()

    def type_text(self, raw_id: str, text: str) -> None:
        self.find_by_id(raw_id).send_keys(text)

    def clear_text(self, raw_id: str) -> None:
        self.find_by_id(raw_id).clear()

    def hide_keyboard(self) -> None:
        try:
            self.driver.hide_keyboard()
        except Exception:
            pass

    def back(self) -> None:
        self.driver.back()

    def find_all_by_exact_text(self, text: str) -> list:
        """Every element whose @text or @content-desc EXACTLY equals `text` --
        used by tap_unique_text so a scenario can act on a uniquely-titled item
        that has no row-container id to scope by (e.g. a scratch event in an
        Agenda list) without ever guessing among several matches. Exact
        equality, like the row-scoped title match in find_rows_by_title --
        never a substring match, which would let e.g. "...-ALPHA" also match
        "...-ALPHA-EXTRA"."""
        from appium.webdriver.common.appiumby import AppiumBy

        lit = self._xpath_text_literal(text)
        xpath = f"//*[@text={lit} or @content-desc={lit}]"
        return self.driver.find_elements(AppiumBy.XPATH, xpath)

    def resolve_unique_text(
        self, text: str, *,
        timeout: "float | None" = None,
        retry_interval: "float | None" = None,
        scroll: bool = False,
        max_swipes: "int | None" = None,
    ) -> RowResolution:
        """Resolve the ONE element whose @text/@content-desc EXACTLY equals
        ``text``, robust to a live/dynamic Android list (Priority 8):

          * RE-QUERY on every attempt -- a match is never cached across attempts,
            so a recycled/rebound list can't hand back a stale node;
          * bounded wait up to ``timeout`` for a DELAYED appearance (the item
            renders a beat after the screen settles), re-querying every
            ``retry_interval``;
          * EXACTLY one exact match required -- zero keeps waiting, more than one
            is a PERMANENT ambiguity that fails immediately (never tap an
            arbitrary first match);
          * optional BOUNDED scrolling (``scroll=True``) to bring an off-screen
            item into view, with bidirectional no-progress detection so it never
            swipes a list that can't move;
          * STALE-element recovery -- a query that raises a stale condition is
            simply retried on the next attempt;
          * on final failure: a diagnostic screenshot + page source, plus the
            attempt count, elapsed time, matches-on-final-attempt, scroll count
            and diagnostic paths, all on the returned/attached RowResolution.

        Returns the RowResolution (``.element`` set) on success; raises
        RowAmbiguityError (permanent) or RowResolutionError (budget exhausted),
        each carrying the RowResolution for the evidence bundle.
        """
        timeout = self.unique_text_timeout if timeout is None else timeout
        retry_interval = self.unique_text_retry_interval if retry_interval is None else retry_interval
        max_swipes = self.unique_text_max_swipes if max_swipes is None else max_swipes

        start = time.monotonic()
        deadline = start + max(0.0, timeout)
        attempts = 0
        scrolls = 0
        last_matched = 0
        last_problem: "str | None" = None
        scroll_dir = "down"
        scroll_directions: "list[str]" = []
        stuck_dirs: "set[str]" = set()
        scroll_exhausted = False

        while True:
            attempts += 1
            try:
                matches = self.find_all_by_exact_text(text)
                last_matched = len(matches)
                if len(matches) > 1:
                    # Permanent ambiguity -- surface immediately, never tap one.
                    shot, src = self._capture_diagnostics(f"ambiguous-text-{text}")
                    raise RowAmbiguityError(
                        f"{len(matches)} elements match exact text/content-desc {text!r} exactly -- "
                        f"ambiguous. tap_unique_text requires exactly one match; use a more specific/"
                        f"unique title.",
                        RowResolution(
                            attempts=attempts, scrolls=scrolls, matched_rows=len(matches),
                            elapsed_seconds=time.monotonic() - start,
                            scroll_directions=list(scroll_directions),
                            screenshot_path=shot, page_source_path=src,
                            problem=f"{len(matches)} elements match {text!r} exactly",
                        ),
                    )
                if len(matches) == 1:
                    return RowResolution(
                        element=matches[0], attempts=attempts, scrolls=scrolls,
                        elapsed_seconds=time.monotonic() - start, matched_rows=1,
                        scroll_directions=list(scroll_directions), scroll_exhausted=scroll_exhausted,
                    )
                last_problem = f"no element with exact text/content-desc {text!r} visible yet"
            except RowAmbiguityError:
                raise
            except Exception as exc:  # StaleElementReference et al. mid-rebind
                last_problem = f"transient driver error: {exc}"

            # Not resolved this attempt: optionally scroll a row into view
            # (bounded), with no-progress detection and direction switching.
            if scroll and scrolls < max_swipes and not scroll_exhausted:
                before = self._page_fingerprint()
                self._swipe(scroll_dir)
                scrolls += 1
                scroll_directions.append(scroll_dir)
                after = self._page_fingerprint()
                made_progress = before is None or after is None or before != after
                if made_progress:
                    stuck_dirs.discard(scroll_dir)
                else:
                    stuck_dirs.add(scroll_dir)
                    scroll_dir = "up" if scroll_dir == "down" else "down"
                    if {"down", "up"} <= stuck_dirs:
                        scroll_exhausted = True

            now = time.monotonic()
            if now >= deadline:
                break
            if retry_interval:
                time.sleep(min(retry_interval, max(0.0, deadline - now)))

        # Budget exhausted: capture diagnostics and fail with the metrics.
        shot, src = self._capture_diagnostics(f"unresolved-text-{text}")
        if scroll_exhausted and last_matched == 0:
            last_problem = (last_problem or "") + " (scroll exhausted: the list could not be scrolled further in either direction)"
        resolution = RowResolution(
            element=None, attempts=attempts, scrolls=scrolls,
            elapsed_seconds=time.monotonic() - start, matched_rows=last_matched,
            screenshot_path=shot, page_source_path=src, problem=last_problem,
            scroll_directions=list(scroll_directions), scroll_exhausted=scroll_exhausted,
        )
        raise RowResolutionError(
            f"No element with exact text/content-desc {text!r} found after {attempts} attempt(s) "
            f"and {scrolls} scroll(s): {last_problem}.",
            resolution,
        )

    def tap_unique_text(
        self, text: str, *,
        timeout: "float | None" = None,
        scroll: bool = False,
        max_swipes: "int | None" = None,
        max_click_retries: "int | None" = None,
    ) -> RowResolution:
        """Tap the ONE element whose text/content-desc exactly equals ``text``.

        Fails loudly on zero or multiple matches rather than silently acting on
        whichever element find_element happens to return first (Appium's
        find_element has no concept of "ambiguous" -- it just returns the first
        DOM match) -- see docs/TABLET_MUTATION_COVERAGE_GAPS.md's "never select
        an arbitrary event row, fail on zero or multiple matching rows"
        requirement, extended here to a flat list with no row-container id to
        scope by (tap_in_row already covers the case where one exists).

        Priority 8: resolution is bounded-retry + optional-scroll (see
        resolve_unique_text), and if the resolved element goes stale between
        resolution and ``.click()`` the exact match is RE-RESOLVED from scratch
        and the FRESH element clicked -- a previously resolved stale element is
        never re-clicked. Returns the RowResolution so the caller can record how
        hard the resolve worked.
        """
        retries = self.row_click_max_retries if max_click_retries is None else max_click_retries
        retries = max(1, retries)
        last_exc: "Exception | None" = None
        saw_stale = False
        for click_attempt in range(1, retries + 1):
            resolution = self.resolve_unique_text(
                text, timeout=timeout, scroll=scroll, max_swipes=max_swipes
            )
            resolution.click_attempts = click_attempt
            resolution.stale_at_click = saw_stale
            try:
                resolution.element.click()
                return resolution
            except Exception as exc:  # noqa: BLE001
                if not self._is_stale_exception(exc):
                    raise
                last_exc = exc
                saw_stale = True
                resolution.stale_at_click = True
                if click_attempt < retries and self.unique_text_retry_interval:
                    time.sleep(self.unique_text_retry_interval)

        # Every re-resolve produced an element that went stale before the click
        # landed: capture diagnostics and fail with the stale-at-click metric.
        shot, src = self._capture_diagnostics(f"stale-at-click-text-{text}")
        resolution = RowResolution(
            element=None, attempts=retries, matched_rows=1, stale_at_click=True,
            click_attempts=retries, screenshot_path=shot, page_source_path=src,
            problem=f"element went stale at click after {retries} re-resolve(s): {last_exc}",
        )
        raise RowResolutionError(
            f"Element with exact text {text!r} went stale at click after {retries} "
            f"re-resolve(s): {last_exc}.",
            resolution,
        )

    def capture_diagnostics(self, label: str) -> "tuple[str | None, str | None]":
        """Public entry point for on-failure diagnostics (screenshot + page
        source), usable by any caller -- not just the row-scoped resolution
        path that originally motivated _capture_diagnostics. Best-effort: see
        _capture_diagnostics for the failure-swallowing rationale."""
        return self._capture_diagnostics(label)

    def id_present(self, raw_id: str) -> bool:
        """Single-shot presence check (no wait loop, no tap) -- used to
        decide whether a toggle control needs pressing without flipping it
        blindly (see runner.py's tap_if_absent action)."""
        try:
            self.find_by_id(raw_id)
            return True
        except Exception:
            return False

    def wait_for_id(self, raw_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.find_by_id(raw_id)
                return True
            except Exception:
                time.sleep(0.5)
        return False

    def wait_for_text(self, text: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.text_present(text):
                return True
            time.sleep(0.5)
        return False

    # ── Row-scoped element resolution (Workstream 4) ────────────────────────
    #
    # A RecyclerView row (a task row `taskItemCard`, a chore row
    # `choresItemCard`) reuses the SAME descendant resource ids on every row --
    # `flCheckboxTarget`, `tvActionMenu`, `ivIcon`, `tvName`/`tvTitle`. Tapping
    # the first global `flCheckboxTarget` would toggle whatever row happens to be
    # bound first, not the fixture row under test. These helpers resolve a
    # descendant control *within the one row whose visible title matches*, and
    # fail loudly on zero or multiple matching rows rather than silently acting
    # on the wrong one.

    @staticmethod
    def _xpath_text_literal(text: str) -> str:
        """An XPath string literal for `text`, safe even when it contains
        quotes (via concat()). REG-* fixture titles never do, but a chore/task
        title supplied at runtime might."""
        if '"' not in text:
            return f'"{text}"'
        if "'" not in text:
            return f"'{text}'"
        parts = text.split('"')
        return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"

    def find_rows_by_title(self, card_id: str, title: str, title_id: "str | None" = None) -> list:
        """Every row card (`card_id`) whose title matches `title` by EXACT text
        equality (Priority 4) -- not substring. A substring match would let
        ``REG-TASK-OPEN-1`` also select ``REG-TASK-OPEN-10``, mutating the wrong
        fixture row.

        When ``title_id`` is given (``tvName`` for task rows, ``tvTitle`` for
        chore rows) the exact match is required on THAT title control, so a task
        whose description happens to equal another task's name cannot collide.
        Matches ``@text`` or ``@content-desc`` (Android exposes the visible label
        as either)."""
        from appium.webdriver.common.appiumby import AppiumBy

        lit = self._xpath_text_literal(title)
        row_id = self._resource_id(card_id)
        if title_id:
            title_rid = self._resource_id(title_id)
            title_pred = (
                f".//*[@resource-id='{title_rid}' and "
                f"(@text={lit} or @content-desc={lit})]"
            )
        else:
            title_pred = f".//*[@text={lit} or @content-desc={lit}]"
        xpath = f"//*[@resource-id='{row_id}'][{title_pred}]"
        return self.driver.find_elements(AppiumBy.XPATH, xpath)

    def _find_row_descendants(self, row, descendant_id: str) -> list:
        from appium.webdriver.common.appiumby import AppiumBy

        target_id = self._resource_id(descendant_id)
        # Relative XPath (leading dot) -> search only within this row's subtree.
        return row.find_elements(AppiumBy.XPATH, f".//*[@resource-id='{target_id}']")

    def _page_fingerprint(self) -> "str | None":
        """A cheap fingerprint of the current screen, used to detect a scroll
        that revealed nothing new (Priority 5.5). Length+hash of the page source;
        None if it can't be read (then progress can't be disproven, so the caller
        does not treat it as "no progress")."""
        import hashlib

        try:
            source = self.driver.page_source or ""
        except Exception:
            return None
        return f"{len(source)}:{hashlib.md5(source.encode('utf-8', 'replace')).hexdigest()}"

    def _swipe(self, direction: str = "down") -> None:
        """Best-effort RecyclerView scroll in ``direction`` (Priority 5.4):

          * ``"down"`` -- reveal rows BELOW the fold (finger swipes up);
          * ``"up"``   -- reveal rows ABOVE the fold (finger swipes down).

        Uses the W3C swipe API, falling back to UiAutomator2's ``mobile:
        scrollGesture``. Any failure is swallowed (a scroll that changed nothing
        is detected separately via the page fingerprint)."""
        try:
            size = self.driver.get_window_size()
            width, height = size["width"], size["height"]
        except Exception:
            width, height = 1000, 1600
        start_x = int(width * 0.5)
        if direction == "up":
            start_y, end_y = int(height * 0.25), int(height * 0.75)
            gesture_dir = "up"
        else:
            start_y, end_y = int(height * 0.75), int(height * 0.25)
            gesture_dir = "down"
        try:
            self.driver.swipe(start_x, start_y, start_x, end_y, 400)
        except Exception:
            try:
                top = min(start_y, end_y)
                self.driver.execute_script(
                    "mobile: scrollGesture",
                    {"left": start_x - 5, "top": top, "width": 10,
                     "height": abs(start_y - end_y), "direction": gesture_dir, "percent": 0.8},
                )
            except Exception:
                pass

    def _swipe_up_once(self) -> None:
        """Backwards-compatible alias: scroll to reveal rows below the fold."""
        self._swipe("down")

    @staticmethod
    def _is_stale_exception(exc: Exception) -> bool:
        """True when ``exc`` is a stale-element condition (the element was valid
        when resolved but its node was recycled before use). Matched by type name
        so it works whether Selenium's StaleElementReferenceException is raised or
        a UiAutomator2 'stale' driver error surfaces as a generic exception."""
        name = type(exc).__name__.lower()
        return "stale" in name or "stale" in str(exc).lower()

    def _capture_diagnostics(self, label: str) -> "tuple[str | None, str | None]":
        """On a final failure, capture a screenshot + page source so a human can
        see what the tablet actually showed. Best-effort: a capture failure must
        never mask the underlying resolution error."""
        import tempfile

        base = self.diagnostics_dir
        try:
            if base:
                Path(base).mkdir(parents=True, exist_ok=True)
                out_dir = Path(base)
            else:
                out_dir = Path(tempfile.mkdtemp(prefix="row-diag-"))
        except Exception:
            return None, None
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in label)[:80]
        shot = out_dir / f"{safe}.png"
        source = out_dir / f"{safe}.xml"
        shot_path = source_path = None
        try:
            self.driver.get_screenshot_as_file(str(shot))
            shot_path = str(shot)
        except Exception:
            shot_path = None
        try:
            source.write_text(self.driver.page_source or "", encoding="utf-8")
            source_path = str(source)
        except Exception:
            source_path = None
        return shot_path, source_path

    def resolve_row_target(
        self, card_id: str, title: str, descendant_id: str, *,
        title_id: "str | None" = None,
        max_attempts: "int | None" = None,
        retry_interval: "float | None" = None,
        max_swipes: "int | None" = None,
    ) -> RowResolution:
        """Resolve the ONE descendant `descendant_id` within the row whose exact
        `title` (on `title_id`) matches, robust to a live RecyclerView (Priority 4):

          * EXACT title equality (never substring);
          * bounded retry for a TEMPORARY zero rows (a mid-rebind empty query);
          * bounded retry for a TEMPORARY missing descendant (row bound before
            its children);
          * stale-element retry;
          * RecyclerView scroll-to-row for an off-screen fixture row;
          * IMMEDIATE failure (no retry) for duplicate exact-title rows or
            duplicate descendants -- a permanent ambiguity;
          * diagnostic screenshot + page source on final failure;
          * attempt count, scroll count and elapsed duration in the result.
        """
        max_attempts = self.row_max_attempts if max_attempts is None else max_attempts
        retry_interval = self.row_retry_interval if retry_interval is None else retry_interval
        max_swipes = self.row_max_swipes if max_swipes is None else max_swipes

        start = time.monotonic()
        attempts = 0
        scrolls = 0
        last_problem = None
        last_matched_rows = 0
        # Bidirectional scroll state (Priority 5.4-5.5): scan down first, switch
        # to up when a scroll reveals nothing new, and stop scrolling once BOTH
        # directions make no progress (the list can't move any further).
        scroll_dir = "down"
        scroll_directions: "list[str]" = []
        stuck_dirs: "set[str]" = set()
        scroll_exhausted = False
        for attempt in range(1, max(1, max_attempts) + 1):
            attempts = attempt
            try:
                rows = self.find_rows_by_title(card_id, title, title_id)
                if len(rows) > 1:
                    # Permanent ambiguity -- surface immediately, never act on one.
                    shot, src = self._capture_diagnostics(f"ambiguous-rows-{title}")
                    raise RowAmbiguityError(
                        f"{len(rows)} {card_id!r} rows match title {title!r} exactly"
                        + (f" (via {title_id!r})" if title_id else "")
                        + " -- ambiguous. Row-scoped actions require exactly one match; use a unique "
                        "fixture title (e.g. a REG-* id).",
                        RowResolution(
                            attempts=attempts, scrolls=scrolls, matched_rows=len(rows),
                            elapsed_seconds=time.monotonic() - start,
                            scroll_directions=list(scroll_directions),
                            screenshot_path=shot, page_source_path=src,
                            problem=f"{len(rows)} rows match title {title!r} exactly",
                        ),
                    )
                if len(rows) == 1:
                    last_matched_rows = 1
                    descendants = self._find_row_descendants(rows[0], descendant_id)
                    if len(descendants) > 1:
                        shot, src = self._capture_diagnostics(f"ambiguous-descendant-{title}")
                        raise RowAmbiguityError(
                            f"Row {title!r} ({card_id!r}) has {len(descendants)} descendants "
                            f"{descendant_id!r} -- ambiguous.",
                            RowResolution(
                                attempts=attempts, scrolls=scrolls, matched_rows=1,
                                elapsed_seconds=time.monotonic() - start,
                                scroll_directions=list(scroll_directions),
                                screenshot_path=shot, page_source_path=src,
                                problem=f"{len(descendants)} descendants {descendant_id!r} in row {title!r}",
                            ),
                        )
                    if len(descendants) == 1:
                        return RowResolution(
                            element=descendants[0], attempts=attempts, scrolls=scrolls,
                            elapsed_seconds=time.monotonic() - start, matched_rows=1,
                            scroll_directions=list(scroll_directions),
                            scroll_exhausted=scroll_exhausted,
                        )
                    last_problem = f"row {title!r} present but descendant {descendant_id!r} not bound yet"
                else:
                    last_matched_rows = 0
                    last_problem = f"no {card_id!r} row with exact title {title!r} visible yet"
            except RowAmbiguityError:
                raise
            except Exception as exc:  # StaleElementReference et al. mid-rebind
                last_problem = f"transient driver error: {exc}"

            # Not resolved this attempt: scroll a row into view (bounded), with
            # no-progress detection so we don't keep swiping a list that can't
            # move, and switch direction to scan the other way.
            if scrolls < max_swipes and not scroll_exhausted:
                before = self._page_fingerprint()
                self._swipe(scroll_dir)
                scrolls += 1
                scroll_directions.append(scroll_dir)
                after = self._page_fingerprint()
                made_progress = before is None or after is None or before != after
                if made_progress:
                    stuck_dirs.discard(scroll_dir)
                else:
                    # This direction revealed nothing; remember it and look the
                    # other way. When BOTH directions are stuck, stop scrolling.
                    stuck_dirs.add(scroll_dir)
                    scroll_dir = "up" if scroll_dir == "down" else "down"
                    if {"down", "up"} <= stuck_dirs:
                        scroll_exhausted = True
            if attempt < max_attempts and retry_interval:
                time.sleep(retry_interval)

        # Exhausted the budget: capture diagnostics and fail with metrics.
        shot, src = self._capture_diagnostics(f"unresolved-{card_id}-{title}")
        if scroll_exhausted and last_matched_rows == 0:
            last_problem = (last_problem or "") + " (scroll exhausted: the list could not be scrolled further in either direction)"
        resolution = RowResolution(
            element=None, attempts=attempts, scrolls=scrolls,
            elapsed_seconds=time.monotonic() - start, matched_rows=last_matched_rows,
            screenshot_path=shot, page_source_path=src, problem=last_problem,
            scroll_directions=list(scroll_directions), scroll_exhausted=scroll_exhausted,
        )
        raise RowResolutionError(
            f"Could not resolve {descendant_id!r} in the {title!r} {card_id!r} row after "
            f"{attempts} attempt(s) and {scrolls} scroll(s): {last_problem}.",
            resolution,
        )

    def tap_in_row(
        self, card_id: str, title: str, descendant_id: str, *, title_id: "str | None" = None,
        max_click_retries: "int | None" = None,
    ) -> RowResolution:
        """Resolve the row's descendant and click it, retrying a stale-at-click
        (Priority 5.1-5.3): if the element is recycled between resolution and
        ``.click()``, RE-RESOLVE the exact row from scratch and click the FRESH
        element -- a previously resolved stale element is never re-clicked.
        """
        retries = self.row_click_max_retries if max_click_retries is None else max_click_retries
        retries = max(1, retries)
        last_exc: "Exception | None" = None
        saw_stale = False
        for click_attempt in range(1, retries + 1):
            resolution = self.resolve_row_target(card_id, title, descendant_id, title_id=title_id)
            resolution.click_attempts = click_attempt
            # Carry forward that an earlier click went stale, so the returned
            # (successful) resolution still records the retry happened.
            resolution.stale_at_click = saw_stale
            try:
                resolution.element.click()
                return resolution
            except Exception as exc:  # noqa: BLE001
                if not self._is_stale_exception(exc):
                    raise
                # Stale between resolve and click: discard this element, loop to
                # RE-RESOLVE. Never click the stale element again.
                last_exc = exc
                saw_stale = True
                resolution.stale_at_click = True
                if click_attempt < retries and self.row_retry_interval:
                    time.sleep(self.row_retry_interval)

        # Every re-resolve produced an element that went stale before the click
        # landed: capture diagnostics and fail with the stale-at-click metric.
        shot, src = self._capture_diagnostics(f"stale-at-click-{card_id}-{title}")
        resolution = RowResolution(
            element=None, attempts=retries, matched_rows=1, stale_at_click=True,
            click_attempts=retries, screenshot_path=shot, page_source_path=src,
            problem=f"element went stale at click after {retries} re-resolve(s): {last_exc}",
        )
        raise RowResolutionError(
            f"Row {title!r} ({card_id!r}) descendant {descendant_id!r} went stale at click "
            f"after {retries} re-resolve(s): {last_exc}.",
            resolution,
        )

    def id_present_in_row(
        self, card_id: str, title: str, descendant_id: str, *, title_id: "str | None" = None,
    ) -> bool:
        """Presence of a descendant within the unique exact-title row. A missing
        or ambiguous ROW still raises (that must not be swallowed); a present row
        whose descendant never binds returns False after the bounded retry."""
        try:
            self.resolve_row_target(card_id, title, descendant_id, title_id=title_id)
            return True
        except RowAmbiguityError:
            raise
        except RowResolutionError as exc:
            res = exc.resolution
            # A resolved-but-descendant-absent outcome is a legitimate False;
            # a never-found ROW is a real error the caller must see.
            if res is not None and res.problem and "descendant" in res.problem:
                return False
            raise
