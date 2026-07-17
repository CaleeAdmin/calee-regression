"""Cross-device synchronization consolidation gating (Workstream 1).

Locks in that sync is a real release-gating component: it is auto-discovered
from reports/runs/<run-id>/sync/results.json, validated against the run ID like
every other component, and a missing / stale / run-ID-mismatched / BLOCKED /
FAILED *mandatory* sync can never read as a release PASS -- while an
optional/excluded sync is still shown, just never blocks.

Three layers are exercised:
  * component_from_sync_report  -- the status mapping (unit, synthetic dicts);
  * build_release_report        -- sync as a gating component + report outputs;
  * the `consolidate` / `sync-smoke` CLIs -- run-scoped, end to end.
"""

from __future__ import annotations

import json
import zipfile

import pytest
from click.testing import CliRunner

from calee_regression import run_context
from calee_regression.cli import main
from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_NOT_RUN,
    STATUS_PASS,
    SYNC_COMPONENT_NAME,
    ManualCheck,
    build_release_report,
    component_from_sync_report,
    write_release_bundle,
)
from calee_regression.models import (
    EXIT_BLOCKED,
    EXIT_REGRESSION,
    EXIT_SUCCESS,
)

RUN_ID = "release-test-sync-001"


def _sync_report(*statuses, run_id=RUN_ID, mandatory=True):
    """A real (flows-shaped) sync report with one flow per given status."""
    flows = [
        {"flow": f"{status}-flow-{i}", "status": status, "steps": []}
        for i, status in enumerate(statuses)
    ]
    return {"runId": run_id, "mandatory": mandatory, "flows": flows}


ALL_OK = _sync_report("ok", "ok", "ok")


# ── component_from_sync_report: the status mapping ────────────────────────────


def test_missing_report_is_not_run():
    c = component_from_sync_report(SYNC_COMPONENT_NAME, None, mandatory=True)
    assert c.status == STATUS_NOT_RUN
    assert c.mandatory is True


def test_all_ok_flows_pass():
    c = component_from_sync_report(SYNC_COMPONENT_NAME, ALL_OK, mandatory=True)
    assert c.status == STATUS_PASS
    assert c.passed == 3 and c.failed == 0 and c.blocked == 0


def test_any_failed_flow_is_fail():
    c = component_from_sync_report(SYNC_COMPONENT_NAME, _sync_report("ok", "failed", "blocked"), mandatory=True)
    # FAIL beats BLOCKED -- a real cross-device sync regression.
    assert c.status == STATUS_FAIL
    assert c.failed == 1 and c.blocked == 1


def test_any_blocked_flow_is_blocked():
    c = component_from_sync_report(SYNC_COMPONENT_NAME, _sync_report("ok", "blocked"), mandatory=True)
    assert c.status == STATUS_BLOCKED


def test_marker_with_explicit_blocked_status_blocks():
    marker = {"runId": RUN_ID, "mandatory": True, "status": "blocked", "flows": [],
              "detail": ["No in-scope CaleeMobile platform available."]}
    c = component_from_sync_report(SYNC_COMPONENT_NAME, marker, mandatory=True)
    assert c.status == STATUS_BLOCKED
    assert "No in-scope CaleeMobile platform available." in c.detail


def test_optional_excluded_marker_is_not_run_and_optional():
    marker = {"runId": RUN_ID, "mandatory": False, "status": "not_run", "flows": [],
              "detail": ["Cross-device synchronization is optional for this release."]}
    c = component_from_sync_report(SYNC_COMPONENT_NAME, marker, mandatory=False)
    assert c.status == STATUS_NOT_RUN
    assert c.mandatory is False


def test_marker_with_unrecognized_status_is_blocked_not_trusted():
    marker = {"runId": RUN_ID, "status": "who-knows", "flows": []}
    c = component_from_sync_report(SYNC_COMPONENT_NAME, marker, mandatory=True)
    assert c.status == STATUS_BLOCKED


# ── build_release_report: sync as a gating component ─────────────────────────

PASSING_TABLET = {
    "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
    "scenarios": [{"name": "a", "status": "passed"}],
}
PASSING_API = {"counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]}
PASSING_ENV = {"status": "pass", "detail": []}


def _passing_manual():
    return [ManualCheck(title="t", instruction="i", expected_result="e", status="pass")]


def _passing_kwargs(**overrides):
    kwargs = dict(
        environment=PASSING_ENV,
        tablet=PASSING_TABLET,
        mobile_api=PASSING_API,
        manual_checks=_passing_manual(),
        android_mandatory=False,
        ios_mandatory=False,
    )
    kwargs.update(overrides)
    return kwargs


def test_sync_mandatory_none_omits_the_component_entirely():
    # Legacy/ad-hoc callers that don't pass sync_mandatory get no sync component
    # at all (backward compatible) -- so existing build_release_report unit
    # tests are unaffected.
    report = build_release_report(**_passing_kwargs())
    assert all(c.name != SYNC_COMPONENT_NAME for c in report.components)


def test_sync_mandatory_missing_report_blocks_overall():
    report = build_release_report(**_passing_kwargs(sync=None, sync_mandatory=True))
    sync = next(c for c in report.components if c.name == SYNC_COMPONENT_NAME)
    assert sync.status == STATUS_NOT_RUN and sync.mandatory is True
    assert report.overall_status == STATUS_BLOCKED


def test_sync_optional_missing_is_shown_but_does_not_block():
    report = build_release_report(**_passing_kwargs(sync=None, sync_mandatory=False))
    sync = next(c for c in report.components if c.name == SYNC_COMPONENT_NAME)
    # Shown as optional, NOT silently omitted -- but doesn't gate the PASS.
    assert sync.mandatory is False and sync.status == STATUS_NOT_RUN
    assert report.overall_status == STATUS_PASS


def test_sync_failed_flow_makes_overall_fail():
    report = build_release_report(**_passing_kwargs(sync=_sync_report("ok", "failed"), sync_mandatory=True))
    assert report.overall_status == STATUS_FAIL


def test_sync_all_ok_contributes_to_overall_pass():
    report = build_release_report(**_passing_kwargs(sync=ALL_OK, sync_mandatory=True))
    sync = next(c for c in report.components if c.name == SYNC_COMPONENT_NAME)
    assert sync.status == STATUS_PASS
    assert report.overall_status == STATUS_PASS


def test_sync_component_is_ordered_before_manual_checks():
    from calee_regression.consolidated_report import ManualCheck
    checks = [ManualCheck(title="t", instruction="i", expected_result="e", status="pass")]
    report = build_release_report(**_passing_kwargs(sync=ALL_OK, sync_mandatory=True, manual_checks=checks))
    names = [c.name for c in report.components]
    assert SYNC_COMPONENT_NAME in names
    assert names.index(SYNC_COMPONENT_NAME) < names.index("manual checks")


def test_sync_appears_in_json_html_junit_and_zip(tmp_path):
    report = build_release_report(**_passing_kwargs(sync=_sync_report("ok", "blocked"), sync_mandatory=True))
    bundle = write_release_bundle(report, tmp_path / "out", build_label="9.9.9")
    out = tmp_path / "out"
    assert SYNC_COMPONENT_NAME in (out / "consolidated-report.json").read_text()
    assert SYNC_COMPONENT_NAME in (out / "consolidated-report.html").read_text()
    assert SYNC_COMPONENT_NAME in (out / "consolidated-report.junit.xml").read_text()
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
        assert "consolidated-report.json" in names
        # sync BLOCKED -> the whole bundle is a BLOCKED bundle.
        assert bundle.name.endswith("-BLOCKED.zip")


# ── The consolidate CLI: run-scoped sync gating end to end ────────────────────


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


def _write(workspace, component, data):
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _write_passing_base(workspace, run_id=RUN_ID):
    _write(workspace, "environment", {"runId": run_id, "status": "pass", "detail": []})
    _write(workspace, "tablet", {"runId": run_id, **PASSING_TABLET})
    _write(workspace, "mobile-api", {"runId": run_id, **PASSING_API})
    _write(workspace, "manual-checks", {
        "runId": run_id,
        "checks": [{"title": "t", "instruction": "i", "expectedResult": "e", "status": "pass"}],
    })


def _consolidate(tmp_path, *extra, run_id=RUN_ID):
    return CliRunner().invoke(
        main,
        ["consolidate", "--run-id", run_id, "--android-optional", "--ios-optional",
         "--allow-unknown-build-identity", "--out-dir", str(tmp_path / "out"), *extra],
    )


def test_cli_mandatory_sync_positive_path_passes(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "sync", {"runId": RUN_ID, "mandatory": True,
                               "flows": [{"flow": "event-sync", "status": "ok", "steps": []}]})
    result = _consolidate(tmp_path, "--sync-mandatory")
    assert result.exit_code == EXIT_SUCCESS
    assert f"{SYNC_COMPONENT_NAME}: PASS" in result.output


def test_cli_mandatory_sync_missing_blocks(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)  # no sync report written
    result = _consolidate(tmp_path, "--sync-mandatory")
    assert result.exit_code == EXIT_BLOCKED
    assert f"{SYNC_COMPONENT_NAME}: NOT_RUN" in result.output


def test_cli_mandatory_sync_blocked_flow_blocks(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "sync", _sync_report("ok", "blocked"))
    result = _consolidate(tmp_path, "--sync-mandatory")
    assert result.exit_code == EXIT_BLOCKED
    assert f"{SYNC_COMPONENT_NAME}: BLOCKED" in result.output


def test_cli_mandatory_sync_failed_flow_fails(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "sync", _sync_report("ok", "failed"))
    result = _consolidate(tmp_path, "--sync-mandatory")
    assert result.exit_code == EXIT_REGRESSION
    assert f"{SYNC_COMPONENT_NAME}: FAIL" in result.output


def test_cli_optional_sync_missing_passes_and_is_shown(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    result = _consolidate(tmp_path, "--sync-optional")
    assert result.exit_code == EXIT_SUCCESS
    # Explicitly shown as optional, never silently omitted.
    assert f"{SYNC_COMPONENT_NAME} (optional):" in result.output


def test_cli_mandatory_sync_report_with_wrong_run_id_is_rejected_and_blocks(tmp_path):
    # A stale/mismatched sync report (wrong run ID) must be rejected and treated
    # as not-executed -- never silently trusted -- so a mandatory sync BLOCKS.
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "sync", _sync_report("ok", "ok", run_id="release-some-other-run"))
    result = _consolidate(tmp_path, "--sync-mandatory")
    assert result.exit_code == EXIT_BLOCKED
    assert "different run" in result.output.lower()
    assert f"{SYNC_COMPONENT_NAME}: NOT_RUN" in result.output or f"{SYNC_COMPONENT_NAME}: BLOCKED" in result.output


def test_cli_mandatory_sync_report_without_run_id_is_rejected(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "sync", {"mandatory": True, "flows": [{"flow": "e", "status": "ok"}]})  # no runId
    result = _consolidate(tmp_path, "--sync-mandatory")
    assert result.exit_code == EXIT_BLOCKED
    assert "no run id" in result.output.lower() or "has no run id" in result.output.lower()


# ── The sync-smoke CLI: markers when the flows can't/shouldn't run ────────────


def test_sync_smoke_optional_records_marker_without_running(tmp_path):
    _make_workspace(tmp_path)
    result = CliRunner().invoke(main, ["sync-smoke", "--run-id", RUN_ID, "--optional"])
    assert result.exit_code == EXIT_SUCCESS
    report = json.loads((tmp_path / "reports" / "runs" / RUN_ID / "sync" / "results.json").read_text())
    assert report["mandatory"] is False and report["status"] == "not_run" and report["flows"] == []


def test_sync_smoke_no_platform_mandatory_records_blocked(tmp_path):
    _make_workspace(tmp_path)
    result = CliRunner().invoke(main, ["sync-smoke", "--run-id", RUN_ID, "--platform", "none", "--mandatory"])
    assert result.exit_code == EXIT_BLOCKED
    report = json.loads((tmp_path / "reports" / "runs" / RUN_ID / "sync" / "results.json").read_text())
    assert report["status"] == "blocked" and report["flows"] == []
    assert "No in-scope CaleeMobile platform" in " ".join(report["detail"])


def test_sync_smoke_reads_verified_backend_from_environment_report(tmp_path):
    # With no --base-url and no CALEE_EXPECTED_BACKEND, sync-smoke uses this
    # run's prepared+verified backend from the environment report. Here we omit
    # credentials so it still BLOCKS, but the point is that resolving the
    # backend from the report doesn't crash and doesn't fabricate a run.
    workspace = _make_workspace(tmp_path)
    _write(workspace, "environment", {
        "runId": RUN_ID, "status": "pass",
        "fixtureVerificationStatus": "ok", "targetEnvironment": "https://hub.example.test",
    })
    result = CliRunner().invoke(main, ["sync-smoke", "--run-id", RUN_ID, "--mandatory", "--email", "a@x"])
    # Missing password -> BLOCKED (not a crash), and the backend read didn't
    # blow up.
    assert result.exit_code == EXIT_BLOCKED
