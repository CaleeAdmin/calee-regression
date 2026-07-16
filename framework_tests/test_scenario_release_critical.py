"""Locks in the removal of false-positive patterns from release-critical
calendar scenarios, and the fix of the "Settings" tap target that could
never be found.

These scenarios previously could pass (or silently no-op) without ever
exercising real event/recurrence content or reaching the Settings screen at
all — see docs/CALENDAR_BIG_CHANGE_COVERAGE.md's "current limitations" and
git history for the prior versions. This test exists so a future edit can't
silently reintroduce an `optional`-wrapped assertion as the only check in
one of these files without a test noticing.
"""

from __future__ import annotations

import yaml

from calee_regression.suites import REPO_ROOT

RELEASE_CRITICAL_CALENDAR_SCENARIOS = [
    "scenarios/calendar_event_fields.yaml",
    "scenarios/calendar_recurring_events.yaml",
]


def _load_steps(relative_path: str) -> list:
    path = REPO_ROOT / relative_path
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["steps"]


def _all_actions(steps: list) -> list:
    actions = []
    for step in steps:
        actions.append(step.get("action"))
        if step.get("action") == "optional" and "step" in step:
            actions.append(f"optional:{step['step'].get('action')}")
    return actions


def test_calendar_scenarios_have_no_optional_steps():
    for relative_path in RELEASE_CRITICAL_CALENDAR_SCENARIOS:
        actions = _all_actions(_load_steps(relative_path))
        assert "optional" not in actions, (
            f"{relative_path} still wraps a step in 'optional' — its defining assertion "
            f"must not be able to silently no-op."
        )


def test_calendar_scenarios_target_deterministic_fixture_titles():
    event_fields_steps = _load_steps("scenarios/calendar_event_fields.yaml")
    event_fields_text = yaml.safe_dump(event_fields_steps)
    assert "REG-EVENT-TIMED-001" in event_fields_text
    assert "REG-EVENT-ALLDAY-001" in event_fields_text

    recurring_steps = _load_steps("scenarios/calendar_recurring_events.yaml")
    recurring_text = yaml.safe_dump(recurring_steps)
    assert "REG-EVENT-RECURRING-001" in recurring_text
    assert "REG-EVENT-EXCEPTION-001" in recurring_text


def test_calendar_scenarios_use_hard_tap_not_tap_if_present_for_fixture_events():
    for relative_path in RELEASE_CRITICAL_CALENDAR_SCENARIOS:
        steps = _load_steps(relative_path)
        tap_steps = [s for s in steps if s.get("action") in ("tap", "tap_if_present") and "text" in s]
        assert tap_steps, f"{relative_path} should tap into its fixture event by exact title"
        for step in tap_steps:
            assert step["action"] == "tap", (
                f"{relative_path} step {step['name']!r} taps a fixture event via "
                f"{step['action']!r} — this must be a hard 'tap', not 'tap_if_present', "
                f"since the fixture guarantees the event exists."
            )


def test_settings_scenarios_target_the_real_gear_icon_not_missing_text():
    for relative_path in ("scenarios/settings_smoke.yaml", "scenarios/home_navigation.yaml"):
        steps = _load_steps(relative_path)
        settings_taps = [s for s in steps if s.get("id") == "ivHomeSetting"]
        assert settings_taps, (
            f"{relative_path} should open Settings via the real ivHomeSetting gear icon id, "
            f"not by searching for text that never appears on the home screen."
        )
        assert all(s["action"] == "tap" for s in settings_taps)
