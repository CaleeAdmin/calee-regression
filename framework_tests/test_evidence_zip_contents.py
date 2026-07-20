"""Priority 9 -- the final release evidence ZIP must contain every mandated
evidence file, not a hand-picked subset, and no two components' files may
collide/overwrite each other inside it.

Builds a complete, fully-populated run workspace (every component this
session's evidence-ZIP requirement lists), runs the REAL `consolidate` CLI
command, and opens the resulting ZIP to assert every target path is present.
"""

from __future__ import annotations

import json
import zipfile

from click.testing import CliRunner

from calee_regression import cli, run_context
from calee_regression.models import EXIT_SUCCESS

RUN_ID = "release-20260720-101010-evidencezip"

# The exact set of evidence files/paths this session's task requires the
# final ZIP to contain (Priority 9), relative to the run workspace root.
MANDATED_EVIDENCE_PATHS = [
    "machine-config/results.json",
    "release-config/results.json",
    "installation/results.json",
    "subscribed-fixture/results.json",
    "subscribed-fixture/reg_sub_today_relative.ics",
    "selector-contract/results.json",
    "environment/results.json",
    "tablet/results.json",
    "mobile-api/results.json",
    "mobile-android/results.json",
    "mobile-ios/results.json",
    "sync/results.json",
    "kiosk-admin/results.json",
    "manual-checks/results.json",
    "identity/pre.json",
    "identity/post.json",
]

# Selector-contract provenance artifacts (Priority 9: "selector-contract
# provenance artifacts").
SELECTOR_PROVENANCE_FILES = [
    "selector-contract/selector-contract-result.json",
    "selector-contract/selector-contract-provenance.json",
    "selector-contract/source-artifact.zip",
    "selector-contract/source-result.json",
    "selector-contract/source-result.sha256",
    "selector-contract/source-artifact.sha256",
    "selector-contract/provenance.json",
]


def _w(workspace, component, data):
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": RUN_ID, **data}))


def _seed_complete_run(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=RUN_ID, started_at="2020-01-01 00:00:00")
    manifest.write(workspace.manifest_path)

    _w(workspace, "machine-config", {
        "status": "ok", "detail": ["authoritative"],
        "selected": {"backendUrl": "https://hub-dev.calee.com.au", "releaseProfile": "staging"},
    })
    _w(workspace, "release-config", {
        "status": "ok", "schemaVersion": 2, "detail": [],
        "machineSelections": {}, "releaseSelections": {}, "deviceIds": {}, "conflicts": [],
    })
    _w(workspace, "installation", {
        "status": "ok", "detail": [],
        "plan": {"releaseId": "2026.07.20-rc3", "steps": []},
    })
    _w(workspace, "environment", {"status": "pass", "detail": []})
    _w(workspace, "tablet", {
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "REG-TABLET", "status": "passed"}],
    })
    _w(workspace, "mobile-api", {"counts": {"PASS": 1}, "steps": [{"name": "api", "status": "PASS"}]})
    _w(workspace, "mobile-android", {"counts": {"PASS": 1}, "steps": [{"name": "android", "status": "PASS"}]})
    _w(workspace, "mobile-ios", {"counts": {"PASS": 1}, "steps": [{"name": "ios", "status": "PASS"}]})
    _w(workspace, "sync", {"mandatory": True, "flows": [{"flow": "event-sync", "status": "ok", "steps": []}]})
    _w(workspace, "kiosk-admin", {"status": "pass", "steps": []})
    _w(workspace, "manual-checks", {"checks": [
        {"title": "Kiosk escape check", "instruction": "swipe down", "expectedResult": "no shade", "status": "pass"},
    ]})

    # subscribed-fixture: results.json + the ICS sidecar (Priority 7's exact filename).
    _w(workspace, "subscribed-fixture", {
        "status": "blocked", "mode": "offline-only", "resolvedDate": "2026-07-20",
        "detail": ["offline-only mode never claims provisioning"],
    })
    (workspace.component_dir("subscribed-fixture") / "reg_sub_today_relative.ics").write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
    )

    # selector-contract: the gate's own results.json + every provenance artifact.
    _w(workspace, "selector-contract", {"status": "blocked", "detail": ["seeded for ZIP-contents test only"]})
    selector_dir = workspace.component_dir("selector-contract")
    for name in (
        "selector-contract-result.json", "selector-contract-provenance.json",
        "source-result.json", "provenance.json",
    ):
        (selector_dir / name).write_text(json.dumps({"seeded": True}))
    for name in ("source-artifact.zip",):
        (selector_dir / name).write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # minimal empty-zip EOCD
    for name in ("source-result.sha256", "source-artifact.sha256"):
        (selector_dir / name).write_text("0" * 64 + "  file\n")

    # identity/pre.json + identity/post.json (not under component_report_path).
    identity_dir = workspace.root / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    base_tablet = {"applicationId": "com.viso.calee", "buildVersion": "founder-v0.3.26", "gitSha": "a" * 40, "versionCode": "326"}
    (identity_dir / "pre.json").write_text(json.dumps({"tablet": base_tablet, "caleemobile": {}}))
    (identity_dir / "post.json").write_text(json.dumps({"tablet": base_tablet, "caleemobile": {}}))

    return workspace


_CONSOLIDATE_ARGS = [
    "--android-mandatory", "--ios-mandatory", "--sync-mandatory",
    "--installation-mandatory", "--machine-config-mandatory", "--release-config-mandatory",
    "--selector-contract-optional",  # seeded data isn't independently re-verifiable; ZIP contents don't depend on it passing
    "--kiosk-admin-mandatory",
    "--meals-optional", "--onboarding-optional", "--google-calendar-optional",
    "--allow-unknown-build-identity",
    "--calee-build-version", "founder-v0.3.26", "--calee-application-id", "com.viso.calee", "--calee-version-code", "326",
]


def test_final_zip_contains_every_mandated_evidence_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    _seed_complete_run(tmp_path)
    out_dir = tmp_path / "out"
    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_CONSOLIDATE_ARGS, "--out-dir", str(out_dir)],
    )
    assert result.exit_code in (EXIT_SUCCESS, 3), result.output  # PASS or BLOCKED (selector-contract seed) -- either way a ZIP must exist

    zips = list(out_dir.glob("*.zip"))
    assert len(zips) == 1, zips
    with zipfile.ZipFile(zips[0]) as zf:
        names = set(zf.namelist())

    for target in MANDATED_EVIDENCE_PATHS:
        arc = f"evidence/{target}"
        assert arc in names, f"{arc!r} missing from evidence ZIP. Present: {sorted(n for n in names if n.startswith('evidence/'))}"

    for target in SELECTOR_PROVENANCE_FILES:
        arc = f"evidence/{target}"
        assert arc in names, f"{arc!r} (selector-contract provenance) missing from evidence ZIP."


def test_zip_never_collides_same_named_results_json_across_components(tmp_path, monkeypatch):
    """The historical bug this closes: every component's report is literally
    named results.json -- a basename-only arcname silently dropped all but
    one. Assert every component's results.json survives as a DISTINCT entry."""
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    _seed_complete_run(tmp_path)
    out_dir = tmp_path / "out"
    CliRunner().invoke(cli.main, ["consolidate", "--run-id", RUN_ID, *_CONSOLIDATE_ARGS, "--out-dir", str(out_dir)])
    zips = list(out_dir.glob("*.zip"))
    with zipfile.ZipFile(zips[0]) as zf:
        results_json_entries = [n for n in zf.namelist() if n.endswith("/results.json")]
    # At least 10 distinct components each contributed their OWN results.json
    # (machine-config, release-config, installation, environment, tablet,
    # mobile-api, mobile-android, mobile-ios, sync, kiosk-admin, manual-checks,
    # subscribed-fixture, selector-contract).
    assert len(results_json_entries) == len(set(results_json_entries)), "duplicate arcnames in the ZIP"
    assert len(results_json_entries) >= 12, results_json_entries


def test_evidence_zip_contents_contract_matches_task_mandated_list():
    """A structural lock: the mandated-paths list above must stay in sync
    with this session's task requirements if this test file is ever edited."""
    assert set(MANDATED_EVIDENCE_PATHS) == {
        "machine-config/results.json", "release-config/results.json", "installation/results.json",
        "subscribed-fixture/results.json", "subscribed-fixture/reg_sub_today_relative.ics",
        "selector-contract/results.json", "environment/results.json", "tablet/results.json",
        "mobile-api/results.json", "mobile-android/results.json", "mobile-ios/results.json",
        "sync/results.json", "kiosk-admin/results.json", "manual-checks/results.json",
        "identity/pre.json", "identity/post.json",
    }
