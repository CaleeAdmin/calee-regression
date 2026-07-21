"""Offline tests for THIS session's distributed-evidence cross-binding and
scope-correct qualification work:

  * Priority 1 -- one canonical CaleeMobile joined version identity;
  * Priority 2 -- GitHub build provenance bound to authenticated run metadata;
  * Priority 3 -- the distributed binary artifact independently identified;
  * Priority 4 -- provider CI artifacts carry raw response + collector
    attestation;
  * Priority 5 -- provider/build marketing-version join;
  * Priority 6 -- both raw source bundles digest-bound and required;
  * Priority 7/8 -- platform-scope-correct preflight with a real
    NOT_APPLICABLE status;
  * Priority 9 -- recording/preflight/consolidation parity.

Everything here runs fully offline -- no network, no devices, no real
provider or GitHub API.
"""

from __future__ import annotations

import datetime
import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from calee_regression import build_provenance as bp
from calee_regression import consolidated_report as cr
from calee_regression import distributed_build_provenance as dbp
from calee_regression import github_artifact as ga
from calee_regression import provider_evidence as pe
from calee_regression import qualification_preflight as qp
from calee_regression.identity_format import compose_full_version, split_marketing_version_and_build_number

SHA = "a" * 40
OTHER_SHA = "b" * 40
RUN_ID = "run-2026-07-21-01"
RELEASE_ID = "rel-1"
FULL_VERSION = "0.0.24+24"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── identity_format.compose_full_version ────────────────────────────────


def test_compose_full_version_round_trips_with_split():
    assert compose_full_version("0.0.24", "24") == "0.0.24+24"
    assert split_marketing_version_and_build_number("0.0.24+24") == ("0.0.24", "24")


@pytest.mark.parametrize("marketing,build", [
    (None, "24"), ("0.0.24", None), ("", "24"), ("0.0.24", ""),
    ("0.0.24+24", "24"), ("founder-v0.3.24", "24"), ("0.0", "24"), ("0.0.24", "x24"),
])
def test_compose_full_version_rejects_ambiguous_or_noncanonical(marketing, build):
    assert compose_full_version(marketing, build) is None


# ── joined-record fixtures ──────────────────────────────────────────────


def _build_record(**overrides) -> bp.BuildProvenanceRecord:
    data = dict(
        schemaVersion=2, component="caleemobile-build-provenance",
        repository="CaleeAdmin/CaleeMobile", workflowRunId="42424242",
        workflowFile=".github/workflows/build-ios.yml",
        sourceGitSha=SHA, sourceRef="refs/heads/main", applicationVersion="0.0.24",
        platform="ios", bundleId="com.viso.caleemobile", platformBuildNumber="24",
        provenanceArtifact={"id": "77777777", "name": "build-provenance"},
        buildArtifact={"id": "88888888", "name": "CaleeMobile-ios-ipa", "sha256": "c" * 64, "platform": "ios"},
        buildTimestamp=_now_iso(),
        generatedBy="github-actions-artifact",
    )
    data.update(overrides)
    record = bp.parse_build_provenance(data)
    record.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    record.raw_bytes = json.dumps(data, sort_keys=True).encode("utf-8")
    return record


def _provider_observation(**overrides) -> dict:
    raw = _provider_raw_bytes()
    data = dict(
        schemaVersion=1, component="caleemobile-provider-observation",
        provider="app_store_connect", platform="ios", channel="testflight",
        releaseId=RELEASE_ID, providerAccountOrProject="app-1", providerRecordId="build-1",
        providerObservedAt=_now_iso(),
        bundleId="com.viso.caleemobile", marketingVersion="0.0.24", buildNumber="24",
        processingState="VALID", releaseStatus=None,
        generatedBy="provider-api",
        sourceDigest=dbp.raw_sha256(raw),
        timestamp=_now_iso(),
    )
    data.update(overrides)
    return data


def _provider_raw_bytes() -> bytes:
    return json.dumps({
        "data": [{"type": "builds", "id": "build-1", "attributes": {"version": "24"}}],
    }).encode("utf-8")


def _join(provider_overrides=None, record_overrides=None, **kwargs):
    provider = _provider_observation(**(provider_overrides or {}))
    record = _build_record(**(record_overrides or {}))
    defaults = dict(
        expected_release_config_git_sha=SHA, expected_release_config_version=FULL_VERSION,
        expected_release_id=RELEASE_ID, release_run_id=RUN_ID,
        provider_raw_sha256=dbp.raw_sha256(_provider_raw_bytes()),
        build_raw_sha256=dbp.raw_sha256(record.raw_bytes or b""),
    )
    defaults.update(kwargs)
    return pe, bp.join_provider_and_build_provenance(provider, record, **defaults)


# ── Priority 1: canonical joined version ────────────────────────────────


def test_joined_evidence_uses_canonical_full_version():
    _, verdict = _join()
    assert verdict.ok, verdict.problems
    assert verdict.evidence["testedVersion"] == FULL_VERSION
    assert verdict.evidence["marketingVersion"] == "0.0.24"
    assert verdict.evidence["platformBuildNumber"] == "24"
    assert verdict.evidence["providerMarketingVersionConfirmation"] == "verified"


def test_join_blocks_on_full_canonical_version_mismatch():
    _, verdict = _join(expected_release_config_version="0.0.25+25")
    assert not verdict.ok
    assert any("0.0.25" in p for p in verdict.problems)


def test_join_blocks_on_marketing_version_mismatch():
    _, verdict = _join(record_overrides={"applicationVersion": "0.0.23"})
    assert not verdict.ok


def test_join_blocks_on_build_number_mismatch():
    _, verdict = _join(record_overrides={"platformBuildNumber": "23"})
    assert not verdict.ok


def test_join_blocks_on_git_sha_mismatch():
    _, verdict = _join(record_overrides={"sourceGitSha": OTHER_SHA})
    assert not verdict.ok
    assert any("sourceGitSha" in p for p in verdict.problems)


def test_marketing_only_joined_tested_version_blocks_with_migration_message():
    """A legacy joined record whose testedVersion is the marketing-only value
    (or that lacks marketingVersion/platformBuildNumber entirely) must BLOCK
    with a migration message -- never be silently reinterpreted."""
    _, verdict = _join()
    legacy = dict(verdict.evidence)
    legacy["testedVersion"] = "0.0.24"
    problems = dbp.validate_distributed_evidence(legacy)
    assert any("MIGRATION REQUIRED" in p for p in problems)

    legacy2 = dict(verdict.evidence)
    legacy2.pop("marketingVersion")
    legacy2.pop("platformBuildNumber")
    legacy2["testedVersion"] = "0.0.24"
    problems2 = dbp.validate_distributed_evidence(legacy2)
    assert any("MIGRATION" in p for p in problems2)


# ── Priority 5: provider marketing-version join ─────────────────────────


def test_ios_join_blocks_when_provider_marketing_version_differs():
    """Provider marketing version differs; provider/build build numbers
    match; build provenance matches release configuration -- the iOS join
    must still block."""
    _, verdict = _join(provider_overrides={"marketingVersion": "0.0.23"})
    assert not verdict.ok
    assert any("marketingVersion" in p for p in verdict.problems)
    assert verdict.evidence["providerMarketingVersionConfirmation"] == "mismatch"


def test_ios_join_records_unavailable_provider_marketing_version_never_verified():
    _, verdict = _join(provider_overrides={"marketingVersion": None})
    assert verdict.evidence["providerMarketingVersionConfirmation"] == "unavailable-from-provider"


def test_play_release_name_is_recorded_as_release_label_not_marketing_confirmation():
    provider = _provider_observation(
        provider="play_console", platform="android", channel="play_console_internal",
        bundleId="au.com.calee.mobile", marketingVersion="July release", buildNumber="24",
    )
    record = _build_record(
        platform="android", bundleId="au.com.calee.mobile",
        workflowFile=".github/workflows/build-android.yml",
        buildArtifact={"id": "88888888", "name": "CaleeMobile-aab", "sha256": "c" * 64, "platform": "android"},
    )
    verdict = bp.join_provider_and_build_provenance(
        provider, record,
        expected_release_config_git_sha=SHA, expected_release_config_version=FULL_VERSION,
        expected_release_id=RELEASE_ID, release_run_id=RUN_ID,
    )
    assert verdict.ok, verdict.problems
    assert verdict.evidence["providerMarketingVersionConfirmation"] == "not-applicable"
    assert verdict.evidence["providerReleaseLabel"] == "July release"


# ── Priority 2: run-metadata binding of GitHub build provenance ─────────

BP_REPO = "CaleeAdmin/CaleeMobile"
BP_WORKFLOW = ".github/workflows/build-ios.yml"
BP_RUN_ID = "42424242"


def _bp_zip(**overrides) -> bytes:
    data = dict(
        schemaVersion=2, component="caleemobile-build-provenance",
        repository=BP_REPO, workflowRunId=BP_RUN_ID, workflowFile=BP_WORKFLOW,
        sourceGitSha=SHA, sourceRef="refs/heads/main", applicationVersion="0.0.24",
        platform="ios", bundleId="com.viso.caleemobile", platformBuildNumber="24",
        provenanceArtifact={"id": "77777777", "name": "build-provenance"},
        buildArtifact={"id": "88888888", "name": "CaleeMobile-ios-ipa", "sha256": "c" * 64, "platform": "ios"},
        buildTimestamp=_now_iso(), generatedBy="github-actions-artifact",
    )
    data.update(overrides)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("build-provenance.json", json.dumps(data))
    return buf.getvalue()


def _bp_run(**overrides) -> ga.WorkflowRunMetadata:
    base = dict(
        run_id=BP_RUN_ID, repo_full_name=BP_REPO, workflow_path=BP_WORKFLOW, workflow_name="build-ios",
        event="push", head_sha=SHA, head_branch="main", status="completed", conclusion="success",
        run_attempt="1",
    )
    base.update(overrides)
    return ga.WorkflowRunMetadata(**base)


def _bp_artifact(zip_bytes, **overrides) -> ga.ArtifactMetadata:
    base = dict(
        artifact_id="77777777", name="build-provenance", expired=False, size_in_bytes=len(zip_bytes),
        digest="sha256:" + ga.sha256_hex(zip_bytes), workflow_run_id=BP_RUN_ID,
        archive_download_url="https://api.github.com/x/zip",
    )
    base.update(overrides)
    return ga.ArtifactMetadata(**base)


def _build_artifact_meta(**overrides) -> ga.ArtifactMetadata:
    base = dict(
        artifact_id="88888888", name="CaleeMobile-ios-ipa", expired=False, size_in_bytes=123456,
        digest="sha256:" + "c" * 64, workflow_run_id=BP_RUN_ID,
        archive_download_url="https://api.github.com/y/zip",
    )
    base.update(overrides)
    return ga.ArtifactMetadata(**base)


def _chain(zb, run=None, build_artifact_metadata=None, **kwargs):
    kwargs.setdefault("expected_repository", BP_REPO)
    kwargs.setdefault("expected_workflow_path", BP_WORKFLOW)
    kwargs.setdefault("expected_artifact_name", "build-provenance")
    kwargs.setdefault("expected_result_filename", "build-provenance.json")
    return bp.verify_build_provenance_artifact_chain(
        run if run is not None else _bp_run(), _bp_artifact(zb), zb,
        build_artifact_metadata=build_artifact_metadata if build_artifact_metadata is not None else _build_artifact_meta(),
        **kwargs,
    )


def test_github_provenance_record_for_other_repository_blocks():
    zb = _bp_zip(repository="someone-else/CaleeMobile")
    chain = _chain(zb)
    assert not chain.ok
    assert any("record.repository" in p for p in chain.problems)


def test_github_provenance_record_naming_other_run_blocks():
    zb = _bp_zip(workflowRunId="999")
    chain = _chain(zb)
    assert not chain.ok
    assert any("record.workflowRunId" in p for p in chain.problems)


def test_github_provenance_record_naming_other_workflow_file_blocks():
    zb = _bp_zip(workflowFile=".github/workflows/evil.yml")
    chain = _chain(zb)
    assert not chain.ok
    assert any("record.workflowFile" in p for p in chain.problems)


def test_github_provenance_record_for_other_sha_blocks():
    """A genuine GitHub artifact containing hand-authored provenance for
    another SHA must BLOCK."""
    zb = _bp_zip(sourceGitSha=OTHER_SHA)
    chain = _chain(zb)
    assert not chain.ok
    assert any("sourceGitSha" in p and "head SHA" in p for p in chain.problems)


def test_github_provenance_record_with_inconsistent_source_ref_blocks():
    zb = _bp_zip(sourceRef="refs/heads/feature-branch")
    chain = _chain(zb)
    assert not chain.ok
    assert any("sourceRef" in p for p in chain.problems)


def test_github_provenance_preserves_authenticated_run_metadata():
    chain = _chain(_bp_zip())
    assert chain.ok, chain.problems
    meta = chain.record.authenticated_run
    assert meta == {
        "id": BP_RUN_ID, "attempt": "1", "event": "push", "headSha": SHA,
        "headBranch": "main", "workflowPath": BP_WORKFLOW, "repository": BP_REPO,
    }


# ── Priority 3: binary-artifact authentication ──────────────────────────


def test_schema_v1_record_cannot_pass_github_artifact_path():
    zb = _bp_zip(
        schemaVersion=1, provenanceArtifact=None, buildArtifact=None,
        artifactId="777", artifactDigest="sha256:" + "9" * 64,
    )
    chain = _chain(zb)
    assert not chain.ok
    assert any("schemaVersion 1 cannot produce a release-gating PASS" in p for p in chain.problems)


def test_binary_artifact_from_another_run_blocks():
    chain = _chain(_bp_zip(), build_artifact_metadata=_build_artifact_meta(workflow_run_id="55555"))
    assert not chain.ok
    assert any("belongs to run" in p for p in chain.problems)


def test_binary_artifact_digest_mismatch_blocks():
    chain = _chain(_bp_zip(), build_artifact_metadata=_build_artifact_meta(digest="sha256:" + "d" * 64))
    assert not chain.ok
    assert any("buildArtifact.sha256" in p for p in chain.problems)


def test_expired_binary_artifact_blocks():
    chain = _chain(_bp_zip(), build_artifact_metadata=_build_artifact_meta(expired=True))
    assert not chain.ok
    assert any("expired" in p for p in chain.problems)


def test_provenance_zip_cannot_masquerade_as_binary_artifact():
    zb = _bp_zip(buildArtifact={
        "id": "77777777", "name": "build-provenance", "sha256": "c" * 64, "platform": "ios",
    })
    chain = _chain(zb)
    assert not chain.ok
    assert any("masquerade" in p for p in chain.problems)


def test_binary_artifact_trust_level_is_explicit():
    chain = _chain(_bp_zip())
    assert chain.ok, chain.problems
    assert chain.record.build_artifact_trust == bp.BUILD_ARTIFACT_TRUST_GITHUB_METADATA


# ── Priority 4: provider CI artifact schema v2 ──────────────────────────

CI_REPO = "CaleeAdmin/calee-regression"
CI_WORKFLOW = ".github/workflows/collect-distributed-build-evidence.yml"
CI_RUN_ID = "555000111"
CI_ENDPOINT = "https://api.appstoreconnect.apple.com/v1/builds?filter[app]=acct&filter[version]=24"
CI_RAW = json.dumps({"data": [{"type": "builds", "id": "rec-1", "attributes": {"version": "24"}}]}).encode()
APPROVED = frozenset({(CI_REPO, CI_WORKFLOW)})


def _ci_observation(**overrides) -> dict:
    data = {
        "schemaVersion": 1, "component": "caleemobile-provider-observation",
        "provider": "app_store_connect", "platform": "ios", "channel": "testflight",
        "releaseId": RELEASE_ID, "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": _now_iso(), "generatedBy": "provider-api",
        "providerEndpoint": CI_ENDPOINT, "buildNumber": "24",
        "sourceDigest": dbp.raw_sha256(CI_RAW), "timestamp": _now_iso(),
    }
    data.update(overrides)
    return data


def _ci_attestation(**overrides) -> dict:
    data = {
        "repository": CI_REPO, "workflowRunId": CI_RUN_ID, "workflowFile": CI_WORKFLOW,
        "provider": "app_store_connect", "providerEndpoint": CI_ENDPOINT,
        "collectionRunId": CI_RUN_ID, "collectorVersion": "1.0.0",
        "observationTimestamp": _now_iso(), "rawResponseDigest": dbp.raw_sha256(CI_RAW),
    }
    data.update(overrides)
    return data


def _ci_zip(members=None, observation=None, attestation=None, raw=CI_RAW) -> bytes:
    files = {
        pe.CI_MEMBER_OBSERVATION: json.dumps(observation if observation is not None else _ci_observation()).encode(),
        pe.CI_MEMBER_RESPONSE_BIN: raw,
        pe.CI_MEMBER_RESPONSE_SHA: (dbp.raw_sha256(raw) + "  provider-response.bin\n").encode(),
        pe.CI_MEMBER_ATTESTATION: json.dumps(attestation if attestation is not None else _ci_attestation()).encode(),
    }
    if members:
        for name, content in members.items():
            if content is None:
                files.pop(name, None)
            else:
                files[name] = content
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _ci_run(**overrides) -> ga.WorkflowRunMetadata:
    base = dict(
        run_id=CI_RUN_ID, repo_full_name=CI_REPO, workflow_path=CI_WORKFLOW, workflow_name="collect",
        event="workflow_dispatch", head_sha="f" * 40, status="completed", conclusion="success",
    )
    base.update(overrides)
    return ga.WorkflowRunMetadata(**base)


def _ci_chain(zb, run=None, approved_collectors=APPROVED):
    artifact = ga.ArtifactMetadata(
        artifact_id="777000222", name="distributed-build-evidence", expired=False, size_in_bytes=len(zb),
        digest="sha256:" + ga.sha256_hex(zb), workflow_run_id=CI_RUN_ID,
        archive_download_url="https://api.github.com/x/zip",
    )
    return pe.verify_provider_ci_artifact_chain(
        run if run is not None else _ci_run(), artifact, zb,
        expected_repository=CI_REPO, expected_workflow_path=CI_WORKFLOW,
        expected_artifact_name="distributed-build-evidence",
        expected_result_filename="distributed-build-evidence.json",
        approved_collectors=approved_collectors,
    )


def test_provider_ci_artifact_lacking_raw_response_blocks():
    zb = _ci_zip(members={pe.CI_MEMBER_RESPONSE_BIN: None, pe.CI_MEMBER_RESPONSE_SHA: None})
    chain = _ci_chain(zb)
    assert not chain.ok
    assert any("missing required schema-v2 member" in p for p in chain.problems)


def test_provider_ci_artifact_with_self_declared_label_and_no_attestation_blocks():
    zb = _ci_zip(members={pe.CI_MEMBER_ATTESTATION: None})
    chain = _ci_chain(zb)
    assert not chain.ok
    assert any("collector attestation" in p or "missing required schema-v2 member" in p for p in chain.problems)


def test_provider_ci_artifact_raw_response_digest_mismatch_blocks():
    altered = CI_RAW + b" "
    zb = _ci_zip(members={pe.CI_MEMBER_RESPONSE_BIN: altered})
    chain = _ci_chain(zb)
    assert not chain.ok
    assert any("digest" in p for p in chain.problems)


def test_provider_ci_artifact_collection_run_id_mismatch_blocks():
    zb = _ci_zip(attestation=_ci_attestation(collectionRunId="999", workflowRunId="999"))
    chain = _ci_chain(zb)
    assert not chain.ok
    assert any("collectionRunId" in p for p in chain.problems)


def test_provider_ci_artifact_unapproved_collector_blocks():
    zb = _ci_zip()
    chain = _ci_chain(zb, approved_collectors=frozenset({("CaleeAdmin/other", "wf.yml")}))
    assert not chain.ok
    assert any("not in the approved provider-collector profile" in p for p in chain.problems)


def test_provider_ci_artifact_default_empty_approved_profile_blocks_with_clear_reason():
    zb = _ci_zip()
    chain = _ci_chain(zb, approved_collectors=None)
    assert not chain.ok
    assert any("no approved provider-collector workflow profile is configured" in p for p in chain.problems)


def test_provider_ci_artifact_unofficial_endpoint_host_blocks():
    endpoint = "https://api.evil.example/v1/builds"
    zb = _ci_zip(
        observation=_ci_observation(providerEndpoint=endpoint),
        attestation=_ci_attestation(providerEndpoint=endpoint),
    )
    chain = _ci_chain(zb)
    assert not chain.ok
    assert any("official" in p for p in chain.problems)


# ── Priority 6 + 9: recorded bundle, tamper detection, parity ───────────


def _recorded_component(tmp_path) -> "tuple[dict, Path]":
    """Record a joined distributed-build acceptance the way the CLI does:
    join, write the standardised source bundle, build+write the provenance
    record and report."""
    provider = _provider_observation()
    record = _build_record()
    provider_raw = _provider_raw_bytes()
    build_raw = record.raw_bytes or b""
    verdict = bp.join_provider_and_build_provenance(
        provider, record,
        expected_release_config_git_sha=SHA, expected_release_config_version=FULL_VERSION,
        expected_release_id=RELEASE_ID, release_run_id=RUN_ID,
        provider_raw_sha256=dbp.raw_sha256(provider_raw), build_raw_sha256=dbp.raw_sha256(build_raw),
    )
    assert verdict.ok, verdict.problems
    joined_bytes = json.dumps(verdict.evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

    component_dir = tmp_path / "distributed-build-acceptance"
    dbp.write_joined_source_bundle(
        component_dir,
        provider_observation=provider, provider_raw_bytes=provider_raw,
        provider_authentication={"sourceLabel": "live:app_store_connect", "runId": RUN_ID},
        build_provenance=record.to_dict(), build_raw_bytes=build_raw,
        build_authentication={"sourceLabel": "github", "runId": RUN_ID},
        joined_evidence_bytes=joined_bytes,
    )
    prov_record = dbp.build_provenance_record(
        verdict.evidence, release_run_id=RUN_ID, adopted_at=_now_iso(), adopted_by="test",
        source_path="joined:test", raw_source_bytes=joined_bytes,
        evidence_tier=pe.TIER_PROVIDER_BUILD_PROVENANCE_JOIN,
    )
    dbp.write_evidence_bundle(component_dir, prov_record, source_bytes=joined_bytes)
    report = {
        "runId": RUN_ID, "component": dbp.DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
        "status": "passed", "evidenceTier": pe.TIER_PROVIDER_BUILD_PROVENANCE_JOIN,
        "provenance": prov_record, "problems": [], "generatedAt": _now_iso(),
    }
    (component_dir / "results.json").write_text(json.dumps(report, indent=2))
    return report, component_dir


def _verify(report, component_dir):
    return cr.verify_distributed_evidence_report(
        report, mandatory=True, run_id=RUN_ID, release_id=RELEASE_ID,
        product_git_sha=SHA, full_version=FULL_VERSION, component_dir=component_dir,
    )


def test_joined_record_passes_recording_and_consolidation_round_trip(tmp_path):
    report, component_dir = _recorded_component(tmp_path)
    result = _verify(report, component_dir)
    assert result.status == cr.STATUS_PASS, result.detail
    assert result.evidence["testedVersion"] == FULL_VERSION
    assert result.evidence["marketingVersion"] == "0.0.24"
    assert result.evidence["platformBuildNumber"] == "24"


def test_missing_provider_raw_bytes_blocks(tmp_path):
    report, component_dir = _recorded_component(tmp_path)
    (component_dir / dbp.JOINED_PROVIDER_RESPONSE_BIN).unlink()
    result = _verify(report, component_dir)
    assert result.status == cr.STATUS_BLOCKED
    assert any(dbp.JOINED_PROVIDER_RESPONSE_BIN in d for d in result.detail)


def test_missing_build_provenance_raw_bytes_blocks(tmp_path):
    report, component_dir = _recorded_component(tmp_path)
    (component_dir / dbp.JOINED_BUILD_SOURCE_BIN).unlink()
    result = _verify(report, component_dir)
    assert result.status == cr.STATUS_BLOCKED


def test_altered_provider_raw_bytes_block(tmp_path):
    report, component_dir = _recorded_component(tmp_path)
    path = component_dir / dbp.JOINED_PROVIDER_RESPONSE_BIN
    path.write_bytes(path.read_bytes() + b" ")
    result = _verify(report, component_dir)
    assert result.status == cr.STATUS_BLOCKED
    assert any("altered or substituted" in d for d in result.detail)


def test_altered_build_raw_bytes_block(tmp_path):
    report, component_dir = _recorded_component(tmp_path)
    path = component_dir / dbp.JOINED_BUILD_SOURCE_BIN
    path.write_bytes(b"{}")
    result = _verify(report, component_dir)
    assert result.status == cr.STATUS_BLOCKED


def test_source_files_swapped_from_another_release_run_block(tmp_path):
    """Evidence recorded under another run ID substituted wholesale must
    block on the binding check."""
    report, component_dir = _recorded_component(tmp_path)
    result = cr.verify_distributed_evidence_report(
        report, mandatory=True, run_id="run-OTHER", release_id=RELEASE_ID,
        product_git_sha=SHA, full_version=FULL_VERSION, component_dir=component_dir,
    )
    assert result.status == cr.STATUS_BLOCKED
    assert any("different run" in d or "another release run" in d or "releaseRunId" in d for d in result.detail)


def test_preflight_and_consolidation_produce_the_same_verdict(tmp_path):
    """Priority 9 contract test: the same fixture through the recording-
    final-validation, preflight, and consolidation paths -- identical
    verdicts."""
    report, component_dir = _recorded_component(tmp_path)

    consolidation = _verify(report, component_dir)
    recording_final = _verify(report, component_dir)  # recording reuses the same entrypoint
    preflight_check = qp.check_distributed_build_evidence_availability(
        component_dir / "results.json", required=True,
        expected_release_id=RELEASE_ID, expected_git_sha=SHA,
        expected_version=FULL_VERSION, expected_release_run_id=RUN_ID,
    )
    assert consolidation.status == cr.STATUS_PASS
    assert recording_final.status == cr.STATUS_PASS
    assert preflight_check.status == qp.STATUS_READY, preflight_check.detail

    # And a tampered fixture fails all three identically.
    (component_dir / dbp.JOINED_PROVIDER_RESPONSE_BIN).write_bytes(b"tampered")
    assert _verify(report, component_dir).status == cr.STATUS_BLOCKED
    tampered_preflight = qp.check_distributed_build_evidence_availability(
        component_dir / "results.json", required=True,
        expected_release_id=RELEASE_ID, expected_git_sha=SHA,
        expected_version=FULL_VERSION, expected_release_run_id=RUN_ID,
    )
    assert tampered_preflight.status == qp.STATUS_BLOCKED


# ── Priority 7/8: platform-scope-correct preflight ──────────────────────

_MACHINE = {
    "tablet_serial": "TAB123", "expected_tablet_state": "logged_in_tablet",
    "calee_package_id": "com.viso.calee", "caleeshell_package_id": "com.viso.caleeshell",
    "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
    "calee_launch_action": "com.viso.calee.action.START",
    "release_bundle_dir": "~/Calee-Releases/current", "backend_url": "https://hub-dev.calee.com.au",
    "release_profile": "staging", "report_dir": "reports", "mobile_platforms": ["android"],
}


def _machine_config(tmp_path, **overrides) -> Path:
    data = dict(_MACHINE, **overrides)
    path = tmp_path / "machine.local.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _preflight(tmp_path, config_path, adb_runner=None):
    return qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=adb_runner or (lambda argv: (_ for _ in ()).throw(AssertionError("adb must not run"))),
        which=lambda name: None,
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        subprocess_runner=lambda argv, **kw: (_ for _ in ()).throw(AssertionError("no subprocess in test")),
        env={},
    )


def test_ios_only_preflight_never_calls_adb(tmp_path):
    config_path = _machine_config(tmp_path, mobile_platforms=["ios"], tablet_serial=None)
    report = _preflight(tmp_path, config_path)  # adb_runner raises if invoked
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["android_sdk_tools"] == qp.STATUS_NOT_APPLICABLE
    assert statuses["adb_device_availability"] == qp.STATUS_NOT_APPLICABLE
    assert statuses["android_build_tools"] == qp.STATUS_NOT_APPLICABLE
    assert "android_device_for_scope" not in statuses


def test_tablet_only_preflight_does_not_require_android_phone_or_flutter(tmp_path):
    config_path = _machine_config(tmp_path, mobile_platforms=[], tablet_serial="TAB123")
    report = qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: type("P", (), {"stdout": "List of devices attached\nTAB123\tdevice\n", "returncode": 0})(),
        which=lambda name: None,
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        env={},
    )
    statuses = {c.name: c.status for c in report.checks}
    assert "android_device_for_scope" not in statuses
    assert statuses["flutter"] == qp.STATUS_NOT_APPLICABLE
    assert statuses["caleemobile_checkout"] == qp.STATUS_NOT_APPLICABLE
    assert statuses["expected_tablet_serial"] == qp.STATUS_READY


def test_android_only_scope_marks_tablet_serial_not_applicable(tmp_path):
    config_path = _machine_config(tmp_path, mobile_platforms=["android"], tablet_serial=None)
    report = qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: type("P", (), {"stdout": "List of devices attached\nPHONE9\tdevice\n", "returncode": 0})(),
        which=lambda name: None,
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        env={},
    )
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["expected_tablet_serial"] == qp.STATUS_NOT_APPLICABLE
    assert "android_device_for_scope" in statuses


def test_not_applicable_does_not_prevent_overall_ready():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_NOT_APPLICABLE, "out of scope", not_applicable=True),
    ])
    assert report.overall == qp.STATUS_READY


def test_genuine_warning_still_prevents_ready():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_WARNING, "unresolved optional concern"),
    ])
    assert report.overall == qp.STATUS_WARNING


def test_diagnostic_only_preflight_is_never_release_qualified(tmp_path):
    config_path = _machine_config(tmp_path, mobile_platforms=[], tablet_serial=None)
    report = _preflight(tmp_path, config_path)
    payload = report.to_dict()
    assert payload["diagnosticOnly"] is True
    assert payload["releaseQualified"] is False
    assert any(c.name == "release_scope_derivation" for c in report.checks)


def test_report_explains_why_out_of_scope_checks_are_not_applicable(tmp_path):
    config_path = _machine_config(tmp_path, mobile_platforms=["ios"], tablet_serial=None)
    report = _preflight(tmp_path, config_path)
    detail = {c.name: c.detail for c in report.checks}
    assert "scope" in detail["android_sdk_tools"]
