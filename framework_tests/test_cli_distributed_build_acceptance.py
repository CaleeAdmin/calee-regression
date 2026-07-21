"""CLI-level tests for `record-distributed-build-acceptance` (Priority 3,
this session): the --source provenance path is the ONLY way to reach PASS;
the legacy manual/flag path can only ever record blocked-unverified evidence.
"""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner

from calee_regression import cli, run_context
from calee_regression import distributed_build_provenance as dbp
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


def test_valid_source_evidence_passes(tmp_path):
    _make_workspace(tmp_path)
    source = _write_source_file(tmp_path)
    result = _invoke(tmp_path, "--source", source)
    assert result.exit_code == EXIT_SUCCESS, result.output

    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    report = json.loads(workspace.component_report_path("distributed-build-acceptance").read_text())
    assert report["status"] == "passed"
    assert "provenance" in report
    assert report["provenance"]["sourceEvidence"]["testedGitSha"] == SHA_RELEASE

    component_dir = workspace.component_dir("distributed-build-acceptance")
    assert (component_dir / dbp.BUNDLE_SOURCE_JSON).is_file()
    assert (component_dir / dbp.BUNDLE_SOURCE_SHA).is_file()
    assert (component_dir / dbp.BUNDLE_PROVENANCE).is_file()
    # The exact bytes given are preserved verbatim.
    assert (component_dir / dbp.BUNDLE_SOURCE_JSON).read_bytes() == open(source, "rb").read()


def test_altered_source_bytes_block_at_consolidation(tmp_path):
    """Priority 3 offline test #11: alter the preserved raw bytes AFTER
    adoption -- the next re-verification (at consolidation) must BLOCK."""
    workspace = _make_workspace(tmp_path)
    source = _write_source_file(tmp_path)
    result = _invoke(tmp_path, "--source", source)
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
