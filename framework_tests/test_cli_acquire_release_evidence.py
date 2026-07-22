"""CLI tests for acquire-release-evidence / inspect-release-evidence and the
qualification-preflight acquisition wiring. Fully offline: bundle
verification and acquisition seams are monkeypatched; no network, no device.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from calee_regression import cli
from calee_regression import evidence_acquisition as ea
from calee_regression import release_installer as ri
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

RUN_ID = "release-cli-acq-000001"
RELEASE_ID = "2026.07-rc1"
CM_SHA = "c" * 40


class _StubVerification:
    def __init__(self, ok=True, manifest=None, errors=None):
        self.ok = ok
        self.errors = errors or []
        self.manifest = manifest


def _manifest():
    return ri.ReleaseManifest(
        release_id=RELEASE_ID, schema_version=ri.RELEASE_MANIFEST_SCHEMA_V2,
        platforms=ri.PlatformScope(tablet=True, mobile_android=False, mobile_ios=False),
        calee_mobile=ri.CaleeMobileExpected(
            version="2.3.4+56", git_sha=CM_SHA,
            selector_evidence_required=False, distributed_build_acceptance_required=False,
        ),
    )


def _env(tmp_path):
    return {"CALEE_REPORT_ROOT": str(tmp_path), "REGRESSION_API_TOKEN": "",
            "GITHUB_TOKEN": "", "GH_TOKEN": ""}


def _prep(tmp_path, monkeypatch, *, ok=True):
    bundle = tmp_path / "bundle"
    bundle.mkdir(exist_ok=True)
    verification = _StubVerification(ok=ok, manifest=_manifest(),
                                     errors=[] if ok else ["checksum mismatch"])
    monkeypatch.setattr(ri, "verify_release_bundle", lambda path: verification)
    baseline = tmp_path / "reports" / "runs" / RUN_ID / "attempts" / "1"
    baseline.mkdir(parents=True, exist_ok=True)
    (baseline / "immutable-inputs.json").write_text(json.dumps({
        "regressionSha": "a" * 40, "caleeMobileRegressionSha": "b" * 40,
    }))
    return bundle


def test_invalid_bundle_exits_2(tmp_path, monkeypatch):
    bundle = _prep(tmp_path, monkeypatch, ok=False)
    result = CliRunner().invoke(cli.main, [
        "acquire-release-evidence", "--bundle", str(bundle), "--run-id", RUN_ID,
    ], env=_env(tmp_path))
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
    assert "INVALID" in result.output


def test_missing_token_exits_3_blocked(tmp_path, monkeypatch):
    bundle = _prep(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.main, [
        "acquire-release-evidence", "--bundle", str(bundle), "--run-id", RUN_ID,
    ], env=_env(tmp_path))
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "GitHub authentication missing" in result.output
    # The manifest is still written, secret-free, into the run workspace.
    manifest = tmp_path / "reports" / "runs" / RUN_ID / "evidence" / "acquisition-manifest.json"
    assert manifest.is_file()


def test_inspect_release_evidence_reports_missing_credentials(tmp_path, monkeypatch):
    bundle = _prep(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli.main, [
        "inspect-release-evidence", "--bundle", str(bundle), "--run-id", RUN_ID,
    ], env=_env(tmp_path))
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "Credentials available: NO" in result.output
    # Read-only: inspection never writes into the run workspace.
    assert not (tmp_path / "reports" / "runs" / RUN_ID / "evidence").exists()


def test_preflight_forwards_discovered_identities(tmp_path, monkeypatch):
    from calee_regression import qualification_preflight as qp

    bundle = _prep(tmp_path, monkeypatch)

    plan = ea.EvidencePlan(run_id=RUN_ID, release_id=RELEASE_ID, bundle_path=str(bundle))
    spec = ea.EvidenceSpec(
        evidence_type=ea.TYPE_CALEE_REGRESSION_MAIN_CI, required=True,
        repository="CaleeAdmin/calee-regression", expected_head_sha="a" * 40,
    )
    item = ea.AcquiredItem(
        spec=spec, status=ea.STATUS_ACQUIRED, source=ea.SOURCE_AUTOMATIC,
        run_data={"id": "101"}, artifact_data={"id": "9101"},
        cached_path=str(tmp_path / "cached.zip"),
    )
    outcome = ea.AcquisitionOutcome(plan=plan, items=[item])
    monkeypatch.setattr(ea, "derive_evidence_plan", lambda **kw: plan)
    monkeypatch.setattr(ea, "acquire_release_evidence", lambda p, **kw: outcome)

    captured = {}

    def _fake_preflight(**kwargs):
        captured.update(kwargs)
        return qp.PreflightReport(checks=[])

    monkeypatch.setattr(qp, "run_qualification_preflight", _fake_preflight)
    result = CliRunner().invoke(cli.main, [
        "qualification-preflight", "--bundle", str(bundle), "--run-id", RUN_ID,
    ], env=_env(tmp_path))
    assert captured["calee_regression_main_workflow_run_id"] == "101", result.output
    assert captured["calee_regression_main_artifact_id"] == "9101"
    assert captured["calee_regression_main_sha"] == "a" * 40
    assert "ACQUIRED" in result.output


def test_preflight_no_acquire_flag_skips_acquisition(tmp_path, monkeypatch):
    from calee_regression import qualification_preflight as qp

    bundle = _prep(tmp_path, monkeypatch)
    monkeypatch.setattr(ea, "acquire_release_evidence",
                        lambda p, **kw: (_ for _ in ()).throw(AssertionError("must not run")))
    monkeypatch.setattr(qp, "run_qualification_preflight",
                        lambda **kwargs: qp.PreflightReport(checks=[]))
    result = CliRunner().invoke(cli.main, [
        "qualification-preflight", "--bundle", str(bundle), "--run-id", RUN_ID, "--no-acquire",
    ], env=_env(tmp_path))
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_preflight_explicit_override_still_supported(tmp_path, monkeypatch):
    from calee_regression import qualification_preflight as qp

    bundle = _prep(tmp_path, monkeypatch)
    seen = {}

    def _fake_acquire(plan, **kwargs):
        seen["overrides"] = kwargs.get("overrides")
        return ea.AcquisitionOutcome(plan=plan, items=[])

    monkeypatch.setattr(ea, "derive_evidence_plan",
                        lambda **kw: ea.EvidencePlan(run_id=RUN_ID, release_id=RELEASE_ID,
                                                     bundle_path=str(bundle)))
    monkeypatch.setattr(ea, "acquire_release_evidence", _fake_acquire)
    captured = {}

    def _fake_preflight(**kwargs):
        captured.update(kwargs)
        return qp.PreflightReport(checks=[])

    monkeypatch.setattr(qp, "run_qualification_preflight", _fake_preflight)
    CliRunner().invoke(cli.main, [
        "qualification-preflight", "--bundle", str(bundle), "--run-id", RUN_ID,
        "--selector-workflow-run-id", "555", "--selector-artifact-id", "9555",
    ], env=_env(tmp_path))
    assert seen["overrides"][ea.TYPE_SELECTOR_CERTIFICATION]["run_id"] == "555"
    # The explicit values (not acquisition output) reach the preflight checks.
    assert captured["selector_workflow_run_id"] == "555"
    assert captured["selector_artifact_id"] == "9555"
