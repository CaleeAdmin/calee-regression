"""CLI-level offline tests for `resume-release`, `inspect-resume`, and
`list-resumable-runs` (see calee_regression/resume_release.py for the
underlying decision engine, tested in isolation in
framework_tests/test_resume_release.py).

Follows this repo's existing CLI-test convention (see
framework_tests/test_cli_installer.py): drive the real Click commands via
CliRunner, monkeypatch REPO_ROOT to a tmp_path sandbox, and never touch a
real adb/device/Appium/network.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from calee_regression import cli, release_installer as ri, resume_release as rr, run_context
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

RUN_ID = "release-cli-resume-000001"


def _workspace(tmp_path: Path, run_id: str = RUN_ID) -> run_context.RunWorkspace:
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=run_id, started_at="2026-07-20 08:00:00")
    manifest.write(workspace.manifest_path)
    return workspace


def _write_release_config(workspace: run_context.RunWorkspace, *, release_id="r1") -> None:
    payload = {
        "runId": workspace.run_id, "status": "ok", "schemaVersion": 2, "releaseId": release_id,
        "releaseConfigDigest": "sha256:" + "a" * 64,
        "releaseSelections": {
            "selectedBackend": "https://hub-dev.calee.com.au", "enabledPlatforms": ["tablet"],
            "enabledFeatures": [], "profile": "staging", "distributedBuildRequired": False,
            "expectedIdentities": {"calee": {"applicationId": "com.viso.calee"}, "caleeShell": {}, "caleeMobile": {}},
        },
    }
    workspace.component_report_path("release-config").write_text(json.dumps(payload), encoding="utf-8")


def _always_pass_prepare(*, run_id=None, repo_root=None, config_path=None, suite_name=None):
    return rr.PrepareOutcome(status="pass", exit_code=EXIT_SUCCESS, detail=["ready"])


def _always_blocked_prepare(*, run_id=None, repo_root=None, config_path=None, suite_name=None):
    return rr.PrepareOutcome(status="blocked", exit_code=EXIT_BLOCKED, detail=["fixture unreachable"])


class TestInspectResumeCli:
    def test_invalid_run_id_exits_invalid_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        result = CliRunner().invoke(cli.main, ["inspect-resume", "--run-id", "not a valid id!"])
        assert result.exit_code == EXIT_INVALID_CONFIG

    def test_missing_workspace_exits_invalid_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        result = CliRunner().invoke(cli.main, ["inspect-resume", "--run-id", "release-does-not-exist"])
        assert result.exit_code == EXIT_INVALID_CONFIG

    def test_never_mutates_the_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        workspace = _workspace(tmp_path)
        _write_release_config(workspace)
        result = CliRunner().invoke(cli.main, ["inspect-resume", "--run-id", RUN_ID])
        assert result.exit_code == EXIT_SUCCESS
        assert rr.existing_attempt_numbers(workspace) == []

    def test_writes_report_when_requested(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        workspace = _workspace(tmp_path)
        _write_release_config(workspace)
        report_path = tmp_path / "inspect.json"
        result = CliRunner().invoke(cli.main, ["inspect-resume", "--run-id", RUN_ID, "--report", str(report_path)])
        assert result.exit_code == EXIT_SUCCESS
        payload = json.loads(report_path.read_text())
        assert payload["runId"] == RUN_ID
        assert "components" in payload


class TestResumeReleaseCli:
    def test_invalid_run_id_exits_invalid_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        result = CliRunner().invoke(cli.main, ["resume-release", "--run-id", "not a valid id!"])
        assert result.exit_code == EXIT_INVALID_CONFIG

    def test_missing_workspace_exits_invalid_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        result = CliRunner().invoke(cli.main, ["resume-release", "--run-id", "release-does-not-exist"])
        assert result.exit_code == EXIT_INVALID_CONFIG

    def test_has_no_force_flag_to_bypass_a_mismatch(self):
        result = CliRunner().invoke(cli.main, ["resume-release", "--help"])
        assert result.exit_code == EXIT_SUCCESS
        assert "--force" not in result.output

    def test_first_resume_bootstraps_and_reruns_blocked_prepare(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "default_prepare_runner", _always_pass_prepare)
        workspace = _workspace(tmp_path)
        _write_release_config(workspace)
        workspace.component_report_path("environment").write_text(
            json.dumps({"runId": RUN_ID, "status": "blocked", "detail": ["x"]}), encoding="utf-8"
        )
        result = CliRunner().invoke(cli.main, ["resume-release", "--run-id", RUN_ID], catch_exceptions=False)
        assert result.exit_code == EXIT_SUCCESS, result.output
        assert "environment" in result.output
        assert sorted(rr.existing_attempt_numbers(workspace)) == [1, 2]

    def test_still_blocked_prepare_exits_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "default_prepare_runner", _always_blocked_prepare)
        workspace = _workspace(tmp_path)
        _write_release_config(workspace)
        workspace.component_report_path("environment").write_text(
            json.dumps({"runId": RUN_ID, "status": "blocked", "detail": ["x"]}), encoding="utf-8"
        )
        result = CliRunner().invoke(cli.main, ["resume-release", "--run-id", RUN_ID])
        assert result.exit_code == EXIT_BLOCKED

    def test_release_id_mismatch_refuses_on_second_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "default_prepare_runner", _always_pass_prepare)
        workspace = _workspace(tmp_path)
        _write_release_config(workspace, release_id="r1")
        first = CliRunner().invoke(cli.main, ["resume-release", "--run-id", RUN_ID])
        assert first.exit_code == EXIT_SUCCESS

        _write_release_config(workspace, release_id="r2-DIFFERENT")
        second = CliRunner().invoke(cli.main, ["resume-release", "--run-id", RUN_ID])
        assert second.exit_code == EXIT_BLOCKED
        assert "releaseId" in second.output

    def test_writes_attempt_report_when_requested(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "default_prepare_runner", _always_pass_prepare)
        workspace = _workspace(tmp_path)
        _write_release_config(workspace)
        report_path = tmp_path / "attempt.json"
        result = CliRunner().invoke(
            cli.main, ["resume-release", "--run-id", RUN_ID, "--report", str(report_path), "--tester", "jane"]
        )
        assert result.exit_code == EXIT_SUCCESS
        payload = json.loads(report_path.read_text())
        assert payload["attemptNumber"] == 2
        assert payload["operator"] == "jane"


class TestListResumableRunsCli:
    def test_no_runs_reports_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        result = CliRunner().invoke(cli.main, ["list-resumable-runs"])
        assert result.exit_code == EXIT_SUCCESS
        assert "No runs found" in result.output

    def test_lists_every_run_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        _workspace(tmp_path, "release-a")
        _workspace(tmp_path, "release-b")
        result = CliRunner().invoke(cli.main, ["list-resumable-runs"])
        assert result.exit_code == EXIT_SUCCESS
        assert "release-a" in result.output
        assert "release-b" in result.output


class TestConsolidatedReportResumeInfo:
    def test_consolidate_attaches_resume_provenance_after_a_resume(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "default_prepare_runner", _always_pass_prepare)

        def fake_adb(argv):
            if "getprop" in argv:
                return ri.AdbResult(0, "[ro.serialno]: [SERIAL1]\n[ro.product.manufacturer]: [Google]\n[ro.product.model]: [Pixel]\n[ro.build.product]: [p]\n")
            if "get-state" in argv:
                return ri.AdbResult(0, "device\n")
            if "dumpsys" in argv and "com.viso.calee" in argv:
                return ri.AdbResult(0, "versionName=1.0.0\nversionCode=100")
            if "dumpsys" in argv:
                return ri.AdbResult(0, "")
            return ri.AdbResult(0, "")

        monkeypatch.setattr(ri, "real_adb_runner", fake_adb)

        workspace = _workspace(tmp_path)
        _write_release_config(workspace)
        workspace.component_report_path("installation").write_text(json.dumps({
            "runId": RUN_ID, "status": "ok", "releaseId": "r1",
            "tabletStableIdentity": {
                "configuredTransport": "TAB1", "serialno": "SERIAL1", "manufacturer": "Google",
                "model": "Pixel", "product": "p", "transportType": "usb",
            },
            "execution": {"installed": [
                {"packageId": "com.viso.calee", "present": True, "versionName": "1.0.0", "versionCode": "100"},
            ]},
        }), encoding="utf-8")

        resume_result = CliRunner().invoke(cli.main, ["resume-release", "--run-id", RUN_ID, "--serial", "TAB1"])
        assert resume_result.exit_code == EXIT_SUCCESS, resume_result.output

        consolidate_result = CliRunner().invoke(cli.main, [
            "consolidate", "--run-id", RUN_ID, "--installation-mandatory", "--build-version", "SAMPLE",
        ])
        assert consolidate_result.exit_code in (EXIT_SUCCESS, EXIT_BLOCKED, 1)
        bundle_json = workspace.consolidated_dir / "consolidated-report.json"
        assert bundle_json.is_file()
        payload = json.loads(bundle_json.read_text())
        installation_component = next(c for c in payload["components"] if c["name"] == "Calee tablet release installation")
        assert installation_component.get("resume", {}).get("executionMode") == "reused"

    def test_consolidate_unaffected_when_run_was_never_resumed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
        workspace = _workspace(tmp_path)
        _write_release_config(workspace)
        consolidate_result = CliRunner().invoke(cli.main, [
            "consolidate", "--run-id", RUN_ID, "--build-version", "SAMPLE",
        ])
        assert consolidate_result.exit_code in (EXIT_SUCCESS, EXIT_BLOCKED, 1)
        bundle_json = workspace.consolidated_dir / "consolidated-report.json"
        payload = json.loads(bundle_json.read_text())
        for component in payload["components"]:
            assert "resume" not in component
