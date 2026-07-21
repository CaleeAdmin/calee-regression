"""CLI-level tests for `record-distributed-build-acceptance` (Priority 3,
this session): only a live-authenticated path (--provider / --signed-export
/ --github-run-id) can reach PASS. Operator-supplied --source evidence and
the legacy manual/flag path can only ever record blocked-unverified
evidence, no matter how well-formed the content looks.
"""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from calee_regression import cli, run_context
from calee_regression import distributed_build_provenance as dbp
from calee_regression import provider_evidence as pe
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

RUN_ID = "release-test-dba-cli-001"
SHA_RELEASE = "a" * 40
VERSION_RELEASE = "0.0.24+24"
RELEASE_ID = "2026.07.21-rc9"


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00")
    manifest.write(workspace.manifest_path)
    return workspace


def _fresh_ts() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_source_file(tmp_path, **overrides) -> "str":
    evidence = dict(
        schemaVersion=2, component="caleemobile-distributed-build-acceptance",
        provider="app_store_connect", channel="testflight", distributedBuildId="TF-9001",
        releaseId=RELEASE_ID, testedGitSha=SHA_RELEASE, testedVersion=VERSION_RELEASE,
        providerAccountOrProject="acct-99", providerRecordId="asc-build-4242",
        providerObservedAt=_fresh_ts(), generatedBy="provider-api",
        sourceDigest="sha256:" + "2" * 64, timestamp=_fresh_ts(),
    )
    evidence.update(overrides)
    path = tmp_path / "distributed-build-evidence.json"
    path.write_text(json.dumps(evidence))
    return str(path)


def _invoke(tmp_path, *args):
    return CliRunner().invoke(cli.main, ["record-distributed-build-acceptance", "--run-id", RUN_ID, *args])


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _write_signed_export_files(tmp_path, **overrides):
    """Priority 3: a genuinely verifiable signed-export -- a real RSA
    keypair generated locally (never a production key), a canonical payload
    signed with the private key, and the PUBLIC key written out as the
    'configured trusted public key' (via --trusted-public-key-file, so no
    Keychain/env var is needed in a test). Returns
    (payload_path, signature_path, public_key_path)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    payload = dict(
        schemaVersion=2, component="caleemobile-distributed-build-acceptance",
        provider="custom_signed_export", channel="testflight", distributedBuildId="TF-9001",
        releaseId=RELEASE_ID, testedGitSha=SHA_RELEASE, testedVersion=VERSION_RELEASE,
        providerAccountOrProject="acct-99", providerRecordId="export-4242",
        providerObservedAt=_fresh_ts(), sourceDigest="sha256:" + "2" * 64, timestamp=_fresh_ts(),
    )
    payload.update(overrides)
    signature = private_key.sign(_canonical(payload), padding.PKCS1v15(), hashes.SHA256())

    payload_path = tmp_path / "signed-export-payload.json"
    payload_path.write_text(json.dumps(payload))
    signature_path = tmp_path / "signed-export.sig"
    signature_path.write_bytes(signature)
    public_key_path = tmp_path / "trusted-public-key.pem"
    public_key_path.write_text(public_pem)
    return str(payload_path), str(signature_path), str(public_key_path)


def _invoke_signed_export(tmp_path, *extra_args, payload_overrides=None):
    payload_path, signature_path, public_key_path = _write_signed_export_files(tmp_path, **(payload_overrides or {}))
    return _invoke(
        tmp_path, "--signed-export", payload_path, "--export-signature", signature_path,
        "--trusted-public-key-file", public_key_path, "--signer-fingerprint", "AA:BB:CC:DD",
        *extra_args,
    )


def test_valid_looking_source_evidence_still_blocks_unverified(tmp_path):
    """The actual Priority 3 requirement: a well-formed, internally
    self-consistent --source file can NEVER reach PASS any more -- it is
    always recorded as an explicit blocked-unverified claim, evidence and
    all (never silently dropped)."""
    _make_workspace(tmp_path)
    source = _write_source_file(tmp_path)
    result = _invoke(tmp_path, "--source", source)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "never independently authenticated" in result.output

    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "blocked-unverified"
    assert report["evidenceTier"] == pe.TIER_MANUAL_UNVERIFIED
    assert "provenance" in report
    assert report["provenance"]["sourceEvidence"]["testedGitSha"] == SHA_RELEASE

    component_dir = workspace.component_dir("distributed-build-acceptance")
    assert (component_dir / dbp.BUNDLE_SOURCE_JSON).is_file()
    assert (component_dir / dbp.BUNDLE_SOURCE_SHA).is_file()
    assert (component_dir / dbp.BUNDLE_PROVENANCE).is_file()
    # The exact bytes given are preserved verbatim.
    assert (component_dir / dbp.BUNDLE_SOURCE_JSON).read_bytes() == open(source, "rb").read()


def test_signed_export_with_genuine_signature_passes(tmp_path):
    _make_workspace(tmp_path)
    result = _invoke_signed_export(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output

    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "passed"
    assert report["evidenceTier"] == pe.TIER_VERIFIED_SIGNED_EXPORT
    assert report["provenance"]["sourceEvidence"]["generatedBy"] == "signed-export"
    assert report["provenance"]["sourceEvidence"]["signatureOrArtifactProvenance"]["signerFingerprint"] == "AA:BB:CC:DD"


def test_signed_export_with_tampered_payload_blocks(tmp_path):
    """A payload altered AFTER signing (so the signature no longer matches)
    is rejected outright -- never recorded as a weaker pass."""
    payload_path, signature_path, public_key_path = _write_signed_export_files(tmp_path)
    tampered = json.loads(open(payload_path).read())
    tampered["testedVersion"] = "9.9.9+9"
    open(payload_path, "w").write(json.dumps(tampered))

    _make_workspace(tmp_path)
    result = _invoke(
        tmp_path, "--signed-export", payload_path, "--export-signature", signature_path,
        "--trusted-public-key-file", public_key_path,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "signature verification FAILED" in result.output


def test_signed_export_requires_export_signature(tmp_path):
    _make_workspace(tmp_path)
    payload_path, _sig, _pub = _write_signed_export_files(tmp_path)
    result = _invoke(tmp_path, "--signed-export", payload_path)
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output


def test_altered_source_bytes_block_at_consolidation(tmp_path):
    """Priority 3 offline test #11: alter the preserved raw bytes AFTER
    adoption -- the next re-verification (at consolidation) must BLOCK. Uses
    a genuinely PASS-able signed-export record (the --source path can never
    pass in the first place, so it can't exercise "tampered after a real
    pass")."""
    workspace = _make_workspace(tmp_path)
    result = _invoke_signed_export(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output

    component_dir = workspace.component_dir("distributed-build-acceptance")
    source_bundle = component_dir / dbp.BUNDLE_SOURCE_JSON
    source_bundle.write_bytes(source_bundle.read_bytes() + b"\ntampered-trailing-byte")

    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    problems = dbp.verify_provenance_record(
        report["provenance"], source_bytes=source_bundle.read_bytes(), expected_release_run_id=RUN_ID,
    )
    assert any("raw-byte digest mismatch" in p for p in problems)


def test_missing_provider_record_id_blocks(tmp_path):
    _make_workspace(tmp_path)
    source = _write_source_file(tmp_path, providerRecordId="")
    result = _invoke(tmp_path, "--source", source)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "providerRecordId" in result.output


def test_wrong_expected_release_id_blocks(tmp_path):
    _make_workspace(tmp_path)
    source = _write_source_file(tmp_path)
    result = _invoke(tmp_path, "--source", source, "--expected-release-id", "2099.01.01-someone-else")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "another release" in result.output


def test_wrong_expected_sha_blocks(tmp_path):
    _make_workspace(tmp_path)
    source = _write_source_file(tmp_path)
    result = _invoke(tmp_path, "--source", source, "--expected-git-sha", "b" * 40)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "different CaleeMobile commit" in result.output


def test_wrong_expected_version_blocks(tmp_path):
    _make_workspace(tmp_path)
    source = _write_source_file(tmp_path)
    result = _invoke(tmp_path, "--source", source, "--expected-version", "9.9.9+9")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "different CaleeMobile version" in result.output


def test_malformed_source_json_is_invalid_config(tmp_path):
    _make_workspace(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    result = _invoke(tmp_path, "--source", str(bad))
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output


# ── live provider-API path (--provider): flag validation, credential-BLOCKED
#    (no test here ever contacts a real provider or uses a real key) ───────


def test_provider_app_store_connect_requires_app_id_and_build_version(tmp_path):
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--provider", "app_store_connect")
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
    assert "--app-id" in result.output and "--build-version" in result.output


def test_provider_play_console_requires_package_name_and_track(tmp_path):
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--provider", "play_console")
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
    assert "--package-name" in result.output and "--track" in result.output


def test_provider_app_store_connect_blocks_without_credentials(tmp_path, monkeypatch):
    for var in ("CALEE_ASC_KEY_ID", "CALEE_ASC_ISSUER_ID", "CALEE_ASC_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--provider", "app_store_connect", "--app-id", "123", "--build-version", "24")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "CALEE_ASC_KEY_ID" in result.output


def test_provider_live_collection_end_to_end_never_leaks_private_key(tmp_path, monkeypatch):
    """Full CLI path with real credentials (a locally-generated EC test key,
    never a production key) and a monkeypatched HTTPS fetcher standing in
    for the network -- no test in this codebase makes a real provider call.
    Proves the whole round trip reaches PASS AND that the private key never
    ends up in the recorded report.json or evidence bundle files."""
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("CALEE_ASC_KEY_ID", "test-key-id")
    monkeypatch.setenv("CALEE_ASC_ISSUER_ID", "test-issuer-id")
    monkeypatch.setenv("CALEE_ASC_PRIVATE_KEY", pem)

    def _fake_fetcher(url, headers):
        assert "Authorization" in headers
        body = {"data": [{"id": "asc-build-1", "attributes": {"version": VERSION_RELEASE}}]}
        return 200, json.dumps(body).encode(), {}

    monkeypatch.setattr(pe, "_default_https_fetcher", _fake_fetcher)

    workspace = _make_workspace(tmp_path)
    result = _invoke(
        tmp_path, "--provider", "app_store_connect", "--app-id", "123", "--build-version", "24",
        "--expected-git-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE, "--expected-release-id", RELEASE_ID,
    )
    assert result.exit_code == EXIT_SUCCESS, result.output

    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "passed"
    assert report["evidenceTier"] == pe.TIER_PROVIDER_API_LIVE
    report_text = json.dumps(report)
    assert pem not in report_text
    assert "BEGIN" not in report_text

    component_dir = workspace.component_dir("distributed-build-acceptance")
    for name in (dbp.BUNDLE_SOURCE_JSON, dbp.BUNDLE_SOURCE_SHA, dbp.BUNDLE_PROVENANCE):
        text = (component_dir / name).read_text()
        assert pem not in text
        assert "BEGIN" not in text


def test_provider_play_console_blocks_without_credentials(tmp_path, monkeypatch):
    for var in ("CALEE_PLAY_ACCESS_TOKEN", "CALEE_PLAY_SERVICE_ACCOUNT_JSON"):
        monkeypatch.delenv(var, raising=False)
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--provider", "play_console", "--package-name", "au.com.calee", "--track", "internal")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "CALEE_PLAY_ACCESS_TOKEN" in result.output or "CALEE_PLAY_SERVICE_ACCOUNT_JSON" in result.output


# ── github-authenticated-artifact path (--github-run-id): flag validation --


def test_github_run_id_requires_the_other_github_flags(tmp_path):
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--github-run-id", "12345")
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
    assert "--github-artifact-id" in result.output


def test_github_run_id_blocks_without_credentials(tmp_path, monkeypatch):
    for var in ("REGRESSION_API_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    _make_workspace(tmp_path)
    result = _invoke(
        tmp_path, "--github-run-id", "111", "--github-artifact-id", "222",
        "--github-repository", "CaleeAdmin/calee-regression",
        "--github-workflow-file", ".github/workflows/collect.yml",
        "--github-artifact-name", "distributed-build-evidence",
        "--github-result-filename", "distributed-build-evidence.json",
    )
    assert result.exit_code == EXIT_BLOCKED, result.output


# ── mutual exclusion of live-verification modes ─────────────────────────


def test_provider_and_signed_export_together_is_invalid_config(tmp_path):
    _make_workspace(tmp_path)
    payload_path, signature_path, public_key_path = _write_signed_export_files(tmp_path)
    result = _invoke(
        tmp_path, "--provider", "app_store_connect", "--app-id", "1", "--build-version", "1",
        "--signed-export", payload_path, "--export-signature", signature_path,
        "--trusted-public-key-file", public_key_path,
    )
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
    assert "Only one of" in result.output


# ── legacy manual path: deprecated, can never PASS ──────────────────────


def test_legacy_manual_claim_with_wellformed_fields_is_blocked_unverified(tmp_path):
    _make_workspace(tmp_path)
    result = _invoke(
        tmp_path,
        "--channel", "testflight", "--distributed-build-id", "TF-1",
        "--tested-git-sha", SHA_RELEASE, "--tested-version", VERSION_RELEASE,
        "--verified-via", "testflight_api",
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "blocked-unverified"
    assert any("DEPRECATED" in p for p in report["problems"])


def test_legacy_manual_local_checkout_still_explicitly_rejected(tmp_path):
    _make_workspace(tmp_path)
    result = _invoke(
        tmp_path,
        "--channel", "testflight", "--distributed-build-id", "TF-1",
        "--tested-git-sha", SHA_RELEASE, "--tested-version", VERSION_RELEASE,
        "--verified-via", "local_checkout",
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "explicitly rejected" in result.output


def test_neither_source_nor_full_legacy_flags_is_invalid_config(tmp_path):
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--channel", "testflight")  # incomplete legacy flags, no --source
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
