"""Distributed-build acceptance as a release-gating consolidated component
(Priority 3).

Mirrors test_selector_mandatory_consolidation.py's shape: proves the
``distributed-build-acceptance`` component is recorded -- PASS, BLOCKED, or an
explicit "not required" -- exactly per the manifest's own
``caleeMobile.distributedBuildAcceptanceRequired`` flag (via this run's
composed release-config), never silently omitted, and that an explicit CLI
override still wins over that composed default.
"""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner

from calee_regression import run_context
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS

RUN_ID = "release-test-distributed-build-001"
SHA_RELEASE = "a" * 40
VERSION_RELEASE = "0.0.23+23"
RELEASE_A = "2026.07.20-rc1"


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00")
    manifest.write(workspace.manifest_path)
    return workspace


def _seed_minimal_release(tmp_path, *, distributed_build_required, run_id=RUN_ID):
    """Seed a workspace with the always-mandatory components passing, plus a
    schema-v2-style release-config composition declaring
    distributedBuildRequired -- so the distributed-build-acceptance component
    is the only variable driving overall status."""
    workspace = _make_workspace(tmp_path, run_id)

    def write(component, data):
        p = workspace.component_report_path(component)
        p.write_text(json.dumps({"runId": run_id, **data}))

    write("environment", {"status": "pass", "detail": []})
    write("tablet", {
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    write("mobile-api", {"counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]})
    write("manual-checks", {
        "checks": [{"title": "Kiosk", "instruction": "swipe", "expectedResult": "no shade", "status": "pass"}],
    })
    write("release-config", {
        "status": "ok", "releaseId": RELEASE_A, "schemaVersion": 2,
        "machineSelections": {}, "deviceIds": {},
        "releaseSelections": {
            "profile": "staging", "selectedBackend": "https://hub-dev.calee.com.au",
            "enabledPlatforms": ["tablet"], "enabledFeatures": [],
            "distributedBuildRequired": distributed_build_required,
            "expectedIdentities": {
                "calee": {}, "caleeShell": {},
                "caleeMobile": {"buildVersion": VERSION_RELEASE, "gitSha": SHA_RELEASE},
            },
        },
        "conflicts": [],
    })
    return workspace


def _fresh_ts() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_acceptance(workspace, **overrides):
    data = dict(
        schemaVersion=1, component="caleemobile-distributed-build-acceptance",
        channel="testflight", distributedBuildId="TF-4821",
        testedGitSha=SHA_RELEASE, testedVersion=VERSION_RELEASE,
        verifiedVia="testflight_api", releaseId=RELEASE_A,
        timestamp=_fresh_ts(),
    )
    data.update(overrides)
    path = workspace.component_report_path("distributed-build-acceptance")
    path.write_text(json.dumps({"runId": RUN_ID, "evidence": data, "status": "passed"}))


def _consolidate(tmp_path, *, extra_args=()):
    return CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID,
         "--build-version", "9.9.9",
         "--android-optional", "--ios-optional", "--sync-optional",
         "--meals-optional", "--onboarding-optional", "--google-calendar-optional", "--kiosk-admin-optional",
         "--selector-contract-optional",
         "--calee-build-version", "0.3.22", "--calee-application-id", "com.viso.calee", "--calee-version-code", "322",
         "--caleemobile-git-sha", SHA_RELEASE, "--caleemobile-build-version", VERSION_RELEASE,
         "--out-dir", str(tmp_path / "out"), *extra_args],
    )


def _component(tmp_path, name):
    report = json.loads((tmp_path / "out" / "consolidated-report.json").read_text())
    for c in report.get("components", []):
        if c["name"] == name:
            return c
    raise AssertionError(f"component {name!r} not found in {report.get('components')}")


def test_required_and_missing_blocks(tmp_path):
    _seed_minimal_release(tmp_path, distributed_build_required=True)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "Distributed-build acceptance" in result.output
    component = _component(tmp_path, "Distributed-build acceptance")
    assert component["status"] == "not_run"
    assert component["mandatory"] is True
    assert any("never inferred from a local checkout" in d for d in component["detail"])


def test_required_and_valid_evidence_passes(tmp_path):
    workspace = _seed_minimal_release(tmp_path, distributed_build_required=True)
    _write_acceptance(workspace)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output
    component = _component(tmp_path, "Distributed-build acceptance")
    assert component["status"] == "pass"


def test_required_but_evidence_claims_local_checkout_blocks(tmp_path):
    # Never fabricate acceptance from a local checkout or unsigned build --
    # even when SOME evidence file exists, an honest verifiedVia is required.
    workspace = _seed_minimal_release(tmp_path, distributed_build_required=True)
    _write_acceptance(workspace, verifiedVia="local_checkout")
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED, result.output
    component = _component(tmp_path, "Distributed-build acceptance")
    assert component["status"] == "blocked"
    joined = " ".join(component["detail"])
    assert "explicitly rejected" in joined and "never be" in joined and "fabricated" in joined


def test_required_but_wrong_build_identity_blocks(tmp_path):
    workspace = _seed_minimal_release(tmp_path, distributed_build_required=True)
    _write_acceptance(workspace, testedGitSha="b" * 40)
    result = _consolidate(tmp_path, extra_args=["--expected-caleemobile-git-sha", SHA_RELEASE])
    assert result.exit_code == EXIT_BLOCKED, result.output
    component = _component(tmp_path, "Distributed-build acceptance")
    assert component["status"] == "blocked"


def test_not_required_records_explicit_optional_component_and_passes(tmp_path):
    # false must be RECORDED, never silently omitted -- and never blocks.
    _seed_minimal_release(tmp_path, distributed_build_required=False)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Distributed-build acceptance" in result.output
    component = _component(tmp_path, "Distributed-build acceptance")
    assert component["mandatory"] is False
    assert any("not required for this release" in d.lower() for d in component["detail"])


def test_explicit_cli_flag_overrides_composed_requirement(tmp_path):
    # The manifest says required -- but an explicit --distributed-build-
    # acceptance-optional override (e.g. a named technical waiver process)
    # still wins, exactly like every other mandatory/optional axis.
    _seed_minimal_release(tmp_path, distributed_build_required=True)
    result = _consolidate(tmp_path, extra_args=["--distributed-build-acceptance-optional"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    component = _component(tmp_path, "Distributed-build acceptance")
    assert component["mandatory"] is False
    assert any("not required for this release" in d.lower() for d in component["detail"])


def test_no_release_config_at_all_omits_component_entirely(tmp_path):
    # Ad-hoc/dev consolidation with no release-config composed for this run:
    # the component does not apply and must not appear (ordinary/legacy
    # consolidation stays unaffected).
    workspace = _make_workspace(tmp_path)

    def write(component, data):
        p = workspace.component_report_path(component)
        p.write_text(json.dumps({"runId": RUN_ID, **data}))

    write("environment", {"status": "pass", "detail": []})
    write("tablet", {
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    write("mobile-api", {"counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]})
    write("manual-checks", {
        "checks": [{"title": "Kiosk", "instruction": "swipe", "expectedResult": "no shade", "status": "pass"}],
    })
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output
    report = json.loads((tmp_path / "out" / "consolidated-report.json").read_text())
    assert not any(c["name"] == "Distributed-build acceptance" for c in report.get("components", []))


def test_evidence_zip_includes_distributed_build_acceptance_report(tmp_path):
    workspace = _seed_minimal_release(tmp_path, distributed_build_required=True)
    _write_acceptance(workspace)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output
    import zipfile
    zip_candidates = list((tmp_path / "out").glob("*.zip"))
    assert zip_candidates, list((tmp_path / "out").iterdir())
    with zipfile.ZipFile(zip_candidates[0]) as zf:
        names = zf.namelist()
    assert any("distributed-build-acceptance" in n for n in names), names
