"""CLI-level contract tests for `verify-main-ci-evidence` (Priority 8, this
session)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from calee_regression import cli
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

SHA = "a" * 40


def _write_summary(tmp_path, **overrides):
    data = dict(
        schemaVersion=1, repository="CaleeAdmin/calee-regression",
        workflowFile=".github/workflows/framework-tests.yml",
        workflow="framework-tests", event="push", ref="refs/heads/main",
        commitSha=SHA, runId="1", runAttempt="1", isMainPush=True, isMergeGroup=False,
        generatedAt="2026-07-21T00:00:00Z",
    )
    data.update(overrides)
    path = tmp_path / "framework-test-summary.json"
    path.write_text(json.dumps(data))
    return path


def _invoke(*args):
    return CliRunner().invoke(cli.main, ["verify-main-ci-evidence", *args])


def test_exact_merged_main_evidence_accepted(tmp_path):
    summary = _write_summary(tmp_path)
    result = _invoke("--expected-sha", SHA, "--summary", str(summary))
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "OK" in result.output


def test_merge_group_evidence_accepted(tmp_path):
    summary = _write_summary(tmp_path, event="merge_group", isMainPush=False, isMergeGroup=True)
    result = _invoke("--expected-sha", SHA, "--summary", str(summary))
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_pr_event_evidence_rejected_as_merged_main_evidence(tmp_path):
    summary = _write_summary(tmp_path, event="pull_request", ref="refs/pull/7/merge", isMainPush=False)
    result = _invoke("--expected-sha", SHA, "--summary", str(summary))
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "pull_request" in result.output


def test_wrong_sha_rejected(tmp_path):
    summary = _write_summary(tmp_path, commitSha="b" * 40)
    result = _invoke("--expected-sha", SHA, "--summary", str(summary))
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "NOT for the commit being verified" in result.output


def test_malformed_summary_file_is_invalid_config(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    result = _invoke("--expected-sha", SHA, "--summary", str(path))
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output


def test_missing_summary_file_errors(tmp_path):
    result = _invoke("--expected-sha", SHA, "--summary", str(tmp_path / "nope.json"))
    assert result.exit_code != EXIT_SUCCESS


def test_required_gate_checked_against_rich_summary(tmp_path):
    path = tmp_path / "ci-summary.json"
    path.write_text(json.dumps({
        "schemaVersion": 1, "commitSha": SHA, "event": "push", "ref": "refs/heads/main",
        "gates": {"apiFrameworkTests": "success", "selectorContract": "failure"},
        "skipClassification": {},
    }))
    result = _invoke("--expected-sha", SHA, "--summary", str(path), "--required-gate", "selectorContract")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "selectorContract" in result.output


def test_artifact_sha256_mismatch_rejected(tmp_path):
    summary = _write_summary(tmp_path)
    result = _invoke("--expected-sha", SHA, "--summary", str(summary), "--artifact-sha256", "0" * 64)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "digest mismatch" in result.output


def test_artifact_sha256_match_accepted(tmp_path):
    import hashlib

    summary = _write_summary(tmp_path)
    digest = hashlib.sha256(summary.read_bytes()).hexdigest()
    result = _invoke("--expected-sha", SHA, "--summary", str(summary), "--artifact-sha256", digest)
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_output_labels_structural_validation_only(tmp_path):
    summary = _write_summary(tmp_path)
    result = _invoke("--expected-sha", SHA, "--summary", str(summary))
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "STRUCTURAL VALIDATION ONLY" in result.output
    assert "ORIGIN NOT AUTHENTICATED" in result.output


def test_missing_schema_version_blocks(tmp_path):
    path = tmp_path / "ci-summary.json"
    path.write_text(json.dumps({
        "commitSha": SHA, "event": "push", "ref": "refs/heads/main",
    }))
    result = _invoke("--expected-sha", SHA, "--summary", str(path))
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "schemaVersion" in result.output


def test_expected_repository_applies_caleemobile_regression_canonical_gates(tmp_path):
    """--expected-repository CaleeAdmin/CaleeMobile-Regression must enforce
    the canonical gate set even though no --required-gate was passed."""
    path = tmp_path / "ci-summary.json"
    path.write_text(json.dumps({
        "schemaVersion": 1, "repository": "CaleeAdmin/CaleeMobile-Regression",
        "workflowFile": ".github/workflows/ci.yml", "workflow": "ci",
        "commitSha": SHA, "event": "push", "ref": "refs/heads/main",
        "gates": {}, "skipClassification": {},
    }))
    result = _invoke(
        "--expected-sha", SHA, "--summary", str(path),
        "--expected-repository", "CaleeAdmin/CaleeMobile-Regression",
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "apiFrameworkTests" in result.output


def test_expected_repository_mismatch_blocks(tmp_path):
    summary = _write_summary(tmp_path)
    result = _invoke(
        "--expected-sha", SHA, "--summary", str(summary),
        "--expected-repository", "CaleeAdmin/some-other-repo",
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "repository" in result.output
