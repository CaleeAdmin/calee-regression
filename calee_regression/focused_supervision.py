"""Bounded child-process supervision for focused-verify (this session's
Workstream 9).

Replaces the previous unbounded ``subprocess.call``: every focused child runs
under a per-step deadline in its OWN process group, and on timeout receives a
graceful SIGTERM, a bounded grace period, then SIGKILL only if it is still
running. The child is ALWAYS reaped; the elapsed time and every termination
action are recorded; a bounded tail of the child's combined output is kept as
an orchestration log (redacted by the caller before it is written anywhere).

Everything is injectable (popen factory, group-signal function, clock) so
normal completion, timeout, graceful termination, force kill and
KeyboardInterrupt are all unit-testable with fakes -- no real processes, no
sleeps.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class SupervisedOutcome:
    """What actually happened to one supervised child."""

    exit_code: "int | None"
    timed_out: bool = False
    terminated: bool = False
    force_killed: bool = False
    interrupted: bool = False
    elapsed_seconds: float = 0.0
    actions: "list[str]" = field(default_factory=list)
    log_tail: "list[str]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "exitCode": self.exit_code,
            "timedOut": self.timed_out,
            "terminated": self.terminated,
            "forceKilled": self.force_killed,
            "interrupted": self.interrupted,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "actions": list(self.actions),
        }


def _real_popen(command, env, cwd):
    return subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        # A dedicated process group/session so a timeout can terminate the
        # whole child tree (flutter/appium wrappers included), never just the
        # immediate child, and no shell job is left suspended or orphaned.
        start_new_session=True,
    )


def _real_signal_group(proc, sig) -> None:
    """Signal the child's whole process group; falls back to the child alone
    when the group is already gone."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def run_supervised(
    command,
    *,
    env=None,
    cwd=None,
    timeout_seconds: float,
    grace_seconds: float = 15.0,
    max_log_lines: int = 200,
    popen_factory=_real_popen,
    signal_group=_real_signal_group,
    monotonic=time.monotonic,
) -> SupervisedOutcome:
    """Run one child to completion under a deadline. Never raises for a child
    failure; ``KeyboardInterrupt`` (and any other unexpected interruption)
    still terminates + reaps the child before propagating, so no orphan
    survives an interrupted orchestration."""
    started = monotonic()
    outcome = SupervisedOutcome(exit_code=None)
    tail: "deque[str]" = deque(maxlen=max_log_lines)

    try:
        proc = popen_factory(command, env, cwd)
    except OSError as exc:
        outcome.actions.append(f"spawn-failed: {exc}")
        outcome.elapsed_seconds = monotonic() - started
        return outcome
    outcome.actions.append("started")

    reader = None
    if getattr(proc, "stdout", None) is not None:
        def _drain():
            try:
                for line in proc.stdout:
                    tail.append(line.rstrip("\n"))
            except (ValueError, OSError):
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

    def _reap(timeout: "float | None") -> "int | None":
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    try:
        code = _reap(timeout_seconds)
        if code is None:
            outcome.timed_out = True
            outcome.actions.append(f"timeout after {timeout_seconds}s: sent SIGTERM to process group")
            signal_group(proc, signal.SIGTERM)
            outcome.terminated = True
            code = _reap(grace_seconds)
            if code is None:
                outcome.actions.append(f"still running after {grace_seconds}s grace: sent SIGKILL to process group")
                signal_group(proc, signal.SIGKILL)
                outcome.force_killed = True
                code = _reap(None)  # SIGKILL cannot be ignored; always reaps
            else:
                outcome.actions.append("exited within the grace period")
        outcome.exit_code = code
    except BaseException:
        # KeyboardInterrupt / SystemExit / termination signals: clean up the
        # child before propagating so framework cleanup still runs and no
        # process survives orphaned.
        outcome.interrupted = True
        outcome.actions.append("orchestration interrupted: sent SIGTERM then SIGKILL to process group")
        signal_group(proc, signal.SIGTERM)
        if _reap(grace_seconds) is None:
            signal_group(proc, signal.SIGKILL)
            _reap(None)
        raise
    finally:
        if reader is not None:
            reader.join(timeout=5)
        outcome.elapsed_seconds = monotonic() - started
        outcome.log_tail = list(tail)

    outcome.actions.append(f"reaped (exit {outcome.exit_code})")
    return outcome
