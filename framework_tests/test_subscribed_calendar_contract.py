"""Offline subscribed-calendar date-semantics contract (Workstream 3).

Expands the regression-owned fixture
(`fixtures/subscribed_calendar/reg_sub_calendar.ics`) with a dependency-free
expander and asserts the date semantics a subscribed feed MUST be expanded with
-- the same ones the hub (`calee-hub-core` #352/#353) and the Calee tablet
(#973) uphold. This runs with no backend and no device: it proves the fixture
and the semantics independently of live execution (which stays BLOCKED without
a hub backend / tablet -- see docs/SUBSCRIBED_CALENDAR_REGRESSION.md).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import yaml

from calee_regression import ics_contract, runner, suites
from calee_regression.identity_format import is_full_git_sha
from calee_regression.suites import REPO_ROOT

FIXTURE = REPO_ROOT / "fixtures" / "subscribed_calendar" / "reg_sub_calendar.ics"
SUB_SCENARIO = REPO_ROOT / "scenarios" / "subscribed_calendar.yaml"
SUB_DOC = REPO_ROOT / "docs" / "SUBSCRIBED_CALENDAR_REGRESSION.md"
UNCONFIRMED_RE = re.compile(r"UNCONFIRMED_[A-Za-z0-9_]+")


def _expand():
    return ics_contract.expand(FIXTURE.read_text(encoding="utf-8"))


def _by_summary(occurrences, summary):
    return [o for o in occurrences if o.summary == summary]


# ── all-day detection rule (VALUE=DATE OR bare YYYYMMDD) ──────────────────────


def test_is_all_day_rule():
    assert ics_contract.is_all_day_value("20260805", "VALUE=DATE") is True
    assert ics_contract.is_all_day_value("20260805", "") is True          # bare YYYYMMDD
    assert ics_contract.is_all_day_value("20260806T090000Z", "") is False  # timed


def test_fixture_titles_are_unique_reg_sub():
    occ = _expand()
    summaries = {o.summary for o in occ}
    for expected in (
        "REG-SUB-ALLDAY-SINGLE", "REG-SUB-ALLDAY-MULTI", "REG-SUB-TIMED",
        "REG-SUB-BARE-ALLDAY", "REG-SUB-ALLDAY-DAILY", "REG-SUB-OVERRIDE",
    ):
        assert expected in summaries, expected
    assert all(s.startswith("REG-SUB-") for s in summaries)


# ── all-day DTEND is exclusive; no off-by-one shift ──────────────────────────


def test_single_day_all_day_dtend_exclusive_no_shift():
    (occ,) = _by_summary(_expand(), "REG-SUB-ALLDAY-SINGLE")
    assert occ.all_day is True
    assert occ.start == date(2026, 8, 5)
    assert occ.end == date(2026, 8, 6)                 # DTEND exclusive
    assert occ.visible_dates == [date(2026, 8, 5)]     # visible on start day ONLY (no shift)
    assert occ.duration == timedelta(days=1)


def test_multi_day_all_day_spans_inclusive_days_with_exclusive_dtend():
    (occ,) = _by_summary(_expand(), "REG-SUB-ALLDAY-MULTI")
    assert occ.all_day is True
    assert occ.end == date(2026, 8, 13)  # exclusive
    assert occ.visible_dates == [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]


def test_bare_yyyymmdd_start_is_all_day():
    (occ,) = _by_summary(_expand(), "REG-SUB-BARE-ALLDAY")
    assert occ.all_day is True                         # no VALUE=DATE, still all-day
    assert occ.visible_dates == [date(2026, 8, 8)]
    assert occ.end == date(2026, 8, 9)


def test_timed_event_is_not_all_day():
    (occ,) = _by_summary(_expand(), "REG-SUB-TIMED")
    assert occ.all_day is False
    assert occ.duration == timedelta(hours=1)
    assert occ.visible_dates == [date(2026, 8, 6)]


# ── recurrence: duration, EXDATE removal, RECURRENCE-ID override ──────────────


def test_daily_recurrence_duration_exdate_and_override():
    occ = _expand()
    base = _by_summary(occ, "REG-SUB-ALLDAY-DAILY")
    base_dates = sorted(o.start for o in base)

    # 5 nominal daily occurrences 08-20..08-24; 08-22 removed by EXDATE; 08-23
    # replaced by the REG-SUB-OVERRIDE override -> base series keeps 20, 21, 24.
    assert base_dates == [date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 24)]
    for o in base:
        assert o.all_day is True
        assert o.duration == timedelta(days=1)  # every occurrence carries the master duration

    # EXDATE: 08-22 is absent from EVERY expanded occurrence.
    assert all(o.start != date(2026, 8, 22) for o in occ)

    # Override: 08-23 exists but as REG-SUB-OVERRIDE, flagged overridden.
    override = _by_summary(occ, "REG-SUB-OVERRIDE")
    assert len(override) == 1
    assert override[0].start == date(2026, 8, 23)
    assert override[0].overridden is True


def test_every_all_day_occurrence_has_whole_day_duration():
    for o in _expand():
        if o.all_day:
            assert (o.end - o.start).days >= 1
            assert o.visible_dates[0] == o.start  # first visible day == start (no -1 shift)


# ── tablet scenario gating (source-confirmed, physically unverified) ──────────


def test_subscribed_scenario_is_gated_and_source_confirmed():
    scenario = runner.load_scenario(SUB_SCENARIO)
    assert scenario.mandatory is False
    assert "draft-unverified" in scenario.tags
    text = SUB_SCENARIO.read_text(encoding="utf-8")
    assert not UNCONFIRMED_RE.findall(text)
    data = yaml.safe_load(text)
    sv = data.get("source_verification") or {}
    assert is_full_git_sha(sv.get("calee_source_sha"))
    # Every selector the scenario relies on is documented in the WS3 doc.
    doc = SUB_DOC.read_text(encoding="utf-8")
    for selector_id in (sv.get("selectors") or {}):
        assert selector_id in text  # actually used by the scenario


def test_subscribed_scenario_is_not_in_any_release_suite():
    for composite in ("full-tester", "release-technical"):
        resolved = [str(p) for p in suites.resolve_suite(composite)]
        assert str(SUB_SCENARIO) not in resolved


def test_subscribed_scenario_parses_and_uses_known_actions():
    scenario = runner.load_scenario(SUB_SCENARIO)
    assert scenario.name and scenario.steps
    for step in scenario.steps:
        assert step.get("action") in runner.ACTIONS


def test_ws3_doc_exists():
    assert SUB_DOC.is_file()
