"""Tests for the technical-owner release-platform profile (Workstream 9).

Locks in that Android/iOS UI mandatory-ness comes from
config/release-platforms.yaml (or its safe True-by-default absence), never
a hard-coded mandatory=False, and exercises the tablet-only /
tablet+android / tablet+android+ios / selected-platform-missing /
optional-platform-omitted combinations end to end through `consolidate`.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import release_platforms
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS


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


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


PASSING_TABLET = {
    "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
    "scenarios": [{"name": "a", "status": "passed"}],
}
PASSING_API = {"counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}]}
PASSING_MOBILE_UI = {"counts": {"PASS": 3}, "steps": [{"name": "y", "status": "PASS"}] * 3}
PASSING_MANUAL = [{"title": "Kiosk escape check", "instruction": "swipe down", "expectedResult": "no shade", "status": "pass"}]


def _consolidate(tmp_path, *extra_args):
    tablet = _write(tmp_path, "tablet.json", PASSING_TABLET)
    api = _write(tmp_path, "api.json", PASSING_API)
    manual = _write(tmp_path, "manual.json", PASSING_MANUAL)
    runner = CliRunner()
    return runner.invoke(
        main,
        [
            "consolidate",
            "--tablet-report", tablet,
            "--mobile-api-report", api,
            "--manual-checks", manual,
            "--out-dir", str(tmp_path / "out"),
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
    android = _write(tmp_path, "android.json", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-optional", "--mobile-android-report", android)
    assert result.exit_code == EXIT_SUCCESS


def test_tablet_plus_android_plus_ios_release_blocks_when_ios_ui_report_missing(tmp_path):
    android = _write(tmp_path, "android.json", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-mandatory", "--mobile-android-report", android)
    assert result.exit_code == EXIT_BLOCKED


def test_tablet_plus_android_plus_ios_release_passes_when_both_ui_reports_given(tmp_path):
    android = _write(tmp_path, "android.json", PASSING_MOBILE_UI)
    ios = _write(tmp_path, "ios.json", PASSING_MOBILE_UI)
    result = _consolidate(
        tmp_path, "--android-mandatory", "--ios-mandatory",
        "--mobile-android-report", android, "--mobile-ios-report", ios,
    )
    assert result.exit_code == EXIT_SUCCESS


def test_selected_platform_missing_report_blocks(tmp_path):
    # iOS selected as mandatory but no --mobile-ios-report given.
    android = _write(tmp_path, "android.json", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--android-mandatory", "--ios-mandatory", "--mobile-android-report", android)
    assert result.exit_code == EXIT_BLOCKED
    assert "CaleeMobile iPhone UI" in result.output


def test_optional_platform_omitted_does_not_block(tmp_path):
    result = _consolidate(tmp_path, "--android-optional", "--ios-optional")
    assert result.exit_code == EXIT_SUCCESS
    assert "(optional)" in result.output


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
    android = _write(tmp_path, "android.json", PASSING_MOBILE_UI)
    result = _consolidate(tmp_path, "--mobile-android-report", android)
    assert result.exit_code == EXIT_SUCCESS
