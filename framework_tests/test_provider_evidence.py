"""Adversarial tests for authenticated distributed-build evidence collection
(Priority 3, this session). Every test injects a fake HTTP fetcher/client or
a locally-generated test key pair -- NO test in this file contacts a real
provider or uses a real production key.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from calee_regression import credentials as credentials_mod
from calee_regression import distributed_build_provenance as dbp
from calee_regression import github_artifact as ga
from calee_regression import provider_evidence as pe


@pytest.fixture()
def ec_keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private_key, pem


@pytest.fixture()
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, private_pem, public_pem


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- JWT construction ---------------------------------------------------


def test_app_store_connect_jwt_has_correct_header_and_claims(ec_keypair):
    _key, pem = ec_keypair
    token = pe.build_app_store_connect_jwt(key_id="KID1", issuer_id="ISS1", private_key_pem=pem, now=1000)
    header_b64, claims_b64, _sig = token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    claims = json.loads(_b64url_decode(claims_b64))
    assert header == {"alg": "ES256", "kid": "KID1", "typ": "JWT"}
    assert claims["iss"] == "ISS1"
    assert claims["aud"] == "appstoreconnect-v1"
    assert claims["exp"] - claims["iat"] == 19 * 60


def test_app_store_connect_jwt_signature_verifies_against_real_public_key(ec_keypair):
    private_key, pem = ec_keypair
    token = pe.build_app_store_connect_jwt(key_id="KID1", issuer_id="ISS1", private_key_pem=pem)
    header_b64, claims_b64, sig_b64 = token.split(".")
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    raw_sig = _b64url_decode(sig_b64)
    r = int.from_bytes(raw_sig[:32], "big")
    s = int.from_bytes(raw_sig[32:], "big")
    der_sig = encode_dss_signature(r, s)
    private_key.public_key().verify(der_sig, f"{header_b64}.{claims_b64}".encode(), ec.ECDSA(hashes.SHA256()))


def test_app_store_connect_jwt_rejects_rsa_key_for_es256(rsa_keypair):
    _key, private_pem, _public_pem = rsa_keypair
    with pytest.raises(pe.ProviderEvidenceError):
        pe.build_app_store_connect_jwt(key_id="KID1", issuer_id="ISS1", private_key_pem=private_pem)


def test_app_store_connect_jwt_rejects_malformed_key():
    with pytest.raises(pe.ProviderEvidenceError):
        pe.build_app_store_connect_jwt(key_id="KID1", issuer_id="ISS1", private_key_pem="not a pem key")


def test_play_console_assertion_jwt_signature_verifies(rsa_keypair):
    private_key, private_pem, _public_pem = rsa_keypair
    token = pe.build_play_console_assertion_jwt(
        service_account_email="svc@proj.iam.gserviceaccount.com", private_key_pem=private_pem,
        scope="scope-x", token_uri="https://oauth2.googleapis.com/token",
    )
    header_b64, claims_b64, sig_b64 = token.split(".")
    private_key.public_key().verify(
        _b64url_decode(sig_b64), f"{header_b64}.{claims_b64}".encode(), padding.PKCS1v15(), hashes.SHA256(),
    )
    claims = json.loads(_b64url_decode(claims_b64))
    assert claims["iss"] == "svc@proj.iam.gserviceaccount.com"
    assert claims["scope"] == "scope-x"


# --- App Store Connect collector -----------------------------------------


def _asc_fake_fetcher(status, body: dict):
    def _fetch(url, headers):
        assert "Authorization" in headers and headers["Authorization"].startswith("Bearer ")
        return status, json.dumps(body).encode("utf-8"), {}
    return _fetch


def test_collect_app_store_connect_evidence_happy_path(ec_keypair):
    _key, pem = ec_keypair
    client = pe.AppStoreConnectClient(
        key_id="KID1", issuer_id="ISS1", private_key_pem=pem,
        fetcher=_asc_fake_fetcher(200, {"data": [{"id": "BUILD-999", "attributes": {"version": "0.0.24+24"}}]}),
    )
    record = pe.collect_app_store_connect_evidence(
        app_id="APPID1", build_version="24", requested_git_sha="a" * 40, requested_version="0.0.24+24",
        release_id="r1", client=client, collection_run_id="run-1",
    )
    assert record.provider == pe.PROVIDER_APP_STORE_CONNECT
    assert record.provider_record_id == "BUILD-999"
    assert record.http_status == 200
    assert record.tested_git_sha == "a" * 40
    assert record.tested_version == "0.0.24+24"
    assert record.collection_run_id == "run-1"
    assert record.credential_source_name == "CALEE_ASC_KEY_ID"

    evidence = record.to_evidence_dict()
    assert evidence["generatedBy"] == "provider-api"
    assert evidence["provider"] == "app_store_connect"
    problems = dbp.validate_distributed_evidence(evidence, expected_git_sha="a" * 40, expected_version="0.0.24+24", expected_release_id="r1")
    assert problems == [], problems


def test_collect_app_store_connect_evidence_non_200_blocks(ec_keypair):
    _key, pem = ec_keypair
    client = pe.AppStoreConnectClient(key_id="K", issuer_id="I", private_key_pem=pem, fetcher=_asc_fake_fetcher(401, {"errors": ["unauthorized"]}))
    with pytest.raises(pe.ProviderEvidenceError, match="401"):
        pe.collect_app_store_connect_evidence(app_id="A", build_version="1", client=client, collection_run_id="run-1")


def test_collect_app_store_connect_evidence_no_matching_build_blocks(ec_keypair):
    _key, pem = ec_keypair
    client = pe.AppStoreConnectClient(key_id="K", issuer_id="I", private_key_pem=pem, fetcher=_asc_fake_fetcher(200, {"data": []}))
    with pytest.raises(pe.ProviderEvidenceError, match="no build"):
        pe.collect_app_store_connect_evidence(app_id="A", build_version="1", client=client, collection_run_id="run-1")


def test_collect_app_store_connect_evidence_blocks_without_credentials():
    """BLOCKS with ProviderEvidenceError -- this module's own exception type,
    not the underlying credentials.CredentialError -- naming the exact
    missing credential. Every caller (the CLI in particular) catches
    ProviderEvidenceError alone to map a missing credential to BLOCKED, so a
    raw CredentialError escaping here would bypass that (this was a real
    inconsistency with collect_play_console_evidence below, which already
    raised ProviderEvidenceError directly; fixed this session)."""
    resolver = credentials_mod.CredentialResolver([credentials_mod.EnvironmentProvider({})])
    with pytest.raises(pe.ProviderEvidenceError, match="CALEE_ASC_KEY_ID"):
        pe.collect_app_store_connect_evidence(app_id="A", build_version="1", resolver=resolver, collection_run_id="run-1")


def test_collect_app_store_connect_evidence_never_leaks_private_key_in_raw_response(ec_keypair):
    _key, pem = ec_keypair
    client = pe.AppStoreConnectClient(
        key_id="K", issuer_id="I", private_key_pem=pem,
        fetcher=_asc_fake_fetcher(200, {"data": [{"id": "B1", "attributes": {"version": "1"}}]}),
    )
    record = pe.collect_app_store_connect_evidence(app_id="A", build_version="1", client=client, collection_run_id="run-1")
    assert pem not in record.raw_response_bytes.decode()
    assert "BEGIN" not in record.raw_response_bytes.decode()


# --- Play Console collector ------------------------------------------------


def _play_fake_fetcher(track_status, track_body, *, edit_status=200, edit_body=None):
    calls = {"n": 0}

    def _fetch(url, headers):
        calls["n"] += 1
        assert "Authorization" in headers and headers["Authorization"].startswith("Bearer ")
        if "/edits" in url and "/tracks/" not in url:
            return edit_status, json.dumps(edit_body or {"id": "EDIT-1"}).encode(), {}
        return track_status, json.dumps(track_body).encode(), {}
    return _fetch


def test_collect_play_console_evidence_happy_path_with_access_token():
    client = pe.PlayConsoleClient(
        access_token="fake-access-token",
        fetcher=_play_fake_fetcher(200, {"releases": [{"name": "0.0.24+24", "versionCodes": ["24"]}]}),
    )
    record = pe.collect_play_console_evidence(
        package_name="com.viso.calee", track="internal", requested_git_sha="a" * 40,
        requested_version="0.0.24+24", release_id="r1", client=client, collection_run_id="run-1",
    )
    assert record.provider == pe.PROVIDER_PLAY_CONSOLE
    assert record.provider_record_id == "24"
    assert record.channel == "play_console_internal"
    evidence = record.to_evidence_dict()
    problems = dbp.validate_distributed_evidence(evidence, expected_git_sha="a" * 40, expected_version="0.0.24+24", expected_release_id="r1")
    assert problems == [], problems


def test_collect_play_console_evidence_via_service_account_jwt_exchange(rsa_keypair):
    _key, private_pem, _public_pem = rsa_keypair
    service_account = {"client_email": "svc@proj.iam.gserviceaccount.com", "private_key": private_pem}

    def fetcher(url, headers):
        if url.startswith(pe.PlayConsoleClient.TOKEN_URI):
            return 200, json.dumps({"access_token": "exchanged-token"}).encode(), {}
        if "/edits" in url and "/tracks/" not in url:
            return 200, json.dumps({"id": "EDIT-1"}).encode(), {}
        assert headers["Authorization"] == "Bearer exchanged-token"
        return 200, json.dumps({"releases": [{"name": "0.0.24+24", "versionCodes": ["24"]}]}).encode(), {}

    client = pe.PlayConsoleClient(service_account=service_account, fetcher=fetcher)
    record = pe.collect_play_console_evidence(package_name="com.viso.calee", track="internal", client=client, collection_run_id="run-1")
    assert record.provider_record_id == "24"


def test_collect_play_console_evidence_no_releases_blocks():
    client = pe.PlayConsoleClient(access_token="t", fetcher=_play_fake_fetcher(200, {"releases": []}))
    with pytest.raises(pe.ProviderEvidenceError, match="no releases"):
        pe.collect_play_console_evidence(package_name="p", track="internal", client=client, collection_run_id="run-1")


def test_collect_play_console_evidence_blocks_without_credentials():
    resolver = credentials_mod.CredentialResolver([credentials_mod.EnvironmentProvider({})])
    with pytest.raises(pe.ProviderEvidenceError, match="CALEE_PLAY"):
        pe.collect_play_console_evidence(package_name="p", track="internal", resolver=resolver, collection_run_id="run-1")


def test_provider_evidence_dicts_never_contain_key_material(ec_keypair, rsa_keypair):
    """Priority 3: 'no credential exposure in ... reports/journals/ZIPs' --
    the recorded evidence dict (what actually gets written to
    distributed-build-source.json / distributed-build-provenance.json /
    the release ZIP) must never contain the private key PEM, only
    ``credentialSourceName`` (the env-var NAME, never a value)."""
    _asc_key, asc_pem = ec_keypair
    _play_key, play_private_pem, _play_public_pem = rsa_keypair

    asc_client = pe.AppStoreConnectClient(
        key_id="K", issuer_id="I", private_key_pem=asc_pem,
        fetcher=_asc_fake_fetcher(200, {"data": [{"id": "B1", "attributes": {"version": "1"}}]}),
    )
    asc_record = pe.collect_app_store_connect_evidence(app_id="A", build_version="1", client=asc_client, collection_run_id="run-1")
    asc_serialized = json.dumps(asc_record.to_evidence_dict())
    assert asc_pem not in asc_serialized
    assert "BEGIN" not in asc_serialized
    assert asc_record.credential_source_name == credentials_mod.APP_STORE_CONNECT_KEY_ID.env_var

    service_account = {"client_email": "svc@proj.iam.gserviceaccount.com", "private_key": play_private_pem}

    def _service_account_fetcher(url, headers):
        if url.startswith(pe.PlayConsoleClient.TOKEN_URI):
            return 200, json.dumps({"access_token": "exchanged-token"}).encode(), {}
        if "/edits" in url and "/tracks/" not in url:
            return 200, json.dumps({"id": "EDIT-1"}).encode(), {}
        return 200, json.dumps({"releases": [{"name": "0.0.24+24", "versionCodes": ["24"]}]}).encode(), {}

    play_client = pe.PlayConsoleClient(service_account=service_account, fetcher=_service_account_fetcher)
    play_record = pe.collect_play_console_evidence(package_name="com.viso.calee", track="internal", client=play_client, collection_run_id="run-1")
    play_serialized = json.dumps(play_record.to_evidence_dict())
    assert play_private_pem not in play_serialized
    assert "BEGIN" not in play_serialized


# --- signed export -----------------------------------------------------


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_verify_signed_export_accepts_genuine_rsa_signature(rsa_keypair):
    private_key, _private_pem, public_pem = rsa_keypair
    payload = _canonical({"testedGitSha": "a" * 40})
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    assert pe.verify_signed_export(payload_bytes=payload, signature_bytes=signature, trusted_public_key_pem=public_pem) == []


def test_verify_signed_export_accepts_genuine_ec_signature(ec_keypair):
    private_key, _pem = ec_keypair
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    payload = _canonical({"testedGitSha": "a" * 40})
    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    assert pe.verify_signed_export(payload_bytes=payload, signature_bytes=signature, trusted_public_key_pem=public_pem) == []


def test_verify_signed_export_rejects_tampered_payload(rsa_keypair):
    private_key, _private_pem, public_pem = rsa_keypair
    payload = _canonical({"testedGitSha": "a" * 40})
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    tampered = _canonical({"testedGitSha": "b" * 40})
    problems = pe.verify_signed_export(payload_bytes=tampered, signature_bytes=signature, trusted_public_key_pem=public_pem)
    assert problems and "FAILED" in problems[0]


def test_verify_signed_export_rejects_signature_from_a_different_key(rsa_keypair):
    _private_key, _private_pem, public_pem = rsa_keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = _canonical({"testedGitSha": "a" * 40})
    wrong_signature = other_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    problems = pe.verify_signed_export(payload_bytes=payload, signature_bytes=wrong_signature, trusted_public_key_pem=public_pem)
    assert problems


def test_verify_signed_export_rejects_nonempty_but_fake_signature_bytes(rsa_keypair):
    """The exact defect Priority 3 closes: a merely-nonempty
    signatureOrArtifactProvenance object (e.g. a literal fake base64 string)
    must NOT pass -- only a cryptographically genuine signature does."""
    _private_key, _private_pem, public_pem = rsa_keypair
    payload = _canonical({"testedGitSha": "a" * 40})
    fake_signature = base64.b64decode("ZmFrZS1zaWduYXR1cmU=")  # "fake-signature", not a real RSA signature
    problems = pe.verify_signed_export(payload_bytes=payload, signature_bytes=fake_signature, trusted_public_key_pem=public_pem)
    assert problems


def test_verify_signed_export_rejects_malformed_public_key():
    problems = pe.verify_signed_export(payload_bytes=b"x", signature_bytes=b"y", trusted_public_key_pem="not a key")
    assert problems and "not a valid" in problems[0]


def test_build_signed_export_evidence_end_to_end(rsa_keypair):
    private_key, _private_pem, public_pem = rsa_keypair
    payload = {
        "schemaVersion": 2, "component": "caleemobile-distributed-build-acceptance",
        "provider": "custom_signed_export", "channel": "testflight", "distributedBuildId": "TF-1",
        "releaseId": "r1", "testedGitSha": "a" * 40, "testedVersion": "0.0.24+24",
        "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": "2026-07-21T00:00:00Z", "timestamp": "2026-07-21T00:00:00Z",
        "sourceDigest": "sha256:" + "0" * 64,
    }
    signature = private_key.sign(_canonical(payload), padding.PKCS1v15(), hashes.SHA256())
    evidence, problems = pe.build_signed_export_evidence(
        payload=payload, signature_bytes=signature, trusted_public_key_pem=public_pem, signer_fingerprint="AA:BB:CC",
    )
    assert problems == []
    assert evidence["generatedBy"] == "signed-export"
    assert isinstance(evidence["signatureOrArtifactProvenance"], dict)
    assert evidence["signatureOrArtifactProvenance"]["signerFingerprint"] == "AA:BB:CC"
    validate_problems = dbp.validate_distributed_evidence(evidence, expected_release_id="r1")
    assert validate_problems == [], validate_problems


def test_build_signed_export_evidence_altered_payload_after_signing_blocks(rsa_keypair):
    private_key, _private_pem, public_pem = rsa_keypair
    payload = {
        "schemaVersion": 2, "provider": "custom_signed_export", "channel": "testflight",
        "distributedBuildId": "TF-1", "releaseId": "r1", "testedGitSha": "a" * 40,
        "testedVersion": "0.0.24+24", "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": "2026-07-21T00:00:00Z", "timestamp": "2026-07-21T00:00:00Z",
    }
    signature = private_key.sign(_canonical(payload), padding.PKCS1v15(), hashes.SHA256())
    altered = dict(payload, testedVersion="9.9.9")  # altered AFTER signing
    evidence, problems = pe.build_signed_export_evidence(
        payload=altered, signature_bytes=signature, trusted_public_key_pem=public_pem, signer_fingerprint="AA:BB",
    )
    assert evidence == {}
    assert problems


# --- nested provider evidence (GitHub artifact path) ------------------------


def test_verify_nested_provider_evidence_accepts_well_formed_provider_api_claim():
    evidence = {
        "schemaVersion": 2, "component": "caleemobile-distributed-build-acceptance",
        "provider": "app_store_connect", "channel": "testflight", "distributedBuildId": "TF-1",
        "releaseId": "r1", "testedGitSha": "a" * 40, "testedVersion": "0.0.24+24",
        "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": "2026-07-21T00:00:00Z", "generatedBy": "provider-api",
        "sourceDigest": "sha256:" + "1" * 64, "timestamp": "2026-07-21T00:00:00Z",
    }
    assert pe.verify_nested_provider_evidence(evidence) == []


def test_verify_nested_provider_evidence_rejects_manual_claim_even_inside_real_artifact():
    """The core Priority 3 requirement: an authenticated CI artifact
    carrying a hand-typed manual claim (never touched a provider) must
    still be rejected -- the ARTIFACT's authenticity doesn't launder the
    CONTENT's lack of provenance."""
    evidence = {
        "schemaVersion": 2, "provider": "app_store_connect", "channel": "testflight",
        "distributedBuildId": "TF-1", "releaseId": "r1", "testedGitSha": "a" * 40,
        "testedVersion": "0.0.24+24", "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": "2026-07-21T00:00:00Z", "generatedBy": "manual_claim",
        "sourceDigest": "sha256:" + "1" * 64, "timestamp": "2026-07-21T00:00:00Z",
    }
    problems = pe.verify_nested_provider_evidence(evidence)
    assert problems and any("manual_claim" in p or "rejected" in p for p in problems)


# --- CI-artifact chain (authenticate the ARTIFACT, then the nested content) -

CI_REPO = "CaleeAdmin/calee-regression"
CI_WORKFLOW_PATH = ".github/workflows/collect-distributed-build-evidence.yml"
CI_RUN_ID = "555000111"
CI_ARTIFACT_ID = "777000222"
CI_ARTIFACT_NAME = "distributed-build-evidence"
CI_RESULT_FILENAME = "distributed-build-evidence.json"


def _nested_evidence(**overrides) -> dict:
    data = {
        "schemaVersion": 2, "component": "caleemobile-distributed-build-acceptance",
        "provider": "app_store_connect", "channel": "testflight", "distributedBuildId": "TF-1",
        "releaseId": "r1", "testedGitSha": "a" * 40, "testedVersion": "0.0.24+24",
        "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": "2026-07-21T00:00:00Z", "generatedBy": "provider-api",
        "sourceDigest": "sha256:" + "1" * 64, "timestamp": "2026-07-21T00:00:00Z",
    }
    data.update(overrides)
    return data


def _ci_zip(**overrides) -> bytes:
    body = json.dumps(_nested_evidence(**overrides)).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(CI_RESULT_FILENAME, body)
    return buf.getvalue()


def _ci_run(**overrides) -> ga.WorkflowRunMetadata:
    base = dict(
        run_id=CI_RUN_ID, repo_full_name=CI_REPO, workflow_path=CI_WORKFLOW_PATH, workflow_name="collect",
        event="workflow_dispatch", head_sha="f" * 40, status="completed", conclusion="success",
    )
    base.update(overrides)
    return ga.WorkflowRunMetadata(**base)


def _ci_artifact(zip_bytes: bytes, **overrides) -> ga.ArtifactMetadata:
    base = dict(
        artifact_id=CI_ARTIFACT_ID, name=CI_ARTIFACT_NAME, expired=False, size_in_bytes=len(zip_bytes),
        digest="sha256:" + ga.sha256_hex(zip_bytes), workflow_run_id=CI_RUN_ID,
        archive_download_url="https://api.github.com/x/zip",
    )
    base.update(overrides)
    return ga.ArtifactMetadata(**base)


def _verify_ci_chain(zb, run=None, artifact=None, **kwargs):
    kwargs.setdefault("expected_repository", CI_REPO)
    kwargs.setdefault("expected_workflow_path", CI_WORKFLOW_PATH)
    kwargs.setdefault("expected_artifact_name", CI_ARTIFACT_NAME)
    kwargs.setdefault("expected_result_filename", CI_RESULT_FILENAME)
    return pe.verify_provider_ci_artifact_chain(
        run if run is not None else _ci_run(),
        artifact if artifact is not None else _ci_artifact(zb),
        zb, **kwargs,
    )


def test_verify_provider_ci_artifact_chain_accepts_well_formed():
    zb = _ci_zip()
    chain = _verify_ci_chain(zb)
    assert chain.ok, chain.problems
    assert chain.result["distributedBuildId"] == "TF-1"


def test_verify_provider_ci_artifact_chain_rejects_wrong_repository():
    zb = _ci_zip()
    chain = _verify_ci_chain(zb, run=_ci_run(repo_full_name="someone-else/fork"))
    assert not chain.ok
    assert any("repository" in p for p in chain.problems)


def test_verify_provider_ci_artifact_chain_rejects_wrong_workflow_path():
    zb = _ci_zip()
    chain = _verify_ci_chain(zb, run=_ci_run(workflow_path=".github/workflows/other.yml"))
    assert not chain.ok
    assert any("workflow path" in p for p in chain.problems)


def test_verify_provider_ci_artifact_chain_rejects_unsuccessful_run():
    zb = _ci_zip()
    chain = _verify_ci_chain(zb, run=_ci_run(conclusion="failure"))
    assert not chain.ok
    assert any("conclusion" in p for p in chain.problems)


def test_verify_provider_ci_artifact_chain_rejects_digest_mismatch():
    zb = _ci_zip()
    tampered = zb + b"\x00"
    chain = _verify_ci_chain(tampered, artifact=_ci_artifact(zb))  # digest recorded for the ORIGINAL bytes
    assert not chain.ok
    assert any("digest" in p or "size_in_bytes" in p for p in chain.problems)


def test_verify_provider_ci_artifact_chain_rejects_artifact_from_another_run():
    zb = _ci_zip()
    chain = _verify_ci_chain(zb, artifact=_ci_artifact(zb, workflow_run_id="999999999"))
    assert not chain.ok
    assert any("belongs to run" in p for p in chain.problems)


def test_verify_provider_ci_artifact_chain_rejects_nested_manual_claim():
    """The core Priority 3 requirement, exercised through the full chain
    this time: authenticating the ARTIFACT never launders a hand-typed
    claim inside it."""
    zb = _ci_zip(generatedBy="manual_claim")
    chain = _verify_ci_chain(zb, artifact=_ci_artifact(zb))
    assert not chain.ok
    assert any("manual_claim" in p or "rejected" in p for p in chain.problems)


def test_verify_provider_ci_artifact_chain_enforces_expected_release_id():
    zb = _ci_zip(releaseId="r1")
    chain = _verify_ci_chain(zb, artifact=_ci_artifact(zb), expected_release_id="r2")
    assert not chain.ok
    assert any("releaseId" in p for p in chain.problems)

    ok_chain = _verify_ci_chain(zb, artifact=_ci_artifact(zb), expected_release_id="r1")
    assert ok_chain.ok, ok_chain.problems


def test_verify_provider_ci_artifact_chain_summary_mentions_build_id_or_rejection():
    zb = _ci_zip()
    ok_chain = _verify_ci_chain(zb)
    assert "TF-1" in ok_chain.summary()
    bad_chain = _verify_ci_chain(zb, run=_ci_run(conclusion="failure"))
    assert "REJECTED" in bad_chain.summary()


# --- acquire_provider_ci_artifact (live layer -- injected fetchers only) ----


def test_acquire_provider_ci_artifact_requires_run_id():
    with pytest.raises(pe.ProviderEvidenceError, match="run id"):
        pe.acquire_provider_ci_artifact(
            repository=CI_REPO, workflow_path=CI_WORKFLOW_PATH, run_id=None, artifact_id=CI_ARTIFACT_ID,
            expected_artifact_name=CI_ARTIFACT_NAME, expected_result_filename=CI_RESULT_FILENAME, env={},
        )


def test_acquire_provider_ci_artifact_requires_artifact_id():
    with pytest.raises(pe.ProviderEvidenceError, match="artifact id"):
        pe.acquire_provider_ci_artifact(
            repository=CI_REPO, workflow_path=CI_WORKFLOW_PATH, run_id=CI_RUN_ID, artifact_id=None,
            expected_artifact_name=CI_ARTIFACT_NAME, expected_result_filename=CI_RESULT_FILENAME, env={},
        )


def test_acquire_provider_ci_artifact_blocks_without_token_naming_the_secret():
    with pytest.raises(pe.ProviderEvidenceError, match="REGRESSION_API_TOKEN"):
        pe.acquire_provider_ci_artifact(
            repository=CI_REPO, workflow_path=CI_WORKFLOW_PATH, run_id=CI_RUN_ID, artifact_id=CI_ARTIFACT_ID,
            expected_artifact_name=CI_ARTIFACT_NAME, expected_result_filename=CI_RESULT_FILENAME, env={},
        )


def test_acquire_provider_ci_artifact_never_contacts_real_network_uses_injected_fetchers_end_to_end():
    zb = _ci_zip()

    def json_fetcher(url: str) -> dict:
        if url.endswith(f"/runs/{CI_RUN_ID}"):
            return {
                "id": int(CI_RUN_ID), "repository": {"full_name": CI_REPO}, "path": CI_WORKFLOW_PATH,
                "name": "collect", "event": "workflow_dispatch", "head_sha": "f" * 40,
                "status": "completed", "conclusion": "success",
            }
        if url.endswith(f"/artifacts/{CI_ARTIFACT_ID}"):
            return {
                "id": int(CI_ARTIFACT_ID), "name": CI_ARTIFACT_NAME, "expired": False,
                "size_in_bytes": len(zb), "digest": "sha256:" + ga.sha256_hex(zb),
                "workflow_run": {"id": int(CI_RUN_ID)}, "archive_download_url": "https://api.github.com/x/zip",
            }
        raise AssertionError(f"unexpected url {url}")

    def bytes_fetcher(url: str) -> bytes:
        assert "zip" in url
        return zb

    chain = pe.acquire_provider_ci_artifact(
        repository=CI_REPO, workflow_path=CI_WORKFLOW_PATH, run_id=CI_RUN_ID, artifact_id=CI_ARTIFACT_ID,
        expected_artifact_name=CI_ARTIFACT_NAME, expected_result_filename=CI_RESULT_FILENAME,
        json_fetcher=json_fetcher, bytes_fetcher=bytes_fetcher, token="fake",
    )
    assert chain.ok, chain.problems
    assert chain.result["distributedBuildId"] == "TF-1"
