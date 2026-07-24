"""Portable, sanitized evidence bundles (Workstream 3).

Proves export is fail-closed and secret-free, verify/inspect are offline and
never touch a live run, digest integrity is enforced, path traversal and
smuggled files are rejected, and the two profiles behave as specified.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from calee_regression import evidence_bundle as eb
from calee_regression.cli import main

TS = "2026-07-24T00:00:00Z"


def _run(tmp_path, *, extra=None, device="emulator-5554"):
    run = tmp_path / "reports" / "runs" / "release-1"
    (run / "environment").mkdir(parents=True)
    (run / "environment" / "results.json").write_text(json.dumps({
        "completenessKey": "tablet-standard", "reportType": "environment", "reportSchemaVersion": 1,
        "certificationEligible": True, "status": "pass", "deviceId": device, "runId": "release-1",
        "releaseRunId": "release-1", "fixtureVersion": "REG-2026-07", "targetEnvironment": "https://hub-dev.calee.com.au",
    }))
    (run / "summary.txt").write_text(f"PASS on {device}\n")
    (run / "setup.log").write_text("verbose adb log that must NOT be exported\n")  # denied type
    if extra:
        for rel, content in extra.items():
            p = run / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return run


# ── roundtrip ───────────────────────────────────────────────────────────────
def test_export_inspect_verify_roundtrip(tmp_path):
    run = _run(tmp_path)
    out = tmp_path / "b.zip"
    manifest = eb.export_bundle(run, out, profile=eb.PROFILE_AUDIT, timestamp=TS)
    assert manifest["sourceRunId"] == "release-1"
    # denied .log excluded, recorded as skipped
    paths = {f["path"] for f in manifest["files"]}
    assert "environment/results.json" in paths and "summary.txt" in paths
    assert "setup.log" not in paths
    assert "setup.log" in manifest["skippedFiles"]

    result = eb.verify_bundle(out)
    assert result["valid"], result["problems"]

    summary = eb.inspect_bundle(out)
    assert summary["sourceRunId"] == "release-1"
    assert summary["fixtureVersion"] == "REG-2026-07"
    assert summary["fileCount"] == 2


# ── profiles ────────────────────────────────────────────────────────────────
def test_audit_profile_pseudonymizes_devices_and_is_non_certifying(tmp_path):
    run = _run(tmp_path, device="00008120-REALUDID")
    out = tmp_path / "audit.zip"
    manifest = eb.export_bundle(run, out, profile=eb.PROFILE_AUDIT, timestamp=TS)
    assert manifest["nonCertifyingAfterImport"] is True
    with zipfile.ZipFile(out) as zf:
        body = zf.read("evidence/environment/results.json").decode()
    assert "00008120-REALUDID" not in body          # real udid gone
    assert "device-1" in body                          # pseudonym present


def test_certification_transfer_preserves_exact_identities(tmp_path):
    run = _run(tmp_path, device="00008120-REALUDID")
    out = tmp_path / "cert.zip"
    manifest = eb.export_bundle(run, out, profile=eb.PROFILE_CERT_TRANSFER, timestamp=TS)
    assert manifest["nonCertifyingAfterImport"] is False
    with zipfile.ZipFile(out) as zf:
        body = zf.read("evidence/environment/results.json").decode()
    assert "00008120-REALUDID" in body                 # exact identity kept
    assert "digests prove the bundle's INTEGRITY" in manifest["integrityNote"]


# ── fail-closed security ────────────────────────────────────────────────────
def test_export_fails_closed_on_a_credential_shape(tmp_path):
    run = _run(tmp_path, extra={"leaky.json": json.dumps({"password": "hunter2secret"})})
    with pytest.raises(eb.EvidenceBundleError) as exc:
        eb.export_bundle(run, tmp_path / "x.zip", profile=eb.PROFILE_AUDIT, timestamp=TS)
    assert "credential shape" in str(exc.value)
    assert "hunter2secret" not in str(exc.value)       # never echoes the value
    assert not (tmp_path / "x.zip").exists()            # nothing written


def test_export_rejects_a_symlink(tmp_path):
    run = _run(tmp_path)
    target = run / "environment" / "results.json"
    (run / "sneaky.json").symlink_to(target)
    with pytest.raises(eb.EvidenceBundleError) as exc:
        eb.export_bundle(run, tmp_path / "x.zip", profile=eb.PROFILE_AUDIT, timestamp=TS)
    assert "symlink" in str(exc.value)


def test_verify_rejects_digest_tampering(tmp_path):
    run = _run(tmp_path)
    out = tmp_path / "b.zip"
    eb.export_bundle(run, out, profile=eb.PROFILE_AUDIT, timestamp=TS)
    # Rebuild the zip with one evidence file altered but its manifest digest unchanged.
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "evidence/summary.txt":
                data = b"TAMPERED\n"
            zout.writestr(item, data)
    result = eb.verify_bundle(tampered)
    assert not result["valid"]
    assert any("digest mismatch" in p for p in result["problems"])


def test_verify_rejects_smuggled_file(tmp_path):
    run = _run(tmp_path)
    out = tmp_path / "b.zip"
    eb.export_bundle(run, out, profile=eb.PROFILE_AUDIT, timestamp=TS)
    smuggled = tmp_path / "smuggled.zip"
    with zipfile.ZipFile(out) as zin, zipfile.ZipFile(smuggled, "w") as zout:
        for item in zin.namelist():
            zout.writestr(item, zin.read(item))
        zout.writestr("evidence/extra.json", json.dumps({"not": "in manifest"}))
    result = eb.verify_bundle(smuggled)
    assert not result["valid"]
    assert any("not in the manifest" in p for p in result["problems"])


def test_verify_rejects_path_traversal_entry(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "schemaVersion": 1, "bundleType": eb.BUNDLE_TYPE, "profile": "audit",
            "sourceRunId": "r", "files": [],
        }))
        zf.writestr("../escape.json", "{}")
    result = eb.verify_bundle(evil)
    assert not result["valid"]
    assert any("traversal" in p for p in result["problems"])


def test_verify_reports_missing_manifest(tmp_path):
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("evidence/x.json", "{}")
    result = eb.verify_bundle(plain)
    assert not result["valid"]
    assert any("manifest" in p.lower() for p in result["problems"])


def test_inspect_never_writes_to_disk(tmp_path):
    run = _run(tmp_path)
    out = tmp_path / "b.zip"
    eb.export_bundle(run, out, profile=eb.PROFILE_AUDIT, timestamp=TS)
    before = {p.name for p in tmp_path.iterdir()}
    eb.inspect_bundle(out)
    after = {p.name for p in tmp_path.iterdir()}
    assert before == after  # no extraction side effects


# ── CLI ─────────────────────────────────────────────────────────────────────
def test_cli_export_verify_inspect(tmp_path):
    _run(tmp_path)
    reports_root = tmp_path / "reports"
    out = tmp_path / "cli.zip"
    r = CliRunner().invoke(main, ["evidence-bundle", "export", "--run-id", "release-1",
                                  "--reports-root", str(reports_root), "--output", str(out), "--profile", "audit"])
    assert r.exit_code == 0, r.output
    assert out.exists()
    v = CliRunner().invoke(main, ["evidence-bundle", "verify", str(out)])
    assert v.exit_code == 0, v.output
    i = CliRunner().invoke(main, ["evidence-bundle", "inspect", str(out)])
    assert i.exit_code == 0, i.output
    assert json.loads(i.output)["sourceRunId"] == "release-1"


def test_cli_export_refuses_secret_bearing_run(tmp_path):
    _run(tmp_path, extra={"leaky.json": json.dumps({"access_token": "abcdef123456"})})
    reports_root = tmp_path / "reports"
    out = tmp_path / "cli.zip"
    r = CliRunner().invoke(main, ["evidence-bundle", "export", "--run-id", "release-1",
                                  "--reports-root", str(reports_root), "--output", str(out)])
    assert r.exit_code != 0
    assert not out.exists()
    assert "abcdef123456" not in r.output
