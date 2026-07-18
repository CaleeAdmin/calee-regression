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


def _run_gate(tmp_path, source_evidence, *, expected_sha=SHA_RELEASE, expected_version=VERSION_RELEASE, run_id=RUN_ID):
    _make_workspace(tmp_path, run_id)
    source = tmp_path / "artifact.json"
    source.write_text(json.dumps(source_evidence))
    return CliRunner().invoke(
        main,
        ["selector-contract", "--run-id", run_id, "--source", str(source),
         "--expected-sha", expected_sha, "--expected-version", expected_version],
    )


def _recorded(tmp_path, run_id=RUN_ID):
    path = run_context.RunWorkspace(tmp_path, run_id).component_report_path("selector-contract")
    return json.loads(path.read_text())


def test_gate_accepts_valid_same_build_evidence(tmp_path):
    result = _run_gate(tmp_path, _evidence())
    assert result.exit_code == EXIT_SUCCESS, result.output
    rec = _recorded(tmp_path)
    assert rec["status"] == "passed"
    # The gate stamps release-run provenance + a digest onto the adopted evidence.
    assert rec["evidence"]["releaseRunId"] == RUN_ID
    assert rec["evidence"]["artifactDigest"].startswith("sha256:")
    assert rec["evidence"]["generatedBy"] == "ci"


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
