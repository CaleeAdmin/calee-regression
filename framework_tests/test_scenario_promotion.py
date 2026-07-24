"""Evidence-backed scenario-promotion evaluator (Workstream 5).

Adversarial: proves a scenario CANNOT be promoted from a diagnostic pass, one
pass when two are required, a stale build, a mismatched backend, missing
cleanup, an audit bundle, tampered report bytes, another scenario's result, or
an ambiguous/duplicate attempt set -- and CAN when every criterion is met.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from calee_regression import scenario_promotion as sp

SCENARIO = "calendar_event_mutation"
SRC_SHA = "d5b99712158c27f435681946326f0c7b8df54a3e"

FULL_ASSERTIONS = {k: True for k in sp.SCENARIO_ASSERTIONS[SCENARIO]}


def _promotion_dir(tmp_path):
    pdir = tmp_path / "promotion"
    pdir.mkdir()
    (pdir / f"{SCENARIO}.yaml").write_text(yaml.safe_dump({
        "scenario": SCENARIO,
        "scenarioFile": f"scenarios/{SCENARIO}.yaml",
        "sourceConfirmed": True,
        "sourceSha": SRC_SHA,
        "offlineTestsPassed": True,
        "physicalConfirmation": {
            "status": "pending",
            "requiredDevice": "physical_tablet",
            "evidenceRequired": ["runId", "tabletModel", "androidVersion", "caleeVersion",
                                 "caleeGitSha", "screenshotPaths", "resultsJson"],
            "evidence": {},
        },
        "releaseSuiteEligible": False,
    }))
    return pdir


def _report(tmp_path, run_id="release-1", *, sidecar=False, **over):
    run = tmp_path / "reports" / "runs" / run_id / "tablet-mutation" / SCENARIO
    run.mkdir(parents=True)
    payload = {
        "scenario": SCENARIO, "completenessKey": "tablet-mutation", "runId": run_id,
        "certificationEligible": True, "status": "pass",
        "diagnosticMode": False, "deviceInitializationMode": "standard", "reportSchemaVersion": 1,
        "attempts": [{"index": 1, "status": "pass"}, {"index": 2, "status": "pass"}],
        "caleeGitSha": SRC_SHA, "sourceSelectorSha": SRC_SHA,
        "tabletModel": "Lenovo-Tab", "deviceId": "adb-a266", "androidVersion": "13", "caleeVersion": "2.3.0",
        "targetEnvironment": "https://hub-dev.calee.com.au", "fixtureVersion": "REG-2026-07",
        "steps": [{"name": "create", "status": "pass"}, {"name": "delete", "status": "pass"}],
        "cleanupVerified": True, "bundleProfile": None,
        "screenshotPaths": ["a.png"], "resultsJson": "results.json",
        "authoritativeAssertions": dict(FULL_ASSERTIONS),
    }
    payload.update(over)
    p = run / "results.json"
    p.write_text(json.dumps(payload))
    if sidecar:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        (run / "results.json.sha256").write_text(digest + "  results.json\n")
    return p


def _evaluate(tmp_path, **kw):
    return sp.evaluate(SCENARIO, kw.pop("run_id", "release-1"),
                       reports_root=tmp_path / "reports", promotion_dir=_promotion_dir(tmp_path), **kw)


# ── the happy path ──────────────────────────────────────────────────────────
def test_full_valid_evidence_is_eligible(tmp_path):
    _report(tmp_path)
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_ELIGIBLE, [c.to_dict() for c in d.failing()]


# ── adversarial: each must NOT promote ──────────────────────────────────────
def test_diagnostic_pass_is_ineligible(tmp_path):
    _report(tmp_path, diagnosticMode=True)
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "not_diagnostic" in {c.name for c in d.failing()}


def test_one_pass_when_two_required_is_ineligible(tmp_path):
    _report(tmp_path, attempts=[{"index": 1, "status": "pass"}])
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "required_attempts" in {c.name for c in d.failing()}


def test_stale_build_is_ineligible(tmp_path):
    _report(tmp_path, caleeGitSha="0000000000000000000000000000000000000000")
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "same_product_build" in {c.name for c in d.failing()}


def test_mismatched_backend_is_ineligible(tmp_path):
    _report(tmp_path)
    d = sp.evaluate(SCENARIO, "release-1", reports_root=tmp_path / "reports",
                    promotion_dir=_promotion_dir(tmp_path), expected_backend="https://hub-OTHER.calee.com.au")
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "expected_backend" in {c.name for c in d.failing()}


def test_missing_cleanup_is_ineligible(tmp_path):
    _report(tmp_path, cleanupVerified=False)
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "cleanup_verified" in {c.name for c in d.failing()}


def test_audit_bundle_evidence_is_ineligible(tmp_path):
    _report(tmp_path, bundleProfile="audit")
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "not_imported_audit_evidence" in {c.name for c in d.failing()}


def test_non_standard_device_init_is_ineligible(tmp_path):
    _report(tmp_path, deviceInitializationMode="skip")
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "standard_device_initialization" in {c.name for c in d.failing()}


def test_failed_step_is_ineligible(tmp_path):
    _report(tmp_path, steps=[{"name": "create", "status": "pass"}, {"name": "delete", "status": "blocked"}])
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "no_failed_or_blocked_step" in {c.name for c in d.failing()}


def test_missing_scenario_assertions_is_ineligible(tmp_path):
    partial = {k: True for k in list(sp.SCENARIO_ASSERTIONS[SCENARIO])[:-1]}  # drop one
    _report(tmp_path, authoritativeAssertions=partial)
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "scenario_authoritative_assertions" in {c.name for c in d.failing()}


def test_tampered_report_bytes_is_ineligible(tmp_path):
    p = _report(tmp_path, sidecar=True)
    # Alter the report bytes AFTER the immutable sidecar digest was recorded.
    obj = json.loads(p.read_text())
    obj["tabletModel"] = "SWAPPED"
    p.write_text(json.dumps(obj))
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "report_digest" in {c.name for c in d.failing()}


def test_another_scenarios_result_is_not_found(tmp_path):
    # Only a DIFFERENT scenario's report exists in the run.
    run = tmp_path / "reports" / "runs" / "release-1" / "x"
    run.mkdir(parents=True)
    (run / "results.json").write_text(json.dumps({"scenario": "tasks_mutation", "status": "pass"}))
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_INELIGIBLE
    assert "physical_report_present" in {c.name for c in d.failing()}


def test_duplicate_attempts_are_ambiguous(tmp_path):
    _report(tmp_path, attempts=[{"index": 1, "status": "pass"}, {"index": 1, "status": "pass"}])
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_AMBIGUOUS


def test_two_reports_for_one_scenario_are_ambiguous(tmp_path):
    _report(tmp_path)
    other = tmp_path / "reports" / "runs" / "release-1" / "dup"
    other.mkdir(parents=True)
    (other / "results.json").write_text(json.dumps({"scenario": SCENARIO, "status": "pass"}))
    d = _evaluate(tmp_path)
    assert d.decision == sp.DECISION_AMBIGUOUS


# ── apply is fail-closed ────────────────────────────────────────────────────
def test_apply_refuses_when_not_eligible(tmp_path):
    _report(tmp_path, diagnosticMode=True)
    d = _evaluate(tmp_path)
    import pytest
    with pytest.raises(sp.ScenarioPromotionError):
        sp.apply_record_update(d, promotion_dir=tmp_path / "promotion")


def test_apply_records_pass_into_promotion_record_when_eligible(tmp_path):
    _report(tmp_path)
    pdir = _promotion_dir(tmp_path)
    d = sp.evaluate(SCENARIO, "release-1", reports_root=tmp_path / "reports", promotion_dir=pdir)
    assert d.decision == sp.DECISION_ELIGIBLE
    summary = sp.apply_record_update(d, promotion_dir=pdir)
    assert summary["statusAfter"] == "passed"
    raw = yaml.safe_load((pdir / f"{SCENARIO}.yaml").read_text())
    assert raw["physicalConfirmation"]["status"] == "passed"
    assert raw["physicalConfirmation"]["evidence"]["runId"]  # evidence populated
    assert raw["releaseSuiteEligible"] is False  # gating remains a separate step
