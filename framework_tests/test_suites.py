import pytest

from calee_regression import suites


def test_all_suite_scenario_files_exist():
    for name in suites.all_suite_names():
        for path in suites.resolve_suite(name):
            assert path.exists(), f"suite {name!r} references missing scenario file {path}"


def test_full_alias_equals_full_tester():
    assert suites.resolve_suite("full") == suites.resolve_suite("full-tester")


def test_release_technical_includes_physical_scenarios():
    assert suites.suite_includes_physical("release-technical") is True
    resolved = [str(p) for p in suites.resolve_suite("release-technical")]
    assert any(p.endswith("kiosk_admin_physical.yaml") for p in resolved)
    assert any(p.endswith("system_receivers.yaml") for p in resolved)


def test_full_tester_excludes_physical_and_system_scenarios():
    assert suites.suite_includes_physical("full-tester") is False
    resolved = [str(p) for p in suites.resolve_suite("full-tester")]
    assert not any(p.endswith("kiosk_admin_physical.yaml") for p in resolved)
    assert not any(p.endswith("system_receivers.yaml") for p in resolved)


def test_unknown_suite_raises():
    with pytest.raises(suites.SuiteError):
        suites.resolve_suite("unknown-suite-xyz")


def test_smoke_fresh_is_only_smoke_fresh_scenario():
    resolved = [p.name for p in suites.resolve_suite("smoke-fresh")]
    assert resolved == ["smoke_fresh.yaml"]


def test_calendar_suite_has_four_scenarios():
    resolved = [p.name for p in suites.resolve_suite("calendar")]
    assert resolved == [
        "calendar_smoke.yaml",
        "calendar_view_modes.yaml",
        "calendar_event_fields.yaml",
        "calendar_recurring_events.yaml",
    ]


def test_list_suites_returns_relative_paths():
    listing = suites.list_suites()
    assert "smoke-fresh" in listing
    assert listing["smoke-fresh"] == ["scenarios/smoke_fresh.yaml"]
