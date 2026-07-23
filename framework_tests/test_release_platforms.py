"""Tests for the technical-owner release-platform profile (Workstream 9).

Locks in that Android/iOS UI mandatory-ness comes from
config/release-platforms.yaml (or its safe True-by-default absence), never
a hard-coded mandatory=False, and exercises the tablet-only /
tablet+android / tablet+android+ios / selected-platform-missing /
optional-platform-omitted combinations end to end through `consolidate`.

Every consolidate call here is run-scoped (see run_context.py): each test
writes its synthetic component reports straight into a run workspace under
tmp_path (with REPO_ROOT monkeypatched there) rather than arbitrary paths,
so the same run-ID/workspace validation a real release run goes through is
exercised here too.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import release_platforms, run_context
from tablet_fixtures import certifying
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS

RUN_ID = "release-test-run-001"


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)


def test_absent_config_defaults_every_platform_to_mandatory(tmp_path):
    platforms = release_platforms.load_release_platforms(tmp_path / "does-not-exist.yaml")
    assert platforms.tablet is True
    assert platforms.mobile_android is True
    assert platforms.mobile_ios is True


def test_config_can_opt_a_platform_out(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_platforms:\n  tablet: true\n  mobile_android: true\n  mobile_ios: false\n")
    platforms = release_platforms.load_release_platforms(config)
    assert platforms.mobile_android is True
    assert platforms.mobile_ios is False


def test_invalid_yaml_raises_a_clear_error(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_platforms: [not, a, mapping]\n")
    with pytest.raises(release_platforms.ReleasePlatformsError):
        release_platforms.load_release_platforms(config)


# --- Workstream 2: release feature profile --------------------------------


def test_absent_config_defaults_every_feature_to_mandatory(tmp_path):
    features = release_platforms.load_release_features(tmp_path / "does-not-exist.yaml")
    assert features.synchronization is True
    assert features.meals is True
    assert features.onboarding is True
    assert features.google_calendar is True
    assert features.kiosk_admin is True


def test_features_section_can_opt_a_feature_out(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text(
        "release_features:\n  synchronization: false\n  meals: true\n  onboarding: false\n"
    )
    features = release_platforms.load_release_features(config)
    assert features.synchronization is False
    assert features.meals is True
    assert features.onboarding is False
    # Unlisted features still default to mandatory.
    assert features.google_calendar is True
    assert features.kiosk_admin is True


def test_features_non_mapping_raises(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_features: [nope]\n")
    with pytest.raises(release_platforms.ReleasePlatformsError):
        release_platforms.load_release_features(config)


# --- Workstream 1: the release-platforms command exports the feature scope ----


def test_release_platforms_command_exports_feature_env_for_children(tmp_path, monkeypatch):
    # The launcher `eval "$(python -m calee_regression release-platforms)"`s this
    # output; the exported CALEE_RELEASE_FEATURE_* lines are what propagate the
    # feature scope down to test_caleemobile.sh -> run_ui_suite.py -> the Dart
    # process, sourced from the SAME parsed YAML the consolidator uses.
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_features:\n  meals: false\n  kiosk_admin: true\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(config))

    result = CliRunner().invoke(main, ["release-platforms"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    out = result.output
    # Exported (child-visible) feature vars, canonical true/false.
    assert "export CALEE_RELEASE_FEATURE_MEALS=false" in out
    assert "export CALEE_RELEASE_FEATURE_KIOSK_ADMIN=true" in out
    # Omitted features default to mandatory=true (never silently optional).
    assert "export CALEE_RELEASE_FEATURE_ONBOARDING=true" in out
    assert "export CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR=true" in out
    # The plain (launcher-branching) vars are still emitted too.
    assert "RELEASE_FEATURE_MEALS=false" in out


# --- Phase 3: expected build identity in the release profile -------------


def test_absent_config_has_no_expected_identity(tmp_path):
    identity = release_platforms.load_expected_build_identity(tmp_path / "nope.yaml")
    assert identity.calee_build_version is None
    assert identity.caleemobile_build_version is None
    assert identity.allow_dirty is False


def test_expected_build_identity_is_loaded_from_the_profile(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text(
        "release_platforms:\n  tablet: true\n"
        "expected_build_identity:\n"
        "  calee_build_version: '0.3.22'\n"
        "  calee_git_sha: 'tab123'\n"
        "  caleemobile_build_version: '0.0.22+22'\n"
        "  caleemobile_git_sha: 'mob456'\n"
        "  allow_dirty: true\n"
    )
    identity = release_platforms.load_expected_build_identity(config)
    assert identity.calee_build_version == "0.3.22"
    assert identity.calee_git_sha == "tab123"
    assert identity.caleemobile_build_version == "0.0.22+22"
    assert identity.caleemobile_git_sha == "mob456"
    assert identity.allow_dirty is True


def test_expected_build_identity_blank_values_are_none(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text(
        "expected_build_identity:\n"
        "  calee_build_version: ''\n"
        "  caleemobile_build_version: '   '\n"
    )
    identity = release_platforms.load_expected_build_identity(config)
    assert identity.calee_build_version is None
    assert identity.caleemobile_build_version is None


def test_expected_build_identity_non_mapping_raises(tmp_path):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("expected_build_identity: [nope]\n")
    with pytest.raises(release_platforms.ReleasePlatformsError):
        release_platforms.load_expected_build_identity(config)


PASSING_ENVIRONMENT = {"runId": RUN_ID, "status": "pass", "detail": ["Environment and fixture ready."]}
PASSING_TABLET = certifying({
    "runId": RUN_ID,
    "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
    "scenarios": [{"name": "a", "status": "passed"}],
})
PASSING_API = {"runId": RUN_ID, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]}
PASSING_MOBILE_UI = {"runId": RUN_ID, "counts": {"PASS": 3}, "steps": [{"name": "y", "status": "PASS"}] * 3}
PASSING_MANUAL = {
    "runId": RUN_ID,
    "checks": [{"title": "Kiosk escape check", "instruction": "swipe down", "expectedResult": "no shade", "status": "pass"}],
}


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    # started_at deliberately in the past -- these synthetic reports are
    # written to disk (today's mtime) *before* the manifest exists, and a
    # manifest self-initialized with started_at=now would make every one
    # of them look like it predates the run.
    manifest = run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00")
    manifest.write(workspace.manifest_path)
    return workspace


def _write_component(workspace, component, data):
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return str(path)


def _consolidate(tmp_path, *extra_args, run_id=RUN_ID):
    workspace = _make_workspace(tmp_path, run_id)
    _write_component(workspace, "environment", PASSING_ENVIRONMENT)
    _write_component(workspace, "tablet", PASSING_TABLET)
    _write_component(workspace, "mobile-api", PASSING_API)
    _write_component(workspace, "manual-checks", PASSING_MANUAL)
    runner = CliRunner()
    return runner.invoke(
        main,
        [
            "consolidate", "--run-id", run_id,
            "--out-dir", str(tmp_path / "out"),
            # These tests exercise platform (Android/iOS/tablet) mandatoriness,
            # not build identity -- opt out of the Phase 3 identity requirement
            # so a missing --*-build-version doesn't confound the platform
            # assertions. Build-identity gating has its own tests.
            "--allow-unknown-build-identity",
            # Likewise, they don't exercise cross-device synchronization -- opt
            # out of the Workstream 1 sync gating (which would otherwise BLOCK
            # on a missing sync report and confound the platform assertions).
            # Sync gating has its own tests (test_sync_consolidation.py).
            "--sync-optional",
            # And they don't exercise the independent release-feature components
            # (Workstream 3) -- opt those optional too, for the same reason.
            # Feature gating has its own tests (test_feature_consolidation.py).
            "--meals-optional", "--onboarding-optional",
            "--google-calendar-optional", "--kiosk-admin-optional",
            # Nor do they exercise the CaleeMobile selector contract (Priority 2),
            # which is mandatory for any mobile release. Opt out via the named
            # waiver the diagnostic path requires (selector evidence has its own
            # tests: test_selector_contract_gate.py) so it doesn't confound the
            # platform assertions.
            "--selector-contract-optional",
            "--waiver-reason", "unit test: platform gating only; selector contract has its own tests",
            "--waiver-approver", "framework-tests",
            "--waiver-timestamp", "2026-07-18T00:00:00Z",
            *extra_args,
        ],
    )


def test_tablet_only_release_passes_without_any_mobile_ui_report(tmp_path):
    result = _consolidate(tmp_path, "--android-optional", "--ios-optional")
    assert result.exit_code == EXIT_SUCCESS


def test_tablet_plus_android_release_blocks_when_android_ui_report_missing(tmp_path):
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-optional")
    assert result.exit_code == EXIT_BLOCKED


def test_tablet_plus_android_release_passes_when_android_ui_report_given(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "mobile-android", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-optional")
    assert result.exit_code == EXIT_SUCCESS


def test_tablet_plus_android_plus_ios_release_blocks_when_ios_ui_report_missing(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "mobile-android", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-mandatory")
    assert result.exit_code == EXIT_BLOCKED


def test_tablet_plus_android_plus_ios_release_passes_when_both_ui_reports_given(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "mobile-android", PASSING_MOBILE_UI)
    _write_component(workspace, "mobile-ios", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-mandatory")
    assert result.exit_code == EXIT_SUCCESS


def test_selected_platform_missing_report_blocks(tmp_path):
    # iOS selected as mandatory but no mobile-ios report given.
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "mobile-android", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-mandatory")
    assert result.exit_code == EXIT_BLOCKED
    assert "CaleeMobile iPhone UI" in result.output


def test_optional_platform_omitted_does_not_block(tmp_path):
    result = _consolidate(tmp_path, "--android-optional", "--ios-optional")
    assert result.exit_code == EXIT_SUCCESS
    assert "(optional)" in result.output


# --- Workstream 2: the tablet is unconditionally in scope for a full solution -


def _write_tablet_only_pass(workspace):
    _write_component(workspace, "environment", PASSING_ENVIRONMENT)
    _write_component(workspace, "tablet", PASSING_TABLET)
    _write_component(workspace, "mobile-api", PASSING_API)
    _write_component(workspace, "manual-checks", PASSING_MANUAL)


def test_tablet_false_config_does_not_relax_tablet_identity_requirement(tmp_path, monkeypatch):
    # Even if a config tries to opt the tablet out, the tablet's build identity
    # stays REQUIRED: execution always runs the tablet and consolidation always
    # treats it as mandatory, so its identity must be required too (they must
    # never disagree). No --calee-build-version + identity required -> BLOCKED.
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_platforms:\n  tablet: false\n  mobile_android: false\n  mobile_ios: false\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(config))

    workspace = _make_workspace(tmp_path)
    _write_tablet_only_pass(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--sync-optional", "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "Calee tablet build identity: BLOCKED" in result.output


def test_tablet_stays_mandatory_and_passes_with_identity_despite_tablet_false(tmp_path, monkeypatch):
    # With the tablet identity provided, the same tablet:false config PASSES --
    # the tablet is run and gated exactly like a normal full solution; the flag
    # simply doesn't opt it out.
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_platforms:\n  tablet: false\n  mobile_android: false\n  mobile_ios: false\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(config))

    workspace = _make_workspace(tmp_path)
    _write_tablet_only_pass(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--sync-optional",
         "--meals-optional", "--onboarding-optional",
         "--google-calendar-optional", "--kiosk-admin-optional",
         "--calee-build-version", "0.3.22",
         "--calee-application-id", "com.viso.calee", "--calee-version-code", "322",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "Calee tablet: PASS" in result.output


def test_release_platforms_config_file_drives_mandatory_when_no_cli_override(tmp_path, monkeypatch):
    config = tmp_path / "release-platforms.yaml"
    config.write_text("release_platforms:\n  tablet: true\n  mobile_android: true\n  mobile_ios: false\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(config))

    # No --android-mandatory/--ios-mandatory override -- config decides:
    # android is mandatory (and missing) -> BLOCKED.
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_BLOCKED

    # Now give the (config-mandatory) android report; ios is config-optional
    # and omitted, so this should pass.
    workspace = run_context.RunWorkspace(tmp_path, RUN_ID)
    _write_component(workspace, "mobile-android", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path)
    assert result.exit_code == EXIT_SUCCESS
