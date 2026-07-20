"""Selector evidence is unavoidable in a mobile release (Priority 2).

Direct ``consolidate`` (not just the full launcher) can no longer omit the
CaleeMobile selector-contract component:

  * whenever a mobile platform (Android or iOS) is in scope it defaults to
    MANDATORY, and a missing report is a visible NOT_RUN/BLOCKED component --
    never omission;
  * in a production mobile release it is UNCONDITIONALLY mandatory and
    --selector-contract-optional is rejected outright;
  * a development/diagnostic release may opt out ONLY through a valid named
    waiver (reason + approver + timestamp).
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import run_context
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

RUN_ID = "p2-selector-001"
SHA_RELEASE = "a" * 40
VERSION_RELEASE = "0.0.23+23"
SELECTOR_NAME = "CaleeMobile selector contract"


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00").write(workspace.manifest_path)
    return workspace


def _write(workspace, component, data):
    workspace.component_report_path(component).write_text(json.dumps(data))


def _seed_base(tmp_path, run_id=RUN_ID, *, android=True, ios=False, selector=None):
    """Seed a passing non-selector base. android/ios add passing UI reports."""
    workspace = _make_workspace(tmp_path, run_id)
    _write(workspace, "environment", {"runId": run_id, "status": "pass", "detail": []})
    _write(workspace, "tablet", {
        "runId": run_id, "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    _write(workspace, "mobile-api", {"runId": run_id, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]})
    _write(workspace, "manual-checks", {
        "runId": run_id,
        "checks": [{"title": "Kiosk", "instruction": "swipe", "expectedResult": "no shade", "status": "pass"}],
    })
    ui = {"runId": run_id, "counts": {"PASS": 1}, "steps": [{"name": "ui", "status": "PASS"}]}
    if android:
        _write(workspace, "mobile-android", ui)
    if ios:
        _write(workspace, "mobile-ios", ui)
    if selector is not None:
        _write(workspace, "selector-contract", selector)
    return workspace


# Development-safe defaults: opt out of the confounding gates (identity, sync,
# features) so only the selector rule under test drives the outcome.
_DIAG = (
    "--allow-unknown-build-identity", "--sync-optional",
    "--meals-optional", "--onboarding-optional",
    "--google-calendar-optional", "--kiosk-admin-optional",
)


def _consolidate(tmp_path, *extra, run_id=RUN_ID):
    return CliRunner().invoke(
        main,
        ["consolidate", "--run-id", run_id, *_DIAG, "--out-dir", str(tmp_path / "out"), *extra],
    )


def _components(tmp_path):
    report = json.loads((tmp_path / "out" / "consolidated-report.json").read_text())
    return {c["name"]: c for c in report["components"]}


# ---------------------------------------------------------------------------
# Default mandatoriness when a mobile platform is in scope
# ---------------------------------------------------------------------------


def test_android_only_release_blocks_without_selector_evidence(tmp_path):
    _seed_base(tmp_path, android=True, ios=False, selector=None)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-optional")
    assert result.exit_code == EXIT_BLOCKED, result.output
    # Visible component, never omission.
    comp = _components(tmp_path)[SELECTOR_NAME]
    assert comp["status"].lower() in ("not_run", "blocked")
    assert comp["mandatory"] is True


def test_ios_only_release_blocks_without_selector_evidence(tmp_path):
    _seed_base(tmp_path, android=False, ios=True, selector=None)
    result = _consolidate(tmp_path, "--android-optional", "--ios-mandatory")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert _components(tmp_path)[SELECTOR_NAME]["status"].lower() in ("not_run", "blocked")


def test_both_mobile_platforms_excluded_does_not_require_selector(tmp_path):
    # No mobile in scope -> selector evidence is not applicable; the release is
    # not blocked by its absence (a tablet-only release).
    _seed_base(tmp_path, android=False, ios=False, selector=None)
    result = _consolidate(tmp_path, "--android-optional", "--ios-optional")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert SELECTOR_NAME not in _components(tmp_path)


# ---------------------------------------------------------------------------
# Production: unconditionally mandatory; optional override rejected
# ---------------------------------------------------------------------------


def test_production_direct_consolidation_blocks_with_no_selector_report(tmp_path):
    _seed_base(tmp_path, android=True, selector=None)
    result = _consolidate(
        tmp_path, "--production", "--android-mandatory", "--ios-optional",
        # production wants an expected identity; provide it so the selector
        # component (not a missing-identity block) is what we assert on.
        "--expected-caleemobile-git-sha", SHA_RELEASE,
        "--expected-caleemobile-build-version", VERSION_RELEASE,
        "--caleemobile-git-sha", SHA_RELEASE, "--caleemobile-build-version", VERSION_RELEASE,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    comp = _components(tmp_path)[SELECTOR_NAME]
    assert comp["mandatory"] is True
    assert comp["status"].lower() in ("not_run", "blocked")


def test_production_rejects_selector_optional_override(tmp_path):
    _seed_base(tmp_path, android=True, selector=None)
    result = _consolidate(
        tmp_path, "--production", "--android-mandatory", "--ios-optional",
        "--selector-contract-optional",
        "--waiver-reason", "r", "--waiver-approver", "a", "--waiver-timestamp", "t",
    )
    # Rejected outright -- a waiver cannot make it optional in production.
    assert result.exit_code == EXIT_INVALID_CONFIG, result.output
    assert "not permitted in a production mobile release" in result.output


# ---------------------------------------------------------------------------
# Development / diagnostic waiver opt-out
# ---------------------------------------------------------------------------


def test_diagnostic_optional_without_waiver_is_refused(tmp_path):
    # --selector-contract-optional alone (no waiver) cannot drop selector
    # evidence from a mobile release: it stays mandatory and BLOCKS.
    _seed_base(tmp_path, android=True, selector=None)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-optional", "--selector-contract-optional")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "Refusing --selector-contract-optional without a named waiver" in result.output
    assert _components(tmp_path)[SELECTOR_NAME]["mandatory"] is True


def test_diagnostic_waiver_allows_optional_selector(tmp_path):
    # With a valid named waiver, a development release may opt out; the release
    # then passes without selector evidence (the waiver is recorded).
    _seed_base(tmp_path, android=True, selector=None)
    result = _consolidate(
        tmp_path, "--android-mandatory", "--ios-optional", "--selector-contract-optional",
        "--waiver-reason", "diagnostic smoke run, no CaleeMobile build",
        "--waiver-approver", "release-eng",
        "--waiver-timestamp", "2026-07-18T00:00:00Z",
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    comps = _components(tmp_path)
    # Either absent or present-but-optional -- never a silent mandatory block.
    if SELECTOR_NAME in comps:
        assert comps[SELECTOR_NAME]["mandatory"] is False


# ---------------------------------------------------------------------------
# Priority 3: caleeMobile.selectorEvidenceRequired (via this run's own
# schema-v2 release-config composition) is the default whenever no explicit
# --selector-contract-mandatory/-optional flag is given.
# ---------------------------------------------------------------------------


def _write_release_config(workspace, *, selector_evidence_required, profile="staging"):
    _write(workspace, "release-config", {
        "runId": RUN_ID, "status": "ok", "releaseId": "2026.07.20-rc1", "schemaVersion": 2,
        "machineSelections": {}, "deviceIds": {},
        "releaseSelections": {
            "profile": profile, "selectedBackend": "https://hub-dev.calee.com.au",
            "enabledPlatforms": ["tablet"], "enabledFeatures": [],
            "expectedIdentities": {
                "calee": {}, "caleeShell": {},
                "caleeMobile": {
                    "buildVersion": VERSION_RELEASE, "gitSha": SHA_RELEASE,
                    "selectorEvidenceRequired": selector_evidence_required,
                },
            },
        },
        "conflicts": [],
    })


def test_manifest_true_forces_mandatory_even_with_no_mobile_in_scope(tmp_path):
    # No Android/iOS in scope would normally make selector evidence "not
    # applicable" -- but the manifest explicitly requires it.
    workspace = _seed_base(tmp_path, android=False, ios=False, selector=None)
    _write_release_config(workspace, selector_evidence_required=True)
    result = _consolidate(tmp_path, "--android-optional", "--ios-optional")
    assert result.exit_code == EXIT_BLOCKED, result.output
    comp = _components(tmp_path)[SELECTOR_NAME]
    assert comp["mandatory"] is True


def test_manifest_false_records_explicit_optional_component_not_omitted(tmp_path):
    workspace = _seed_base(tmp_path, android=True, ios=False, selector=None)
    _write_release_config(workspace, selector_evidence_required=False)
    result = _consolidate(
        tmp_path, "--android-mandatory", "--ios-optional",
        "--caleemobile-git-sha", SHA_RELEASE, "--caleemobile-build-version", VERSION_RELEASE,
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    comps = _components(tmp_path)
    assert SELECTOR_NAME in comps, "false must be RECORDED, never silently omitted"
    assert comps[SELECTOR_NAME]["mandatory"] is False


def test_explicit_cli_flag_still_wins_over_manifest_false(tmp_path):
    workspace = _seed_base(tmp_path, android=True, ios=False, selector=None)
    _write_release_config(workspace, selector_evidence_required=False)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-optional", "--selector-contract-mandatory")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert _components(tmp_path)[SELECTOR_NAME]["mandatory"] is True


def test_production_forces_selector_evidence_despite_manifest_false(tmp_path):
    # requirement: "production policy may still require selector evidence
    # regardless of a false flag."
    workspace = _seed_base(tmp_path, android=True, ios=False, selector=None)
    _write_release_config(workspace, selector_evidence_required=False, profile="production")
    result = _consolidate(
        tmp_path, "--production", "--android-mandatory", "--ios-optional",
        "--expected-caleemobile-git-sha", SHA_RELEASE,
        "--expected-caleemobile-build-version", VERSION_RELEASE,
        "--caleemobile-git-sha", SHA_RELEASE, "--caleemobile-build-version", VERSION_RELEASE,
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    comp = _components(tmp_path)[SELECTOR_NAME]
    assert comp["mandatory"] is True
