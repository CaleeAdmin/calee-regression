from __future__ import annotations

import os
from pathlib import Path

SCENARIO_GROUPS = {
    "smoke-fresh": ["scenarios/smoke_fresh.yaml"],
    "smoke-tablet": ["scenarios/smoke_tablet.yaml", "scenarios/home_navigation.yaml"],
    "calendar": [
        "scenarios/calendar_smoke.yaml",
        "scenarios/calendar_view_modes.yaml",
        "scenarios/calendar_event_fields.yaml",
        "scenarios/calendar_recurring_events.yaml",
    ],
    "tasks_smoke": ["scenarios/tasks_smoke.yaml"],
    "chores_smoke": ["scenarios/chores_smoke.yaml"],
    "settings_smoke": ["scenarios/settings_smoke.yaml"],
    "weather_system_messages": ["scenarios/weather_system_messages.yaml"],
    "login_qr_states": ["scenarios/login_qr_states.yaml"],
    "kiosk_admin_physical": ["scenarios/kiosk_admin_physical.yaml"],
    "system_receivers": ["scenarios/system_receivers.yaml"],
}

COMPOSITE_SUITES = {
    "full-tester": [
        "smoke-tablet",
        "calendar",
        "tasks_smoke",
        "chores_smoke",
        "settings_smoke",
        "weather_system_messages",
    ],
    "release-technical": ["full-tester", "kiosk_admin_physical", "system_receivers"],
}

SUITE_ALIASES = {
    "full": "full-tester",
    # Aliases matching the project's canonical suite-profile names (see
    # docs/SUITE_REFERENCE.md) so `tablet-smoke`/`tablet-full`/`full-release`
    # work here even though the underlying suites predate that naming.
    "tablet-smoke": "smoke-tablet",
    "tablet-full": "full-tester",
    "full-release": "full-tester",
}

PHYSICAL_ONLY_SCENARIOS = {
    "scenarios/kiosk_admin_physical.yaml",
    "scenarios/system_receivers.yaml",
}

# Scenarios that assert against the deterministic REG-* fixture (see
# docs/TEST_DATA_RESET_CONTRACT.md) -- kept in sync with
# framework_tests/test_scenario_release_critical.py's
# RELEASE_CRITICAL_CALENDAR_SCENARIOS list. Any suite resolving to one of
# these requires a real fixture reset+verify in `prepare`; it must never be
# silently bypassed with --skip-fixture/--allow-no-fixture (see cli.py).
FIXTURE_DEPENDENT_SCENARIOS = {
    "scenarios/calendar_event_fields.yaml",
    "scenarios/calendar_recurring_events.yaml",
}

REPO_ROOT = Path(__file__).resolve().parents[1]


class SuiteError(Exception):
    pass


def all_suite_names() -> list:
    return sorted(set(SCENARIO_GROUPS) | set(COMPOSITE_SUITES) | set(SUITE_ALIASES))


def resolve_suite(name: str, repo_root=None) -> list:
    repo_root = repo_root or REPO_ROOT
    canonical = SUITE_ALIASES.get(name, name)

    if canonical in COMPOSITE_SUITES:
        resolved = []
        seen = set()
        for member in COMPOSITE_SUITES[canonical]:
            for path in resolve_suite(member, repo_root):
                if path not in seen:
                    seen.add(path)
                    resolved.append(path)
        return resolved

    if canonical in SCENARIO_GROUPS:
        return [repo_root / p for p in SCENARIO_GROUPS[canonical]]

    raise SuiteError(f"Unknown suite {name!r}. Run: python -m calee_regression list-suites")


def suite_includes_physical(name: str, repo_root=None) -> bool:
    repo_root = repo_root or REPO_ROOT
    paths = resolve_suite(name, repo_root)
    return any(
        str(p.relative_to(repo_root)).replace(os.sep, "/") in PHYSICAL_ONLY_SCENARIOS
        for p in paths
    )


def suite_requires_fixture(name: str, repo_root=None) -> bool:
    """Whether resolving this suite includes a scenario that asserts
    against the deterministic REG-* fixture -- used by `prepare` to refuse
    --skip-fixture/--allow-no-fixture for a release-gating profile like
    tablet-full/full-release (see docs/TEST_DATA_RESET_CONTRACT.md)."""
    repo_root = repo_root or REPO_ROOT
    paths = resolve_suite(name, repo_root)
    return any(
        str(p.relative_to(repo_root)).replace(os.sep, "/") in FIXTURE_DEPENDENT_SCENARIOS
        for p in paths
    )


def list_suites(repo_root=None) -> dict:
    repo_root = repo_root or REPO_ROOT
    return {
        name: [str(p.relative_to(repo_root)).replace(os.sep, "/") for p in resolve_suite(name, repo_root)]
        for name in all_suite_names()
    }
