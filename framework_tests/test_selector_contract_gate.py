"""CaleeMobile selector evidence as an automatic release gate (Priority 1).

Two independent gates are exercised here:

  * the ``selector-contract`` command -- obtains/validates/records selector
    evidence for the EXACT release build and exits BLOCKED on any problem; and
  * ``consolidate`` -- re-validates the recorded evidence independently and
    includes it as a mandatory component in every report format, so a release
    can never PASS without valid selector evidence for the build being released.
"""

from __future__ import annotations

import datetime
import json
import zipfile

import pytest
from click.testing import CliRunner

from calee_regression import run_context
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS

RUN_ID = "release-test-selector-001"
SHA_RELEASE = "a" * 40
SHA_OTHER = "b" * 40
REGRESSION_SHA = "c" * 40
VERSION_RELEASE = "0.0.23+23"
UTC = datetime.timezone.utc


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)


def _fresh_ts() -> str:
    return (datetime.datetime.now(UTC) - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _evidence(**overrides) -> dict:
    """A raw selector-contract-result shape (as CaleeMobile-Regression emits)."""
    data = {
        "schemaVersion": 1,
        "component": "caleemobile-selector-contract",
        "caleemobileRef": "dev",
        "testedSha": SHA_RELEASE,
        "pubspecVersion": VERSION_RELEASE,
        "flutterVersion": "3.44.1",
        "contract": "PASS",
        "selectorsChecked": 62,
        "selectorsPresent": 62,
        "missing": [],
        "timestamp": _fresh_ts(),
    }
    data.update(overrides)
    return data


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00")
    manifest.write(workspace.manifest_path)
    return workspace


# ---------------------------------------------------------------------------
# The selector-contract command (release gate, --source path)
# ---------------------------------------------------------------------------


def _ci_provenance() -> dict:
    """The provenance a real CI-produced selector artifact carries (Problem B):
    how it was produced (ci), which CI run, and which regression commit."""
    return {"generatedBy": "ci", "workflowRunId": "1234567890", "regressionSha": REGRESSION_SHA}


def _run_gate(
    tmp_path, source_evidence, *, expected_sha=SHA_RELEASE, expected_version=VERSION_RELEASE,
    run_id=RUN_ID, extra_args=(),
):
    _make_workspace(tmp_path, run_id)
    # A realistic adopted --source is a CI artifact carrying its own provenance;
    # start from that and let the test's overrides win (so a test can drop or
    # corrupt a provenance field deliberately).
    src = {**_ci_provenance(), **source_evidence}
    source = tmp_path / "artifact.json"
    source.write_text(json.dumps(src))
    return CliRunner().invoke(
        main,
        ["selector-contract", "--run-id", run_id, "--source", str(source),
         "--expected-sha", expected_sha, "--expected-version", expected_version, *extra_args],
    )


def _recorded(tmp_path, run_id=RUN_ID):
    path = run_context.RunWorkspace(tmp_path, run_id).component_report_path("selector-contract")
    return json.loads(path.read_text())


def test_gate_accepts_valid_same_build_evidence(tmp_path):
    result = _run_gate(tmp_path, _evidence())
    assert result.exit_code == EXIT_SUCCESS, result.output
    rec = _recorded(tmp_path)
    assert rec["status"] == "passed"
    # Problem B: the gate records IMMUTABLE source provenance plus a SEPARATE
    # adoption block -- it never overwrites the source artifact's own fields.
    prov = rec["provenance"]
    assert prov["adoption"]["releaseRunId"] == RUN_ID
    assert prov["adoption"]["sourcePath"]
    assert prov["sourceContentDigest"].startswith("sha256:")
    # Source evidence preserved byte-for-byte, its own provenance intact.
    assert prov["sourceEvidence"]["generatedBy"] == "ci"
    assert prov["sourceEvidence"]["workflowRunId"] == "1234567890"
    assert prov["sourceEvidence"]["regressionSha"] == REGRESSION_SHA
    # The verification view is the source evidence itself: the gate did NOT
    # inject this run's ID into the source (that lives only in adoption).
    assert rec["evidence"]["testedSha"] == SHA_RELEASE
    assert "releaseRunId" not in rec["evidence"]


def test_gate_blocks_on_another_sha(tmp_path):
    result = _run_gate(tmp_path, _evidence(testedSha=SHA_OTHER))
    assert result.exit_code == EXIT_BLOCKED
    assert "different CaleeMobile commit" in result.output
    assert _recorded(tmp_path)["status"] == "blocked"


def test_gate_blocks_on_another_version(tmp_path):
    result = _run_gate(tmp_path, _evidence(pubspecVersion="0.0.22+22"))
    assert result.exit_code == EXIT_BLOCKED
    assert "different CaleeMobile version" in result.output


def test_gate_blocks_on_wrong_flutter(tmp_path):
    result = _run_gate(tmp_path, _evidence(flutterVersion="3.43.0"))
    assert result.exit_code == EXIT_BLOCKED
    assert "different toolchain" in result.output


def test_gate_blocks_on_not_pass_with_missing_selector(tmp_path):
    result = _run_gate(tmp_path, _evidence(contract="FAIL", selectorsPresent=61, missing=["meal_save_button"]))
    assert result.exit_code == EXIT_BLOCKED
    assert "did not PASS" in result.output or "missing selector" in result.output


def test_gate_blocks_on_stale_evidence(tmp_path):
    result = _run_gate(tmp_path, _evidence(timestamp="2020-01-01T00:00:00Z"))
    assert result.exit_code == EXIT_BLOCKED
    assert "stale" in result.output


def test_gate_blocks_on_malformed_evidence(tmp_path):
    # Unknown/unsupported schema version -> malformed (parse refuses it).
    result = _run_gate(tmp_path, _evidence(schemaVersion=999))
    assert result.exit_code == EXIT_BLOCKED
    assert "malformed" in result.output.lower() or "unsupported" in result.output.lower()


def test_gate_blocks_when_evidence_cannot_be_obtained(tmp_path):
    # No --source and no CaleeMobile-Regression/CaleeMobile checkouts next to the
    # isolated repo root -> nothing to generate from -> BLOCKED (never a pass).
    _make_workspace(tmp_path)
    result = CliRunner().invoke(
        main,
        ["selector-contract", "--run-id", RUN_ID,
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert _recorded(tmp_path)["status"] == "blocked"


def test_gate_blocks_when_expected_identity_unresolved(tmp_path):
    # No flags, no config, no detectable checkout -> cannot name the exact build.
    _make_workspace(tmp_path)
    result = CliRunner().invoke(main, ["selector-contract", "--run-id", RUN_ID])
    assert result.exit_code == EXIT_BLOCKED
    assert "expected CaleeMobile release identity" in result.output


# ---------------------------------------------------------------------------
# Priority 1, Problem B: immutable source provenance vs. release adoption
# ---------------------------------------------------------------------------


def test_gate_preserves_source_own_release_run_id(tmp_path):
    # A CI artifact that was generated FOR an earlier release run keeps its own
    # releaseRunId in sourceEvidence; the current run is recorded only in the
    # adoption block -- the source's provenance is never relabelled.
    result = _run_gate(tmp_path, _evidence(releaseRunId="an-earlier-run"))
    assert result.exit_code == EXIT_SUCCESS, result.output
    prov = _recorded(tmp_path)["provenance"]
    assert prov["sourceEvidence"]["releaseRunId"] == "an-earlier-run"
    assert prov["adoption"]["releaseRunId"] == RUN_ID


def test_gate_retains_github_artifact_identity(tmp_path):
    result = _run_gate(
        tmp_path, _evidence(),
        extra_args=["--source-artifact-id", "987654", "--source-artifact-digest", "sha256:" + "d" * 64],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    prov = _recorded(tmp_path)["provenance"]
    assert prov["sourceArtifactId"] == "987654"
    assert prov["sourceArtifactDigest"] == "sha256:" + "d" * 64


def test_gate_blocks_on_missing_generated_by(tmp_path):
    result = _run_gate(tmp_path, _evidence(generatedBy=None))
    assert result.exit_code == EXIT_BLOCKED
    assert "generatedBy" in result.output


def test_gate_blocks_on_invalid_generated_by(tmp_path):
    result = _run_gate(tmp_path, _evidence(generatedBy="jenkins"))
    assert result.exit_code == EXIT_BLOCKED
    assert "not exactly 'ci' or 'local'" in result.output


def test_gate_blocks_ci_source_without_workflow_run_id(tmp_path):
    result = _run_gate(tmp_path, _evidence(workflowRunId=None))
    assert result.exit_code == EXIT_BLOCKED
    assert "workflowRunId" in result.output


def test_gate_blocks_on_abbreviated_regression_sha(tmp_path):
    result = _run_gate(tmp_path, _evidence(regressionSha="abc1234"))
    assert result.exit_code == EXIT_BLOCKED
    assert "40-character" in result.output


def test_gate_blocks_on_self_declared_digest_mismatch(tmp_path):
    # The source claims a fingerprint it does not have -> contradictory.
    result = _run_gate(tmp_path, _evidence(artifactDigest="sha256:" + "0" * 64))
    assert result.exit_code == EXIT_BLOCKED
    assert "does not match its actual content digest" in result.output


# ---------------------------------------------------------------------------
# Priority 1, Problem A: production policy + real toolchain verification
# ---------------------------------------------------------------------------


def test_production_gate_rejects_local_generation(tmp_path):
    # Production accepts ONLY a CI artifact; with no --source it must not fall
    # back to local generation (which cannot prove the release toolchain).
    _make_workspace(tmp_path)
    result = CliRunner().invoke(
        main,
        ["selector-contract", "--run-id", RUN_ID, "--production",
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "CI-produced selector artifact" in result.output
    assert _recorded(tmp_path)["production"] is True


def test_production_gate_rejects_non_ci_source(tmp_path):
    # A locally-produced artifact is not acceptable proof for a production build.
    result = _run_gate(tmp_path, _evidence(generatedBy="local"), extra_args=["--production"])
    assert result.exit_code == EXIT_BLOCKED
    assert "generatedBy='ci'" in result.output or "CI-produced" in result.output


def test_development_gate_accepts_ci_source(tmp_path):
    # The same CI artifact is fine for a development gate.
    result = _run_gate(tmp_path, _evidence(), extra_args=["--development"])
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_local_generation_blocks_when_flutter_absent(tmp_path):
    # Development local generation must ACTUALLY run the Flutter toolchain
    # (Problem A). With no flutter on PATH the toolchain cannot be verified, so
    # a caller-supplied version string can never become proof -> BLOCKED.
    _make_workspace(tmp_path)
    cm = tmp_path / "cm"
    (cm / "lib").mkdir(parents=True)
    (cm / "pubspec.yaml").write_text("version: 0.0.23+23\n")
    reg = tmp_path / "reg"
    (reg / "ui").mkdir(parents=True)
    (reg / "ui" / "selector_contract.py").write_text("# stub emitter\n")
    (reg / "ui" / "test_selector_contract.py").write_text("# stub tests\n")
    result = CliRunner().invoke(
        main,
        ["selector-contract", "--run-id", RUN_ID, "--development",
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE,
         "--caleemobile-source", str(cm), "--regression-source", str(reg)],
    )
    assert result.exit_code == EXIT_BLOCKED
    out = result.output.lower()
    assert "toolchain" in out or "flutter is not on path" in out
    # The recorded report keeps the failed local-verification evidence.
    prov = _recorded(tmp_path)["provenance"]
    assert prov["localVerification"]["ok"] is False


# ---------------------------------------------------------------------------
# consolidate integration: mandatory component in every report format
# ---------------------------------------------------------------------------


def _seed_release(tmp_path, run_id=RUN_ID, *, selector_report="valid", selector_run_id=None):
    """Seed a workspace whose only variable is the selector-contract evidence,
    so the overall status is driven by the selector component."""
    workspace = _make_workspace(tmp_path, run_id)

    def write(component, data):
        p = workspace.component_report_path(component)
        p.write_text(json.dumps(data))

    write("environment", {"runId": run_id, "status": "pass", "detail": []})
    write("tablet", {
        "runId": run_id, "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    write("mobile-api", {"runId": run_id, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]})
    write("manual-checks", {
        "runId": run_id,
        "checks": [{"title": "Kiosk", "instruction": "swipe", "expectedResult": "no shade", "status": "pass"}],
    })

    if selector_report is not None:
        embedded = None
        status = "passed"
        if selector_report == "valid":
            embedded = _evidence(releaseRunId=run_id, generatedBy="ci", workflowRunId="42")
        elif selector_report == "other_sha":
            embedded = _evidence(testedSha=SHA_OTHER, releaseRunId=run_id, generatedBy="ci")
            status = "passed"  # even if the recorded status lies, consolidate re-checks
        elif selector_report == "other_version":
            embedded = _evidence(pubspecVersion="0.0.22+22", releaseRunId=run_id, generatedBy="ci")
        elif selector_report == "stale":
            embedded = _evidence(timestamp="2020-01-01T00:00:00Z", releaseRunId=run_id, generatedBy="ci")
        elif selector_report == "malformed":
            embedded = {"nonsense": True}
        elif selector_report == "no_provenance":
            embedded = _evidence()  # no releaseRunId/generatedBy/workflowRunId
        report = {
            "component": "caleemobile-selector-contract-gate",
            "releaseRunId": selector_run_id or run_id,
            "runId": selector_run_id or run_id,
            "status": status,
            "evidence": embedded,
        }
        workspace.component_report_path("selector-contract").write_text(json.dumps(report))
        # The gate command also writes the raw stamped evidence under a clear
        # filename; mirror that so the ZIP-artifact assertion is realistic.
        if isinstance(embedded, dict):
            (workspace.component_dir("selector-contract") / "selector-contract-result.json").write_text(
                json.dumps(embedded)
            )
    return workspace


def _consolidate(tmp_path, run_id=RUN_ID, extra_args=()):
    return CliRunner().invoke(
        main,
        ["consolidate", "--run-id", run_id,
         "--build-version", "9.9.9",
         "--android-optional", "--ios-optional", "--sync-optional",
         "--meals-optional", "--onboarding-optional", "--google-calendar-optional", "--kiosk-admin-optional",
         "--calee-build-version", "0.3.22", "--calee-application-id", "com.viso.calee", "--calee-version-code", "322",
         "--caleemobile-git-sha", SHA_RELEASE, "--caleemobile-build-version", VERSION_RELEASE,
         "--selector-contract-mandatory",
         "--out-dir", str(tmp_path / "out"), *extra_args],
    )


def test_consolidate_passes_with_valid_same_run_evidence(tmp_path):
    _seed_release(tmp_path, selector_report="valid")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "CaleeMobile selector contract" in result.output


def test_consolidate_blocks_when_evidence_missing(tmp_path):
    _seed_release(tmp_path, selector_report=None)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED
    assert "selector contract" in result.output.lower()


def test_consolidate_blocks_on_cross_run_evidence(tmp_path):
    # A selector report stamped with a DIFFERENT run ID is rejected by run-ID
    # validation -> treated as not executed -> BLOCKED.
    _seed_release(tmp_path, selector_report="valid", selector_run_id="some-other-run")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED


def test_consolidate_blocks_on_another_sha(tmp_path):
    _seed_release(tmp_path, selector_report="other_sha")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED


def test_consolidate_blocks_on_another_version(tmp_path):
    _seed_release(tmp_path, selector_report="other_version")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED


def test_consolidate_blocks_on_stale_evidence(tmp_path):
    _seed_release(tmp_path, selector_report="stale")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED


def test_consolidate_blocks_on_malformed_evidence(tmp_path):
    _seed_release(tmp_path, selector_report="malformed")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED


def test_consolidate_blocks_on_missing_provenance(tmp_path):
    _seed_release(tmp_path, selector_report="no_provenance")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED


def _seed_and_gate(tmp_path, run_id=RUN_ID):
    """Seed a release workspace and produce a REAL selector report (with an
    immutable provenance record + content digest) via the gate, so tampering
    tests operate on genuine gate output rather than a hand-built shape."""
    _seed_release(tmp_path, run_id, selector_report=None)
    src = {**_evidence(), **_ci_provenance()}
    source = tmp_path / "artifact.json"
    source.write_text(json.dumps(src))
    r = CliRunner().invoke(
        main,
        ["selector-contract", "--run-id", run_id, "--source", str(source),
         "--expected-sha", SHA_RELEASE, "--expected-version", VERSION_RELEASE],
    )
    assert r.exit_code == EXIT_SUCCESS, r.output
    return run_context.RunWorkspace(tmp_path, run_id).component_report_path("selector-contract")


def _selector_component(tmp_path):
    """The selector-contract component from the written consolidated report."""
    report = json.loads((tmp_path / "out" / "consolidated-report.json").read_text())
    for c in report["components"]:
        if c["name"] == "CaleeMobile selector contract":
            return c
    raise AssertionError(f"no selector component in {[c['name'] for c in report['components']]}")


def _tampered_value(field, current):
    if field in ("selectorsChecked", "selectorsPresent"):
        return (current or 0) + 1
    if field == "schemaVersion":
        return 999
    if isinstance(current, str):
        return current + "X"
    return "tampered"


@pytest.mark.parametrize(
    "field",
    [
        "schemaVersion", "component", "testedSha", "pubspecVersion", "flutterVersion",
        "contract", "selectorsChecked", "selectorsPresent", "timestamp",
        "generatedBy", "workflowRunId", "regressionSha",
    ],
)
def test_consolidation_blocks_on_tampered_source_evidence(tmp_path, field):
    # Problem B: modify EVERY field of the preserved source evidence after the
    # digest was generated and prove consolidation BLOCKS on the digest mismatch.
    report_path = _seed_and_gate(tmp_path)
    report = json.loads(report_path.read_text())
    src_ev = report["provenance"]["sourceEvidence"]
    src_ev[field] = _tampered_value(field, src_ev.get(field))
    report_path.write_text(json.dumps(report))
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED, result.output
    component = _selector_component(tmp_path)
    assert component["status"].lower() == "blocked"
    detail = " ".join(component["detail"])
    assert "digest mismatch" in detail or "modified after adoption" in detail


def test_consolidation_blocks_on_tampered_adoption_run_id(tmp_path):
    # Re-pointing the adoption to a different run must not smuggle evidence into
    # this run: the adoption releaseRunId is checked against the current run.
    report_path = _seed_and_gate(tmp_path)
    report = json.loads(report_path.read_text())
    report["provenance"]["adoption"]["releaseRunId"] = "some-other-run"
    report_path.write_text(json.dumps(report))
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED, result.output
    detail = " ".join(_selector_component(tmp_path)["detail"])
    assert "adoption releaseRunId" in detail


def test_consolidation_passes_untampered_gate_output(tmp_path):
    # Control: the genuine gate output consolidates to PASS (proves the tampering
    # tests fail for the right reason, not because the path always blocks).
    _seed_and_gate(tmp_path)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_selector_component_included_in_every_report_format(tmp_path):
    _seed_release(tmp_path, selector_report="valid")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output
    out = tmp_path / "out"

    report = json.loads((out / "consolidated-report.json").read_text())
    names = [c["name"] for c in report["components"]]
    assert "CaleeMobile selector contract" in names

    html = (out / "consolidated-report.html").read_text()
    assert "CaleeMobile selector contract" in html

    junit = (out / "consolidated-report.junit.xml").read_text()
    assert "CaleeMobile selector contract" in junit

    bundles = list(out.glob("*.zip"))
    assert len(bundles) == 1
    with zipfile.ZipFile(bundles[0]) as zf:
        names = zf.namelist()
        assert "consolidated-report.json" in names
        assert "consolidated-report.html" in names
        assert "consolidated-report.junit.xml" in names
        # The raw selector evidence travels with the bundle as an artifact.
        assert any(n.startswith("evidence/") and "selector-contract-result" in n for n in names)
