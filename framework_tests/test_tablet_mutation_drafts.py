"""Tests for Workstream 10's draft tablet mutation scenarios.

These scenarios (calendar create/edit/delete, task complete/reopen, chore
skip) depend on tablet UI resource ids that have never been confirmed
against the real Calee app -- see docs/TABLET_MUTATION_COVERAGE_GAPS.md.
They must stay structurally valid (so they're ready to run the moment a
technical owner fills in the real ids) while being provably incapable of
affecting any real release run in the meantime. This file locks in that
second property so a future edit can't silently regress it.
"""

from __future__ import annotations

import re

from calee_regression import runner, suites
from calee_regression.suites import REPO_ROOT

DRAFT_SUITES = ("calendar_event_mutation", "tasks_mutation", "chores_mutation")

GAPS_DOC = REPO_ROOT / "docs" / "TABLET_MUTATION_COVERAGE_GAPS.md"

UNCONFIRMED_TOKEN_RE = re.compile(r"UNCONFIRMED_[A-Za-z0-9_]+")


def test_draft_suites_are_not_reachable_from_full_tester_or_release_technical():
    for composite in ("full-tester", "release-technical"):
        resolved = [str(p) for p in suites.resolve_suite(composite)]
        for draft in DRAFT_SUITES:
            draft_paths = [str(p) for p in suites.resolve_suite(draft)]
            for path in draft_paths:
                assert path not in resolved, (
                    f"draft suite {draft!r}'s scenario {path} leaked into {composite!r} -- "
                    f"draft mutation scenarios must never be reachable from a real release run"
                )


def test_draft_suites_resolve_standalone():
    # Sanity check: they must still be usable via an explicit --suite for a
    # technical owner testing them directly.
    for draft in DRAFT_SUITES:
        resolved = suites.resolve_suite(draft)
        assert len(resolved) == 1
        assert resolved[0].exists()


def test_draft_scenarios_are_explicitly_not_mandatory():
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            scenario = runner.load_scenario(path)
            assert scenario.mandatory is False, (
                f"{path} must be mandatory: false -- it depends on unconfirmed resource ids "
                f"and must never block a release by itself"
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


def test_gaps_doc_exists_and_documents_every_unconfirmed_token_in_use():
    assert GAPS_DOC.is_file(), "docs/TABLET_MUTATION_COVERAGE_GAPS.md must exist alongside the draft scenarios"
    doc_text = GAPS_DOC.read_text(encoding="utf-8")

    tokens_in_use = set()
    for draft in DRAFT_SUITES:
        for path in suites.resolve_suite(draft):
            text = path.read_text(encoding="utf-8")
            tokens_in_use.update(UNCONFIRMED_TOKEN_RE.findall(text))

    assert tokens_in_use, "expected at least one UNCONFIRMED_ placeholder across the draft scenarios"
    missing = sorted(token for token in tokens_in_use if token not in doc_text)
    assert not missing, f"docs/TABLET_MUTATION_COVERAGE_GAPS.md does not mention: {missing}"


def test_no_composite_suite_definition_references_a_draft_suite_by_name():
    # Defense in depth beyond the resolve-based check above: the raw
    # COMPOSITE_SUITES definition itself must never name a draft suite,
    # even indirectly through a future refactor of full-tester's members.
    for members in suites.COMPOSITE_SUITES.values():
        for draft in DRAFT_SUITES:
            assert draft not in members
