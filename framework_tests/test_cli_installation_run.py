"""CLI tests for run-scoped installation evidence + the machine-config snapshot
(Priorities 4 & 6): one run ID owns the machine-config snapshot and the
installation component, both consolidated and gating.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml
from click.testing import CliRunner

from calee_regression import cli, run_context
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS, EXIT_INVALID_CONFIG

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)


def _bundle(tmp_path):
    bundle = tmp_path / "external" / "Calee-Releases" / "current"
    bundle.mkdir(parents=True)
    calee_bytes = b"calee-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    manifest = {
        "releaseId": "2026.07.20-rc1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": _sha256(calee_bytes)},
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{_sha256(calee_bytes)}  calee.apk\n")
    return bundle


def _machine_yaml(tmp_path, bundle):
    return {
        "tablet_serial": "TAB123",
        "expected_tablet_state": "logged_in_tablet",
        "calee_package_id": "com.viso.calee",
        "caleeshell_package_id": "com.viso.caleeshell",
        "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
        "calee_launch_action": "com.viso.calee.action.START",
        "release_bundle_dir": str(bundle),
        "backend_url": "https://hub-dev.calee.com.au",
        "release_profile": "production",
        "report_dir": "reports",
        "mobile_platforms": ["android"],
    }


def test_machine_config_snapshot_writes_run_scoped_evidence(tmp_path):
    bundle = _bundle(tmp_path)
    machine_path = tmp_path / "machine.local.yaml"
    machine_path.write_text(yaml.safe_dump(_machine_yaml(tmp_path, bundle)))
    run_id = "release-20260720-000000-abc123"

    result = CliRunner().invoke(
        cli.main, ["machine-config-snapshot", "--config", str(machine_path), "--run-id", run_id]
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    # The authoritative env vars are emitted for the launcher to eval.
    assert "MACHINE_BACKEND_URL=https://hub-dev.calee.com.au" in result.output
    assert "MACHINE_PLATFORM_ANDROID=true" in result.output
    assert "MACHINE_PLATFORM_IOS=false" in result.output
    assert "MACHINE_EFFECTIVE_CONFIG=" in result.output

    # The snapshot is written into the run workspace, secrets excluded, with the
    # selected backend/devices/packages/profile in the evidence.
    snap_path = tmp_path / "reports" / "runs" / run_id / "machine-config" / "results.json"
    snap = json.loads(snap_path.read_text())
    assert snap["runId"] == run_id
    assert snap["status"] == "ok"
    assert snap["selected"]["backendUrl"] == "https://hub-dev.calee.com.au"
    assert snap["selected"]["releaseProfile"] == "production"
    assert snap["selected"]["tabletSerial"] == "TAB123"
    assert snap["selected"]["caleePackageId"] == "com.viso.calee"
    assert "password" not in snap_path.read_text().lower()


def test_machine_config_snapshot_blocks_on_secret(tmp_path):
    bundle = _bundle(tmp_path)
    machine = _machine_yaml(tmp_path, bundle)
    machine["regression_password"] = "hunter2"
    machine_path = tmp_path / "machine.local.yaml"
    machine_path.write_text(yaml.safe_dump(machine))
    run_id = "release-20260720-000000-secret"

    result = CliRunner().invoke(
        cli.main, ["machine-config-snapshot", "--config", str(machine_path), "--run-id", run_id]
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "hunter2" not in result.output
    snap = json.loads((tmp_path / "reports" / "runs" / run_id / "machine-config" / "results.json").read_text())
    assert snap["status"] == run_context.__dict__.get("STATUS_BLOCKED", "blocked") or snap["status"] == "blocked"


def test_install_tablet_release_run_scoped_records_installation_component(tmp_path, monkeypatch):
    # No SDK tools on PATH -> APK content inspection BLOCKS (Priority 5), which
    # is the honest offline outcome; the installation component records BLOCKED
    # into the run workspace (Priority 6), never a silent skip or a false pass.
    bundle = _bundle(tmp_path)
    run_id = "release-20260720-000000-inst01"
    result = CliRunner().invoke(
        cli.main, ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB123", "--run-id", run_id]
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    inst_path = tmp_path / "reports" / "runs" / run_id / "installation" / "results.json"
    payload = json.loads(inst_path.read_text())
    assert payload["runId"] == run_id
    assert payload["status"] == "blocked"
    # The absolute APK path (Priority 1) is present in the plan argv evidence.
    plan_argv = [a for step in payload["plan"]["steps"] for a in step["argv"]]
    assert any(a.endswith("/calee.apk") and a.startswith("/") for a in plan_argv)


def test_install_tablet_release_invalid_bundle_records_installation_invalid(tmp_path):
    bundle = _bundle(tmp_path)
    # Corrupt the manifest sha so verification fails.
    manifest = json.loads((bundle / "release-manifest.json").read_text())
    manifest["calee"]["sha256"] = "0" * 64
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    run_id = "release-20260720-000000-inst02"
    result = CliRunner().invoke(
        cli.main, ["install-tablet-release", "--bundle", str(bundle), "--run-id", run_id]
    )
    assert result.exit_code == EXIT_INVALID_CONFIG
    payload = json.loads((tmp_path / "reports" / "runs" / run_id / "installation" / "results.json").read_text())
    assert payload["status"] == "invalid"


def test_consolidate_blocks_when_installation_blocked(tmp_path):
    # A run whose installation component is BLOCKED can never consolidate to PASS.
    run_id = "release-20260720-000000-cons01"
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=run_id, started_at="2026-07-20 00:00:00")
    manifest.write(workspace.manifest_path)
    # Machine-config OK + installation BLOCKED.
    (workspace.component_report_path("machine-config")).write_text(
        json.dumps({"runId": run_id, "status": "ok", "detail": [], "selected": {}}) + "\n"
    )
    (workspace.component_report_path("installation")).write_text(
        json.dumps({"runId": run_id, "status": "blocked", "detail": ["No device."]}) + "\n"
    )
    result = CliRunner().invoke(
        cli.main,
        ["consolidate", "--run-id", run_id,
         "--installation-mandatory", "--machine-config-mandatory",
         "--sync-optional", "--selector-contract-optional",
         "--android-optional", "--ios-optional", "--allow-unknown-build-identity"],
    )
    # BLOCKED overall (installation is a mandatory component in blocked state).
    assert "Overall: BLOCKED" in result.output
    assert "Calee tablet release installation" in result.output


def test_early_gate_invalid_bundle_consolidates_with_downstream_not_run(tmp_path):
    """Priority 7: an early gate (invalid bundle) still consolidates -- the
    INVALID installation is recorded, every downstream component is marked
    NOT_RUN because of the gate, reports/latest-run is updated, and the run
    reports BLOCKED. This is what the 00 launcher's consolidate_gate produces."""
    run_id = "release-20260720-000000-p7gate"
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    run_context.RunManifest(run_id=run_id, started_at="2026-07-20 00:00:00").write(workspace.manifest_path)
    workspace.component_report_path("machine-config").write_text(
        json.dumps({"runId": run_id, "status": "ok", "detail": [], "selected": {}}) + "\n"
    )
    workspace.component_report_path("installation").write_text(
        json.dumps({"runId": run_id, "status": "invalid", "detail": ["Bundle failed verification."]}) + "\n"
    )
    result = CliRunner().invoke(
        cli.main,
        ["consolidate", "--run-id", run_id, "--allow-unknown-build-identity",
         "--machine-config-mandatory", "--installation-mandatory"],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "Overall: BLOCKED" in result.output
    # Downstream components are consolidated as NOT_RUN (never silently omitted).
    report = json.loads((workspace.consolidated_dir / "consolidated-report.json").read_text())
    statuses = {c["name"]: c["status"] for c in report["components"]}
    not_run = [name for name, st in statuses.items() if st == "not_run"]
    assert any("tablet" in n.lower() for n in not_run)
    assert any("selector" in n.lower() for n in not_run)
    # reports/latest-run now points at this run.
    latest = tmp_path / "reports" / "latest-run"
    assert latest.is_symlink() or latest.exists()
