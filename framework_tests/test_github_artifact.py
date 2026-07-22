"""Adversarial tests for the GitHub artifact authenticity chain (Priority 2).

Every rejection path in ``github_artifact`` is exercised: wrong repo/workflow/
event, a non-success selector job, a foreign/expired/mis-named artifact, a
digest/size mismatch, and every hardened-extraction failure (malformed ZIP,
path traversal, duplicate entries, extra files, oversized member). The happy
path is built from the *real* shapes observed on run 29641311999 so the tests
track reality, not an invented schema.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from calee_regression import github_artifact as ga

RUN_HEAD_SHA = "25f47d3671cfd4b1311132a5ab9cb9344880d6cd"
RUN_ID = "29641311999"
ARTIFACT_ID = "8428705832"


def _result_json(**overrides) -> dict:
    data = {
        "schemaVersion": 1,
        "component": "caleemobile-selector-contract",
        "caleemobileRef": "41c97a97eddaf8676d43bb5efd5b2018d51b7faa",
        "testedSha": "41c97a97eddaf8676d43bb5efd5b2018d51b7faa",
        "pubspecVersion": "0.0.24+24",
        "flutterVersion": "3.44.1",
        "contract": "PASS",
        "selectorsChecked": 62,
        "selectorsPresent": 62,
        "missing": [],
        "timestamp": "2026-07-18T10:43:47Z",
        "regressionSha": RUN_HEAD_SHA,
        "workflowRunId": RUN_ID,
        "generatedBy": "ci",
    }
    data.update(overrides)
    return data


def _zip_with(members: "dict[str, bytes]", *, duplicate: "tuple[str, bytes] | None" = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
        if duplicate is not None:
            zf.writestr(duplicate[0], duplicate[1])
    return buf.getvalue()


def _valid_zip(**overrides) -> bytes:
    body = json.dumps(_result_json(**overrides)).encode("utf-8")
    return _zip_with({ga.EXPECTED_RESULT_FILENAME: body})


def _run(**overrides) -> ga.WorkflowRunMetadata:
    base = dict(
        run_id=RUN_ID,
        repo_full_name=ga.EXPECTED_WORKFLOW_REPO,
        workflow_path=ga.EXPECTED_WORKFLOW_PATH,
        workflow_name="ci",
        event="workflow_dispatch",
        head_sha=RUN_HEAD_SHA,
        status="completed",
        conclusion="success",
    )
    base.update(overrides)
    return ga.WorkflowRunMetadata(**base)


def _jobs(conclusion: str = "success") -> "list[ga.JobMetadata]":
    return [
        ga.JobMetadata(name="API framework self-tests", status="completed", conclusion="success"),
        ga.JobMetadata(
            name="CaleeMobile selector contract (must pass before UI analysis)",
            status="completed",
            conclusion=conclusion,
        ),
    ]


def _artifact(zip_bytes: bytes, **overrides) -> ga.ArtifactMetadata:
    base = dict(
        artifact_id=ARTIFACT_ID,
        name=ga.EXPECTED_ARTIFACT_NAME,
        expired=False,
        size_in_bytes=len(zip_bytes),
        digest="sha256:" + ga.sha256_hex(zip_bytes),
        workflow_run_id=RUN_ID,
        archive_download_url="https://api.github.com/x/zip",
    )
    base.update(overrides)
    return ga.ArtifactMetadata(**base)


# --- happy path --------------------------------------------------------------


def test_valid_chain_accepts_and_preserves_raw_bytes():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(), _jobs(), _artifact(zb), zb,
        expected_regression_sha=RUN_HEAD_SHA,
        expected_tested_sha="41c97a97eddaf8676d43bb5efd5b2018d51b7faa",
        expected_version="0.0.24+24",
    )
    assert chain.ok, chain.problems
    # raw bytes retained + hashed (feeds Priority 3)
    assert chain.zip_bytes == zb
    assert chain.zip_sha256 == ga.sha256_hex(zb)
    assert chain.result_bytes is not None and chain.result_sha256 == ga.sha256_hex(chain.result_bytes)
    assert chain.result["testedSha"] == "41c97a97eddaf8676d43bb5efd5b2018d51b7faa"


# --- workflow-run rejections -------------------------------------------------


def test_rejects_wrong_repository():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(repo_full_name="attacker/evil"), _jobs(), _artifact(zb), zb)
    assert not chain.ok
    assert any("repository" in p for p in chain.problems)


def test_rejects_wrong_workflow_path_and_name():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(workflow_path=".github/workflows/other.yml", workflow_name="other"), _jobs(), _artifact(zb), zb
    )
    assert not chain.ok
    assert any("workflow" in p for p in chain.problems)


def test_rejects_when_only_name_matches_but_path_missing():
    # P7.1: the workflow name "ci" is diagnostic only and never substitutes for
    # the path. A run with no path is refused even when it is named "ci".
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(workflow_path=None, workflow_name="ci"), _jobs(), _artifact(zb), zb,
        expected_regression_sha=RUN_HEAD_SHA,
    )
    assert not chain.ok
    assert any("workflow path" in p for p in chain.problems)


def test_rejects_same_repo_wrong_workflow_named_ci():
    # P7.9: same repository, a DIFFERENT workflow file that is also named "ci".
    # The name must not launder a foreign workflow past the path check.
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(workflow_path=".github/workflows/not-ci.yml", workflow_name="ci"),
        _jobs(), _artifact(zb), zb, expected_regression_sha=RUN_HEAD_SHA,
    )
    assert not chain.ok
    assert any("workflow path" in p for p in chain.problems)


@pytest.mark.parametrize("event", ["push", "pull_request", "schedule"])
def test_rejects_non_dispatch_event(event):
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(event=event), _jobs(), _artifact(zb), zb)
    assert not chain.ok
    assert any("dispatch event" in p for p in chain.problems)


def test_rejects_head_sha_mismatch_with_evidence():
    other = "b" * 40
    zb = _valid_zip(regressionSha=other)  # evidence claims a different regressionSha than run head
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), _artifact(zb), zb)
    assert not chain.ok
    assert any("regressionSha" in p and "head_sha" in p for p in chain.problems)


def test_rejects_incomplete_run():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(status="in_progress"), _jobs(), _artifact(zb), zb)
    assert not chain.ok
    assert any("not completed" in p for p in chain.problems)


# --- job rejections ----------------------------------------------------------


def test_rejects_selector_job_failure():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(), _jobs(conclusion="failure"), _artifact(zb), zb)
    assert not chain.ok
    assert any("selector-contract job did not conclude success" in p for p in chain.problems)


def test_rejects_missing_selector_job():
    zb = _valid_zip()
    jobs = [ga.JobMetadata(name="API framework self-tests", conclusion="success")]
    chain = ga.verify_github_artifact_chain(_run(), jobs, _artifact(zb), zb)
    assert not chain.ok
    assert any("no selector-contract job" in p for p in chain.problems)


# --- artifact rejections -----------------------------------------------------


def test_rejects_foreign_artifact():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), _artifact(zb, workflow_run_id="99999"), zb)
    assert not chain.ok
    assert any("belongs to run" in p for p in chain.problems)


def test_rejects_wrong_artifact_name():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), _artifact(zb, name="something-else"), zb)
    assert not chain.ok
    assert any("artifact name" in p for p in chain.problems)


def test_rejects_expired_artifact():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), _artifact(zb, expired=True), zb)
    assert not chain.ok
    assert any("expired" in p for p in chain.problems)


def test_rejects_missing_digest():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), _artifact(zb, digest=None), zb)
    assert not chain.ok
    assert any("no GitHub digest" in p for p in chain.problems)


def test_rejects_digest_mismatch():
    zb = _valid_zip()
    bad = ga.ArtifactMetadata(
        artifact_id=ARTIFACT_ID, name=ga.EXPECTED_ARTIFACT_NAME, expired=False,
        size_in_bytes=len(zb), digest="sha256:" + ("0" * 64), workflow_run_id=RUN_ID,
    )
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), bad, zb)
    assert not chain.ok
    assert any("do not match what GitHub stored" in p for p in chain.problems)


def test_rejects_size_mismatch():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), _artifact(zb, size_in_bytes=len(zb) + 10), zb)
    assert not chain.ok
    assert any("incomplete or altered" in p for p in chain.problems)


# --- hardened extraction -----------------------------------------------------


def test_extract_rejects_malformed_zip():
    with pytest.raises(ga.GithubArtifactError, match="not a valid ZIP"):
        ga.extract_single_result(b"this is not a zip")


def test_extract_rejects_path_traversal():
    zb = _zip_with({"../../etc/passwd": b"{}"})
    with pytest.raises(ga.GithubArtifactError, match="unsafe"):
        ga.extract_single_result(zb)


def test_extract_rejects_absolute_path():
    zb = _zip_with({"/etc/passwd": b"{}"})
    with pytest.raises(ga.GithubArtifactError, match="unsafe"):
        ga.extract_single_result(zb)


def test_extract_rejects_nested_directory_member():
    zb = _zip_with({"sub/dir/result.json": b"{}"})
    with pytest.raises(ga.GithubArtifactError, match="unsafe"):
        ga.extract_single_result(zb)


def test_extract_rejects_duplicate_entries():
    zb = _zip_with(
        {ga.EXPECTED_RESULT_FILENAME: b'{"a":1}'},
        duplicate=(ga.EXPECTED_RESULT_FILENAME, b'{"a":2}'),
    )
    with pytest.raises(ga.GithubArtifactError, match="duplicate"):
        ga.extract_single_result(zb)


def test_extract_rejects_extra_files():
    zb = _zip_with({ga.EXPECTED_RESULT_FILENAME: b"{}", "README.txt": b"hi"})
    with pytest.raises(ga.GithubArtifactError, match="exactly one file"):
        ga.extract_single_result(zb)


def test_extract_rejects_missing_expected_file():
    zb = _zip_with({"unexpected.json": b"{}"})
    with pytest.raises(ga.GithubArtifactError, match="exactly one file"):
        ga.extract_single_result(zb)


def test_extract_rejects_oversized_member():
    big = b"{}" + b" " * (ga.MAX_EXTRACTED_MEMBER_BYTES + 5)
    zb = _zip_with({ga.EXPECTED_RESULT_FILENAME: big})
    with pytest.raises(ga.GithubArtifactError, match="limit"):
        ga.extract_single_result(zb)


def test_extract_rejects_non_object_json():
    zb = _zip_with({ga.EXPECTED_RESULT_FILENAME: b"[1,2,3]"})
    with pytest.raises(ga.GithubArtifactError, match="not a JSON object"):
        ga.extract_single_result(zb)


def test_malformed_zip_surfaces_as_problem_in_chain():
    bad_zip = b"not a zip at all"
    # The oversized/ digest checks may also fire; the point is chain is not ok
    # and the malformed-zip problem is surfaced (not raised out of the verdict).
    art = ga.ArtifactMetadata(
        artifact_id=ARTIFACT_ID, name=ga.EXPECTED_ARTIFACT_NAME, expired=False,
        size_in_bytes=len(bad_zip), digest="sha256:" + ga.sha256_hex(bad_zip), workflow_run_id=RUN_ID,
    )
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), art, bad_zip)
    assert not chain.ok
    assert any("valid ZIP" in p for p in chain.problems)


# --- live acquisition: BLOCKED without credentials, and the id requirements --


def test_acquire_requires_run_id():
    with pytest.raises(ga.GithubArtifactError, match="run id"):
        ga.acquire_github_artifact(run_id=None, artifact_id=ARTIFACT_ID, env={})


def test_acquire_requires_artifact_id():
    with pytest.raises(ga.GithubArtifactError, match="artifact id"):
        ga.acquire_github_artifact(run_id=RUN_ID, artifact_id=None, env={})


def test_acquire_blocks_without_token_naming_the_secret():
    with pytest.raises(ga.GithubArtifactError, match="REGRESSION_API_TOKEN"):
        ga.acquire_github_artifact(run_id=RUN_ID, artifact_id=ARTIFACT_ID, env={})


def test_acquire_with_injected_fetchers_verifies_end_to_end():
    zb = _valid_zip()

    def json_fetcher(url: str) -> dict:
        if url.endswith(f"/runs/{RUN_ID}"):
            return {
                "id": int(RUN_ID),
                "repository": {"full_name": ga.EXPECTED_WORKFLOW_REPO},
                "path": ga.EXPECTED_WORKFLOW_PATH,
                "name": "ci",
                "event": "workflow_dispatch",
                "head_sha": RUN_HEAD_SHA,
                "status": "completed",
                "conclusion": "success",
            }
        if url.endswith(f"/runs/{RUN_ID}/jobs"):
            return {"jobs": [
                {"name": "CaleeMobile selector contract (must pass before UI analysis)",
                 "status": "completed", "conclusion": "success"},
            ]}
        if url.endswith(f"/artifacts/{ARTIFACT_ID}"):
            return {
                "id": int(ARTIFACT_ID), "name": ga.EXPECTED_ARTIFACT_NAME, "expired": False,
                "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
                "workflow_run": {"id": int(RUN_ID)},
                "archive_download_url": "https://api.github.com/x/zip",
            }
        raise AssertionError(f"unexpected url {url}")

    def bytes_fetcher(url: str) -> bytes:
        assert "zip" in url
        return zb

    chain = ga.acquire_github_artifact(
        run_id=RUN_ID, artifact_id=ARTIFACT_ID,
        expected_regression_sha=RUN_HEAD_SHA, expected_version="0.0.24+24",
        json_fetcher=json_fetcher, bytes_fetcher=bytes_fetcher, token="fake",
    )
    assert chain.ok, chain.problems
    assert chain.result["pubspecVersion"] == "0.0.24+24"


def test_acquire_local_zip_authenticates_metadata_without_redirect_download(tmp_path):
    """A cached ZIP must still pass GitHub metadata/digest checks, but never fetch its archive URL."""
    zb = _valid_zip(releaseId="release-42")
    cached_zip = tmp_path / "selector-contract-result.zip"
    cached_zip.write_bytes(zb)

    def json_fetcher(url: str) -> dict:
        if url.endswith(f"/runs/{RUN_ID}"):
            return {"id": int(RUN_ID), "repository": {"full_name": ga.EXPECTED_WORKFLOW_REPO},
                    "path": ga.EXPECTED_WORKFLOW_PATH, "event": "workflow_dispatch", "head_sha": RUN_HEAD_SHA,
                    "status": "completed", "conclusion": "success"}
        if url.endswith(f"/runs/{RUN_ID}/jobs"):
            return {"jobs": [{"name": "CaleeMobile selector contract", "status": "completed", "conclusion": "success"}]}
        if url.endswith(f"/artifacts/{ARTIFACT_ID}"):
            return {"id": int(ARTIFACT_ID), "name": ga.EXPECTED_ARTIFACT_NAME, "expired": False,
                    "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
                    "workflow_run": {"id": int(RUN_ID)}, "archive_download_url": "https://redirect.example/zip"}
        raise AssertionError(f"unexpected metadata URL {url}")

    def redirect_download_must_not_run(url: str) -> bytes:
        raise AssertionError(f"redirect download should not be attempted: {url}")

    chain = ga.acquire_github_artifact(
        run_id=RUN_ID, artifact_id=ARTIFACT_ID, local_zip_path=str(cached_zip),
        expected_regression_sha=RUN_HEAD_SHA, expected_release_id="release-42",
        json_fetcher=json_fetcher, bytes_fetcher=redirect_download_must_not_run, token="fake",
    )
    assert chain.ok, chain.problems


def test_acquire_rejects_local_zip_for_another_release(tmp_path):
    zb = _valid_zip(releaseId="other-release")
    cached_zip = tmp_path / "selector-contract-result.zip"
    cached_zip.write_bytes(zb)
    run = _run()
    artifact = _artifact(zb)

    responses = {
        f"/runs/{RUN_ID}": {"id": int(RUN_ID), "repository": {"full_name": run.repo_full_name},
                            "path": run.workflow_path, "event": run.event, "head_sha": run.head_sha,
                            "status": run.status, "conclusion": run.conclusion},
        f"/runs/{RUN_ID}/jobs": {"jobs": [{"name": "selector contract", "status": "completed", "conclusion": "success"}]},
        f"/artifacts/{ARTIFACT_ID}": {"id": int(ARTIFACT_ID), "name": artifact.name, "expired": False,
                                      "size_in_bytes": len(zb), "digest": artifact.digest,
                                      "workflow_run": {"id": int(RUN_ID)}},
    }
    chain = ga.acquire_github_artifact(
        run_id=RUN_ID, artifact_id=ARTIFACT_ID, local_zip_path=str(cached_zip), expected_release_id="release-42",
        json_fetcher=lambda url: next(value for suffix, value in responses.items() if url.endswith(suffix)),
        bytes_fetcher=lambda _url: pytest.fail("local ZIP must prevent archive download"), token="fake",
    )
    assert not chain.ok
    assert any("releaseId" in problem for problem in chain.problems)


# --- P7 hardening: adversarial rejections -----------------------------------


def test_rejects_successful_selector_job_in_failed_overall_run():
    # P7.3/P7.9: the selector-contract job succeeded but the OVERALL run failed
    # (another job failed). Production evidence requires the run to conclude
    # success, not just the one job.
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(conclusion="failure"), _jobs(conclusion="success"), _artifact(zb), zb,
        expected_regression_sha=RUN_HEAD_SHA,
    )
    assert not chain.ok
    assert any("conclusion" in p and "success" in p for p in chain.problems)


def test_rejects_run_missing_conclusion():
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(conclusion=None), _jobs(), _artifact(zb), zb, expected_regression_sha=RUN_HEAD_SHA,
    )
    assert not chain.ok
    assert any("conclusion" in p for p in chain.problems)


def test_rejects_two_selector_like_jobs():
    # P7.4/P7.9: two jobs whose names match the selector marker are ambiguous.
    zb = _valid_zip()
    jobs = [
        ga.JobMetadata(name="CaleeMobile selector contract (must pass before UI analysis)",
                       status="completed", conclusion="success"),
        ga.JobMetadata(name="CaleeMobile selector contract (shadow copy)",
                       status="completed", conclusion="failure"),
    ]
    chain = ga.verify_github_artifact_chain(_run(), jobs, _artifact(zb), zb,
                                            expected_regression_sha=RUN_HEAD_SHA)
    assert not chain.ok
    assert any("multiple selector-contract jobs" in p for p in chain.problems)


def test_rejects_selector_job_not_completed():
    zb = _valid_zip()
    jobs = [
        ga.JobMetadata(name="CaleeMobile selector contract (must pass before UI analysis)",
                       status="in_progress", conclusion=None),
    ]
    chain = ga.verify_github_artifact_chain(_run(), jobs, _artifact(zb), zb,
                                            expected_regression_sha=RUN_HEAD_SHA)
    assert not chain.ok
    assert any("has not completed" in p for p in chain.problems)


def test_rejects_artifact_missing_run_ownership():
    # P7.5/P7.9: artifact metadata that does not record its workflow_run id is
    # refused -- a missing relationship is not accepted.
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(), _jobs(), _artifact(zb, workflow_run_id=None), zb,
        expected_regression_sha=RUN_HEAD_SHA,
    )
    assert not chain.ok
    assert any("does not record its workflow_run id" in p for p in chain.problems)


def test_rejects_evidence_missing_workflow_run_id():
    # P7.6/P7.9: the extracted JSON has no workflowRunId.
    body = json.dumps({k: v for k, v in _result_json().items() if k != "workflowRunId"}).encode()
    zb = _zip_with({ga.EXPECTED_RESULT_FILENAME: body})
    chain = ga.verify_github_artifact_chain(_run(), _jobs(), _artifact(zb), zb,
                                            expected_regression_sha=RUN_HEAD_SHA)
    assert not chain.ok
    assert any("no workflowRunId" in p for p in chain.problems)


def test_rejects_evidence_short_regression_sha():
    # P7.7: regressionSha must be a full 40-char SHA.
    zb = _valid_zip(regressionSha="25f47d3")
    chain = ga.verify_github_artifact_chain(_run(head_sha="25f47d3"), _jobs(), _artifact(zb), zb)
    assert not chain.ok
    assert any("not a full 40-character SHA" in p for p in chain.problems)


def test_rejects_repository_dispatch_without_legacy_mode():
    # P7.2/P7.9: repository_dispatch evidence is refused by default.
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(event="repository_dispatch"), _jobs(), _artifact(zb), zb,
        expected_regression_sha=RUN_HEAD_SHA,
    )
    assert not chain.ok
    assert any("dispatch event" in p for p in chain.problems)


def test_accepts_repository_dispatch_in_explicit_legacy_mode():
    # P7.2: repository_dispatch is accepted ONLY when legacy mode is explicit.
    zb = _valid_zip()
    chain = ga.verify_github_artifact_chain(
        _run(event="repository_dispatch"), _jobs(), _artifact(zb), zb,
        expected_regression_sha=RUN_HEAD_SHA, allow_legacy_repository_dispatch=True,
    )
    assert chain.ok, chain.problems


def test_acquire_repository_dispatch_blocked_without_legacy_mode():
    # P7.2/P7.9 at the acquisition layer: a repository_dispatch run does not
    # authenticate unless legacy mode is explicitly requested.
    zb = _valid_zip()

    def json_fetcher(url: str) -> dict:
        if url.endswith(f"/runs/{RUN_ID}"):
            return {
                "id": int(RUN_ID), "repository": {"full_name": ga.EXPECTED_WORKFLOW_REPO},
                "path": ga.EXPECTED_WORKFLOW_PATH, "name": "ci", "event": "repository_dispatch",
                "head_sha": RUN_HEAD_SHA, "status": "completed", "conclusion": "success",
            }
        if url.endswith(f"/runs/{RUN_ID}/jobs"):
            return {"jobs": [{"name": "CaleeMobile selector contract (must pass before UI analysis)",
                              "status": "completed", "conclusion": "success"}]}
        if url.endswith(f"/artifacts/{ARTIFACT_ID}"):
            return {"id": int(ARTIFACT_ID), "name": ga.EXPECTED_ARTIFACT_NAME, "expired": False,
                    "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
                    "workflow_run": {"id": int(RUN_ID)}, "archive_download_url": "https://api.github.com/x/zip"}
        raise AssertionError(f"unexpected url {url}")

    def bytes_fetcher(url: str) -> bytes:
        return zb

    blocked = ga.acquire_github_artifact(
        run_id=RUN_ID, artifact_id=ARTIFACT_ID, expected_regression_sha=RUN_HEAD_SHA,
        json_fetcher=json_fetcher, bytes_fetcher=bytes_fetcher, token="fake",
    )
    assert not blocked.ok
    assert any("dispatch event" in p for p in blocked.problems)

    allowed = ga.acquire_github_artifact(
        run_id=RUN_ID, artifact_id=ARTIFACT_ID, expected_regression_sha=RUN_HEAD_SHA,
        json_fetcher=json_fetcher, bytes_fetcher=bytes_fetcher, token="fake",
        allow_legacy_repository_dispatch=True,
    )
    assert allowed.ok, allowed.problems
