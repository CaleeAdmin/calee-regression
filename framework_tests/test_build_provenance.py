"""Tests for the build-provenance module (Priority 1, this session): the
SOURCE side of the distributed-build identity chain, and the join with a
provider observation. No test here contacts a real provider or GitHub API --
every acquisition test injects a fake fetcher.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from calee_regression import build_provenance as bp
from calee_regression import github_artifact as ga
from calee_regression import provider_evidence as pe

SHA = "a" * 40
OTHER_SHA = "b" * 40


def _bp_dict(**overrides) -> dict:
    data = dict(
        schemaVersion=1, component="caleemobile-build-provenance",
        repository="CaleeAdmin/CaleeMobile", workflowRunId="42", workflowFile=".github/workflows/build-ios.yml",
        sourceGitSha=SHA, sourceRef="refs/heads/main", applicationVersion="0.0.24",
        platform="ios", bundleId="com.viso.caleemobile", platformBuildNumber="24",
        artifactId="777", artifactDigest="sha256:" + "9" * 64, buildTimestamp="2026-07-21T00:00:00Z",
        generatedBy="github-actions-artifact",
    )
    data.update(overrides)
    return data


def _provider_observation(**overrides) -> dict:
    data = dict(
        schemaVersion=1, component="caleemobile-provider-observation",
        provider="app_store_connect", platform="ios", channel="testflight",
        releaseId="r1", providerAccountOrProject="app-1", providerRecordId="build-1",
        providerObservedAt="2026-07-20T00:00:00Z",
        bundleId="com.viso.caleemobile", marketingVersion="0.0.24", buildNumber="24",
        processingState="VALID", releaseStatus=None,
        generatedBy="provider-api", sourceDigest="sha256:" + "1" * 64, timestamp="2026-07-20T00:00:00Z",
    )
    data.update(overrides)
    return data


# --- parse_build_provenance / validate_build_provenance ---------------------


def test_parse_build_provenance_reads_every_field():
    record = bp.parse_build_provenance(_bp_dict())
    assert record.repository == "CaleeAdmin/CaleeMobile"
    assert record.source_git_sha == SHA
    assert record.platform == "ios"
    assert record.bundle_id == "com.viso.caleemobile"
    assert record.platform_build_number == "24"
    assert record.application_version == "0.0.24"


def test_parse_build_provenance_rejects_non_dict():
    with pytest.raises(bp.BuildProvenanceError):
        bp.parse_build_provenance([1, 2, 3])


def test_parse_build_provenance_rejects_unsupported_schema_version():
    with pytest.raises(bp.BuildProvenanceError):
        bp.parse_build_provenance(_bp_dict(schemaVersion=999))


def test_validate_build_provenance_accepts_well_formed():
    record = bp.parse_build_provenance(_bp_dict())
    record.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    assert bp.validate_build_provenance(record) == []


def test_validate_build_provenance_rejects_abbreviated_sha():
    record = bp.parse_build_provenance(_bp_dict(sourceGitSha="abc1234"))
    record.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    problems = bp.validate_build_provenance(record)
    assert any("abbreviated" in p for p in problems)


def test_validate_build_provenance_rejects_missing_fields():
    record = bp.BuildProvenanceRecord(
        repository=None, workflow_run_id=None, workflow_file=None, source_git_sha=None, source_ref=None,
        application_version=None, platform=None, bundle_id=None, platform_build_number=None,
        artifact_id=None, artifact_digest=None, build_timestamp=None, generated_by=None,
    )
    problems = bp.validate_build_provenance(record)
    assert len(problems) >= 10


def test_validate_build_provenance_rejects_generated_by_outside_allowlist():
    record = bp.parse_build_provenance(_bp_dict())
    record.generated_by = "local_checkout"
    problems = bp.validate_build_provenance(record)
    assert any("not a recognised authenticated origin" in p for p in problems)


# --- authenticated origin 1: GitHub Actions artifact -------------------------

BP_REPO = "CaleeAdmin/CaleeMobile"
BP_WORKFLOW_PATH = ".github/workflows/build-ios.yml"
BP_RUN_ID = "42424242"
BP_ARTIFACT_ID = "77777777"
BP_ARTIFACT_NAME = "build-provenance"
BP_RESULT_FILENAME = "build-provenance.json"


def _bp_zip(**overrides) -> bytes:
    body = json.dumps(_bp_dict(**overrides)).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(BP_RESULT_FILENAME, body)
    return buf.getvalue()


def _bp_run(**overrides) -> ga.WorkflowRunMetadata:
    base = dict(
        run_id=BP_RUN_ID, repo_full_name=BP_REPO, workflow_path=BP_WORKFLOW_PATH, workflow_name="build-ios",
        event="push", head_sha=SHA, status="completed", conclusion="success",
    )
    base.update(overrides)
    return ga.WorkflowRunMetadata(**base)


def _bp_artifact(zip_bytes: bytes, **overrides) -> ga.ArtifactMetadata:
    base = dict(
        artifact_id=BP_ARTIFACT_ID, name=BP_ARTIFACT_NAME, expired=False, size_in_bytes=len(zip_bytes),
        digest="sha256:" + ga.sha256_hex(zip_bytes), workflow_run_id=BP_RUN_ID,
        archive_download_url="https://api.github.com/x/zip",
    )
    base.update(overrides)
    return ga.ArtifactMetadata(**base)


def _verify_bp_chain(zb, run=None, artifact=None, **kwargs):
    kwargs.setdefault("expected_repository", BP_REPO)
    kwargs.setdefault("expected_workflow_path", BP_WORKFLOW_PATH)
    kwargs.setdefault("expected_artifact_name", BP_ARTIFACT_NAME)
    kwargs.setdefault("expected_result_filename", BP_RESULT_FILENAME)
    return bp.verify_build_provenance_artifact_chain(
        run if run is not None else _bp_run(), artifact if artifact is not None else _bp_artifact(zb), zb, **kwargs,
    )


def test_verify_build_provenance_artifact_chain_accepts_well_formed():
    zb = _bp_zip()
    chain = _verify_bp_chain(zb)
    assert chain.ok, chain.problems
    assert chain.record.source_git_sha == SHA
    assert chain.record.generated_by == bp.GENERATED_BY_GITHUB_ARTIFACT


def test_verify_build_provenance_artifact_chain_rejects_wrong_repository():
    zb = _bp_zip()
    chain = _verify_bp_chain(zb, run=_bp_run(repo_full_name="someone-else/fork"))
    assert not chain.ok
    assert any("repository" in p for p in chain.problems)


def test_verify_build_provenance_artifact_chain_rejects_digest_mismatch():
    zb = _bp_zip()
    tampered = zb + b"\x00"
    chain = _verify_bp_chain(tampered, artifact=_bp_artifact(zb))
    assert not chain.ok
    assert any("digest" in p or "size_in_bytes" in p for p in chain.problems)


def test_verify_build_provenance_artifact_chain_rejects_unsuccessful_run():
    zb = _bp_zip()
    chain = _verify_bp_chain(zb, run=_bp_run(conclusion="failure"))
    assert not chain.ok
    assert any("conclusion" in p for p in chain.problems)


def test_verify_build_provenance_artifact_chain_rejects_malformed_content():
    zb = _bp_zip(sourceGitSha="not-a-sha")
    chain = _verify_bp_chain(zb, artifact=_bp_artifact(zb))
    assert not chain.ok
    assert any("abbreviated" in p for p in chain.problems)


def test_acquire_build_provenance_artifact_requires_run_id():
    with pytest.raises(bp.BuildProvenanceError, match="run id"):
        bp.acquire_build_provenance_artifact(
            repository=BP_REPO, workflow_path=BP_WORKFLOW_PATH, run_id=None, artifact_id=BP_ARTIFACT_ID,
            expected_artifact_name=BP_ARTIFACT_NAME, expected_result_filename=BP_RESULT_FILENAME, env={},
        )


def test_acquire_build_provenance_artifact_blocks_without_token_naming_the_secret():
    with pytest.raises(bp.BuildProvenanceError, match="REGRESSION_API_TOKEN"):
        bp.acquire_build_provenance_artifact(
            repository=BP_REPO, workflow_path=BP_WORKFLOW_PATH, run_id=BP_RUN_ID, artifact_id=BP_ARTIFACT_ID,
            expected_artifact_name=BP_ARTIFACT_NAME, expected_result_filename=BP_RESULT_FILENAME, env={},
        )


def test_acquire_build_provenance_artifact_end_to_end_with_injected_fetchers():
    zb = _bp_zip()

    def json_fetcher(url: str) -> dict:
        if url.endswith(f"/runs/{BP_RUN_ID}"):
            return {
                "id": int(BP_RUN_ID), "repository": {"full_name": BP_REPO}, "path": BP_WORKFLOW_PATH,
                "name": "build-ios", "event": "push", "head_sha": SHA,
                "status": "completed", "conclusion": "success",
            }
        if url.endswith(f"/artifacts/{BP_ARTIFACT_ID}"):
            return {
                "id": int(BP_ARTIFACT_ID), "name": BP_ARTIFACT_NAME, "expired": False,
                "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
                "workflow_run": {"id": int(BP_RUN_ID)}, "archive_download_url": "https://api.github.com/x/zip",
            }
        raise AssertionError(f"unexpected url {url}")

    def bytes_fetcher(url: str) -> bytes:
        assert "zip" in url
        return zb

    chain = bp.acquire_build_provenance_artifact(
        repository=BP_REPO, workflow_path=BP_WORKFLOW_PATH, run_id=BP_RUN_ID, artifact_id=BP_ARTIFACT_ID,
        expected_artifact_name=BP_ARTIFACT_NAME, expected_result_filename=BP_RESULT_FILENAME,
        json_fetcher=json_fetcher, bytes_fetcher=bytes_fetcher, token="fake",
    )
    assert chain.ok, chain.problems
    assert chain.record.source_git_sha == SHA


# --- authenticated origin 2: signed export -----------------------------------


@pytest.fixture()
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_build_signed_build_provenance_accepts_genuine_signature(rsa_keypair):
    private_key, public_pem = rsa_keypair
    payload = _bp_dict(generatedBy=None)
    signature = private_key.sign(_canonical(payload), padding.PKCS1v15(), hashes.SHA256())
    record, problems = bp.build_signed_build_provenance(payload=payload, signature_bytes=signature, trusted_public_key_pem=public_pem)
    assert problems == []
    assert record.generated_by == bp.GENERATED_BY_SIGNED_EXPORT
    assert record.source_git_sha == SHA


def test_build_signed_build_provenance_rejects_tampered_payload(rsa_keypair):
    private_key, public_pem = rsa_keypair
    payload = _bp_dict()
    signature = private_key.sign(_canonical(payload), padding.PKCS1v15(), hashes.SHA256())
    tampered = dict(payload, sourceGitSha=OTHER_SHA)
    record, problems = bp.build_signed_build_provenance(payload=tampered, signature_bytes=signature, trusted_public_key_pem=public_pem)
    assert record is None
    assert problems and "FAILED" in problems[0]


def test_build_signed_build_provenance_rejects_signature_from_different_key(rsa_keypair):
    _key, public_pem = rsa_keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = _bp_dict()
    wrong_signature = other_key.sign(_canonical(payload), padding.PKCS1v15(), hashes.SHA256())
    record, problems = bp.build_signed_build_provenance(payload=payload, signature_bytes=wrong_signature, trusted_public_key_pem=public_pem)
    assert record is None
    assert problems


# --- the join: the actual distributed-build identity chain ------------------


def test_join_provider_and_build_provenance_matching_passes():
    """Offline test #4: 'Provider plus matching build provenance passes.'"""
    provider = _provider_observation()
    build = bp.parse_build_provenance(_bp_dict())
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(
        provider, build, expected_release_config_git_sha=SHA, expected_release_config_version="0.0.24+24",
        now=__import__("datetime").datetime(2026, 7, 21, tzinfo=__import__("datetime").timezone.utc),
    )
    assert verdict.ok, verdict.problems
    assert verdict.evidence["testedGitSha"] == SHA
    assert verdict.evidence["testedVersion"] == "0.0.24"
    assert verdict.evidence["generatedBy"] == "provider-build-provenance-join"
    assert verdict.evidence["providerObservation"]["providerRecordId"] == "build-1"
    assert verdict.evidence["buildProvenance"]["sourceGitSha"] == SHA


def test_join_provider_and_build_provenance_wrong_build_number_blocks():
    """Offline test #5: 'Provider plus wrong build number blocks.'"""
    provider = _provider_observation(buildNumber="24")
    build = bp.parse_build_provenance(_bp_dict(platformBuildNumber="99"))
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build)
    assert not verdict.ok
    assert any("platformBuildNumber" in p or "buildNumber" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_wrong_git_sha_blocks():
    """Offline test #6: 'Provider plus wrong Git SHA blocks.'"""
    provider = _provider_observation()
    build = bp.parse_build_provenance(_bp_dict())
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build, expected_release_config_git_sha=OTHER_SHA)
    assert not verdict.ok
    assert any("sourceGitSha" in p and "!=" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_wrong_bundle_id_blocks():
    """Offline test 3: exact application/package match."""
    provider = _provider_observation(bundleId="com.viso.caleemobile")
    build = bp.parse_build_provenance(_bp_dict(bundleId="com.viso.somethingelse"))
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build)
    assert not verdict.ok
    assert any("bundleId" in p and "!=" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_wrong_platform_blocks():
    provider = _provider_observation(platform="ios")
    build = bp.parse_build_provenance(_bp_dict(platform="android"))
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build)
    assert not verdict.ok
    assert any("platform" in p and "!=" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_provider_processing_failed_blocks():
    """Offline test: 'Expired or invalid TestFlight build.'"""
    provider = _provider_observation(processingState="FAILED")
    build = bp.parse_build_provenance(_bp_dict())
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build)
    assert not verdict.ok
    assert any("processingState" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_draft_release_status_blocks():
    provider = _provider_observation(provider="play_console", platform="android", releaseStatus="draft")
    build = bp.parse_build_provenance(_bp_dict(platform="android"))
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build)
    assert not verdict.ok
    assert any("draft" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_stale_provider_observation_blocks():
    import datetime

    provider = _provider_observation(providerObservedAt="2020-01-01T00:00:00Z")
    build = bp.parse_build_provenance(_bp_dict())
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(
        provider, build, now=datetime.datetime(2026, 7, 21, tzinfo=datetime.timezone.utc),
    )
    assert not verdict.ok
    assert any("stale" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_ambiguous_expected_version_blocks():
    """Priority 2: reject ambiguous parsing of a value like '0.0.24+24' when
    it can't be unambiguously split (here: no build number to split at all)."""
    provider = _provider_observation()
    build = bp.parse_build_provenance(_bp_dict())
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build, expected_release_config_version="0.0.24")
    assert not verdict.ok
    assert any("cannot be unambiguously split" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_matching_marketing_wrong_build_number():
    """Offline fixture: matching marketing version but wrong build number."""
    provider = _provider_observation()
    build = bp.parse_build_provenance(_bp_dict(applicationVersion="0.0.24", platformBuildNumber="99"))
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build, expected_release_config_version="0.0.24+24")
    assert not verdict.ok
    assert any("platformBuildNumber" in p and "!=" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_matching_build_number_wrong_marketing():
    """Offline fixture: matching build number but wrong marketing version."""
    provider = _provider_observation(marketingVersion="9.9.9", buildNumber="24")
    build = bp.parse_build_provenance(_bp_dict(applicationVersion="9.9.9", platformBuildNumber="24"))
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build, expected_release_config_version="0.0.24+24")
    assert not verdict.ok
    assert any("applicationVersion" in p and "!=" in p for p in verdict.problems)


def test_join_provider_and_build_provenance_unauthenticated_provider_dict_blocks():
    """Requirement 1: provider observation authentication is re-checked here
    too (format-level) -- a malformed/incomplete provider dict blocks the
    join even if the build-provenance side is perfect."""
    provider = {"generatedBy": "manual_claim"}  # missing everything
    build = bp.parse_build_provenance(_bp_dict())
    build.generated_by = bp.GENERATED_BY_GITHUB_ARTIFACT
    verdict = bp.join_provider_and_build_provenance(provider, build)
    assert not verdict.ok
    assert len(verdict.problems) > 3


def test_join_provider_and_build_provenance_unauthenticated_build_side_blocks():
    """Requirement 2: build-provenance authentication is re-checked here too
    -- an unauthenticated-origin generatedBy blocks the join even if
    everything else matches."""
    provider = _provider_observation()
    build = bp.parse_build_provenance(_bp_dict())
    build.generated_by = "local_checkout"  # never a real acquisition path
    verdict = bp.join_provider_and_build_provenance(provider, build, expected_release_config_git_sha=SHA)
    assert not verdict.ok
    assert any("not a recognised authenticated origin" in p for p in verdict.problems)
