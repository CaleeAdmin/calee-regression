"""A small, dependency-free iCalendar expander for the subscribed-calendar
regression contract (Workstream 3).

This is NOT a general RFC 5545 implementation. It expands exactly the shapes the
regression fixture (`fixtures/subscribed_calendar/reg_sub_calendar.ics`) uses,
so an offline test can prove the *date semantics* a subscribed-calendar feed
must be expanded with -- the same semantics the hub
(`calee-hub-core`'s `core_client_subscription_sources.php`) and the Calee tablet
(#973) uphold:

  * all-day DTEND is **exclusive** (a single-day all-day event's DTEND is the
    NEXT day; it is visible on the start day only);
  * a **bare** ``DTSTART:YYYYMMDD`` with no ``VALUE=DATE`` parameter is still
    all-day;
  * every recurring occurrence carries the master's duration;
  * ``EXDATE`` removes exactly the named occurrence;
  * a ``RECURRENCE-ID`` override replaces exactly the named occurrence.

Supported RRULE: ``FREQ=DAILY`` with ``COUNT`` or ``UNTIL`` (and ``INTERVAL``).
Anything else raises IcsContractError rather than silently mis-expanding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

_BARE_DATE_RE = re.compile(r"^\d{8}$")
_DATETIME_RE = re.compile(r"^(\d{8})T(\d{6})(Z)?$")


class IcsContractError(Exception):
    pass


@dataclass
class RawEvent:
    uid: str
    props: dict  # NAME -> {"value": str, "params": str}
    exdates: list  # list of (value, params)
    recurrence_id: "tuple[str, str] | None" = None  # (value, params) or None


@dataclass
class Occurrence:
    uid: str
    summary: str
    all_day: bool
    start: object  # date (all-day) or datetime (timed)
    end: object    # exclusive; date (all-day) or datetime (timed)
    overridden: bool = False

    @property
    def duration(self) -> timedelta:
        s = _as_dt(self.start)
        e = _as_dt(self.end)
        return e - s

    @property
    def visible_dates(self) -> list:
        """Dates on which the event is shown. All-day uses the EXCLUSIVE DTEND
        (so [start .. end-1 day]); a timed event shows on its start date."""
        if self.all_day:
            days = (self.end - self.start).days
            return [self.start + timedelta(days=i) for i in range(days)]
        return [self.start.date()]


def _as_dt(value):
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _unfold(text: str) -> list:
    # RFC 5545 line folding: a CRLF followed by a space/tab continues the line.
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _split_prop(line: str):
    """`NAME;PARAM=x:value` -> (NAME, params, value)."""
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    if ";" in head:
        name, params = head.split(";", 1)
    else:
        name, params = head, ""
    return name.upper(), params, value


def parse_ics(text: str) -> list:
    events: list = []
    cur: "dict | None" = None
    exdates: list = []
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur, exdates = {}, []
            continue
        if line == "END:VEVENT":
            if cur is not None:
                uid = cur.get("UID", {}).get("value", "")
                rid = cur.get("RECURRENCE-ID")
                events.append(
                    RawEvent(
                        uid=uid,
                        props=cur,
                        exdates=exdates,
                        recurrence_id=(rid["value"], rid["params"]) if rid else None,
                    )
                )
            cur = None
            continue
        if cur is None:
            continue
        parsed = _split_prop(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "EXDATE":
            exdates.append((value, params))
        else:
            cur[name] = {"value": value, "params": params}
    return events


def is_all_day_value(value: str, params: str) -> bool:
    """The hub rule: all-day when the property is declared VALUE=DATE, OR its
    value is a bare 8-digit YYYYMMDD (many real feeds omit VALUE=DATE)."""
    if "VALUE=DATE" in (params or "").upper():
        return True
    return bool(_BARE_DATE_RE.match((value or "").strip()))


def _parse_value(value: str, all_day: bool):
    value = (value or "").strip()
    if all_day:
        if not _BARE_DATE_RE.match(value):
            raise IcsContractError(f"all-day value {value!r} is not YYYYMMDD")
        return datetime.strptime(value, "%Y%m%d").date()
    m = _DATETIME_RE.match(value)
    if not m:
        raise IcsContractError(f"timed value {value!r} is not YYYYMMDDThhmmss[Z]")
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return dt.replace(tzinfo=timezone.utc)


def _rrule_map(rrule: str) -> dict:
    out = {}
    for part in rrule.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def _one_occurrence(ev: RawEvent) -> Occurrence:
    dtstart = ev.props.get("DTSTART")
    if not dtstart:
        raise IcsContractError(f"event {ev.uid!r} has no DTSTART")
    all_day = is_all_day_value(dtstart["value"], dtstart["params"])
    start = _parse_value(dtstart["value"], all_day)
    dtend = ev.props.get("DTEND")
    if dtend:
        end = _parse_value(dtend["value"], is_all_day_value(dtend["value"], dtend["params"]))
    elif all_day:
        end = start + timedelta(days=1)  # default all-day duration: 1 day
    else:
        end = start + timedelta(hours=1)
    summary = ev.props.get("SUMMARY", {}).get("value", "")
    return Occurrence(uid=ev.uid, summary=summary, all_day=all_day, start=start, end=end)


def expand(text: str) -> list:
    """Expand every VEVENT in `text` into concrete occurrences, applying RRULE,
    EXDATE and RECURRENCE-ID overrides. Occurrences are returned in start order.
    """
    events = parse_ics(text)
    masters = [e for e in events if e.recurrence_id is None]
    overrides: dict = {}
    for e in events:
        if e.recurrence_id is not None:
            rid_value, rid_params = e.recurrence_id
            rid_all_day = is_all_day_value(rid_value, rid_params)
            key = (e.uid, _parse_value(rid_value, rid_all_day))
            overrides[key] = e

    occurrences: list = []
    for master in masters:
        base = _one_occurrence(master)
        rrule = master.props.get("RRULE", {}).get("value")
        if not rrule:
            occurrences.append(base)
            continue

        rmap = _rrule_map(rrule)
        if rmap.get("FREQ") != "DAILY":
            raise IcsContractError(f"unsupported RRULE FREQ {rmap.get('FREQ')!r} (fixture uses FREQ=DAILY)")
        interval = int(rmap.get("INTERVAL", "1"))
        duration = base.end - base.start

        exdate_dates = set()
        for value, params in master.exdates:
            exdate_dates.add(_parse_value(value, is_all_day_value(value, params)))

        count = int(rmap["COUNT"]) if "COUNT" in rmap else None
        until = None
        if "UNTIL" in rmap:
            until = _parse_value(rmap["UNTIL"], base.all_day)
        if count is None and until is None:
            raise IcsContractError(f"unbounded RRULE for {master.uid!r} (need COUNT or UNTIL)")

        step = timedelta(days=interval)
        generated = 0
        cur = base.start
        # COUNT counts nominal occurrences BEFORE EXDATE removal (RFC 5545).
        while True:
            if count is not None and generated >= count:
                break
            if until is not None and cur > until:
                break
            generated += 1
            occ_key = (master.uid, cur)
            if cur in exdate_dates:
                cur = cur + step
                continue
            if occ_key in overrides:
                occurrences.append(_one_occurrence(overrides[occ_key]))
                occurrences[-1].overridden = True
            else:
                occurrences.append(
                    Occurrence(
                        uid=base.uid, summary=base.summary, all_day=base.all_day,
                        start=cur, end=cur + duration,
                    )
                )
            cur = cur + step
            if count is None and until is not None and generated > 3660:
                raise IcsContractError("runaway UNTIL expansion")

    occurrences.sort(key=lambda o: _as_dt(o.start))
    return occurrences
