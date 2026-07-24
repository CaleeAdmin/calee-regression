"""The distributed fixture-exclusivity limitation must stay explicit.

Phase 8 decided (with evidence) that no safe atomic backend primitive exists to
back a cross-host regression lease, so the host-local lock is retained and
multi-host certification is BLOCKED. These tests stop that decision from being
silently "resolved" -- if someone flips the lock's scope or deletes the
decision record without adding a real lease, the build fails.
"""

from __future__ import annotations

from pathlib import Path

from calee_regression import fixture_ownership
from calee_regression import framework_completeness as fc

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISION_DOC = REPO_ROOT / "docs" / "DISTRIBUTED_FIXTURE_LEASE_DECISION.md"


def test_lock_scope_is_still_host_local_and_records_its_limitation():
    assert fixture_ownership.EXCLUSIVITY_SCOPE == "host-local"
    limitation = fixture_ownership.EXCLUSIVITY_LIMITATION.lower()
    assert "cross-host" in limitation and "not established" in limitation
    # Every LockResult carries the limitation into evidence.
    assert "exclusivityLimitation" in fixture_ownership.LockResult(state="acquired").to_dict()


def test_decision_doc_exists_and_records_blocked_multihost():
    assert DECISION_DOC.is_file(), "the distributed-lease decision record must exist"
    text = DECISION_DOC.read_text(encoding="utf-8").lower()
    assert "blocked" in text
    assert "host-local lock retained" in text
    # It must document the seven properties a real lease needs (spot-check).
    for required in ("atomic acquire", "compare-and-release", "expiry", "no credential"):
        assert required in text, f"decision doc must state the lease requirement: {required!r}"


def test_completeness_fixture_dimension_matches_the_decision():
    dim = fc.build_report().dimension("fixtureExclusivity")
    assert dim.status == fc.STATUS_PARTIAL
    assert dim.release_gating is False
    assert any("distributed" in b.lower() for b in dim.blockers)
    # The dimension points at the decision record as its evidence.
    assert any("DISTRIBUTED_FIXTURE_LEASE_DECISION" in e for e in dim.implementation_evidence)
