"""CLI-level contract tests for `verify-main-ci-artifact` (Priority 6, this
session). Uses monkeypatch to inject fake fetchers through
main_ci_artifact.acquire_main_ci_artifact -- no real network in any test.
"""

from __future__ import annotations

import io
import json
import zipfile

from click.testing import CliRunner

from calee_regression import cli
from calee_regression import github_artifact as ga
from calee_regression import main_ci_artifact as mca
from calee_regression import main_ci_evidence as mce
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

MERGE_SHA = "25f47d3671cfd4b1311132a5ab9cb9344880d6cd"
RUN_ID = "111"
ARTIFACT_ID = "222"
REPO = mce.CALEEMOBILE_REGRESSION_REPOSITORY
WORKFLOW_PATH = mce.CALEEMOBILE_REGRESSION_WORKFLOW_FILE
ARTIFACT_NAME = f"ci-summary-{MERGE_SHA}"
RESULT_FILENAME = "ci-summary.json"


def _summary_json(**overrides) -> dict:
    data = {
        "schemaVersion": 1, "repository": REPO, "workflow": "ci", "workflowFile": WORKFLOW_PATH,
        "event": "push", "ref": "refs/heads/main", "commitSha": MERGE_SHA, "runId": RUN_ID,
        "runAttempt": "1", "isMainPush": True, "isMergeGroup": False,
        "gates": {gate: "success" for gate in mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES},
        "skipClassification": {}, "generatedAt": "2026-07-21T00:00:00Z",
    }
    data.update(overrides)
    return data


def _valid_zip(**overrides) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(RESULT_FILENAME, json.dumps(_summary_json(**overrides)))
    return buf.getvalue()


def _install_fake_acquisition(monkeypatch, zb, *, run_overrides=None, artifact_overrides=None):
    run_overrides = run_overrides or {}
    artifact_overrides = artifact_overrides or {}

    def _fake_acquire(**kwargs):
        run_data = {
            "id": int(RUN_ID), "repository": {"full_name": REPO}, "path": WORKFLOW_PATH,
            "name": "ci", "event": "push", "head_sha": MERGE_SHA, "head_branch": "main",
            "status": "completed", "conclusion": "success",
        }
        run_data.update(run_overrides)
        artifact_data = {
            "id": int(ARTIFACT_ID), "name": ARTIFACT_NAME, "expired": False,
            "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
            "workflow_run": {"id": int(RUN_ID)},
        }
        artifact_data.update(artifact_overrides)
        run = ga.WorkflowRunMetadata.from_api(run_data)
        artifact = ga.ArtifactMetadata.from_api(artifact_data)
        return mca.verify_main_ci_artifact_chain(
            run, artifact, zb,
            expected_repository=kwargs["repository"], expected_workflow_path=kwargs["workflow_path"],
            expected_merge_sha=kwargs["expected_merge_sha"],
            expected_artifact_name=kwargs["expected_artifact_name"],
            expected_result_filename=kwargs["expected_result_filename"],
            expected_run_id=kwargs["run_id"], expected_artifact_id=kwargs["artifact_id"],
            required_gates=kwargs.get("required_gates"),
            canonical_required_gates=kwargs.get("canonical_required_gates"),
        )

    monkeypatch.setattr(mca, "acquire_main_ci_artifact", _fake_acquire)


def _invoke(*args):
    return CliRunner().invoke(cli.main, ["verify-main-ci-artifact", *args])


def test_authenticated_artifact_accepted(monkeypatch):
    zb = _valid_zip()
    _install_fake_acquisition(monkeypatch, zb)
    result = _invoke(
        "--repository", REPO, "--workflow-run-id", RUN_ID, "--artifact-id", ARTIFACT_ID,
        "--expected-merge-sha", MERGE_SHA,
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Authenticated merged-main CI artifact verified" in result.output


def test_wrong_repository_run_blocks(monkeypatch):
    zb = _valid_zip()
    _install_fake_acquisition(monkeypatch, zb, run_overrides={"repository": {"full_name": "someone/else"}})
    result = _invoke(
        "--repository", REPO, "--workflow-run-id", RUN_ID, "--artifact-id", ARTIFACT_ID,
        "--expected-merge-sha", MERGE_SHA,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "repository" in result.output


def test_workflow_dispatch_run_blocks(monkeypatch):
    zb = _valid_zip()
    _install_fake_acquisition(monkeypatch, zb, run_overrides={"event": "workflow_dispatch"})
    result = _invoke(
        "--repository", REPO, "--workflow-run-id", RUN_ID, "--artifact-id", ARTIFACT_ID,
        "--expected-merge-sha", MERGE_SHA,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "push-to-main or merge_group" in result.output


def test_unrecognised_repository_without_overrides_is_invalid_config(monkeypatch):
    result = _invoke(
        "--repository", "someone/unknown-repo", "--workflow-run-id", RUN_ID, "--artifact-id", ARTIFACT_ID,
        "--expected-merge-sha", MERGE_SHA,
    )
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
    assert "--workflow-file" in result.output


def test_calee_regression_profile_resolves_without_overrides(monkeypatch):
    repo = "CaleeAdmin/calee-regression"
    workflow_path = ".github/workflows/framework-tests.yml"
    artifact_name = f"framework-test-summary-{MERGE_SHA}"
    result_filename = "framework-test-summary.json"

    def _fake_acquire(**kwargs):
        assert kwargs["workflow_path"] == workflow_path
        assert kwargs["expected_artifact_name"] == artifact_name
        assert kwargs["expected_result_filename"] == result_filename
        return mca.MainCiArtifactChain(ok=True, problems=[], result={"commitSha": MERGE_SHA})

    monkeypatch.setattr(mca, "acquire_main_ci_artifact", _fake_acquire)
    result = _invoke(
        "--repository", repo, "--workflow-run-id", RUN_ID, "--artifact-id", ARTIFACT_ID,
        "--expected-merge-sha", MERGE_SHA,
    )
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_no_credentials_blocks_naming_the_secret(monkeypatch):
    """The real (un-mocked) acquisition path is exercised here -- no fake
    fetchers installed -- to prove the CLI genuinely surfaces the
    credential-BLOCKED error rather than ever faking a pass."""
    monkeypatch.delenv("REGRESSION_API_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    result = _invoke(
        "--repository", REPO, "--workflow-run-id", RUN_ID, "--artifact-id", ARTIFACT_ID,
        "--expected-merge-sha", MERGE_SHA,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "REGRESSION_API_TOKEN" in result.output


def test_missing_gate_blocks_with_canonical_set_applied_automatically(monkeypatch):
    gates = dict(_summary_json()["gates"])
    del gates["releaseCertificationGuard"]
    zb = _valid_zip(gates=gates)
    _install_fake_acquisition(monkeypatch, zb)
    result = _invoke(
        "--repository", REPO, "--workflow-run-id", RUN_ID, "--artifact-id", ARTIFACT_ID,
        "--expected-merge-sha", MERGE_SHA,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "releaseCertificationGuard" in result.output
