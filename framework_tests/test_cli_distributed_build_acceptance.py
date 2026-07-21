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
    signed with the private key. Returns
    (payload_path, signature_path, public_key_path, public_pem)."""
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
    return str(payload_path), str(signature_path), str(public_key_path), public_pem


_MINIMAL_MACHINE_CONFIG_FIELDS = dict(
    expected_tablet_state="fresh", calee_package_id="com.viso.calee", caleeshell_package_id="com.viso.caleeshell",
    home_activity="com.viso.caleeshell/.ui.LauncherActivity", calee_launch_action="com.viso.calee.action.START",
    release_bundle_dir=".", backend_url="https://example.invalid", release_profile="development", report_dir=".",
)


def _pin_trusted_signed_export_key(tmp_path, monkeypatch, public_pem: str) -> None:
    """Priority 4: pin the given key's fingerprint in machine config (at the
    default config/machine.local.yaml location under the isolated REPO_ROOT)
    and resolve the PEM itself from CALEE_SIGNED_EXPORT_PUBLIC_KEY -- the
    ONLY combination that can ever reach tier verified-signed-export."""
    import yaml

    from calee_regression import provider_evidence as pe_mod

    fingerprint = pe_mod.compute_public_key_sha256_fingerprint(public_pem)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_data = dict(_MINIMAL_MACHINE_CONFIG_FIELDS, trusted_signed_export_public_key_sha256=fingerprint)
    (config_dir / "machine.local.yaml").write_text(yaml.safe_dump(config_data))
    monkeypatch.setenv("CALEE_SIGNED_EXPORT_PUBLIC_KEY", public_pem)


def _invoke_signed_export(tmp_path, monkeypatch, *extra_args, payload_overrides=None):
    payload_path, signature_path, _public_key_path, public_pem = _write_signed_export_files(
        tmp_path, **(payload_overrides or {})
    )
    _pin_trusted_signed_export_key(tmp_path, monkeypatch, public_pem)
    return _invoke(
        tmp_path, "--signed-export", payload_path, "--export-signature", signature_path,
        "--signer-fingerprint", "AA:BB:CC:DD",
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


def test_signed_export_with_genuine_signature_passes(tmp_path, monkeypatch):
    _make_workspace(tmp_path)
    result = _invoke_signed_export(tmp_path, monkeypatch)
    assert result.exit_code == EXIT_SUCCESS, result.output

    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "passed"
    assert report["evidenceTier"] == pe.TIER_VERIFIED_SIGNED_EXPORT
    assert report["provenance"]["sourceEvidence"]["generatedBy"] == "signed-export"
    # Priority 4: the recorded signerFingerprint is the VERIFIED key's own
    # computed fingerprint, never the operator-supplied label.
    signature_provenance = report["provenance"]["sourceEvidence"]["signatureOrArtifactProvenance"]
    assert signature_provenance["signerFingerprint"] != "AA:BB:CC:DD"
    assert len(signature_provenance["signerFingerprint"]) == 64
    assert signature_provenance["operatorDeclaredSignerLabel"] == "AA:BB:CC:DD"


def test_signed_export_with_tampered_payload_blocks(tmp_path):
    """A payload altered AFTER signing (so the signature no longer matches)
    is rejected outright -- never recorded as a weaker pass. Uses the
    diagnostic --trusted-public-key-file path (simplest setup for a test
    that only cares about signature-mismatch detection, not tier)."""
    payload_path, signature_path, public_key_path, _public_pem = _write_signed_export_files(tmp_path)
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
    payload_path, _sig, _pub, _pem = _write_signed_export_files(tmp_path)
    result = _invoke(tmp_path, "--signed-export", payload_path)
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output


def test_signed_export_diagnostic_override_never_passes_even_with_a_genuine_signature(tmp_path):
    """Priority 4's core requirement: a per-command --trusted-public-key-file
    override can NEVER produce a release-gating PASS, however genuine the
    signature -- its result is always blocked-unverified, tier
    diagnostic-unpinned-key."""
    payload_path, signature_path, public_key_path, _public_pem = _write_signed_export_files(tmp_path)
    _make_workspace(tmp_path)
    result = _invoke(
        tmp_path, "--signed-export", payload_path, "--export-signature", signature_path,
        "--trusted-public-key-file", public_key_path,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "DIAGNOSTIC" in result.output

    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "blocked-unverified"
    assert report["evidenceTier"] == pe.TIER_DIAGNOSTIC_UNPINNED_KEY
    assert report["evidenceTier"] not in pe.AUTHENTICATED_TIERS


def test_signed_export_without_pinned_fingerprint_blocks(tmp_path, monkeypatch):
    """No machine config at all (no pinned fingerprint) -- the pinned path
    must BLOCK rather than silently accept whatever CALEE_SIGNED_EXPORT_
    PUBLIC_KEY happens to resolve to."""
    payload_path, signature_path, _public_key_path, public_pem = _write_signed_export_files(tmp_path)
    monkeypatch.setenv("CALEE_SIGNED_EXPORT_PUBLIC_KEY", public_pem)
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--signed-export", payload_path, "--export-signature", signature_path)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "no trusted signed-export public-key fingerprint is pinned" in result.output


def test_signed_export_with_wrong_pinned_fingerprint_blocks(tmp_path, monkeypatch):
    """The pinned fingerprint in machine config does not match the resolved
    key -- BLOCKS even though the resolved key/signature are both genuine."""
    import yaml

    payload_path, signature_path, _public_key_path, public_pem = _write_signed_export_files(tmp_path)
    monkeypatch.setenv("CALEE_SIGNED_EXPORT_PUBLIC_KEY", public_pem)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "machine.local.yaml").write_text(
        yaml.safe_dump(dict(_MINIMAL_MACHINE_CONFIG_FIELDS, trusted_signed_export_public_key_sha256="0" * 64))
    )
    _make_workspace(tmp_path)
    result = _invoke(tmp_path, "--signed-export", payload_path, "--export-signature", signature_path)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "does not match" in result.output


def test_altered_source_bytes_block_at_consolidation(tmp_path, monkeypatch):
    """Priority 3 offline test #11: alter the preserved raw bytes AFTER
    adoption -- the next re-verification (at consolidation) must BLOCK. Uses
    a genuinely PASS-able signed-export record (the --source path can never
    pass in the first place, so it can't exercise "tampered after a real
    pass")."""
    workspace = _make_workspace(tmp_path)
    result = _invoke_signed_export(tmp_path, monkeypatch)
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


def test_provider_alone_blocks_build_provenance_unavailable(tmp_path, monkeypatch):
    """Priority 1's core requirement: a provider observation ALONE -- however
    genuinely authenticated -- can never PASS. It must BLOCK naming the
    missing build-provenance side, not silently accept."""
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
        body = {"data": [{"id": "asc-build-1", "attributes": {"version": "24"}}]}
        return 200, json.dumps(body).encode(), {}

    monkeypatch.setattr(pe, "_default_https_fetcher", _fake_fetcher)

    _make_workspace(tmp_path)
    result = _invoke(
        tmp_path, "--provider", "app_store_connect", "--app-id", "123", "--build-version", "24",
        "--expected-git-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE, "--expected-release-id", RELEASE_ID,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "build provenance unavailable" in result.output


def _write_build_provenance_signed_export_files(tmp_path, **overrides):
    """A genuinely verifiable signed BUILD-PROVENANCE export -- mirrors
    _write_signed_export_files but for the build-provenance payload shape."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    payload = dict(
        schemaVersion=1, component="caleemobile-build-provenance",
        repository="CaleeAdmin/CaleeMobile", workflowRunId="42", workflowFile=".github/workflows/build-ios.yml",
        sourceGitSha=SHA_RELEASE, sourceRef="refs/heads/main", applicationVersion="0.0.24",
        platform="ios", bundleId="com.viso.caleemobile", platformBuildNumber="24",
        artifactId="777", artifactDigest="sha256:" + "9" * 64, buildTimestamp=_fresh_ts(),
    )
    payload.update(overrides)
    signature = private_key.sign(_canonical(payload), padding.PKCS1v15(), hashes.SHA256())
    payload_path = tmp_path / "build-provenance-payload.json"
    payload_path.write_text(json.dumps(payload))
    signature_path = tmp_path / "build-provenance.sig"
    signature_path.write_bytes(signature)
    return str(payload_path), str(signature_path), public_pem


def test_provider_and_build_provenance_join_end_to_end_never_leaks_private_key(tmp_path, monkeypatch):
    """Full CLI path, BOTH sides of the identity chain: a live ASC provider
    collection (a locally-generated EC test key, never a production key,
    behind a monkeypatched HTTPS fetcher -- no test in this codebase makes a
    real provider call) joined against a signed build-provenance export
    (pinned trust root). Proves the whole round trip reaches PASS AND that
    no private key material ever ends up in the recorded report.json or
    evidence bundle files -- including the two new raw source-bundle files
    (Priority 1.9: both source bundles included alongside the chain
    evidence)."""
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
        body = {
            "data": [{
                "id": "asc-build-1",
                "attributes": {"version": "24", "processingState": "VALID"},
                "relationships": {
                    "app": {"data": {"id": "APP-1", "type": "apps"}},
                    "preReleaseVersion": {"data": {"id": "PRV-1", "type": "preReleaseVersions"}},
                },
            }],
            "included": [
                {"id": "APP-1", "type": "apps", "attributes": {"bundleId": "com.viso.caleemobile"}},
                {"id": "PRV-1", "type": "preReleaseVersions", "attributes": {"version": "0.0.24"}},
            ],
        }
        return 200, json.dumps(body).encode(), {}

    monkeypatch.setattr(pe, "_default_https_fetcher", _fake_fetcher)

    bp_payload_path, bp_signature_path, bp_public_pem = _write_build_provenance_signed_export_files(tmp_path)
    _pin_trusted_signed_export_key(tmp_path, monkeypatch, bp_public_pem)

    workspace = _make_workspace(tmp_path)
    result = _invoke(
        tmp_path, "--provider", "app_store_connect", "--app-id", "123", "--build-version", "24",
        "--build-provenance-signed-export", bp_payload_path, "--build-provenance-signature", bp_signature_path,
        "--expected-git-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE, "--expected-release-id", RELEASE_ID,
    )
    assert result.exit_code == EXIT_SUCCESS, result.output

    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "passed"
    assert report["evidenceTier"] == pe.TIER_PROVIDER_BUILD_PROVENANCE_JOIN
    assert report["provenance"]["sourceEvidence"]["testedGitSha"] == SHA_RELEASE
    assert report["provenance"]["sourceEvidence"]["testedVersion"] == "0.0.24+24"
    assert report["provenance"]["sourceEvidence"]["marketingVersion"] == "0.0.24"
    assert report["provenance"]["sourceEvidence"]["platformBuildNumber"] == "24"
    report_text = json.dumps(report)
    assert pem not in report_text
    assert "BEGIN" not in report_text

    component_dir = workspace.component_dir("distributed-build-acceptance")
    for name in (dbp.BUNDLE_SOURCE_JSON, dbp.BUNDLE_SOURCE_SHA, dbp.BUNDLE_PROVENANCE) + tuple(
        n for n in dbp.JOINED_MANDATORY_FILES if n != dbp.BUNDLE_PROVENANCE
    ):
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
    payload_path, signature_path, public_key_path, _public_pem = _write_signed_export_files(tmp_path)
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
