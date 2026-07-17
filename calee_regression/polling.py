"""Bounded polling for cross-device sync verification (Workstream 11).

Never an arbitrary sleep-and-hope pattern -- every poll has an explicit
timeout and interval, and records exactly how many attempts it took (or
exhausted) and what was last observed, so a sync-flow failure report can
show real evidence instead of "it didn't work". See sync_smoke.py and
docs/RELEASE_POLICY.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class PollResult:
    succeeded: bool
    attempts: int
    elapsed_seconds: float
    last_observed: "object | None" = None
    last_error: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "succeeded": self.succeeded,
            "attempts": self.attempts,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "lastObserved": self.last_observed,
            "lastError": self.last_error,
        }


def poll_until(
    check,
    *,
    timeout_seconds: float,
    interval_seconds: float = 1.0,
    is_success=None,
    clock=None,
    sleep=None,
) -> PollResult:
    """Calls `check()` repeatedly until it signals success or `timeout_seconds` elapses.

    `check()` may return any observation (a bool, a dict, a parsed API
    response, ...); by default a truthy return counts as success, or pass
    `is_success` to interpret a non-bool observation. `check()` may also
    raise -- that counts as a failed attempt (recorded in `last_error`,
    the poll keeps going), not a hard error, so one transient network
    hiccup doesn't abort the whole poll.

    `clock`/`sleep` are injectable purely so unit tests can run this
    deterministically and instantly (see test_polling.py) -- real callers
    never need to pass them.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    is_success = is_success or (lambda observed: bool(observed))

    started = clock()
    deadline = started + timeout_seconds
    attempts = 0
    last_observed = None
    last_error = None

    while True:
        attempts += 1
        try:
            last_observed = check()
            last_error = None
            if is_success(last_observed):
                return PollResult(
                    succeeded=True,
                    attempts=attempts,
                    elapsed_seconds=clock() - started,
                    last_observed=last_observed,
                )
        except Exception as exc:
            last_error = str(exc)

        if clock() >= deadline:
            return PollResult(
                succeeded=False,
                attempts=attempts,
                elapsed_seconds=clock() - started,
                last_observed=last_observed,
                last_error=last_error,
            )
        sleep(interval_seconds)
