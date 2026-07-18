"""Required intended release identity + waiver auditing (Workstream 3).

For a PRODUCTION release profile the *expected* identity must be stated up
front -- a missing expected CaleeMobile SHA/version, tablet
applicationId/versionName/versionCode/source SHA, or (when CaleeShell is in
scope) CaleeShell version BLOCKS the release. Consistency of the observed build
is not evidence of release intent. A dirty source tree in production needs an
explicit named waiver (reason + approver + timestamp), recorded in the report.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import release_platforms, run_context
from calee_regression.cli import main
from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_PASS,
    component_from_release_intent,
)
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS

FULL_SHA_A = "a" * 40
FULL_SHA_TAB = "b" * 40
RUN_ID = "release-test-intent-001"


def _full_expected(**overrides):
    kwargs = dict(
        production=True,
        caleemobile_in_scope=True,
        tablet_in_scope=True,
        caleeshell_in_scope=False,
        expected_caleemobile_build_version="0.0.22+22",
        expected_caleemobile_git_sha=FULL_SHA_A,
        expected_calee_build_version="0.3.22",
        expected_calee_git_sha=FULL_SHA_TAB,
        expected_calee_application_id="com.viso.calee",
        expected_calee_version_code="322",
        detected_calee_application_id="com.viso.calee",
        detected_calee_version_code="322",
        tablet_source_sha_available=True,
    )
    kwargs.update(overrides)
    return kwargs


# ── component_from_release_intent unit behaviour ──────────────────────────────


def test_non_production_returns_no_component():
    assert component_from_release_intent(**{**_full_expected(), "production": False}) is None


def test_production_all_expected_configured_and_matched_passes():
    c = component_from_release_intent(**_full_expected())
    assert c.status == STATUS_PASS


def test_missing_expected_caleemobile_sha_blocks():
    c = component_from_release_intent(**_full_expected(expected_caleemobile_git_sha=None))
    assert c.status == STATUS_BLOCKED
    assert "expected CaleeMobile Git SHA" in " ".join(c.detail)


def test_missing_expected_tablet_package_identity_blocks():
    c = component_from_release_intent(**_full_expected(expected_calee_application_id=None, expected_calee_version_code=None))
    assert c.status == STATUS_BLOCKED
    joined = " ".join(c.detail)
    assert "expected tablet application id" in joined and "expected tablet versionCode" in joined


def test_abbreviated_expected_sha_blocks():
    c = component_from_release_intent(**_full_expected(expected_caleemobile_git_sha="abc1234"))
    assert c.status == STATUS_BLOCKED
    assert "abbreviated" in " ".join(c.detail).lower()


def test_expected_tablet_source_sha_required_only_when_available():
    # No source SHA available (pipeline can't provide it) -> not required.
    c = component_from_release_intent(**_full_expected(expected_calee_git_sha=None, tablet_source_sha_available=False))
    assert c.status == STATUS_PASS
    # Source SHA available but expectation missing -> BLOCKED.
    c2 = component_from_release_intent(**_full_expected(expected_calee_git_sha=None, tablet_source_sha_available=True))
    assert c2.status == STATUS_BLOCKED
    assert "expected tablet source Git SHA" in " ".join(c2.detail)


def test_caleeshell_expected_required_when_in_scope():
    c = component_from_release_intent(**_full_expected(caleeshell_in_scope=True, expected_caleeshell_version=None))
    assert c.status == STATUS_BLOCKED
    assert "expected CaleeShell version" in " ".join(c.detail)


def test_caleeshell_mismatch_blocks():
    c = component_from_release_intent(**_full_expected(
        caleeshell_in_scope=True, expected_caleeshell_version="1.2.3", detected_caleeshell_version="9.9.9",
    ))
    assert c.status == STATUS_BLOCKED
    assert "CaleeShell version" in " ".join(c.detail)


def test_tablet_application_id_mismatch_blocks():
    c = component_from_release_intent(**_full_expected(detected_calee_application_id="com.other.app"))
    assert c.status == STATUS_BLOCKED
    assert "application id" in " ".join(c.detail).lower()


# ── waiver auditing ───────────────────────────────────────────────────────────

VALID_WAIVER = {"reason": "hotfix build from a dirty tree", "approver": "release-manager", "timestamp": "2026-07-17T10:00:00Z"}


def test_dirty_without_waiver_blocks():
    c = component_from_release_intent(**_full_expected(calee_dirty=True))
    assert c.status == STATUS_BLOCKED
    assert "waiver" in " ".join(c.detail).lower()


def test_dirty_with_incomplete_waiver_blocks():
    incomplete = {"reason": "x", "approver": "", "timestamp": "2026-07-17"}
    c = component_from_release_intent(**_full_expected(caleemobile_dirty=True, waiver=incomplete))
    assert c.status == STATUS_BLOCKED


def test_dirty_with_valid_waiver_passes_and_records_it():
    c = component_from_release_intent(**_full_expected(calee_dirty=True, waiver=VALID_WAIVER))
    assert c.status == STATUS_PASS
    assert c.evidence["waiver"]["approver"] == "release-manager"
    assert "release-manager" in " ".join(c.detail)


# ── profile + waiver loading ──────────────────────────────────────────────────


def test_production_flag_loads_from_profile(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("expected_build_identity:\n  production: true\n  calee_application_id: 'com.viso.calee'\n")
    identity = release_platforms.load_expected_build_identity(config)
    assert identity.production is True
    assert identity.calee_application_id == "com.viso.calee"


def test_release_profile_top_level_marks_production(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_profile: production\n")
    assert release_platforms.load_expected_build_identity(config).production is True


def test_waiver_loads_from_profile_and_validity(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text(
        "waiver:\n  reason: 'approved hotfix'\n  approver: 'RM'\n  timestamp: '2026-07-17T00:00:00Z'\n"
    )
    waiver = release_platforms.load_waiver(config)
    assert waiver.is_valid is True
    assert waiver.approver == "RM"


def test_incomplete_waiver_is_invalid(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("waiver:\n  reason: 'x'\n")
    assert release_platforms.load_waiver(config).is_valid is False


# ── consolidate CLI end to end (production profile) ────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)


def _make_workspace(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    workspace.ensure_created()
    run_context.RunManifest(run_id=RUN_ID, started_at="2020-01-01 00:00:00").write(workspace.manifest_path)
    return workspace


def _write(workspace, component, data):
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _write_passing_base(workspace):
    _write(workspace, "environment", {"runId": RUN_ID, "status": "pass", "detail": []})
    _write(workspace, "tablet", {"runId": RUN_ID, "passed_count": 1, "failed_count": 0,
                                 "blocked_count": 0, "skipped_count": 0,
                                 "scenarios": [{"name": "a", "status": "passed"}]})
    _write(workspace, "mobile-api", {"runId": RUN_ID, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]})
    _write(workspace, "manual-checks", {"runId": RUN_ID,
                                        "checks": [{"title": "t", "instruction": "i", "expectedResult": "e", "status": "pass"}]})
    _write(workspace, "sync", {"runId": RUN_ID, "mandatory": True,
                               "flows": [{"flow": "event-sync", "status": "ok", "steps": []}]})


def _production_config(tmp_path, monkeypatch, extra=""):
    config = tmp_path / "release-platforms.yaml"
    config.write_text(
        "release_platforms:\n  tablet: true\n  mobile_android: false\n  mobile_ios: false\n"
        # Tablet-only scope: the mobile features can't be exercised without a
        # mobile platform, so this release excludes them (Workstream 3). Only
        # sync stays mandatory (it has its own passing report in the base).
        "release_features:\n  synchronization: true\n  kiosk_admin: false\n"
        "  meals: false\n  onboarding: false\n  google_calendar: false\n"
        "expected_build_identity:\n  production: true\n"
        "  calee_build_version: '0.3.22'\n"
        "  calee_application_id: 'com.viso.calee'\n"
        "  calee_version_code: '322'\n"
        + extra
    )
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(config))
    return config


def _consolidate(tmp_path, *extra):
    return CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--out-dir", str(tmp_path / "out"),
         "--calee-build-version", "0.3.22",
         "--calee-application-id", "com.viso.calee", "--calee-version-code", "322", *extra],
    )


def test_cli_production_full_expected_passes(tmp_path, monkeypatch):
    _production_config(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "Release identity intent (production): PASS" in result.output


def test_cli_production_missing_expected_appid_blocks(tmp_path, monkeypatch):
    # Drop the expected application id from the profile -> production BLOCKS.
    config = tmp_path / "release-platforms.yaml"
    config.write_text(
        "release_platforms:\n  tablet: true\n  mobile_android: false\n  mobile_ios: false\n"
        "release_features:\n  kiosk_admin: false\n"
        "expected_build_identity:\n  production: true\n"
        "  calee_build_version: '0.3.22'\n  calee_version_code: '322'\n"
    )
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(config))
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED
    assert "Release identity intent (production): BLOCKED" in result.output
    report = json.loads((tmp_path / "out" / "consolidated-report.json").read_text())
    intent = next(c for c in report["components"] if c["name"].startswith("Release identity intent"))
    assert "expected tablet application id" in " ".join(intent["detail"])


def test_cli_production_dirty_without_waiver_blocks(tmp_path, monkeypatch):
    _production_config(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    result = _consolidate(tmp_path, "--calee-dirty")
    assert result.exit_code == EXIT_BLOCKED
    # allow_dirty can't bypass the waiver in production.
    result2 = _consolidate(tmp_path, "--calee-dirty", "--allow-dirty")
    assert result2.exit_code == EXIT_BLOCKED


def test_cli_production_dirty_with_waiver_passes(tmp_path, monkeypatch):
    _production_config(tmp_path, monkeypatch)
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    result = _consolidate(
        tmp_path, "--calee-dirty",
        "--waiver-reason", "approved hotfix from dirty tree",
        "--waiver-approver", "release-manager",
        "--waiver-timestamp", "2026-07-17T10:00:00Z",
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    # The waiver is recorded in the consolidated report for audit.
    report = json.loads((tmp_path / "out" / "consolidated-report.json").read_text())
    intent = next(c for c in report["components"] if c["name"].startswith("Release identity intent"))
    assert intent["evidence"]["waiver"]["approver"] == "release-manager"
