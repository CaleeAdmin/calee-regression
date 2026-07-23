"""Bounded child-process supervision (this session's Workstream 9): normal
completion, timeout -> graceful termination, force kill, spawn failure and
KeyboardInterrupt -- all with fake processes, no real subprocess, no sleeps."""

from __future__ import annotations

import signal
import subprocess

import pytest

from calee_regression import focused_supervision as sup


class FakeProc:
    """A fake Popen: `waits` scripts successive wait() outcomes -- an int
    exits, None raises TimeoutExpired."""

    def __init__(self, waits):
        self.pid = 4242
        self.stdout = iter(["line-1\n", "line-2\n"])
        self._waits = list(waits)
        self.wait_calls = []

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        result = self._waits.pop(0)
        if result is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return result


def _clock():
    times = iter([0.0, 12.5, 13.0, 14.0, 15.0, 16.0])
    return lambda: next(times)


def test_normal_completion_records_exit_and_log_tail():
    proc = FakeProc(waits=[0])
    signals = []
    outcome = sup.run_supervised(
        ["cmd"], timeout_seconds=60, popen_factory=lambda c, e, w: proc,
        signal_group=lambda p, s: signals.append(s), monotonic=_clock(),
    )
    assert outcome.exit_code == 0
    assert not outcome.timed_out and not outcome.terminated and not outcome.force_killed
    assert signals == []
    assert outcome.log_tail == ["line-1", "line-2"]
    assert outcome.elapsed_seconds > 0
    assert any("reaped" in a for a in outcome.actions)


def test_timeout_sends_sigterm_then_reaps_within_grace():
    proc = FakeProc(waits=[None, 143])  # deadline expires, then exits on SIGTERM
    signals = []
    outcome = sup.run_supervised(
        ["cmd"], timeout_seconds=5, grace_seconds=2,
        popen_factory=lambda c, e, w: proc,
        signal_group=lambda p, s: signals.append(s), monotonic=_clock(),
    )
    assert outcome.timed_out and outcome.terminated and not outcome.force_killed
    assert signals == [signal.SIGTERM]
    assert outcome.exit_code == 143
    assert any("SIGTERM" in a for a in outcome.actions)


def test_still_running_after_grace_is_force_killed_and_always_reaped():
    proc = FakeProc(waits=[None, None, -9])  # survives SIGTERM grace, dies on SIGKILL
    signals = []
    outcome = sup.run_supervised(
        ["cmd"], timeout_seconds=5, grace_seconds=2,
        popen_factory=lambda c, e, w: proc,
        signal_group=lambda p, s: signals.append(s), monotonic=_clock(),
    )
    assert outcome.timed_out and outcome.terminated and outcome.force_killed
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert outcome.exit_code == -9
    assert proc.wait_calls[-1] is None  # the final reap is unconditional


def test_spawn_failure_is_structured_not_raised():
    def failing_factory(c, e, w):
        raise OSError("no such binary")

    outcome = sup.run_supervised(
        ["cmd"], timeout_seconds=5, popen_factory=failing_factory, monotonic=_clock(),
    )
    assert outcome.exit_code is None
    assert any("spawn-failed" in a for a in outcome.actions)


def test_keyboard_interrupt_terminates_child_then_propagates():
    class InterruptingProc(FakeProc):
        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if len(self.wait_calls) == 1:
                raise KeyboardInterrupt()
            return 130

    proc = InterruptingProc(waits=[])
    signals = []
    with pytest.raises(KeyboardInterrupt):
        sup.run_supervised(
            ["cmd"], timeout_seconds=5, grace_seconds=1,
            popen_factory=lambda c, e, w: proc,
            signal_group=lambda p, s: signals.append(s), monotonic=_clock(),
        )
    # The child was terminated and reaped BEFORE the interrupt propagated --
    # no orphan survives an interrupted orchestration.
    assert signal.SIGTERM in signals
    assert len(proc.wait_calls) >= 2
