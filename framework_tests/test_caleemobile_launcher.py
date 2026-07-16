"""Tests for scripts/test_caleemobile.sh (Workstream 3).

Combines content assertions (the concrete defect fix: credentials must be
passed through, `-d android`/`-d ios` must not be hardcoded) with real
dry-runs of the script against a fake CaleeMobile-Regression sibling, so a
future edit can't silently regress either the fix itself or its behavior.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

from calee_regression.suites import REPO_ROOT

SCRIPT_PATH = REPO_ROOT / "scripts" / "test_caleemobile.sh"


def _read_script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_script_no_longer_hardcodes_dash_d_platform_literal():
    text = _read_script()
    assert '-d "$PLATFORM"' not in text
    # The script no longer invokes `flutter test` directly at all -- device
    # resolution now happens inside run_ui_suite.py's resolve_device().
    assert "flutter test" not in text


def test_script_does_not_pass_credentials_as_bare_cli_arguments():
    # Credentials must flow through the environment into run_ui_suite.py
    # (which reads CALEE_TEST_EMAIL/CALEE_TEST_PASSWORD itself), never as
    # a literal --email/--password on the command line where `ps` could
    # see them.
    text = _read_script()
    assert "--email" not in text
    assert "--password" not in text
    assert "CALEE_TEST_EMAIL" in text
    assert "CALEE_TEST_PASSWORD" in text


def test_script_delegates_ui_run_to_the_structured_report_wrapper():
    text = _read_script()
    assert "run_ui_suite.py" in text
    assert "--report" in text
    assert "--log" in text


def _copy_calee_regression(workspace):
    """The script locates its sibling as `../CaleeMobile-Regression`
    relative to its OWN directory (via BASH_SOURCE), not relative to the
    caller's cwd -- so the fake sibling must be a real sibling directory
    of the copied calee-regression, both directly under `workspace`."""
    calee_regression_copy = workspace / "calee-regression"
    shutil.copytree(REPO_ROOT, calee_regression_copy, ignore=shutil.ignore_patterns(".git", "reports"))
    (calee_regression_copy / "reports").mkdir(exist_ok=True)
    return calee_regression_copy


def _make_fake_sibling(workspace):
    sibling = workspace / "CaleeMobile-Regression"
    api_dir = sibling / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "run_regression.py").write_text(
        "import sys, json\n"
        "idx = sys.argv.index('--report')\n"
        "with open(sys.argv[idx + 1], 'w') as f:\n"
        "    json.dump({'runId': 'r', 'counts': {'PASS': 1}, 'steps': [{'name': 'x', 'status': 'PASS'}]}, f)\n"
        "sys.exit(0)\n"
    )
    (sibling / "ui").mkdir(parents=True)
    return sibling


def _run_script(calee_regression_copy, workspace, platform, env_overrides=None):
    env = dict(os.environ)
    # Deliberately hide any real `flutter` on PATH so these dry runs
    # exercise the "flutter toolchain unavailable" BLOCKED path
    # regardless of what happens to be installed in CI/this sandbox.
    env["PATH"] = "/usr/bin:/bin"
    env.pop("CALEE_TEST_EMAIL", None)
    env.pop("CALEE_TEST_PASSWORD", None)
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        ["bash", str(calee_regression_copy / "scripts" / "test_caleemobile.sh"), platform],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_dry_run_blocks_when_sibling_repo_is_missing(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    result = _run_script(calee_regression_copy, tmp_path, "android")

    assert result.returncode == 3
    assert "BLOCKED" in result.stdout
    assert "was not found next to this folder" in result.stdout


def test_dry_run_blocks_with_clear_message_when_credentials_are_missing(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path)
    result = _run_script(calee_regression_copy, tmp_path, "android")

    assert result.returncode == 3
    assert "CALEE_TEST_EMAIL and CALEE_TEST_PASSWORD" in result.stdout
    # Never echo whatever password *was* configured, even accidentally.
    assert "hunter2" not in result.stdout


FULL_SOLUTION_SCRIPT = REPO_ROOT / "tester" / "06 Test Full Calee Solution.command"


def test_full_solution_launcher_generates_and_shares_one_run_id():
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    assert "CALEE_RUN_ID=" in text
    assert "export CALEE_RUN_ID" in text
    # Every component-producing step must be handed the same run ID --
    # see calee_regression/run_context.py and Workstream 3.
    for required in (
        'prepare --config "$CALEE_TEST_CONFIG" --suite tablet-full --run-id "$CALEE_RUN_ID"',
        'suite --config "$CALEE_TEST_CONFIG" --suite full-tester --run-id "$CALEE_RUN_ID"',
        'record-manual-checks --run-id "$CALEE_RUN_ID"',
        '--run-id "$CALEE_RUN_ID"',  # consolidate
    ):
        assert required in text, f"{FULL_SOLUTION_SCRIPT.name} does not wire the shared run ID into: {required}"


def test_full_solution_launcher_does_not_use_forbidden_discovery_patterns():
    # These are exactly the patterns that let a stale/foreign report slip
    # into consolidation undetected -- see Workstream 3 and
    # docs/RELEASE_POLICY.md. consolidate now auto-discovers every
    # component from this run's fixed workspace paths instead.
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    assert "ls -1dt" not in text
    assert "head -n1" not in text
    assert "mobile-api-latest.json" not in text
    assert "manual-checks-latest.json" not in text
    assert "environment-status-latest.json" not in text
    for forbidden in ("--tablet-report", "--mobile-api-report", "--mobile-android-report", "--mobile-ios-report"):
        assert forbidden not in text, f"{FULL_SOLUTION_SCRIPT.name} should let consolidate auto-discover {forbidden}"


def test_full_solution_launcher_runs_manual_checks_and_stops_appium():
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    assert "record-manual-checks" in text
    assert "stop-appium" in text
    assert "release-platforms" in text


def test_full_solution_launcher_respects_release_platform_profile_for_mandatory_flags():
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    assert "--android-mandatory" in text
    assert "--android-optional" in text
    assert "--ios-mandatory" in text
    assert "--ios-optional" in text
    assert "RELEASE_PLATFORM_ANDROID" in text
    assert "RELEASE_PLATFORM_IOS" in text


def test_dry_run_blocks_on_missing_flutter_toolchain_when_credentials_are_present(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path)
    result = _run_script(
        calee_regression_copy, tmp_path, "android",
        env_overrides={"CALEE_TEST_EMAIL": "demo@example.com", "CALEE_TEST_PASSWORD": "hunter2"},
    )

    assert result.returncode == 3
    assert "Flutter installed" in result.stdout
    assert "hunter2" not in result.stdout
    assert "demo@example.com" not in result.stdout
