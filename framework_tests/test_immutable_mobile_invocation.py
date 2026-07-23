"""Immutable mobile invocation evidence (Workstream 4).

Re-invoking the same mobile platform with the same run id must NEVER erase or
improve an earlier invocation's evidence -- on disk (per-invocation snapshot)
AND in the manifest (worst-wins). A later PASS can never launder an earlier
FAIL.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import cli, run_context
from calee_regression.cli import main


def _workspace(report_root, run_id="release-1"):
    ws = run_context.RunWorkspace(report_root, run_id)
    ws.ensure_created()
    run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00").write(ws.manifest_path)
    return ws


def _write_report(path, exit_code):
    path.parent.mkdir(parents=True, exist_ok=True)
    status = {0: "PASS", 1: "FAIL", 3: "BLOCKED"}[exit_code]
    path.write_text(json.dumps({
        "reportType": "mobile-serial-aggregate", "reportSchemaVersion": 1,
        "status": status, "counts": {status: 1},
        "steps": [{"name": "s", "status": status, "mandatory": True}],
    }), encoding="utf-8")


def _record(report_root, component, report_path, exit_code, invocation_id):
    return CliRunner().invoke(main, [
        "record-component", "--run-id", "release-1", "--component", component,
        "--report-path", str(report_path), "--exit-code", str(exit_code),
        "--invocation-id", invocation_id,
    ])


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)


def test_later_pass_cannot_erase_earlier_fail_on_disk_or_in_manifest(tmp_path):
    ws = _workspace(tmp_path)
    report = ws.component_dir("mobile-android") / "results.json"

    # First invocation: a product FAIL.
    _write_report(report, 1)
    r1 = _record(tmp_path, "mobile-android", report, 1, "inv-A")
    assert r1.exit_code == 0, r1.output

    # Second invocation overwrites the CANONICAL report with a PASS.
    _write_report(report, 0)
    r2 = _record(tmp_path, "mobile-android", report, 0, "inv-B")
    assert r2.exit_code == 0, r2.output

    # On disk: BOTH invocations' immutable snapshots survive, each with its own
    # verdict -- the FAIL is not erased.
    inv_a = ws.component_dir("mobile-android") / "invocations" / "inv-A" / "results.json"
    inv_b = ws.component_dir("mobile-android") / "invocations" / "inv-B" / "results.json"
    assert json.loads(inv_a.read_text())["status"] == "FAIL"
    assert json.loads(inv_b.read_text())["status"] == "PASS"

    # In the manifest: worst-wins keeps the FAIL as the effective result, and the
    # full attempt history (with invocation ids) is preserved.
    manifest = run_context.RunManifest.load(ws.manifest_path)
    assert manifest.effective_exit_code("mobile-android") == 1  # FAIL survives the later PASS
    attempts = manifest.component_attempts["mobile-android"]
    assert [a.get("invocationId") for a in attempts] == ["inv-A", "inv-B"]


def test_reusing_an_invocation_id_is_refused(tmp_path):
    ws = _workspace(tmp_path)
    report = ws.component_dir("mobile-ios") / "results.json"
    _write_report(report, 0)
    assert _record(tmp_path, "mobile-ios", report, 0, "inv-X").exit_code == 0
    # Re-using the same invocation id must not overwrite the earlier snapshot.
    second = _record(tmp_path, "mobile-ios", report, 0, "inv-X")
    assert second.exit_code != 0
    assert "already exists" in second.output


def test_api_component_is_snapshotted_too(tmp_path):
    ws = _workspace(tmp_path)
    report = ws.component_dir("mobile-api") / "results.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"reportType": "mobile-api-suite", "reportSchemaVersion": 1,
                                  "counts": {"PASS": 1}, "steps": []}), encoding="utf-8")
    assert _record(tmp_path, "mobile-api", report, 0, "inv-1").exit_code == 0
    assert (ws.component_dir("mobile-api") / "invocations" / "inv-1" / "results.json").is_file()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
