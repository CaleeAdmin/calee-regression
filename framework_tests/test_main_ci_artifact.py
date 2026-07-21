"""Adversarial tests for authenticated merged-main CI artifact verification
(Priority 6, this session).

Mirrors test_github_artifact.py's pattern (fake WorkflowRunMetadata/
ArtifactMetadata + hand-built ZIPs for the pure core; injected fetchers for
the acquisition layer -- no real network in any test).
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from calee_regression import github_artifact as ga
from calee_regression import main_ci_artifact as mca
from calee_regression import main_ci_evidence as mce

MERGE_SHA = "25f47d3671cfd4b1311132a5ab9cb9344880d6cd"
RUN_ID = "29641311999"
ARTIFACT_ID = "8428705832"
REPO = mce.CALEEMOBILE_REGRESSION_REPOSITORY
WORKFLOW_PATH = mce.CALEEMOBILE_REGRESSION_WORKFLOW_FILE
ARTIFACT_NAME = f"ci-summary-{MERGE_SHA}"
RESULT_FILENAME = "ci-summary.json"


def _summary_json(**overrides) -> dict:
    data = {
        "schemaVersion": 1,
        "repository": REPO,
        "workflow": "ci",
        "workflowFile": WORKFLOW_PATH,
        "event": "push",
        "ref": "refs/heads/main",
        "commitSha": MERGE_SHA,
        "runId": RUN_ID,
        "runAttempt": "1",
        "isMainPush": True,
        "isMergeGroup": False,
        "gates": {gate: "success" for gate in mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES},
        "skipClassification": {},
        "generatedAt": "2026-07-21T00:00:00Z",
    }
    data.update(overrides)
    return data


def _zip_with(members: "dict[str, bytes]") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _valid_zip(**overrides) -> bytes:
    body = json.dumps(_summary_json(**overrides)).encode("utf-8")
    return _zip_with({RESULT_FILENAME: body})


def _run(**overrides) -> ga.WorkflowRunMetadata:
    base = dict(
        run_id=RUN_ID, repo_full_name=REPO, workflow_path=WORKFLOW_PATH, workflow_name="ci",
        event="push", head_sha=MERGE_SHA, head_branch="main", status="completed", conclusion="success",
    )
    base.update(overrides)
    return ga.WorkflowRunMetadata(**base)


def _artifact(zip_bytes: bytes, **overrides) -> ga.ArtifactMetadata:
    base = dict(
        artifact_id=ARTIFACT_ID, name=ARTIFACT_NAME, expired=False, size_in_bytes=len(zip_bytes),
        digest="sha256:" + ga.sha256_hex(zip_bytes), workflow_run_id=RUN_ID,
        archive_download_url="https://api.github.com/x/zip",
    )
    base.update(overrides)
    return ga.ArtifactMetadata(**base)


def _verify(zb, run=None, artifact=None, **kwargs):
    kwargs.setdefault("expected_repository", REPO)
    kwargs.setdefault("expected_workflow_path", WORKFLOW_PATH)
    kwargs.setdefault("expected_merge_sha", MERGE_SHA)
    kwargs.setdefault("expected_artifact_name", ARTIFACT_NAME)
    kwargs.setdefault("expected_result_filename", RESULT_FILENAME)
    kwargs.setdefault("canonical_required_gates", mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES)
    return mca.verify_main_ci_artifact_chain(
        run or _run(), artifact or _artifact(zb), zb, **kwargs,
    )


# --- happy path --------------------------------------------------------------


def test_valid_chain_accepted_on_push_to_main():
    zb = _valid_zip()
    chain = _verify(zb)
    assert chain.ok, chain.problems
    assert chain.result["commitSha"] == MERGE_SHA
    assert chain.zip_sha256 == ga.sha256_hex(zb)
    assert chain.result_bytes is not None


def test_valid_chain_accepted_on_merge_group():
    zb = _valid_zip(event="merge_group", ref="refs/heads/main", isMainPush=False, isMergeGroup=True)
    chain = _verify(zb, run=_run(event="merge_group"))
    assert chain.ok, chain.problems


# --- Priority 10: authenticated head_branch cross-check ---------------------


def test_push_run_with_wrong_head_branch_rejected():
    """The run's OWN authenticated head_branch (from GitHub's run resource,
    independent of the extracted evidence's self-reported ref) must agree
    with it being a main-branch push."""
    zb = _valid_zip()
    chain = _verify(zb, run=_run(head_branch="some-other-branch"))
    assert not chain.ok
    assert any("head_branch" in p and "!= expected" in p for p in chain.problems)


def test_push_run_with_missing_head_branch_rejected():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(head_branch=None))
    assert not chain.ok
    assert any("no head_branch recorded" in p for p in chain.problems)


def test_merge_group_run_does_not_require_head_branch_to_equal_main():
    """A merge_group run's head_branch names GitHub's synthetic merge-queue
    ref, never plain 'main' -- this must not be exact-matched against
    'main', unlike a push."""
    zb = _valid_zip(event="merge_group", ref="refs/heads/main", isMainPush=False, isMergeGroup=True)
    chain = _verify(zb, run=_run(event="merge_group", head_branch="gh-readonly-queue/main/pr-1-abc"))
    assert chain.ok, chain.problems


# --- P6 hardening: origin-authentication adversarial rejections -------------


def test_wrong_repository_rejected():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(repo_full_name="someone-else/CaleeMobile-Regression"))
    assert not chain.ok
    assert any("repository" in p for p in chain.problems)


def test_workflow_dispatch_event_rejected():
    """The core defect this closes: a workflow_dispatch (or PR) run's
    evidence is NOT proof about what landed on main, even if everything else
    about the run is perfectly legitimate."""
    zb = _valid_zip()
    chain = _verify(zb, run=_run(event="workflow_dispatch"))
    assert not chain.ok
    assert any("push-to-main or merge_group" in p for p in chain.problems)


def test_pull_request_event_rejected():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(event="pull_request"))
    assert not chain.ok
    assert any("push-to-main or merge_group" in p for p in chain.problems)


def test_wrong_workflow_path_rejected():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(workflow_path=".github/workflows/other.yml"))
    assert not chain.ok
    assert any("workflow path" in p for p in chain.problems)


def test_workflow_name_does_not_substitute_for_path():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(workflow_path=None, workflow_name="ci"))
    assert not chain.ok
    assert any("workflow path" in p for p in chain.problems)


def test_run_conclusion_failure_rejected():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(conclusion="failure"))
    assert not chain.ok
    assert any("conclusion" in p for p in chain.problems)


def test_run_not_completed_rejected():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(status="in_progress"))
    assert not chain.ok
    assert any("not completed" in p for p in chain.problems)


def test_wrong_head_sha_rejected():
    zb = _valid_zip()
    chain = _verify(zb, run=_run(head_sha="b" * 40))
    assert not chain.ok
    assert any("head_sha" in p and "expected merge SHA" in p for p in chain.problems)


def test_artifact_not_owned_by_run_rejected():
    zb = _valid_zip()
    chain = _verify(zb, artifact=_artifact(zb, workflow_run_id="99999999999"))
    assert not chain.ok
    assert any("belongs to run" in p for p in chain.problems)


def test_artifact_missing_workflow_run_id_rejected():
    zb = _valid_zip()
    chain = _verify(zb, artifact=_artifact(zb, workflow_run_id=None))
    assert not chain.ok
    assert any("does not record its workflow_run id" in p for p in chain.problems)


def test_artifact_name_without_exact_sha_rejected():
    zb = _valid_zip()
    chain = _verify(zb, artifact=_artifact(zb, name="ci-summary-latest"))
    assert not chain.ok
    assert any("artifact name" in p for p in chain.problems)


def test_artifact_expired_rejected():
    zb = _valid_zip()
    chain = _verify(zb, artifact=_artifact(zb, expired=True))
    assert not chain.ok
    assert any("expired" in p for p in chain.problems)


def test_artifact_missing_digest_rejected():
    zb = _valid_zip()
    chain = _verify(zb, artifact=_artifact(zb, digest=None))
    assert not chain.ok
    assert any("no GitHub digest" in p for p in chain.problems)


def test_digest_mismatch_rejected():
    zb = _valid_zip()
    chain = _verify(zb, artifact=_artifact(zb, digest="sha256:" + "0" * 64))
    assert not chain.ok
    assert any("digest" in p and "do not match" in p for p in chain.problems)


def test_size_mismatch_rejected():
    zb = _valid_zip()
    chain = _verify(zb, artifact=_artifact(zb, size_in_bytes=len(zb) + 1))
    assert not chain.ok
    assert any("size_in_bytes" in p for p in chain.problems)


# --- hardened extraction is genuinely reused (a couple of spot checks) -----


def test_extra_file_in_zip_rejected():
    zb = _zip_with({RESULT_FILENAME: json.dumps(_summary_json()).encode(), "extra.txt": b"nope"})
    chain = _verify(zb, artifact=_artifact(zb))
    assert not chain.ok
    assert any("exactly one file" in p for p in chain.problems)


def test_malformed_zip_rejected():
    zb = b"not a zip file at all"
    chain = _verify(zb, artifact=_artifact(zb))
    assert not chain.ok
    assert any("not a valid ZIP" in p for p in chain.problems)


# --- content/schema/gate composition (Priority 5's canonical verifier) -----


def test_missing_canonical_gate_in_extracted_summary_rejected():
    gates = dict(_summary_json()["gates"])
    del gates["selectorContract"]
    zb = _valid_zip(gates=gates)
    chain = _verify(zb, artifact=_artifact(zb))
    assert not chain.ok
    assert any("selectorContract" in p and "not present" in p for p in chain.problems)


def test_empty_gates_in_extracted_summary_rejected():
    zb = _valid_zip(gates={})
    chain = _verify(zb, artifact=_artifact(zb))
    assert not chain.ok
    assert any("empty" in p for p in chain.problems)


def test_failed_gate_in_extracted_summary_rejected():
    gates = dict(_summary_json()["gates"])
    gates["apiFrameworkTests"] = "failure"
    zb = _valid_zip(gates=gates)
    chain = _verify(zb, artifact=_artifact(zb))
    assert not chain.ok
    assert any("apiFrameworkTests" in p and "did not succeed" in p for p in chain.problems)


def test_unsupported_schema_version_in_extracted_summary_rejected():
    zb = _valid_zip(schemaVersion=999)
    chain = _verify(zb, artifact=_artifact(zb))
    assert not chain.ok
    assert any("schemaVersion" in p and "not supported" in p for p in chain.problems)


def test_repository_mismatch_in_extracted_summary_rejected():
    zb = _valid_zip(repository="someone-else/other-repo")
    chain = _verify(zb, artifact=_artifact(zb))
    assert not chain.ok
    assert any("repository" in p for p in chain.problems)


# --- acquisition layer: BLOCKED without credentials, never faked -----------


def test_acquire_requires_run_id():
    with pytest.raises(mca.MainCiArtifactError, match="run id"):
        mca.acquire_main_ci_artifact(
            repository=REPO, workflow_path=WORKFLOW_PATH, run_id=None, artifact_id=ARTIFACT_ID,
            expected_merge_sha=MERGE_SHA, expected_artifact_name=ARTIFACT_NAME,
            expected_result_filename=RESULT_FILENAME, env={},
        )


def test_acquire_requires_artifact_id():
    with pytest.raises(mca.MainCiArtifactError, match="artifact id"):
        mca.acquire_main_ci_artifact(
            repository=REPO, workflow_path=WORKFLOW_PATH, run_id=RUN_ID, artifact_id=None,
            expected_merge_sha=MERGE_SHA, expected_artifact_name=ARTIFACT_NAME,
            expected_result_filename=RESULT_FILENAME, env={},
        )


def test_acquire_blocks_without_token_naming_the_secret():
    with pytest.raises(mca.MainCiArtifactError, match="REGRESSION_API_TOKEN"):
        mca.acquire_main_ci_artifact(
            repository=REPO, workflow_path=WORKFLOW_PATH, run_id=RUN_ID, artifact_id=ARTIFACT_ID,
            expected_merge_sha=MERGE_SHA, expected_artifact_name=ARTIFACT_NAME,
            expected_result_filename=RESULT_FILENAME, env={},
        )


def test_acquire_never_contacts_real_network_uses_injected_fetchers_end_to_end():
    zb = _valid_zip()

    def json_fetcher(url: str) -> dict:
        if url.endswith(f"/runs/{RUN_ID}"):
            return {
                "id": int(RUN_ID), "repository": {"full_name": REPO}, "path": WORKFLOW_PATH,
                "name": "ci", "event": "push", "head_sha": MERGE_SHA, "head_branch": "main",
                "status": "completed", "conclusion": "success",
            }
        if url.endswith(f"/artifacts/{ARTIFACT_ID}"):
            return {
                "id": int(ARTIFACT_ID), "name": ARTIFACT_NAME, "expired": False,
                "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
                "workflow_run": {"id": int(RUN_ID)}, "archive_download_url": "https://api.github.com/x/zip",
            }
        raise AssertionError(f"unexpected url {url}")

    def bytes_fetcher(url: str) -> bytes:
        assert "zip" in url
        return zb

    chain = mca.acquire_main_ci_artifact(
        repository=REPO, workflow_path=WORKFLOW_PATH, run_id=RUN_ID, artifact_id=ARTIFACT_ID,
        expected_merge_sha=MERGE_SHA, expected_artifact_name=ARTIFACT_NAME,
        expected_result_filename=RESULT_FILENAME, json_fetcher=json_fetcher, bytes_fetcher=bytes_fetcher,
        token="fake", canonical_required_gates=mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES,
    )
    assert chain.ok, chain.problems
    assert chain.result["commitSha"] == MERGE_SHA


def test_acquire_with_local_zip_path_still_authenticates_metadata(tmp_path):
    """--artifact-zip (an already-downloaded ZIP) skips the bytes fetch, but
    metadata (run/artifact ownership, digest) is still authenticated via the
    API -- an operator-supplied ZIP alone is never sufficient."""
    zb = _valid_zip()
    zip_path = tmp_path / "ci-summary.zip"
    zip_path.write_bytes(zb)

    def json_fetcher(url: str) -> dict:
        if url.endswith(f"/runs/{RUN_ID}"):
            return {
                "id": int(RUN_ID), "repository": {"full_name": REPO}, "path": WORKFLOW_PATH,
                "name": "ci", "event": "push", "head_sha": MERGE_SHA, "head_branch": "main",
                "status": "completed", "conclusion": "success",
            }
        if url.endswith(f"/artifacts/{ARTIFACT_ID}"):
            return {
                "id": int(ARTIFACT_ID), "name": ARTIFACT_NAME, "expired": False,
                "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
                "workflow_run": {"id": int(RUN_ID)},
            }
        raise AssertionError(f"unexpected url {url}")

    def bytes_fetcher(url: str) -> bytes:  # pragma: no cover - must not be called
        raise AssertionError("bytes_fetcher must not be called when a local ZIP path is given")

    chain = mca.acquire_main_ci_artifact(
        repository=REPO, workflow_path=WORKFLOW_PATH, run_id=RUN_ID, artifact_id=ARTIFACT_ID,
        expected_merge_sha=MERGE_SHA, expected_artifact_name=ARTIFACT_NAME,
        expected_result_filename=RESULT_FILENAME, local_zip_path=str(zip_path),
        json_fetcher=json_fetcher, bytes_fetcher=bytes_fetcher, token="fake",
        canonical_required_gates=mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES,
    )
    assert chain.ok, chain.problems
