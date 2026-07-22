"""Offline tests for exact-identity release evidence acquisition
(calee_regression/evidence_acquisition.py).

Everything here is offline: a FakeGithubClient stands in for the GitHub REST
API, provider evidence is produced by injected fake collectors, and the
"verified release bundle" is injected as a stub verification so no APKs are
needed. The rules under test are the session's acceptance criteria: exact
matching only (never "latest successful run"), fail-closed on zero/ambiguous/
expired/unauthenticated evidence, digest authentication, hardened run-scoped
caching, and a secret-free acquisition manifest.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from calee_regression import evidence_acquisition as ea
from calee_regression import github_artifact as ga
from calee_regression import main_ci_evidence as mce
from calee_regression import provider_evidence as pe
from calee_regression import release_installer as ri
from calee_regression import resume_release as rr
from calee_regression import run_context

RUN_ID = "release-acq-test-000001"
RELEASE_ID = "2026.07-rc1"
REGRESSION_SHA = "a" * 40
CM_REGRESSION_SHA = "b" * 40
CM_SHA = "c" * 40
CM_VERSION = "2.3.4+56"
SELECTOR_HEAD_SHA = "d" * 40


# ---------------------------------------------------------------------------
# Fixtures: stub bundle verification, plan, fake GitHub client
# ---------------------------------------------------------------------------


class _StubVerification:
    ok = True
    errors: "list[str]" = []

    def __init__(self, manifest):
        self.manifest = manifest


def _manifest(**overrides):
    kwargs = dict(
        release_id=RELEASE_ID,
        schema_version=ri.RELEASE_MANIFEST_SCHEMA_V2,
        platforms=ri.PlatformScope(tablet=True, mobile_android=True, mobile_ios=True),
        calee_mobile=ri.CaleeMobileExpected(
            version=CM_VERSION, git_sha=CM_SHA,
            selector_evidence_required=True, distributed_build_acceptance_required=True,
        ),
    )
    kwargs.update(overrides)
    return ri.ReleaseManifest(**kwargs)


def _write_baseline(report_root: Path, run_id: str = RUN_ID) -> None:
    baseline = report_root / "reports" / "runs" / run_id / "attempts" / "1"
    baseline.mkdir(parents=True, exist_ok=True)
    (baseline / "immutable-inputs.json").write_text(json.dumps({
        "regressionSha": REGRESSION_SHA,
        "caleeMobileRegressionSha": CM_REGRESSION_SHA,
    }))


def _plan(tmp_path: Path, manifest=None, run_id: str = RUN_ID) -> ea.EvidencePlan:
    _write_baseline(tmp_path, run_id)
    return ea.derive_evidence_plan(
        bundle_path=tmp_path / "bundle", run_id=run_id,
        repo_root=tmp_path / "repo", report_root=tmp_path,
        verification=_StubVerification(manifest or _manifest()),
    )


def _zip_bytes(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, payload)
    return buf.getvalue()


def _main_ci_summary(repository: str, sha: str, run_id: str, **overrides) -> dict:
    data = dict(
        schemaVersion=1, repository=repository,
        workflowFile=mce.CALEEMOBILE_REGRESSION_WORKFLOW_FILE
        if repository == mce.CALEEMOBILE_REGRESSION_REPOSITORY
        else ".github/workflows/framework-tests.yml",
        workflow="ci" if repository == mce.CALEEMOBILE_REGRESSION_REPOSITORY else "framework-tests",
        event="push", ref="refs/heads/main", commitSha=sha, runId=run_id,
        runAttempt="1", isMainPush=True, isMergeGroup=False,
        generatedAt="2026-07-21T00:00:00Z",
    )
    if repository == mce.CALEEMOBILE_REGRESSION_REPOSITORY:
        data["gates"] = {g: "success" for g in mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES}
    data.update(overrides)
    return data


def _fresh_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _selector_result(run_id: str, **overrides) -> dict:
    data = dict(
        schemaVersion=1, component="caleemobile-selector-contract",
        caleemobileRef="release", testedSha=CM_SHA, pubspecVersion=CM_VERSION,
        flutterVersion="3.44.1", contract="PASS", selectorsChecked=62,
        selectorsPresent=62, missing=[], timestamp=_fresh_ts(),
        regressionSha=SELECTOR_HEAD_SHA, workflowRunId=run_id,
        releaseId=RELEASE_ID,
    )
    data.update(overrides)
    return data


class FakeGithubClient(ea.GithubEvidenceClient):
    def __init__(self):
        self.runs: "list[dict]" = []
        self.artifacts: "dict[str, list[dict]]" = {}
        self.zips: "dict[str, bytes]" = {}
        self.download_calls = 0

    # -- population helpers ------------------------------------------------
    def add_run(self, run: dict, artifacts: "list[dict]" = (), zips: "dict[str, bytes]" = None):
        self.runs.append(run)
        self.artifacts[str(run["id"])] = list(artifacts)
        for art_id, data in (zips or {}).items():
            self.zips[str(art_id)] = data

    # -- protocol ------------------------------------------------------------
    def list_workflow_runs(self, repository, workflow_file, *, head_sha=None, event=None, branch=None):
        out = []
        for run in self.runs:
            if (run.get("repository") or {}).get("full_name") != repository:
                continue
            if Path(str(run.get("path"))).name != Path(workflow_file).name:
                continue
            if head_sha and run.get("head_sha") != head_sha:
                continue
            if event and run.get("event") != event:
                continue
            if branch and run.get("head_branch") != branch:
                continue
            out.append(run)
        return out

    def get_workflow_run(self, repository, run_id):
        for run in self.runs:
            if str(run.get("id")) == str(run_id):
                return run
        raise ea.AcquisitionError(f"no such run {run_id}")

    def list_run_artifacts(self, repository, run_id):
        return list(self.artifacts.get(str(run_id), []))

    def get_artifact(self, repository, artifact_id):
        for arts in self.artifacts.values():
            for art in arts:
                if str(art.get("id")) == str(artifact_id):
                    return art
        raise ea.AcquisitionError(f"no such artifact {artifact_id}")

    def download_artifact_zip(self, repository, artifact_id):
        self.download_calls += 1
        try:
            return self.zips[str(artifact_id)]
        except KeyError:
            raise ea.AcquisitionError(f"no zip for artifact {artifact_id}")


def _run_dict(run_id: str, repository: str, workflow_file: str, sha: str, **overrides) -> dict:
    data = dict(
        id=run_id, repository={"full_name": repository}, path=workflow_file,
        name="ci", event="push", head_branch="main", head_sha=sha,
        status="completed", conclusion="success", run_attempt=1,
        html_url=f"https://github.com/{repository}/actions/runs/{run_id}",
    )
    data.update(overrides)
    return data


def _artifact_dict(artifact_id: str, name: str, run_id: str, zip_data: bytes, **overrides) -> dict:
    data = dict(
        id=artifact_id, name=name, expired=False, size_in_bytes=len(zip_data),
        digest="sha256:" + ga.sha256_hex(zip_data), workflow_run={"id": run_id},
        expires_at="2026-10-01T00:00:00Z",
    )
    data.update(overrides)
    return data


def _client_with_main_ci(repository: str, sha: str, *, run_id="101", artifact_id="9101",
                         run_overrides=None, artifact_overrides=None,
                         summary_overrides=None, zip_data=None) -> FakeGithubClient:
    profile = __import__("calee_regression.main_ci_artifact", fromlist=["KNOWN_PROFILES"]).KNOWN_PROFILES[repository]
    summary = _main_ci_summary(repository, sha, run_id, **(summary_overrides or {}))
    data = zip_data if zip_data is not None else _zip_bytes(
        profile["result_filename"], json.dumps(summary).encode())
    run = _run_dict(run_id, repository, profile["workflow_path"], sha, **(run_overrides or {}))
    artifact = _artifact_dict(artifact_id, profile["artifact_prefix"] + sha, run_id, data,
                              **(artifact_overrides or {}))
    client = FakeGithubClient()
    client.add_run(run, [artifact], {artifact_id: data})
    return client


def _client_full(tmp_path) -> FakeGithubClient:
    """A client carrying exact matches for BOTH main-CI items + the selector."""
    client = FakeGithubClient()
    for repo, sha, rid, aid in (
        ("CaleeAdmin/calee-regression", REGRESSION_SHA, "101", "9101"),
        (mce.CALEEMOBILE_REGRESSION_REPOSITORY, CM_REGRESSION_SHA, "202", "9202"),
    ):
        c = _client_with_main_ci(repo, sha, run_id=rid, artifact_id=aid)
        client.runs.extend(c.runs)
        client.artifacts.update(c.artifacts)
        client.zips.update(c.zips)
    sel_zip = _zip_bytes(ga.EXPECTED_RESULT_FILENAME, json.dumps(_selector_result("303")).encode())
    sel_run = _run_dict("303", ga.EXPECTED_WORKFLOW_REPO, ga.EXPECTED_WORKFLOW_PATH,
                        SELECTOR_HEAD_SHA, event="workflow_dispatch")
    sel_art = _artifact_dict("9303", ga.EXPECTED_ARTIFACT_NAME, "303", sel_zip)
    client.add_run(sel_run, [sel_art], {"9303": sel_zip})
    return client


def _acquire(tmp_path, client, plan=None, **kwargs):
    plan = plan or _plan(tmp_path)
    return ea.acquire_release_evidence(plan, report_root=tmp_path, client=client, **kwargs)


def _android_ios_collectors():
    def _collector(platform):
        def _collect(spec):
            record = pe.ProviderEvidenceRecord(
                provider=pe.PROVIDER_APP_STORE_CONNECT if platform == pe.PLATFORM_IOS
                else pe.PROVIDER_PLAY_CONSOLE,
                platform=platform,
                provider_account_or_project="acct-1",
                provider_endpoint="https://example.invalid/api",
                provider_record_id="rec-1",
                http_status=200,
                observed_at=_fresh_ts(),
                raw_response_bytes=b"{}",
                credential_source_name="INJECTED",
                collector_version=pe.COLLECTOR_VERSION,
                collection_run_id=RUN_ID,
                release_id=spec.release_id,
                channel="testflight" if platform == pe.PLATFORM_IOS else "play_console_internal",
                marketing_version=CM_VERSION,
                build_number="56",
            )
            return record.to_provider_observation_dict()
        return _collect
    return {pe.PLATFORM_ANDROID: _collector(pe.PLATFORM_ANDROID),
            pe.PLATFORM_IOS: _collector(pe.PLATFORM_IOS)}


# ---------------------------------------------------------------------------
# Plan derivation
# ---------------------------------------------------------------------------


class TestPlanDerivation:
    def test_invalid_bundle_fails_before_any_lookup(self, tmp_path):
        bad = _StubVerification(_manifest())
        bad.ok = False
        bad.errors = ["checksum mismatch"]
        with pytest.raises(ea.AcquisitionUsageError, match="checksum mismatch"):
            ea.derive_evidence_plan(bundle_path=tmp_path, run_id=RUN_ID,
                                    repo_root=tmp_path, report_root=tmp_path,
                                    verification=bad)

    def test_invalid_run_id_rejected(self, tmp_path):
        with pytest.raises(ea.AcquisitionUsageError, match="run id"):
            ea.derive_evidence_plan(bundle_path=tmp_path, run_id="../evil",
                                    repo_root=tmp_path, report_root=tmp_path,
                                    verification=_StubVerification(_manifest()))

    def test_expected_shas_come_from_recorded_baseline(self, tmp_path):
        plan = _plan(tmp_path)
        spec = plan.spec(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert spec.expected_head_sha == REGRESSION_SHA
        assert "immutable baseline" in spec.derivation
        assert plan.spec(ea.TYPE_CALEEMOBILE_REGRESSION_MAIN_CI).expected_head_sha == CM_REGRESSION_SHA
        sel = plan.spec(ea.TYPE_SELECTOR_CERTIFICATION)
        assert (sel.expected_product_sha, sel.expected_version, sel.release_id) == (
            CM_SHA, CM_VERSION, RELEASE_ID)

    def test_out_of_scope_platform_is_not_required(self, tmp_path):
        manifest = _manifest(platforms=ri.PlatformScope(tablet=True, mobile_android=True,
                                                        mobile_ios=False))
        plan = _plan(tmp_path, manifest)
        assert plan.spec(ea.TYPE_DISTRIBUTED_BUILD_IOS).required is False
        assert plan.spec(ea.TYPE_DISTRIBUTED_BUILD_ANDROID).required is True


# ---------------------------------------------------------------------------
# Exact matching
# ---------------------------------------------------------------------------


class TestExactMatching:
    def test_exact_calee_regression_main_sha_found(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA)
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_ACQUIRED, item.problems
        assert item.verification_result == "verified"
        assert Path(item.cached_path).is_file()
        assert item.github_digest == item.observed_digest

    def test_exact_caleemobile_regression_main_sha_found(self, tmp_path):
        client = _client_with_main_ci(mce.CALEEMOBILE_REGRESSION_REPOSITORY, CM_REGRESSION_SHA)
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_CALEEMOBILE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_ACQUIRED, item.problems

    def test_exact_selector_tuple_found(self, tmp_path):
        outcome = _acquire(tmp_path, _client_full(tmp_path))
        item = outcome.item(ea.TYPE_SELECTOR_CERTIFICATION)
        assert item.status == ea.STATUS_ACQUIRED, item.problems

    def test_full_acquisition_with_providers_exits_zero(self, tmp_path):
        outcome = _acquire(tmp_path, _client_full(tmp_path),
                           provider_collectors=_android_ios_collectors())
        assert [i.status for i in outcome.items] == [ea.STATUS_ACQUIRED] * 5
        assert outcome.exit_code == 0
        manifest = json.loads(Path(outcome.manifest_path).read_text())
        assert manifest["releaseId"] == RELEASE_ID
        assert len(manifest["items"]) == 5
        # Secret-free: no token-ish content anywhere in the manifest.
        text = json.dumps(manifest).lower()
        assert "authorization" not in text and "token" not in text

    def test_provider_android_and_ios_evidence_found(self, tmp_path):
        outcome = _acquire(tmp_path, _client_full(tmp_path),
                           provider_collectors=_android_ios_collectors())
        for t in (ea.TYPE_DISTRIBUTED_BUILD_ANDROID, ea.TYPE_DISTRIBUTED_BUILD_IOS):
            item = outcome.item(t)
            assert item.status == ea.STATUS_ACQUIRED, item.problems
            assert Path(item.cached_path).is_file()


# ---------------------------------------------------------------------------
# Rejections (each one must BLOCK, never pass)
# ---------------------------------------------------------------------------


def _assert_main_ci_blocked(tmp_path, client, needle: str):
    outcome = _acquire(tmp_path, client)
    item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
    assert item.status == ea.STATUS_BLOCKED, item.status
    assert needle in " ".join(item.problems), item.problems
    assert outcome.exit_code == 3


class TestRejections:
    def test_pr_head_run_rejected(self, tmp_path):
        client = _client_with_main_ci(
            "CaleeAdmin/calee-regression", REGRESSION_SHA,
            run_overrides={"event": "pull_request", "head_branch": "feature"})
        _assert_main_ci_blocked(tmp_path, client, "no matching successful run")

    def test_workflow_dispatch_main_run_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      run_overrides={"event": "workflow_dispatch"})
        _assert_main_ci_blocked(tmp_path, client, "no matching successful run")

    def test_wrong_branch_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      run_overrides={"head_branch": "release-hotfix"})
        _assert_main_ci_blocked(tmp_path, client, "no matching successful run")

    def test_wrong_repository_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA)
        client.runs[0]["repository"] = {"full_name": "Evil/calee-regression"}
        _assert_main_ci_blocked(tmp_path, client, "no matching successful run")

    def test_wrong_workflow_file_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA)
        client.runs[0]["path"] = ".github/workflows/framework-tests.yml.bak/framework-tests.yml"
        _assert_main_ci_blocked(tmp_path, client, "no matching successful run")

    def test_unsuccessful_conclusion_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      run_overrides={"conclusion": "failure"})
        _assert_main_ci_blocked(tmp_path, client, "no matching successful run")

    def test_wrong_head_sha_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", "e" * 40, run_id="101")
        _assert_main_ci_blocked(tmp_path, client, "no matching successful run")

    def test_artifact_belongs_to_another_run_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      artifact_overrides={"workflow_run": {"id": "999"}})
        _assert_main_ci_blocked(tmp_path, client, "belongs to run")

    def test_github_digest_mismatch_rejected(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      artifact_overrides={"digest": "sha256:" + "0" * 64})
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_BLOCKED
        assert "digest" in " ".join(item.problems)

    def test_malformed_artifact_rejected(self, tmp_path):
        garbage = b"this is not a zip"
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      zip_data=garbage)
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_BLOCKED
        assert item.verification_result == "malformed-artifact"

    def test_expired_artifact_blocked(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      artifact_overrides={"expired": True})
        _assert_main_ci_blocked(tmp_path, client, "expired")

    def test_duplicate_valid_matches_ambiguous(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA, run_id="101")
        dup = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                   run_id="102", artifact_id="9102")
        client.runs.extend(dup.runs)
        client.artifacts.update(dup.artifacts)
        client.zips.update(dup.zips)
        _assert_main_ci_blocked(tmp_path, client, "ambiguous")

    def test_no_matches_blocked_with_remediation(self, tmp_path):
        outcome = _acquire(tmp_path, FakeGithubClient())
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_BLOCKED
        assert "merged-main" in item.remediation

    def test_token_missing_blocks_never_unauthenticated(self, tmp_path):
        plan = _plan(tmp_path)
        outcome = ea.acquire_release_evidence(plan, report_root=tmp_path, client=None, env={})
        for t in (ea.TYPE_CALEE_REGRESSION_MAIN_CI, ea.TYPE_CALEEMOBILE_REGRESSION_MAIN_CI,
                  ea.TYPE_SELECTOR_CERTIFICATION):
            item = outcome.item(t)
            assert item.status == ea.STATUS_BLOCKED
            assert "REGRESSION_API_TOKEN" in item.remediation
        assert outcome.exit_code == 3

    def test_selector_wrong_caleemobile_version_rejected(self, tmp_path):
        client = FakeGithubClient()
        sel_zip = _zip_bytes(ga.EXPECTED_RESULT_FILENAME,
                             json.dumps(_selector_result("303", pubspecVersion="9.9.9+1")).encode())
        run = _run_dict("303", ga.EXPECTED_WORKFLOW_REPO, ga.EXPECTED_WORKFLOW_PATH,
                        SELECTOR_HEAD_SHA, event="workflow_dispatch")
        client.add_run(run, [_artifact_dict("9303", ga.EXPECTED_ARTIFACT_NAME, "303", sel_zip)],
                       {"9303": sel_zip})
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_SELECTOR_CERTIFICATION)
        assert item.status == ea.STATUS_BLOCKED
        assert "selector certification has not been run" in item.remediation

    def test_selector_wrong_release_id_rejected(self, tmp_path):
        client = FakeGithubClient()
        sel_zip = _zip_bytes(ga.EXPECTED_RESULT_FILENAME,
                             json.dumps(_selector_result("303", releaseId="other-release")).encode())
        run = _run_dict("303", ga.EXPECTED_WORKFLOW_REPO, ga.EXPECTED_WORKFLOW_PATH,
                        SELECTOR_HEAD_SHA, event="workflow_dispatch")
        client.add_run(run, [_artifact_dict("9303", ga.EXPECTED_ARTIFACT_NAME, "303", sel_zip)],
                       {"9303": sel_zip})
        outcome = _acquire(tmp_path, client)
        assert outcome.item(ea.TYPE_SELECTOR_CERTIFICATION).status == ea.STATUS_BLOCKED

    def test_selector_ambiguous_duplicate_tuple_blocked(self, tmp_path):
        client = FakeGithubClient()
        for rid, aid in (("303", "9303"), ("304", "9304")):
            sel_zip = _zip_bytes(ga.EXPECTED_RESULT_FILENAME,
                                 json.dumps(_selector_result(rid)).encode())
            run = _run_dict(rid, ga.EXPECTED_WORKFLOW_REPO, ga.EXPECTED_WORKFLOW_PATH,
                            SELECTOR_HEAD_SHA, event="workflow_dispatch")
            client.add_run(run, [_artifact_dict(aid, ga.EXPECTED_ARTIFACT_NAME, rid, sel_zip)],
                           {aid: sel_zip})
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_SELECTOR_CERTIFICATION)
        assert item.status == ea.STATUS_BLOCKED
        assert "ambiguous" in " ".join(item.problems)

    def test_gate_failure_on_exact_run_is_contradiction_exit_1(self, tmp_path):
        gates = {g: "success" for g in mce.CALEEMOBILE_REGRESSION_REQUIRED_GATES}
        gates["selectorContract"] = "failure"
        client = _client_with_main_ci(mce.CALEEMOBILE_REGRESSION_REPOSITORY, CM_REGRESSION_SHA,
                                      summary_overrides={"gates": gates})
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_CALEEMOBILE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_CONTRADICTED
        assert outcome.exit_code == 1


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestCache:
    def test_valid_cache_reused_after_metadata_revalidation(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA)
        first = _acquire(tmp_path, client)
        assert first.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI).status == ea.STATUS_ACQUIRED
        downloads = client.download_calls
        second = _acquire(tmp_path, client)
        item = second.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_REUSED_CACHE
        assert client.download_calls == downloads  # no redownload

    def test_changed_cache_digest_rejected_and_redownloaded(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA)
        first = _acquire(tmp_path, client)
        cached = Path(first.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI).cached_path)
        cached.write_bytes(b"tampered")
        downloads = client.download_calls
        second = _acquire(tmp_path, client)
        item = second.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_ACQUIRED  # re-downloaded, not trusted
        assert client.download_calls == downloads + 1

    def test_missing_cache_file_redownloaded(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA)
        first = _acquire(tmp_path, client)
        Path(first.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI).cached_path).unlink()
        second = _acquire(tmp_path, client)
        assert second.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI).status == ea.STATUS_ACQUIRED

    def test_cache_is_run_scoped_never_shared(self, tmp_path):
        a = ea.cache_path_for(tmp_path, "run-a", evidence_type="t",
                              repository="o/r", workflow_run_id="1", artifact_id="2")
        b = ea.cache_path_for(tmp_path, "run-b", evidence_type="t",
                              repository="o/r", workflow_run_id="1", artifact_id="2")
        assert a != b
        assert "run-a" in str(a) and "run-b" in str(b)

    def test_cache_filename_embeds_full_identity(self, tmp_path):
        p = ea.cache_path_for(tmp_path, RUN_ID, evidence_type="selector-certification",
                              repository="CaleeAdmin/CaleeMobile-Regression",
                              workflow_run_id="303", artifact_id="9303")
        assert "selector-certification" in p.name
        assert "CaleeAdmin-CaleeMobile-Regression" in p.name
        assert "run303" in p.name and "art9303" in p.name

    def test_interrupted_atomic_write_leftover_is_cleaned(self, tmp_path):
        acquired = ea.evidence_dir(tmp_path, RUN_ID) / ea.ACQUIRED_DIRNAME
        acquired.mkdir(parents=True)
        leftover = acquired / "whatever.zip.abc.tmp"
        leftover.write_bytes(b"partial")
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA)
        _acquire(tmp_path, client)
        assert not leftover.exists()

    def test_symlink_cache_rejected(self, tmp_path):
        target = tmp_path / "outside.zip"
        target.write_bytes(b"data")
        link = tmp_path / "link.zip"
        link.symlink_to(target)
        assert ea.load_cached_zip(link, expected_digest_hex=ga.sha256_hex(b"data")) is None

    def test_atomic_write_sets_private_permissions(self, tmp_path):
        path = tmp_path / "sub" / "file.zip"
        ea.write_atomic_private(path, b"payload")
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_unsafe_zip_member_rejected(self, tmp_path):
        evil = _zip_bytes("../escape.json", b"{}")
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA, zip_data=evil)
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_BLOCKED
        assert item.verification_result == "malformed-artifact"

    def test_duplicate_zip_entry_rejected(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("framework-test-summary.json", b"{}")
            zf.writestr("framework-test-summary.json", b"{\"a\":1}")
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      zip_data=buf.getvalue())
        outcome = _acquire(tmp_path, client)
        assert outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI).status == ea.STATUS_BLOCKED

    def test_oversized_zip_rejected(self, tmp_path):
        big = _zip_bytes("framework-test-summary.json", b"0" * (ga.MAX_ARTIFACT_ZIP_BYTES + 10))
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA, zip_data=big)
        outcome = _acquire(tmp_path, client)
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_BLOCKED

    def test_cache_path_escape_refused(self, tmp_path):
        with pytest.raises(ea.AcquisitionError, match="escapes"):
            ea._ensure_inside(ea.evidence_dir(tmp_path, RUN_ID), tmp_path / "elsewhere.zip")


# ---------------------------------------------------------------------------
# Overrides, providers, inspection, resume
# ---------------------------------------------------------------------------


class TestOverridesAndIntegration:
    def test_explicit_override_supported_and_authenticated(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", REGRESSION_SHA,
                                      run_id="101", artifact_id="9101")
        outcome = _acquire(tmp_path, client, overrides={
            ea.TYPE_CALEE_REGRESSION_MAIN_CI: {"run_id": "101", "artifact_id": "9101"},
        })
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_ACQUIRED
        assert item.source == ea.SOURCE_EXPLICIT_OVERRIDE

    def test_explicit_override_mismatch_blocks(self, tmp_path):
        client = _client_with_main_ci("CaleeAdmin/calee-regression", "f" * 40,
                                      run_id="777", artifact_id="8777")
        outcome = _acquire(tmp_path, client, overrides={
            ea.TYPE_CALEE_REGRESSION_MAIN_CI: {"run_id": "777", "artifact_id": "8777"},
        })
        item = outcome.item(ea.TYPE_CALEE_REGRESSION_MAIN_CI)
        assert item.status == ea.STATUS_BLOCKED
        assert "head_sha" in " ".join(item.problems)

    def test_out_of_scope_platform_never_triggers_provider_call(self, tmp_path):
        calls = {"n": 0}

        def _collector(spec):
            calls["n"] += 1
            return {}

        manifest = _manifest(platforms=ri.PlatformScope(tablet=True, mobile_android=False,
                                                        mobile_ios=False))
        plan = _plan(tmp_path, manifest)
        outcome = _acquire(tmp_path, _client_full(tmp_path), plan=plan,
                           provider_collectors={pe.PLATFORM_ANDROID: _collector,
                                                pe.PLATFORM_IOS: _collector})
        assert calls["n"] == 0
        assert outcome.item(ea.TYPE_DISTRIBUTED_BUILD_ANDROID).status == ea.STATUS_NOT_APPLICABLE
        assert outcome.item(ea.TYPE_DISTRIBUTED_BUILD_IOS).status == ea.STATUS_NOT_APPLICABLE

    def test_required_platform_without_evidence_blocks(self, tmp_path):
        outcome = _acquire(tmp_path, _client_full(tmp_path))  # no collectors
        item = outcome.item(ea.TYPE_DISTRIBUTED_BUILD_ANDROID)
        assert item.status == ea.STATUS_BLOCKED
        assert "Play Console" in item.remediation
        ios = outcome.item(ea.TYPE_DISTRIBUTED_BUILD_IOS)
        assert "App Store Connect" in ios.remediation
        assert outcome.exit_code == 3

    def test_provider_collector_failure_blocks_never_fabricates(self, tmp_path):
        def _boom(spec):
            raise pe.ProviderEvidenceError("provider API said no")

        outcome = _acquire(tmp_path, _client_full(tmp_path),
                           provider_collectors={pe.PLATFORM_ANDROID: _boom,
                                                pe.PLATFORM_IOS: _boom})
        assert outcome.item(ea.TYPE_DISTRIBUTED_BUILD_ANDROID).status == ea.STATUS_BLOCKED

    def test_inspect_without_credentials_reports_blocked(self, tmp_path):
        plan = _plan(tmp_path)
        result = ea.inspect_release_evidence(plan, report_root=tmp_path, env={})
        assert result["credentialsAvailable"] is False
        assert result["canProceed"] is False

    def test_inspect_with_exact_matches_can_proceed(self, tmp_path):
        plan = _plan(tmp_path)
        result = ea.inspect_release_evidence(
            plan, report_root=tmp_path, client=_client_full(tmp_path),
            provider_collectors=_android_ios_collectors())
        assert result["credentialsAvailable"] is True
        main = [e for e in result["items"]
                if e["spec"]["evidenceType"] == ea.TYPE_CALEE_REGRESSION_MAIN_CI][0]
        assert main["matchingRuns"] == 1
        assert result["canProceed"] is True

    def test_resume_binds_acquisition_to_new_attempt_and_preserves_prior(self, tmp_path):
        run_id = "release-resume-acq-01"
        workspace = run_context.RunWorkspace(tmp_path, run_id)
        workspace.ensure_created()

        def _pass_prepare():
            return rr.PrepareOutcome(status="pass", exit_code=0)

        first = rr.perform_resume(run_id, repo_root=tmp_path, prepare_runner=_pass_prepare,
                                  evidence_acquirer=lambda ws: {"status": "ok", "items": {}})
        assert first.attempt is not None
        assert first.attempt.evidence_acquisition == {"status": "ok", "items": {}}
        attempt1 = rr.load_attempt(workspace, 1)
        snapshot = attempt1.to_dict()

        second = rr.perform_resume(run_id, repo_root=tmp_path, prepare_runner=_pass_prepare,
                                   evidence_acquirer=lambda ws: {"status": "blocked",
                                                                 "detail": "no token"})
        assert second.attempt.evidence_acquisition["status"] == "blocked"
        # The earlier attempt is immutable -- never rewritten by a later one.
        assert rr.load_attempt(workspace, 1).to_dict() == snapshot

    def test_recorded_distributed_build_evidence_for_other_release_not_reused(self, tmp_path):
        # A recorded distributed-build report belonging to ANOTHER release
        # must never satisfy this release's requirement.
        report_dir = tmp_path / "reports" / "runs" / RUN_ID / "distributed-build-acceptance"
        report_dir.mkdir(parents=True)
        (report_dir / "results.json").write_text(json.dumps({
            "provenance": {"sourceEvidence": {"platform": "android",
                                              "releaseId": "another-release"},
                           "evidenceTier": "provider-api-live"},
        }))
        outcome = _acquire(tmp_path, _client_full(tmp_path))
        assert outcome.item(ea.TYPE_DISTRIBUTED_BUILD_ANDROID).status == ea.STATUS_BLOCKED


# ---------------------------------------------------------------------------
# URL / redirect validation
# ---------------------------------------------------------------------------


class TestUrlValidation:
    def test_api_url_must_match_approved_host(self):
        assert ea.validate_github_api_url("https://api.github.com/repos/x/y") == []
        assert ea.validate_github_api_url("https://evil.example/repos/x/y")
        assert ea.validate_github_api_url("http://api.github.com/repos/x/y")
        assert ea.validate_github_api_url("https://user:pw@api.github.com/x")

    def test_redirect_hosts(self):
        assert ea.is_approved_redirect_host("https://objects.githubusercontent.com/x")
        assert ea.is_approved_redirect_host("https://productionresults.blob.core.windows.net/x")
        assert not ea.is_approved_redirect_host("https://evil.example/x")
        assert not ea.is_approved_redirect_host("http://objects.githubusercontent.com/x")
