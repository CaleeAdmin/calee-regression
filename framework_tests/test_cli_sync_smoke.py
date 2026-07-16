"""Tests for the `sync-smoke` CLI command's own wiring (Workstream 11) --
argument validation and exit codes that are specific to this command, not
already covered by test_sync_smoke.py (orchestration logic, fakes) or
test_sync_smoke_bridge.py (subprocess bridges, fake sibling scripts).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from calee_regression import cli
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    # REPO_ROOT drives where the run workspace (reports/runs/<run-id>/) is
    # created -- redirect it under tmp_path so these tests never write into
    # this checkout's working tree (same pattern as test_cli_prepare.py).
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)


def test_rejects_invalid_run_id():
    runner = CliRunner()
    result = runner.invoke(main, ["sync-smoke", "--run-id", "bad run id!", "--base-url", "x", "--email", "a", "--password", "p"])
    assert result.exit_code == EXIT_INVALID_CONFIG
    assert "Invalid --run-id" in result.output


def test_missing_credentials_is_blocked_not_a_crash():
    runner = CliRunner()
    result = runner.invoke(main, ["sync-smoke", "--run-id", "release-test-001"])
    assert result.exit_code == EXIT_BLOCKED
    assert "BLOCKED" in result.output
    assert "base-url" in result.output.lower()


def test_missing_only_password_is_still_blocked():
    runner = CliRunner()
    result = runner.invoke(
        main, ["sync-smoke", "--run-id", "release-test-002", "--base-url", "https://x", "--email", "a@x"],
    )
    assert result.exit_code == EXIT_BLOCKED


def test_missing_credentials_creates_no_run_workspace(tmp_path):
    # The credential guard must fire before any workspace/report directory
    # is created -- a BLOCKED-before-anything-ran invocation shouldn't
    # leave a half-formed reports/runs/<id>/ behind.
    runner = CliRunner()
    runner.invoke(main, ["sync-smoke", "--run-id", "release-test-003"])
    assert not (tmp_path / "reports" / "runs" / "release-test-003").exists()
