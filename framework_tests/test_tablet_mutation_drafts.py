"""Promotion state machine for the source-backed tablet mutation scenarios
(Workstream 4).

The calendar/task/chore mutation scenarios now use resource ids read from the
real Calee tablet source (CaleeAdmin/Calee) -- they are SOURCE-CONFIRMED. What
they are not yet is PHYSICALLY-CONFIRMED (run end to end against a prepared
tablet). Until that happens they must stay gated: `mandatory: false`, tagged
`draft-unverified`, and unreachable from any release suite.

This file locks in that two-state machine so neither an accidental promotion
(adding an unverified scenario to a release suite) nor a regression of the
source-confirmation work (a placeholder token creeping back, or the recorded
source SHA going missing/abbreviated) can slip through:

  * DRAFT  (physical-unverified): mandatory: false + `draft-unverified` tag +
    not in full-tester/release-technical + a full 40-char Calee source SHA
    recorded + no UNCONFIRMED_ placeholders left.
  * PROMOTED (physically verified): the release owner drops `draft-unverified`,
    unsets `mandatory: false`, and adds the suite to full-tester -- and then a
    promoted scenario must carry NO UNCONFIRMED_ token and MUST record the
    Calee source SHA used for selector verification.
"""

from __future__ import annotations

import re

import yaml

from calee_regression import runner, suites
from calee_regression.identity_format import is_full_git_sha
from calee_regression.suites import REPO_ROOT

DRAFT_SUITES = ("calendar_event_mutation", "tasks_mutation", "chores_mutation")

GAPS_DOC = REPO_ROOT / "docs" / "TABLET_MUTATION_COVERAGE_GAPS.md"

UNCONFIRMED_TOKEN_RE = re.compile(r"UNCONFIRMED_[A-Za-z0-9_]+")


def _scenario_paths(suite):
    return list(suites.resolve_suite(suite))


def _raw_yaml(path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── Safety: draft scenarios can never gate a release ──────────────────────────


def test_draft_suites_are_not_reachable_from_full_tester_or_release_technical():
    for composite in ("full-tester", "release-technical"):
        resolved = [str(p) for p in suites.resolve_suite(composite)]
        for draft in DRAFT_SUITES:
            for path in _scenario_paths(draft):
                assert str(path) not in resolved, (
                    f"draft suite {draft!r}'s scenario {path} leaked into {composite!r} -- "
                    f"a physically-unverified mutation scenario must never gate a release run"
                )


def test_no_composite_suite_definition_references_a_draft_suite_by_name():
    for members in suites.COMPOSITE_SUITES.values():
        for draft in DRAFT_SUITES:
            assert draft not in members


def test_draft_suites_resolve_standalone():
    # Still usable via an explicit --suite for a technical owner running them directly.
    for draft in DRAFT_SUITES:
        resolved = suites.resolve_suite(draft)
        assert len(resolved) == 1
        assert resolved[0].exists()


def test_draft_scenarios_are_explicitly_not_mandatory():
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            scenario = runner.load_scenario(path)
            assert scenario.mandatory is False, (
                f"{path} must be mandatory: false while physically unverified"
            )


def test_draft_scenarios_are_tagged_draft_unverified():
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            scenario = runner.load_scenario(path)
            assert "draft-unverified" in scenario.tags, (
                f"{path} must keep the 'draft-unverified' tag until a physical pass"
            )


def test_draft_scenarios_parse_as_valid_scenarios():
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            scenario = runner.load_scenario(path)
            assert scenario.name
            assert scenario.steps
            for step in scenario.steps:
                action = step.get("action")
                assert action in runner.ACTIONS, f"{path}: unknown action {action!r} in step {step.get('name')!r}"


# ── Source-confirmation: real ids in, placeholders out, SHA recorded ──────────


def test_no_unconfirmed_placeholder_tokens_remain():
    # Source confirmation (Workstream 4) replaced every UNCONFIRMED_* placeholder
    # with a real id read from the Calee source -- none may remain.
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            leftover = UNCONFIRMED_TOKEN_RE.findall(path.read_text(encoding="utf-8"))
            assert not leftover, f"{path} still contains placeholder tokens: {sorted(set(leftover))}"


def test_each_draft_records_a_full_calee_source_sha():
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            data = _raw_yaml(path)
            sv = data.get("source_verification")
            assert isinstance(sv, dict), f"{path} must have a source_verification block (Workstream 4)"
            sha = sv.get("calee_source_sha")
            assert is_full_git_sha(sha), (
                f"{path} source_verification.calee_source_sha must be a full 40-char SHA, got {sha!r}"
            )
            assert sv.get("selectors"), f"{path} must list the source-confirmed selectors it uses"


def test_gaps_doc_exists_and_documents_every_confirmed_selector():
    assert GAPS_DOC.is_file(), "docs/TABLET_MUTATION_COVERAGE_GAPS.md must exist alongside the scenarios"
    doc_text = GAPS_DOC.read_text(encoding="utf-8")
    missing = []
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            selectors = (_raw_yaml(path).get("source_verification") or {}).get("selectors") or {}
            for selector_id in selectors:
                if selector_id not in doc_text:
                    missing.append(f"{path.name}:{selector_id}")
    assert not missing, f"docs/TABLET_MUTATION_COVERAGE_GAPS.md does not document: {missing}"


# ── Promotion invariant (enforced for ANY scenario, present or future) ────────


def _all_scenarios_in_full_tester():
    return list(suites.resolve_suite("full-tester"))


def test_promotion_invariant_for_full_tester_members():
    # Any scenario that IS in full-tester (a promoted one) must satisfy every
    # promotion requirement: no draft-unverified tag, mandatory not false, no
    # UNCONFIRMED_ token, and -- if it carries a source_verification block --
    # a full recorded Calee source SHA. This makes the promotion rules
    # self-enforcing: you cannot add a mutation scenario to full-tester while
    # leaving it in the draft state.
    for path in _all_scenarios_in_full_tester():
        scenario = runner.load_scenario(path)
        text = path.read_text(encoding="utf-8")
        assert "draft-unverified" not in scenario.tags, f"{path} is in full-tester but still tagged draft-unverified"
        assert scenario.mandatory is not False, f"{path} is in full-tester but mandatory: false"
        assert not UNCONFIRMED_TOKEN_RE.findall(text), f"{path} is in full-tester but has UNCONFIRMED_ tokens"
        sv = _raw_yaml(path).get("source_verification")
        if isinstance(sv, dict):
            assert is_full_git_sha(sv.get("calee_source_sha")), (
                f"{path} is in full-tester but records no full Calee source SHA"
            )
