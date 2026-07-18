"""Kiosk/admin physical-suite gating (Workstream 4).

Locks in that `release_features.kiosk_admin: true` makes the full launcher run
(or explicitly BLOCK) a real physical kiosk suite -- never a PASS from the
insufficient optional find.text("Admin") probe:

  * kiosk/admin OPTIONAL  -> recorded as an explicit optional not-run component
    (never silently omitted, never gating);
  * kiosk/admin MANDATORY, physical run not confirmed  -> BLOCKED with the
    specific unmet prerequisite;
  * kiosk/admin MANDATORY, confirmed but no disposable tablet connected ->
    BLOCKED with the specific unmet prerequisite;
  * the kiosk-admin report feeds the INDEPENDENT kiosk/admin feature component in
    consolidation, so a mandatory kiosk/admin can never read as a release PASS
    until the confirmed physical suite runs and passes.

The command never issues a destructive device-owner/factory-reset command.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import run_context
from calee_regression.cli import KIOSK_COMPONENT, main
from calee_regression.consolidated_report import FEATURE_COMPONENT_NAMES
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS

RUN_ID = "release-test-kiosk-001"
KIOSK_NAME = FEATURE_COMPONENT_NAMES["kiosk_admin"]


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)
    # Force the release-features default (all mandatory) regardless of any real
    # config on disk, so --mandatory/--optional here is the only lever.
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(tmp_path / "no-such-config.yaml"))


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00").write(workspace.manifest_path)
    return workspace


def _run(tmp_path, *extra, run_id=RUN_ID):
    _make_workspace(tmp_path, run_id)
    return CliRunner().invoke(main, ["kiosk-admin", "--run-id", run_id, *extra])


def _report(tmp_path, run_id=RUN_ID):
    path = run_context.RunWorkspace(tmp_path, run_id).component_report_path(KIOSK_COMPONENT)
    return json.loads(path.read_text())


def test_optional_kiosk_is_recorded_not_run_and_exits_success(tmp_path):
    result = _run(tmp_path, "--optional")
    assert result.exit_code == EXIT_SUCCESS, result.output
    report = _report(tmp_path)
    assert report["status"] == "not_run"
    assert report["mandatory"] is False
    # Feature-tagged so the consolidator's kiosk/admin component reads it.
    assert report["steps"][0]["feature"] == "kiosk_admin"
    assert report["steps"][0]["mandatory"] is False


def test_mandatory_kiosk_without_confirmation_blocks(tmp_path):
    result = _run(tmp_path, "--mandatory")
    assert result.exit_code == EXIT_BLOCKED, result.output
    report = _report(tmp_path)
    assert report["status"] == "blocked"
    assert report["steps"][0]["status"] == "BLOCKED"
    assert report["steps"][0]["feature"] == "kiosk_admin"
    assert "confirm" in report["detail"][0].lower()


def test_mandatory_kiosk_confirmed_but_no_tablet_blocks(tmp_path):
    # adb is unavailable in this environment, so no disposable tablet can be
    # detected -> BLOCKED with the tablet prerequisite (never a PASS).
    result = _run(tmp_path, "--mandatory", "--confirm-technical")
    assert result.exit_code == EXIT_BLOCKED, result.output
    report = _report(tmp_path)
    assert report["status"] == "blocked"
    assert "tablet" in report["detail"][0].lower()


def test_mandatory_kiosk_never_passes_from_the_probe(tmp_path):
    # There is no invocation shape that yields a kiosk/admin PASS today: the
    # real confirmed physical suite doesn't exist yet, so the strongest possible
    # inputs still BLOCK.
    result = _run(tmp_path, "--mandatory", "--confirm-technical", "--tablet-serial", "ZZ123",
                  "--caleeshell-version", "1.4.0")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert _report(tmp_path)["status"] != "pass"


# ── consolidation: kiosk/admin as an independent gating feature component ──────


def _write(workspace, component, data):
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _write_passing_base(workspace, run_id=RUN_ID):
    _write(workspace, "environment", {"runId": run_id, "status": "pass", "detail": []})
    _write(workspace, "tablet", {"runId": run_id, "passed_count": 1, "failed_count": 0,
                                 "blocked_count": 0, "skipped_count": 0,
                                 "scenarios": [{"name": "a", "status": "passed"}]})
    _write(workspace, "mobile-api", {"runId": run_id, "counts": {"PASS": 1},
                                     "steps": [{"name": "x", "status": "PASS"}]})
    _write(workspace, "manual-checks", {"runId": run_id,
                                        "checks": [{"title": "t", "instruction": "i",
                                                    "expectedResult": "e", "status": "pass"}]})


_ISOLATE = (
    "--android-optional", "--ios-optional", "--allow-unknown-build-identity",
    "--sync-optional", "--meals-optional", "--onboarding-optional", "--google-calendar-optional",
)


def test_consolidation_mandatory_kiosk_blocked_report_blocks_release(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    # The kiosk-admin command produced a BLOCKED marker (no tablet).
    _write(workspace, KIOSK_COMPONENT, {
        "runId": RUN_ID, "mandatory": True, "status": "blocked", "feature": "kiosk_admin",
        "steps": [{"name": "CaleeShell kiosk/admin physical suite", "status": "BLOCKED",
                   "mandatory": True, "feature": "kiosk_admin", "detail": "no tablet"}],
    })
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, *_ISOLATE, "--kiosk-admin-mandatory",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert f"{KIOSK_NAME}: BLOCKED" in result.output


def test_consolidation_optional_kiosk_not_run_does_not_block(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, KIOSK_COMPONENT, {
        "runId": RUN_ID, "mandatory": False, "status": "not_run", "feature": "kiosk_admin",
        "steps": [{"name": "CaleeShell kiosk/admin physical suite", "status": "SKIP",
                   "mandatory": False, "skipCategory": "optional_feature",
                   "feature": "kiosk_admin", "detail": "excluded this release"}],
    })
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, *_ISOLATE, "--kiosk-admin-optional",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    # Shown as an explicit optional component (NOT_RUN), never silently omitted.
    assert KIOSK_NAME in result.output


def test_consolidation_mandatory_kiosk_absent_blocks(tmp_path):
    # No kiosk-admin report at all -> a mandatory kiosk/admin has no evidence ->
    # NOT_RUN -> blocks.
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, *_ISOLATE, "--kiosk-admin-mandatory",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert f"{KIOSK_NAME}: NOT_RUN" in result.output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
