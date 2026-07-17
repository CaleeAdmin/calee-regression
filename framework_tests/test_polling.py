"""Tests for calee_regression/polling.py -- the bounded-polling primitive
Workstream 11's sync-smoke flows build on. Uses an injectable fake
clock/sleep so every test runs instantly and deterministically instead of
actually waiting in real time.
"""

from __future__ import annotations

import pytest

from calee_regression.polling import PollResult, poll_until


class _FakeClock:
    """A clock that advances by `interval_seconds` every time `sleep` is
    called, and not otherwise -- lets tests assert exact attempt counts
    without any real waiting."""

    def __init__(self):
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_succeeds_on_first_check():
    fake = _FakeClock()
    result = poll_until(lambda: True, timeout_seconds=10, interval_seconds=1, clock=fake.clock, sleep=fake.sleep)

    assert result.succeeded is True
    assert result.attempts == 1
    assert result.last_observed is True


def test_succeeds_after_several_failed_checks():
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] >= 3

    fake = _FakeClock()
    result = poll_until(check, timeout_seconds=10, interval_seconds=1, clock=fake.clock, sleep=fake.sleep)

    assert result.succeeded is True
    assert result.attempts == 3


def test_times_out_when_condition_never_becomes_true():
    fake = _FakeClock()
    result = poll_until(lambda: False, timeout_seconds=5, interval_seconds=1, clock=fake.clock, sleep=fake.sleep)

    assert result.succeeded is False
    assert result.attempts >= 1
    assert result.elapsed_seconds >= 5


def test_exception_in_check_is_recorded_as_last_error_and_polling_continues():
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient network blip")
        return True

    fake = _FakeClock()
    result = poll_until(check, timeout_seconds=10, interval_seconds=1, clock=fake.clock, sleep=fake.sleep)

    assert result.succeeded is True
    assert result.attempts == 3


def test_exception_on_every_attempt_times_out_with_last_error_set():
    fake = _FakeClock()
    result = poll_until(
        lambda: (_ for _ in ()).throw(RuntimeError("always fails")),
        timeout_seconds=3, interval_seconds=1, clock=fake.clock, sleep=fake.sleep,
    )

    assert result.succeeded is False
    assert result.last_error == "always fails"
    assert result.last_observed is None


def test_custom_is_success_predicate_for_non_bool_observations():
    fake = _FakeClock()
    result = poll_until(
        lambda: {"found": True, "status": "ready"},
        timeout_seconds=10, interval_seconds=1,
        is_success=lambda observed: observed.get("status") == "ready",
        clock=fake.clock, sleep=fake.sleep,
    )

    assert result.succeeded is True
    assert result.last_observed == {"found": True, "status": "ready"}


def test_negative_is_success_short_circuits_before_timeout_is_reached_if_check_eventually_true():
    # Regression guard: a falsy-but-not-exactly-False observation (e.g. an
    # empty dict on the way to a populated one) must not be misread as success.
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return {} if calls["n"] < 2 else {"ok": True}

    fake = _FakeClock()
    result = poll_until(
        check, timeout_seconds=10, interval_seconds=1,
        is_success=lambda observed: bool(observed),
        clock=fake.clock, sleep=fake.sleep,
    )

    assert result.succeeded is True
    assert result.attempts == 2


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_rejects_non_positive_timeout(bad_timeout):
    with pytest.raises(ValueError):
        poll_until(lambda: True, timeout_seconds=bad_timeout, interval_seconds=1)


@pytest.mark.parametrize("bad_interval", [0, -1])
def test_rejects_non_positive_interval(bad_interval):
    with pytest.raises(ValueError):
        poll_until(lambda: True, timeout_seconds=10, interval_seconds=bad_interval)


def test_to_dict_shape():
    result = PollResult(succeeded=True, attempts=2, elapsed_seconds=1.23456, last_observed="x", last_error=None)
    d = result.to_dict()

    assert d == {
        "succeeded": True,
        "attempts": 2,
        "elapsedSeconds": 1.235,
        "lastObserved": "x",
        "lastError": None,
    }


def test_real_clock_and_sleep_are_used_by_default_and_actually_pass_quickly():
    # No injected clock/sleep -- exercises the real time.monotonic/time.sleep
    # path (kept tiny so the suite stays fast).
    result = poll_until(lambda: True, timeout_seconds=1, interval_seconds=0.01)
    assert result.succeeded is True
    assert result.attempts == 1
