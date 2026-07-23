"""Child-report validation + verified-context construction + the vendored
cross-repository focused contract (this session's Workstreams 3/7/12)."""

from __future__ import annotations

import json

import pytest

from calee_regression import focused_context, focused_contract, focused_report_validation as frv


def _write(tmp_path, doc, name="results.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    return path


def _api_report(**overrides):
    doc = {
        "reportType": "mobile-api-suite", "reportSchemaVersion": 1,
        "runId": "local", "releaseRunId": "run-1", "releaseId": "rel-1",
        "backend": {"requested": "https://staging.calee.invalid"},
        "fixtureVersion": "REG-9", "executionPurpose": "focused-post-fix-verification",
        "certificationEligible": False, "status": "PASS",
    }
    doc.update(overrides)
    return doc


EXPECT = dict(
    expected_type="mobile-api-suite", child_exit_code=0, expected_run_id="run-1",
    expected_release_id="rel-1", expected_backend="https://staging.calee.invalid",
    expected_fixture_version="REG-9", expected_purpose="focused-post-fix-verification",
)


# ── validation rules ───────────────────────────────────────────────────────
def test_valid_report_passes_and_is_digest_bound(tmp_path):
    path = _write(tmp_path, _api_report())
    result = frv.validate_child_report(path, **EXPECT)
    assert result.ok, result.problems
    assert result.digest == frv.sha256_of_file(path)


def test_missing_report_after_exit_0_blocks(tmp_path):
    result = frv.validate_child_report(tmp_path / "absent.json", **EXPECT)
    assert not result.ok
    assert "does not exist" in result.problems[0]


@pytest.mark.parametrize("overrides,needle", [
    ({"reportType": "something-else"}, "reportType"),
    ({"reportSchemaVersion": 99}, "reportSchemaVersion"),
    ({"releaseRunId": "another-run"}, "run identity"),
    ({"releaseId": "another-release"}, "releaseId"),
    ({"backend": {"requested": "https://prod.calee.invalid"}}, "backend"),
    ({"fixtureVersion": "REG-1"}, "fixtureVersion"),
    ({"executionPurpose": "release-certification"}, "executionPurpose"),
    ({"certificationEligible": True}, "certificationEligible"),
    ({"status": "FAIL"}, "disagreement"),
])
def test_each_mismatch_blocks(tmp_path, overrides, needle):
    path = _write(tmp_path, _api_report(**overrides))
    result = frv.validate_child_report(path, **EXPECT)
    assert not result.ok
    assert any(needle in p for p in result.problems), result.problems


def test_malformed_json_blocks(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    result = frv.validate_child_report(path, **EXPECT)
    assert not result.ok
    assert "not valid JSON" in result.problems[0]


def test_fail_exit_with_matching_fail_report_is_valid(tmp_path):
    path = _write(tmp_path, _api_report(status="FAIL"))
    result = frv.validate_child_report(path, **{**EXPECT, "child_exit_code": 1})
    assert result.ok, result.problems  # a proven product FAIL stands


def test_unsupported_report_type_blocks(tmp_path):
    path = _write(tmp_path, _api_report())
    result = frv.validate_child_report(path, **{**EXPECT, "expected_type": "unknown-type"})
    assert not result.ok


# ── verified-context construction ──────────────────────────────────────────
def _fixture_report(**overrides):
    doc = {
        "runId": "run-1", "fixtureVerificationStatus": "ok",
        "targetEnvironment": "https://staging.calee.invalid", "fixtureVersion": "REG-9",
    }
    doc.update(overrides)
    return doc


def test_context_builds_from_verified_same_run_report():
    ctx = focused_context.build_verified_context(
        _fixture_report(), run_id="run-1", release_id="rel-1",
        regression_shas={"calee-regression": "abc"},
    )
    assert ctx.backend == "https://staging.calee.invalid"
    assert ctx.fixture_version == "REG-9"
    with pytest.raises(Exception):
        ctx.backend = "mutated"  # frozen
    with pytest.raises(TypeError):
        ctx.regression_shas["x"] = "y"  # deep-frozen


@pytest.mark.parametrize("overrides", [
    {"runId": "another-run"},
    {"fixtureVerificationStatus": "blocked"},
    {"targetEnvironment": None},
    {"fixtureVersion": None},
])
def test_context_construction_blocks_on_missing_mandatory_evidence(overrides):
    with pytest.raises(focused_context.FocusedContextError):
        focused_context.build_verified_context(
            _fixture_report(**overrides), run_id="run-1", release_id="rel-1")


# ── vendored cross-repository contract ─────────────────────────────────────
def test_vendored_contract_loads_and_is_supported():
    contract = focused_contract.load_contract()
    assert contract["focusedContractVersion"] in focused_contract.SUPPORTED_CONTRACT_VERSIONS
    assert "chores-stop-repeating" in contract["apiSuites"]


def test_unsupported_contract_version_blocks(tmp_path):
    path = _write(tmp_path, {"contractType": "focused-execution-contract", "focusedContractVersion": 99})
    with pytest.raises(focused_contract.FocusedContractError):
        focused_contract.load_contract(path)


def test_renamed_api_suite_is_detected():
    contract = focused_contract.load_contract()
    problems = focused_contract.validate_focused_invocation(
        contract, api_suite="chores-stop-repeating-renamed",
        execution_purposes=["focused-environment-check"],
        ui_target="integration_test/app_boot_test.dart",
    )
    assert problems and "not in the mobile contract" in problems[0]


def test_orchestrator_options_are_all_declared_by_the_contract():
    contract = focused_contract.load_contract()
    for opt in ("--require-explicit-context", "--release-run-id", "--fixture-version",
                "--execution-purpose", "--base-url"):
        assert opt in contract["focusedApiContextOptions"]
    for opt in ("--expected-backend", "--mobile-backend", "--fixture-status",
                "--execution-purpose", "--device-id"):
        assert opt in contract["focusedUiContextOptions"]


def test_vendored_contract_matches_sibling_checkout_when_present():
    sibling = focused_contract.REPO_ROOT.parent / "CaleeMobile-Regression"
    module_path = sibling / "api" / "caleemobile_regression" / "focused_contract.py"
    if not module_path.is_file():
        pytest.skip("no sibling CaleeMobile-Regression checkout")
    import importlib.util

    spec = importlib.util.spec_from_file_location("mobile_focused_contract", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.describe_contract() == focused_contract.load_contract()
