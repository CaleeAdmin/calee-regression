"""Gating/meta-tests for the calendar-appearance tablet scenarios (calee-hub-core
PATCH /client/v1/calendars/{id}/appearance; Calee PR CaleeAdmin/Calee#977).

Mirrors test_subscribed_calendar_contract.py's gating style (mandatory-false +
draft-unverified + valid actions + reachable-from-no-release-suite) and
test_tablet_mutation_drafts.py's source-confirmation checks (no UNCONFIRMED_
placeholders, a full recorded Calee source SHA), applied to the three new
scenario files:

  scenarios/calendar_appearance_subscription.yaml    (appearanceMode subscription_mapping)
  scenarios/calendar_appearance_owned.yaml            (appearanceMode source_metadata)
  scenarios/calendar_appearance_shared_readonly.yaml  (appearanceMode unsupported)

These scenarios run with no backend and no device -- what's proven here is
that they parse, use only real actions, stay correctly gated (mandatory:
false, draft-unverified, absent from every release suite), and are
internally consistent with the source-confirmed selectors/strings recorded
in their own source_verification blocks. Live execution stays BLOCKED (no
Appium/adb/emulator in this sandbox) -- see docs/CALENDAR_APPEARANCE_REGRESSION.md.
"""

from __future__ import annotations

import re

import yaml

from calee_regression import runner, suites
from calee_regression.identity_format import is_full_git_sha
from calee_regression.suites import REPO_ROOT

APPEARANCE_SCENARIOS = [
    REPO_ROOT / "scenarios" / "calendar_appearance_subscription.yaml",
    REPO_ROOT / "scenarios" / "calendar_appearance_owned.yaml",
    REPO_ROOT / "scenarios" / "calendar_appearance_shared_readonly.yaml",
]
APPEARANCE_DOC = REPO_ROOT / "docs" / "CALENDAR_APPEARANCE_REGRESSION.md"
UNCONFIRMED_RE = re.compile(r"UNCONFIRMED_[A-Za-z0-9_]+")

# The exact commit the task's source diff was transcribed from -- CaleeAdmin/Calee,
# branch claude/calendar-name-colour-editing-balgeo, PR #977.
EXPECTED_CALEE_SOURCE_SHA = "f1b92ddae9275cb0abea0f6df34126930e3aa71d"

CRASH_GUARD_TEXTS = ["Unfortunately", "has stopped", "Force Close", "Error"]


def _raw_yaml(path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── every scenario file exists and is wired into suites.py ───────────────────


def test_all_three_appearance_scenario_files_exist():
    for path in APPEARANCE_SCENARIOS:
        assert path.is_file(), f"missing scenario file: {path}"


def test_calendar_appearance_suite_is_registered_and_reachable():
    assert "calendar_appearance" in suites.all_suite_names()
    resolved = suites.resolve_suite("calendar_appearance")
    assert resolved == APPEARANCE_SCENARIOS


def test_calendar_appearance_suite_resolves_via_list_suites():
    listing = suites.list_suites()
    assert listing["calendar_appearance"] == [
        "scenarios/calendar_appearance_subscription.yaml",
        "scenarios/calendar_appearance_owned.yaml",
        "scenarios/calendar_appearance_shared_readonly.yaml",
    ]


# ── gating: mandatory-false, draft-unverified, absent from release suites ────


def test_every_appearance_scenario_is_mandatory_false():
    for path in APPEARANCE_SCENARIOS:
        scenario = runner.load_scenario(path)
        assert scenario.mandatory is False, f"{path} must be mandatory: false while physically unverified"


def test_every_appearance_scenario_is_tagged_draft_unverified():
    for path in APPEARANCE_SCENARIOS:
        scenario = runner.load_scenario(path)
        assert "draft-unverified" in scenario.tags, f"{path} must carry the draft-unverified tag"


def test_no_appearance_scenario_is_reachable_from_a_release_suite():
    for composite in ("full-tester", "release-technical"):
        resolved = [str(p) for p in suites.resolve_suite(composite)]
        for path in APPEARANCE_SCENARIOS:
            assert str(path) not in resolved, f"{path} leaked into release suite {composite!r}"


def test_calendar_appearance_suite_is_not_referenced_by_any_composite_suite():
    for members in suites.COMPOSITE_SUITES.values():
        assert "calendar_appearance" not in members


# ── parsing: valid scenarios, known actions only, no placeholders ────────────


def test_every_appearance_scenario_parses_and_uses_known_actions():
    for path in APPEARANCE_SCENARIOS:
        scenario = runner.load_scenario(path)
        assert scenario.name and scenario.steps
        for step in scenario.steps:
            action = step.get("action")
            assert action in runner.ACTIONS, f"{path}: unknown action {action!r} in step {step.get('name')!r}"


def test_no_appearance_scenario_has_unconfirmed_placeholder_tokens():
    for path in APPEARANCE_SCENARIOS:
        leftover = UNCONFIRMED_RE.findall(path.read_text(encoding="utf-8"))
        assert not leftover, f"{path} still contains placeholder tokens: {sorted(set(leftover))}"


def test_every_appearance_scenario_ends_with_the_crash_dialog_guard():
    for path in APPEARANCE_SCENARIOS:
        scenario = runner.load_scenario(path)
        last = scenario.steps[-1]
        assert last.get("action") == "fail_if_text", f"{path}'s last step must be the crash-dialog guard"
        assert last.get("texts") == CRASH_GUARD_TEXTS, f"{path}'s crash-dialog guard texts must match the repo-wide idiom exactly"


# ── source-confirmation: real ids in, full SHA recorded, selectors documented ─


def test_every_appearance_scenario_records_the_expected_calee_source_sha():
    for path in APPEARANCE_SCENARIOS:
        data = _raw_yaml(path)
        sv = data.get("source_verification")
        assert isinstance(sv, dict), f"{path} must have a source_verification block"
        assert is_full_git_sha(sv.get("calee_source_sha"))
        # Locks in the SPECIFIC commit this feature's selectors were
        # transcribed from -- not just "a" full SHA, but the right one, so a
        # future edit can't silently swap in an unrelated/unverified commit.
        assert sv.get("calee_source_sha") == EXPECTED_CALEE_SOURCE_SHA, path
        assert sv.get("status") == "source-confirmed", path
        assert sv.get("physical_confirmation") == "pending", path
        assert sv.get("selectors"), f"{path} must list the source-confirmed selectors it uses"


def test_every_selector_the_scenario_records_is_actually_used_in_its_own_steps():
    for path in APPEARANCE_SCENARIOS:
        text = path.read_text(encoding="utf-8")
        sv = _raw_yaml(path).get("source_verification") or {}
        for selector_id in sv.get("selectors") or {}:
            assert selector_id in text, f"{path} records selector {selector_id!r} but never uses it"


def test_appearance_doc_exists_and_documents_every_recorded_selector():
    assert APPEARANCE_DOC.is_file(), "docs/CALENDAR_APPEARANCE_REGRESSION.md must exist alongside the scenarios"
    doc_text = APPEARANCE_DOC.read_text(encoding="utf-8")
    missing = []
    for path in APPEARANCE_SCENARIOS:
        selectors = (_raw_yaml(path).get("source_verification") or {}).get("selectors") or {}
        for selector_id in selectors:
            if selector_id not in doc_text:
                missing.append(f"{path.name}:{selector_id}")
    assert not missing, f"docs/CALENDAR_APPEARANCE_REGRESSION.md does not document: {missing}"


# ── feature-specific content checks (per appearanceMode) ─────────────────────


def test_subscription_scenario_asserts_the_local_only_appearance_note():
    text = (REPO_ROOT / "scenarios" / "calendar_appearance_subscription.yaml").read_text(encoding="utf-8")
    assert "These changes only affect how this calendar appears in Calee." in text
    assert "This updates the calendar name and colour." not in text


def test_owned_scenario_asserts_the_source_metadata_appearance_note():
    text = (REPO_ROOT / "scenarios" / "calendar_appearance_owned.yaml").read_text(encoding="utf-8")
    assert "This updates the calendar name and colour." in text
    assert "These changes only affect how this calendar appears in Calee." not in text


def test_subscription_and_owned_scenarios_assert_ivedit_present_not_absent():
    for name in ("calendar_appearance_subscription.yaml", "calendar_appearance_owned.yaml"):
        scenario = runner.load_scenario(REPO_ROOT / "scenarios" / name)
        ivedit_steps = [s for s in scenario.steps if s.get("id") == "ivEdit"]
        assert ivedit_steps, f"{name} must reference ivEdit"
        assert all(s.get("action") in ("assert_id", "tap") for s in ivedit_steps), (
            f"{name} expects ivEdit to be present/tappable (canEditAppearance == true), never fail_if_id"
        )


def test_shared_readonly_scenario_asserts_ivedit_absent_and_owner_managed_note_present():
    scenario = runner.load_scenario(REPO_ROOT / "scenarios" / "calendar_appearance_shared_readonly.yaml")
    ivedit_steps = [s for s in scenario.steps if s.get("id") == "ivEdit"]
    assert ivedit_steps, "calendar_appearance_shared_readonly.yaml must reference ivEdit"
    assert all(s.get("action") == "fail_if_id" for s in ivedit_steps), (
        "a shared read-only calendar's ivEdit must be asserted ABSENT (fail_if_id), never tapped/asserted present"
    )
    note_steps = [s for s in scenario.steps if s.get("id") == "tvOwnerManagedNote"]
    assert any(s.get("action") == "assert_id" for s in note_steps)
    text = "\n".join(str(v) for step in scenario.steps for v in step.values())
    assert "This shared calendar is managed by its owner." in text


def test_shared_readonly_scenario_never_opens_the_edit_dialog():
    # unsupported-mode calendars never reach HubCalendarEditDialog -- there is
    # nothing to assert about tvCollectionTitle/etc. here, unlike the other two.
    scenario = runner.load_scenario(REPO_ROOT / "scenarios" / "calendar_appearance_shared_readonly.yaml")
    dialog_ids = {"tvCollectionTitle", "etCollectionName", "tvCollectionAppearanceNote", "btnCollectionSave"}
    used_ids = {s.get("id") for s in scenario.steps if s.get("id")}
    assert not (dialog_ids & used_ids), "the shared-readonly scenario should never reach the edit dialog"


def test_appearance_scenarios_never_save_only_cancel():
    # None of the three scenarios should tap btnCollectionSave -- they only
    # observe dialog contents and close without mutating the shared fixture.
    for path in APPEARANCE_SCENARIOS:
        scenario = runner.load_scenario(path)
        save_taps = [s for s in scenario.steps if s.get("action") == "tap" and s.get("id") == "btnCollectionSave"]
        assert not save_taps, f"{path} must not tap btnCollectionSave (never mutate the shared fixture)"
