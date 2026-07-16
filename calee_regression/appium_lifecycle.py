"""Automatic Appium server lifecycle management (Workstream 8).

Removes the requirement for a non-technical tester to open a separate
Terminal window and run `appium` manually. `ensure_appium_running`:

1. Checks whether the configured Appium endpoint is already healthy.
2. If not, starts Appium in the background with the required flags.
3. Writes its output to a log file (reports/appium.log).
4. Waits for readiness with a bounded timeout.
5. Raises AppiumLifecycleError (a plain-language message) if it can't
   become healthy -- the caller maps this to BLOCKED.

The PID of a process WE started is recorded in a pid file. `stop_appium`
only ever stops a PID it finds recorded there, and only if it still
matches what we started -- it never touches an Appium process that was
already running before we looked (started_by_us=False), and never kills a
PID just because "something is listening on the port".
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARGS = ["--base-path", "/wd/hub", "--allow-insecure", "uiautomator2:adb_shell"]


class AppiumLifecycleError(Exception):
    pass


@dataclass
class AppiumHandle:
    started_by_us: bool
    pid: "int | None"
    pid_file: "Path | None"
    log_path: "Path | None"
    base_url: str


def is_appium_healthy(base_url: str, timeout_seconds: float = 5) -> bool:
    url = base_url.rstrip("/") + "/status"
    try:
        urllib.request.urlopen(url, timeout=timeout_seconds)
        return True
    except Exception:
        return False


def find_appium_executable(which=None) -> "str | None":
    import shutil

    which = which or shutil.which
    return which("appium")


def start_appium(
    *,
    base_url: str,
    log_path: Path,
    pid_file: Path,
    extra_args=None,
    ready_timeout_seconds: float = 60,
    poll_interval_seconds: float = 1.0,
    popen=subprocess.Popen,
    is_healthy=None,
    which=None,
) -> AppiumHandle:
    """Starts `appium` in the background and waits for /status to answer.

    Raises AppiumLifecycleError with a plain-language message on any
    failure (missing executable, immediate exit, readiness timeout) so
    the caller can report BLOCKED without guessing why.

    `is_healthy` defaults to None (resolved to is_appium_healthy inside
    the function body, not as a bound default value) so that patching the
    module-level is_appium_healthy -- e.g. in tests -- still takes effect;
    a `is_healthy=is_appium_healthy` default would bind the function
    object at definition time and ignore a later monkeypatch.
    """
    is_healthy = is_healthy or is_appium_healthy
    executable = find_appium_executable(which)
    if executable is None:
        raise AppiumLifecycleError(
            "The 'appium' command was not found. Ask your technical owner to install Appium "
            "(npm install -g appium) or check that it's on PATH."
        )

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    args = list(extra_args) if extra_args is not None else list(DEFAULT_ARGS)

    log_file = log_path.open("a", encoding="utf-8")
    try:
        proc = popen([executable, *args], stdout=log_file, stderr=subprocess.STDOUT)
    except OSError as exc:
        raise AppiumLifecycleError(f"Could not start Appium ({executable}): {exc}") from exc

    pid_file = Path(pid_file)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid), encoding="utf-8")

    deadline = time.monotonic() + ready_timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pid_file.unlink(missing_ok=True)
            raise AppiumLifecycleError(
                f"Appium exited immediately (exit code {proc.returncode}) -- see {log_path} for details."
            )
        if is_healthy(base_url, timeout_seconds=2):
            return AppiumHandle(
                started_by_us=True, pid=proc.pid, pid_file=pid_file, log_path=log_path, base_url=base_url,
            )
        time.sleep(poll_interval_seconds)

    # Timed out -- stop what we started rather than leaving an orphaned,
    # never-ready process running in the background.
    try:
        proc.terminate()
    except Exception:
        pass
    pid_file.unlink(missing_ok=True)
    raise AppiumLifecycleError(
        f"Appium did not become ready within {ready_timeout_seconds}s -- see {log_path} for details."
    )


def ensure_appium_running(
    *,
    base_url: str,
    log_path: Path,
    pid_file: Path,
    ready_timeout_seconds: float = 60,
    is_healthy=None,
    **start_kwargs,
) -> AppiumHandle:
    """If base_url already answers, use it as-is (started_by_us=False --
    nothing to clean up later, and this NEVER touches whatever process is
    already serving it). Otherwise starts a fresh Appium and waits for it.

    See start_appium's docstring for why `is_healthy` defaults to None
    rather than `is_appium_healthy` directly.
    """
    is_healthy = is_healthy or is_appium_healthy
    if is_healthy(base_url):
        return AppiumHandle(started_by_us=False, pid=None, pid_file=None, log_path=None, base_url=base_url)
    return start_appium(
        base_url=base_url, log_path=log_path, pid_file=pid_file,
        ready_timeout_seconds=ready_timeout_seconds, is_healthy=is_healthy, **start_kwargs,
    )


def stop_appium(handle: AppiumHandle, *, kill=None) -> None:
    """Stops only the PID recorded in handle.pid_file, and only if it
    still matches what we started -- never an unrelated Appium process."""
    if not handle.started_by_us or handle.pid is None or handle.pid_file is None:
        return
    pid_file = Path(handle.pid_file)
    if not pid_file.is_file():
        return
    try:
        recorded_pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    if recorded_pid != handle.pid:
        # The pid file was overwritten/changed out from under us -- don't
        # guess, don't kill a possibly-unrelated process.
        return

    kill = kill or os.kill
    try:
        kill(handle.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        pass
    finally:
        pid_file.unlink(missing_ok=True)


def stop_appium_from_pid_file(pid_file: Path, *, kill=None) -> bool:
    """Stateless variant of stop_appium for a separate process invocation
    (e.g. a later shell script step) that has no in-memory AppiumHandle --
    it only knows the pid file path. Returns True if it stopped a process,
    False if there was nothing recorded (e.g. Appium was already running
    before this session touched it, so nothing was ever started by us).
    """
    pid_file = Path(pid_file)
    if not pid_file.is_file():
        return False
    try:
        recorded_pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False

    kill = kill or os.kill
    try:
        kill(recorded_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        pass
    finally:
        pid_file.unlink(missing_ok=True)
    return True
