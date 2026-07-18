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
    retried -- acting on any one of them could mutate the wrong fixture."""


class RowResolutionError(LookupError):
    """The row (or its descendant) could not be resolved within the bounded
    retry/scroll budget: a temporary zero-row / missing-descendant / stale-element
    condition that never settled, or the row is simply not present. Carries the
    diagnostic capture (screenshot + page source) and attempt/scroll metrics."""

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

    def to_dict(self) -> dict:
        return {
            "attempts": self.attempts,
            "scrolls": self.scrolls,
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

    def _swipe_up_once(self) -> None:
        """Best-effort RecyclerView scroll: swipe up so a row below the fold
        comes into view. Uses the W3C touch API; any failure is swallowed (the
        resolver treats a scroll that changed nothing as simply "no new rows").
        """
        try:
            size = self.driver.get_window_size()
            width, height = size["width"], size["height"]
        except Exception:
            width, height = 1000, 1600
        start_x = int(width * 0.5)
        start_y = int(height * 0.75)
        end_y = int(height * 0.25)
        try:
            # Prefer UiAutomator2's mobile scroll gesture; fall back to swipe.
            self.driver.swipe(start_x, start_y, start_x, end_y, 400)
        except Exception:
            try:
                self.driver.execute_script(
                    "mobile: scrollGesture",
                    {"left": start_x - 5, "top": end_y, "width": 10,
                     "height": start_y - end_y, "direction": "down", "percent": 0.8},
                )
            except Exception:
                pass

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
                        "fixture title (e.g. a REG-* id)."
                    )
                if len(rows) == 1:
                    descendants = self._find_row_descendants(rows[0], descendant_id)
                    if len(descendants) > 1:
                        self._capture_diagnostics(f"ambiguous-descendant-{title}")
                        raise RowAmbiguityError(
                            f"Row {title!r} ({card_id!r}) has {len(descendants)} descendants "
                            f"{descendant_id!r} -- ambiguous."
                        )
                    if len(descendants) == 1:
                        return RowResolution(
                            element=descendants[0], attempts=attempts, scrolls=scrolls,
                            elapsed_seconds=time.monotonic() - start, matched_rows=1,
                        )
                    last_problem = f"row {title!r} present but descendant {descendant_id!r} not bound yet"
                else:
                    last_problem = f"no {card_id!r} row with exact title {title!r} visible yet"
            except RowAmbiguityError:
                raise
            except Exception as exc:  # StaleElementReference et al. mid-rebind
                last_problem = f"transient driver error: {exc}"

            # Not resolved this attempt: scroll a row into view (bounded), then
            # wait for the rebind to settle before retrying.
            if scrolls < max_swipes:
                self._swipe_up_once()
                scrolls += 1
            if attempt < max_attempts and retry_interval:
                time.sleep(retry_interval)

        # Exhausted the budget: capture diagnostics and fail with metrics.
        shot, src = self._capture_diagnostics(f"unresolved-{card_id}-{title}")
        resolution = RowResolution(
            element=None, attempts=attempts, scrolls=scrolls,
            elapsed_seconds=time.monotonic() - start, matched_rows=0,
            screenshot_path=shot, page_source_path=src, problem=last_problem,
        )
        raise RowResolutionError(
            f"Could not resolve {descendant_id!r} in the {title!r} {card_id!r} row after "
            f"{attempts} attempt(s) and {scrolls} scroll(s): {last_problem}.",
            resolution,
        )

    def tap_in_row(
        self, card_id: str, title: str, descendant_id: str, *, title_id: "str | None" = None,
    ) -> RowResolution:
        resolution = self.resolve_row_target(card_id, title, descendant_id, title_id=title_id)
        resolution.element.click()
        return resolution

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
