"""Today-relative subscribed-calendar fixture generation (Priority 7).

The regression-owned subscribed feed used to be a FIXED-DATE static ICS
(fixtures/subscribed_calendar/reg_sub_calendar.ics, pinned to 2026-08), so any
tablet assertion tied to the device's "Today" was only ever correct on the one
day the fixture's date matched the run date. This module produces the same
REG-SUB shapes RELATIVE to a resolved target date, so a future authenticated
provisioning step can load a fixture whose events genuinely fall on the run's
"Today" -- date-correct on ANY execution date, never a hardcoded calendar day.

It is a pure, dependency-free generator (validated by round-tripping through
ics_contract.expand), so it is fully offline-testable and provisions nothing by
itself. Actual provisioning still needs the authenticated regression
subscription-source endpoint (unavailable here, and never an unauthenticated
production reset), so the subscribed scenario stays BLOCKED/draft-unverified;
this only makes its date contract correct for when it does run physically.

Design points the caller relies on:

  * **Resolve the target date ONCE per run** (``resolve_target_date``) and thread
    it everywhere, so every generated event and every recorded expectation agree
    on the same day.
  * **Timezone-safe.** All-day events are date-only (no timezone, no possible
    off-by-one shift). The timed event uses a FLOATING LOCAL midday time (no
    ``Z``), so its visible day equals the target date in ANY timezone -- never a
    UTC-vs-local day boundary surprise (the CaleeMobile app's own tests run in
    Australia/Perth, UTC+8).
  * **Unique event names.** Each run's events embed a run/fixture token, so a
    run's subscribed events are identifiable and never collide with a stale
    fixture's.
  * **Recorded evidence** (``fixture_evidence``) captures the resolved date, the
    timezone label, the run token and the exact event names for the run report.
"""

from __future__ import annotations

import datetime as _dt

# The timezone the resolved "today" is understood in. The CaleeMobile app's own
# tests fix the zone to Australia/Perth; recorded in evidence, never used to
# shift a date (the timed event is floating-local by construction).
DEFAULT_TIMEZONE = "Australia/Perth"

TIMED_PREFIX = "REG-SUB-TIMED"
ALLDAY_PREFIX = "REG-SUB-ALLDAY"


def resolve_target_date(today: "_dt.date | None" = None) -> _dt.date:
    """Resolve the subscribed-fixture target date ONCE for a run. Defaults to
    the host's current date; an explicit value is honoured verbatim (so a run
    can pin the date and record it). Callers must resolve once and reuse the
    result for both generation and the recorded expectations."""
    return today if today is not None else _dt.date.today()


def timed_event_name(run_token: str) -> str:
    return f"{TIMED_PREFIX}-{run_token}"


def allday_event_name(run_token: str) -> str:
    return f"{ALLDAY_PREFIX}-{run_token}"


def generate_today_relative_ics(
    target_date: _dt.date, *, run_token: str,
) -> str:
    """A minimal REG-SUB subscribed-calendar ICS whose events fall on
    ``target_date``:

      * a TIMED event 12:00-13:00 FLOATING LOCAL (no ``Z``) -- visible on
        ``target_date`` in any timezone;
      * an ALL-DAY event on ``target_date`` (date-only, exclusive DTEND).

    ``run_token`` makes the event names unique per run. The output parses with
    ics_contract.expand (used in tests to prove date-correctness)."""
    if not run_token or not str(run_token).strip():
        raise ValueError("run_token is required and must be non-empty for unique event names.")
    d = target_date.strftime("%Y%m%d")
    d_next = (target_date + _dt.timedelta(days=1)).strftime("%Y%m%d")
    timed = timed_event_name(run_token)
    allday = allday_event_name(run_token)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Calee Regression//REG-SUB today-relative//EN",
        "BEGIN:VEVENT",
        f"UID:reg-sub-timed-{run_token}@calee.regression",
        f"SUMMARY:{timed}",
        f"DTSTART:{d}T120000",
        f"DTEND:{d}T130000",
        "END:VEVENT",
        "BEGIN:VEVENT",
        f"UID:reg-sub-allday-{run_token}@calee.regression",
        f"SUMMARY:{allday}",
        f"DTSTART;VALUE=DATE:{d}",
        f"DTEND;VALUE=DATE:{d_next}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def fixture_evidence(
    target_date: _dt.date, *, run_token: str, timezone: str = DEFAULT_TIMEZONE,
) -> dict:
    """The run-evidence record for a today-relative subscribed fixture: the
    resolved date, the timezone it is understood in, the run token, and the
    exact event names the tablet should assert. Written into the run so a report
    proves which day the subscribed Today/Calendar checks were resolved for."""
    return {
        "resolvedDate": target_date.isoformat(),
        "timezone": timezone,
        "runToken": run_token,
        "events": {
            "timed": timed_event_name(run_token),
            "allDay": allday_event_name(run_token),
        },
    }
