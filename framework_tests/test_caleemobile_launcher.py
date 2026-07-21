"""Tests for scripts/test_caleemobile.sh (Workstream 3).

Combines content assertions (the concrete defect fix: credentials must be
passed through, `-d android`/`-d ios` must not be hardcoded) with real
dry-runs of the script against a fake CaleeMobile-Regression sibling, so a
future edit can't silently regress either the fix itself or its behavior.
"""

from __future__ import annotations

import json
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


def test_script_wires_fixture_and_backend_status_into_the_ui_run():
    # Workstream 7: before any UI assertion runs, run_ui_suite.py must be
    # able to see this run's own fixture-verification status and target
    # backend (from prepare's environment/results.json) so it can BLOCK
    # instead of asserting against unverified/misdirected data -- see
    # run_ui_suite.py::check_fixture_and_backend_alignment.
    text = _read_script()
    assert "CALEE_FIXTURE_STATUS" in text
    assert "CALEE_EXPECTED_BACKEND" in text
    assert "fixtureVerificationStatus" in text
    assert "environment/results.json" in text or "environment', 'results.json'" in text


def _copy_calee_regression(workspace):
    """The script locates its sibling as `../CaleeMobile-Regression`
    relative to its OWN directory (via BASH_SOURCE), not relative to the
    caller's cwd -- so the fake sibling must be a real sibling directory
    of the copied calee-regression, both directly under `workspace`."""
    calee_regression_copy = workspace / "calee-regression"
    shutil.copytree(REPO_ROOT, calee_regression_copy, ignore=shutil.ignore_patterns(".git", "reports"))
    (calee_regression_copy / "reports").mkdir(exist_ok=True)
    return calee_regression_copy


def _make_fake_sibling(workspace, ui_recorder=False):
    sibling = workspace / "CaleeMobile-Regression"
    api_dir = sibling / "api"
    api_dir.mkdir(parents=True)
    # The stand-in run_regression.py appends "API" to FAKE_ORDER_LOG (when
    # set) and records the backend env var it saw into FAKE_API_RECORD (when
    # set) before writing a PASS report -- so a test can confirm BOTH that
    # the Client API suite never ran ahead of the Prepare gate, and that the
    # one verified backend reached it. When neither env var is set it behaves
    # exactly as before (write report, exit 0).
    (api_dir / "run_regression.py").write_text(
        "import sys, os, json\n"
        "idx = sys.argv.index('--report')\n"
        "order_log = os.environ.get('FAKE_ORDER_LOG')\n"
        "if order_log:\n"
        "    with open(order_log, 'a') as f:\n"
        "        f.write('API\\n')\n"
        "api_record = os.environ.get('FAKE_API_RECORD')\n"
        "if api_record:\n"
        "    with open(api_record, 'w') as f:\n"
        "        json.dump({'CALEE_API_BASE': os.environ.get('CALEE_API_BASE')}, f)\n"
        "with open(sys.argv[idx + 1], 'w') as f:\n"
        "    json.dump({'runId': 'r', 'counts': {'PASS': 1}, 'steps': [{'name': 'x', 'status': 'PASS'}]}, f)\n"
        "sys.exit(0)\n"
    )
    ui_dir = sibling / "ui"
    ui_dir.mkdir(parents=True)
    if ui_recorder:
        # A stand-in run_ui_suite.py that records the backend/fixture env vars
        # the launcher exported for it, and writes a PASS report -- so a test
        # can confirm the verified backend actually reached the UI step. It
        # also appends "UI" to FAKE_ORDER_LOG (when set) so a test can assert
        # the API suite ran strictly before it.
        (ui_dir / "run_ui_suite.py").write_text(
            "import sys, os, json\n"
            "report = ''\n"
            "for i, a in enumerate(sys.argv):\n"
            "    if a == '--report':\n"
            "        report = sys.argv[i + 1]\n"
            "order_log = os.environ.get('FAKE_ORDER_LOG')\n"
            "if order_log:\n"
            "    with open(order_log, 'a') as f:\n"
            "        f.write('UI\\n')\n"
            "rec = {\n"
            "    'CALEE_MOBILE_BACKEND': os.environ.get('CALEE_MOBILE_BACKEND'),\n"
            "    'CALEE_EXPECTED_BACKEND': os.environ.get('CALEE_EXPECTED_BACKEND'),\n"
            "    'CALEE_FIXTURE_STATUS': os.environ.get('CALEE_FIXTURE_STATUS'),\n"
            "    'argv': sys.argv,\n"
            "}\n"
            "with open(os.environ['FAKE_UI_RECORD'], 'w') as f:\n"
            "    json.dump(rec, f)\n"
            "if report:\n"
            "    with open(report, 'w') as f:\n"
            "        json.dump({'runId': 'ui', 'counts': {'PASS': 1}, 'steps': [{'name': 'x', 'status': 'PASS'}]}, f)\n"
            "sys.exit(0)\n"
        )
    return sibling


def _write_env_report(
    calee_regression_copy, run_id, *, run_id_in_report=None,
    fixture_status="ok", backend="https://hub-dev.calee.com.au",
):
    """Pre-creates this run's environment/results.json (as prepare would),
    so tests can drive the "environment already prepared" path without
    invoking Prepare."""
    env_dir = calee_regression_copy / "reports" / "runs" / run_id / "environment"
    env_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "runId": run_id if run_id_in_report is None else run_id_in_report,
        "status": "pass" if fixture_status == "ok" else "blocked",
        "fixtureVerificationStatus": fixture_status,
        "targetEnvironment": backend,
    }
    report_path = env_dir / "results.json"
    report_path.write_text(json.dumps(payload) + "\n")
    return report_path


def _make_fakebin(workspace, *, with_flutter=False):
    """A fakebin dir whose `python` shim intercepts
    `python -m calee_regression prepare/record-component` (so the self-
    prepare step is deterministic, never touching a real backend/Appium),
    and optionally a `flutter` shim (so `flutter pub get` succeeds)."""
    fakebin = workspace / "fakebin"
    fakebin.mkdir(exist_ok=True)
    py = fakebin / "python"
    py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, json\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['-m', 'calee_regression']:\n"
        "    cmd = argv[2] if len(argv) > 2 else ''\n"
        "    if cmd == 'prepare':\n"
        "        run_id = ''\n"
        "        for i, a in enumerate(argv):\n"
        "            if a == '--run-id' and i + 1 < len(argv):\n"
        "                run_id = argv[i + 1]\n"
        "        with open(os.environ['FAKE_PREPARE_CALL_LOG'], 'a') as f:\n"
        "            f.write(' '.join(argv) + '\\n')\n"
        "        if os.environ.get('FAKE_PREPARE_WRITES_REPORT') == '1':\n"
        "            d = os.path.join(os.environ['FAKE_REPO_ROOT'], 'reports', 'runs', run_id, 'environment')\n"
        "            os.makedirs(d, exist_ok=True)\n"
        "            report = json.loads(os.environ.get('FAKE_PREPARE_REPORT', '{}'))\n"
        "            report.setdefault('runId', run_id)\n"
        "            with open(os.path.join(d, 'results.json'), 'w') as f:\n"
        "                json.dump(report, f)\n"
        "        sys.exit(int(os.environ.get('FAKE_PREPARE_EXIT', '0')))\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n"
    )
    py.chmod(0o755)
    if with_flutter:
        fl = fakebin / "flutter"
        fl.write_text("#!/bin/bash\nexit 0\n")
        fl.chmod(0o755)
    return fakebin


_HERMETIC_ENV_KEYS = (
    "CALEE_TEST_EMAIL", "CALEE_TEST_PASSWORD", "CALEE_API_BASE",
    "CALEE_TEST_CONFIG", "CALEE_RUN_ID", "CALEE_FIXTURE_STATUS",
    "CALEE_EXPECTED_BACKEND", "CALEE_MOBILE_BACKEND",
)


def _run_script(calee_regression_copy, workspace, platform, env_overrides=None):
    env = dict(os.environ)
    # Deliberately hide any real `flutter` on PATH so these dry runs
    # exercise the "flutter toolchain unavailable" BLOCKED path
    # regardless of what happens to be installed in CI/this sandbox.
    env["PATH"] = "/usr/bin:/bin"
    for key in _HERMETIC_ENV_KEYS:
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        ["bash", str(calee_regression_copy / "scripts" / "test_caleemobile.sh"), platform],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _run_script_fakebin(calee_regression_copy, workspace, platform, *, fakebin, extra_env):
    """Like _run_script but with a fakebin (its `python` shim) first on PATH
    and a real PATH behind it, so the self-prepare step runs against the
    shim while python3/bash still resolve for real."""
    env = dict(os.environ)
    env["PATH"] = f"{fakebin}{os.pathsep}/usr/bin:/bin"
    for key in _HERMETIC_ENV_KEYS:
        env.pop(key, None)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(calee_regression_copy / "scripts" / "test_caleemobile.sh"), platform],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
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
    # A tester config exists, but no fixture credentials -> the self-prepare
    # step can't reset/verify the fixture, so the mobile UI is BLOCKED with a
    # clear credentials message (never the old "not checked" pass-through).
    example_config = calee_regression_copy / "config" / "tester.local.example.yaml"
    result = _run_script(
        calee_regression_copy, tmp_path, "android",
        env_overrides={"CALEE_TEST_CONFIG": str(example_config)},
    )

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


def test_full_solution_launcher_auto_collects_build_identity():
    # Phase 3: build identity must be collected automatically (not only when a
    # technical owner manually set env vars) and passed to consolidate, with
    # the dirty/availability flags wired through.
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    assert "build-identity" in text
    assert "AUTO_CALEEMOBILE_BUILD_VERSION" in text
    assert "AUTO_CALEE_BUILD_VERSION" in text
    assert "--caleemobile-dirty" in text
    assert "--calee-identity-unavailable" in text
    # The auto value only fills a gap -- a manually-set env var still wins.
    assert '${CALEEMOBILE_BUILD_VERSION:-${AUTO_CALEEMOBILE_BUILD_VERSION' in text


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
    # A verified same-run environment report already exists (as after "06 Test
    # Full Calee Solution" prepared it), so self-prepare is skipped, gating
    # passes, and the run reaches the flutter check -- which BLOCKS because no
    # flutter is on PATH.
    run_id = "release-20260101-000000-abc123"
    _write_env_report(calee_regression_copy, run_id, fixture_status="ok")
    result = _run_script(
        calee_regression_copy, tmp_path, "android",
        env_overrides={
            "CALEE_RUN_ID": run_id,
            "CALEE_TEST_EMAIL": "demo@example.com",
            "CALEE_TEST_PASSWORD": "hunter2",
        },
    )

    assert result.returncode == 3
    assert "Flutter installed" in result.stdout
    assert "hunter2" not in result.stdout
    assert "demo@example.com" not in result.stdout
    # Gating passed on the pre-existing report -- self-prepare was skipped and
    # the verified backend was surfaced before the UI step.
    assert "Environment verified" in result.stdout
    assert "preparing the environment first" not in result.stdout


# --- Phase 2: same-run Prepare enforcement (the result policy) -----------

DEV_BACKEND = "https://hub-dev.calee.com.au"


def test_script_self_prepares_with_same_run_id():
    text = _read_script()
    # It runs Prepare with THIS run's ID when there is no environment report.
    assert 'prepare --config "$CALEE_TEST_CONFIG" --run-id "$CALEE_RUN_ID"' in text
    # It validates the report belongs to this run and the fixture verified ok.
    assert "fixtureVerificationStatus" in text
    assert "does not match this run" in text
    # It hands the verified backend to the UI step to build against.
    assert "CALEE_MOBILE_BACKEND" in text


def _prepare_env(calee_regression_copy, call_log, *, exit_code, writes_report, report):
    example_config = calee_regression_copy / "config" / "tester.local.example.yaml"
    return {
        "CALEE_TEST_CONFIG": str(example_config),
        "CALEE_API_BASE": DEV_BACKEND,
        "CALEE_TEST_EMAIL": "demo@example.com",
        "CALEE_TEST_PASSWORD": "hunter2",
        "FAKE_REPO_ROOT": str(calee_regression_copy),
        "FAKE_PREPARE_CALL_LOG": str(call_log),
        "FAKE_PREPARE_EXIT": str(exit_code),
        "FAKE_PREPARE_WRITES_REPORT": "1" if writes_report else "0",
        "FAKE_PREPARE_REPORT": json.dumps(report),
    }


def test_standalone_run_self_prepares_and_passes_backend_when_no_env_report(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    record = tmp_path / "ui_record.json"
    _make_fake_sibling(tmp_path, ui_recorder=True)
    fakebin = _make_fakebin(tmp_path, with_flutter=True)
    call_log = tmp_path / "prepare_calls.log"
    run_id = "release-20260101-000000-aaa111"

    extra_env = _prepare_env(
        calee_regression_copy, call_log,
        exit_code=0, writes_report=True,
        report={"fixtureVerificationStatus": "ok", "targetEnvironment": DEV_BACKEND},
    )
    extra_env["CALEE_RUN_ID"] = run_id
    extra_env["FAKE_UI_RECORD"] = str(record)

    result = _run_script_fakebin(
        calee_regression_copy, tmp_path, "android", fakebin=fakebin, extra_env=extra_env
    )

    # Prepare was invoked automatically, with THIS run's ID.
    assert call_log.is_file()
    calls = call_log.read_text()
    assert "prepare" in calls
    assert f"--run-id {run_id}" in calls
    # The verified backend reached run_ui_suite via the environment.
    assert record.is_file(), result.stdout + result.stderr
    rec = json.loads(record.read_text())
    assert rec["CALEE_MOBILE_BACKEND"] == DEV_BACKEND
    assert rec["CALEE_EXPECTED_BACKEND"] == DEV_BACKEND
    assert rec["CALEE_FIXTURE_STATUS"] == "ok"
    assert result.returncode == 0
    assert "Environment verified" in result.stdout


def test_self_prepare_blocked_blocks_mobile_ui(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path, ui_recorder=True)
    fakebin = _make_fakebin(tmp_path, with_flutter=True)
    call_log = tmp_path / "prepare_calls.log"

    extra_env = _prepare_env(
        calee_regression_copy, call_log,
        exit_code=3, writes_report=False, report={},
    )
    extra_env["CALEE_RUN_ID"] = "release-20260101-000000-ccc333"

    result = _run_script_fakebin(
        calee_regression_copy, tmp_path, "android", fakebin=fakebin, extra_env=extra_env
    )

    assert result.returncode == 3
    assert "Prepare did not pass" in result.stdout


def test_env_report_missing_after_prepare_blocks(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path, ui_recorder=True)
    fakebin = _make_fakebin(tmp_path, with_flutter=True)
    call_log = tmp_path / "prepare_calls.log"

    # Prepare "passes" (exit 0) but writes no environment report.
    extra_env = _prepare_env(
        calee_regression_copy, call_log,
        exit_code=0, writes_report=False, report={},
    )
    extra_env["CALEE_RUN_ID"] = "release-20260101-000000-ddd444"

    result = _run_script_fakebin(
        calee_regression_copy, tmp_path, "android", fakebin=fakebin, extra_env=extra_env
    )

    assert result.returncode == 3
    assert "wrote no environment report" in result.stdout


def test_env_report_run_id_mismatch_blocks(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path)
    run_id = "release-20260101-000000-bbb222"
    # A same-run environment report exists, but it carries a different run ID.
    _write_env_report(
        calee_regression_copy, run_id,
        run_id_in_report="release-SOMEOTHER-999999", fixture_status="ok",
    )
    result = _run_script(
        calee_regression_copy, tmp_path, "android",
        env_overrides={
            "CALEE_RUN_ID": run_id,
            "CALEE_TEST_EMAIL": "demo@example.com",
            "CALEE_TEST_PASSWORD": "hunter2",
        },
    )

    assert result.returncode == 3
    assert "does not match this run" in result.stdout


def test_env_report_fixture_not_ok_blocks(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path)
    run_id = "release-20260101-000000-eee555"
    _write_env_report(calee_regression_copy, run_id, fixture_status="blocked")
    result = _run_script(
        calee_regression_copy, tmp_path, "android",
        env_overrides={
            "CALEE_RUN_ID": run_id,
            "CALEE_TEST_EMAIL": "demo@example.com",
            "CALEE_TEST_PASSWORD": "hunter2",
        },
    )

    assert result.returncode == 3
    assert "fixture was not verified" in result.stdout


# --- Phase 1: Prepare-before-API execution ordering ----------------------
# NEITHER the Client API regression NOR the mobile UI regression may run
# before Prepare has passed and this run's environment/fixture/backend have
# been verified. The old ordering ran the Client API suite first, against a
# not-yet-prepared environment.


def test_script_runs_client_api_after_the_prepare_gate_not_before():
    # Structural guard: the run_regression.py invocation must appear AFTER
    # the environment-verification gate (the "[OK] Environment verified"
    # line the gate emits), never before it, so a future edit can't quietly
    # move the API suite back ahead of Prepare.
    text = _read_script()
    gate_marker = "Environment verified for run"
    api_marker = "python3 run_regression.py"
    assert gate_marker in text
    assert api_marker in text
    assert text.index(api_marker) > text.index(gate_marker), (
        "the Client API regression must be invoked after the environment "
        "verification gate, not before it"
    )
    # And it must live inside the "gate passed" branch, alongside the guard
    # comment that documents the ordering contract.
    assert "only once the gate has passed" in text


def test_verified_report_runs_client_api_then_ui_with_the_verified_backend(tmp_path):
    # A verified same-run environment report already exists (the "06 Test
    # Full Calee Solution" case). Both suites must run, the API strictly
    # before the UI, and each must see the one verified backend.
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path, ui_recorder=True)
    fakebin = _make_fakebin(tmp_path, with_flutter=True)
    run_id = "release-20260101-000000-order1"
    _write_env_report(calee_regression_copy, run_id, fixture_status="ok", backend=DEV_BACKEND)

    order_log = tmp_path / "order.log"
    api_record = tmp_path / "api_record.json"
    ui_record = tmp_path / "ui_record.json"
    result = _run_script_fakebin(
        calee_regression_copy, tmp_path, "android", fakebin=fakebin,
        extra_env={
            "CALEE_RUN_ID": run_id,
            "CALEE_TEST_EMAIL": "demo@example.com",
            "CALEE_TEST_PASSWORD": "hunter2",
            "FAKE_ORDER_LOG": str(order_log),
            "FAKE_API_RECORD": str(api_record),
            "FAKE_UI_RECORD": str(ui_record),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # API ran strictly before UI.
    assert order_log.read_text() == "API\nUI\n"
    # The one verified backend reached the Client API suite as CALEE_API_BASE...
    assert json.loads(api_record.read_text())["CALEE_API_BASE"] == DEV_BACKEND
    # ...and reached the UI suite as CALEE_MOBILE_BACKEND / CALEE_EXPECTED_BACKEND.
    ui_rec = json.loads(ui_record.read_text())
    assert ui_rec["CALEE_MOBILE_BACKEND"] == DEV_BACKEND
    assert ui_rec["CALEE_EXPECTED_BACKEND"] == DEV_BACKEND
    assert ui_rec["CALEE_FIXTURE_STATUS"] == "ok"


def test_prepare_failure_prevents_client_api_from_running(tmp_path):
    # Prepare exits non-zero -> the whole mobile run is BLOCKED and the
    # Client API suite must NOT have executed (the old bug ran it first).
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path, ui_recorder=True)
    fakebin = _make_fakebin(tmp_path, with_flutter=True)
    call_log = tmp_path / "prepare_calls.log"
    order_log = tmp_path / "order.log"

    extra_env = _prepare_env(
        calee_regression_copy, call_log, exit_code=3, writes_report=False, report={},
    )
    extra_env["CALEE_RUN_ID"] = "release-20260101-000000-block1"
    extra_env["FAKE_ORDER_LOG"] = str(order_log)

    result = _run_script_fakebin(
        calee_regression_copy, tmp_path, "android", fakebin=fakebin, extra_env=extra_env
    )

    assert result.returncode == 3
    assert "Prepare did not pass" in result.stdout
    assert "Neither the Client API checks nor the android UI checks ran" in result.stdout
    # The Client API suite never ran -- the order log was never written.
    assert not order_log.exists()


def test_fixture_not_ok_prevents_client_api_from_running(tmp_path):
    # A same-run report exists but the fixture wasn't verified -> both
    # suites are gated out, including the Client API suite.
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path)
    run_id = "release-20260101-000000-block2"
    _write_env_report(calee_regression_copy, run_id, fixture_status="blocked")
    order_log = tmp_path / "order.log"

    result = _run_script(
        calee_regression_copy, tmp_path, "android",
        env_overrides={
            "CALEE_RUN_ID": run_id,
            "CALEE_TEST_EMAIL": "demo@example.com",
            "CALEE_TEST_PASSWORD": "hunter2",
            "FAKE_ORDER_LOG": str(order_log),
        },
    )

    assert result.returncode == 3
    assert "fixture was not verified" in result.stdout
    assert not order_log.exists()


# --- Phase 3: the Client API regression runs EXACTLY ONCE per release -----


def _run_script_args(calee_regression_copy, workspace, args, env_overrides=None):
    """Like _run_script but passes an explicit argv (platform and/or mode)."""
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    for key in _HERMETIC_ENV_KEYS:
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(calee_regression_copy / "scripts" / "test_caleemobile.sh"), *args],
        cwd=str(workspace), env=env, capture_output=True, text=True, timeout=60,
    )


def _run_script_fakebin_args(calee_regression_copy, workspace, args, *, fakebin, extra_env):
    env = dict(os.environ)
    env["PATH"] = f"{fakebin}{os.pathsep}/usr/bin:/bin"
    for key in _HERMETIC_ENV_KEYS:
        env.pop(key, None)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(calee_regression_copy / "scripts" / "test_caleemobile.sh"), *args],
        cwd=str(workspace), env=env, capture_output=True, text=True, timeout=60,
    )


def test_full_solution_runs_client_api_once_and_ui_only_per_platform():
    # The Client API suite is device-independent, so the full-solution launcher
    # runs it EXACTLY ONCE (api-only) and then the per-platform UI ONLY
    # (--ui-only). This is what stops an Android and an iOS run from each
    # re-running and overwriting the one mobile-api/results.json (Phase 3).
    #
    # Priority 2 (this session): two MUTUALLY EXCLUSIVE branches now exist --
    # the selector-contract PASS branch, and the selector-OPTIONAL-and-failed
    # branch (device-independent API may still proceed there; see the
    # selector-contract gate step) -- so the literal text contains api-only
    # twice, but any single actual run takes exactly one of these branches and
    # so still runs api-only exactly once.
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    assert text.count("test_caleemobile.sh api-only") == 2
    assert "test_caleemobile.sh android --ui-only" in text
    assert "test_caleemobile.sh ios --ui-only" in text
    # The old "run the whole thing (incl. API) per platform" calls are gone.
    assert "test_caleemobile.sh android\n" not in text
    assert "test_caleemobile.sh ios\n" not in text


def test_full_solution_runs_api_once_before_and_outside_the_platform_blocks():
    # Requirement 5: excluding an optional platform must not stop or re-run the
    # API. The single api-only call sits before (and outside) both
    # per-platform if-blocks, so platform selection can't affect it.
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    api_idx = text.index("test_caleemobile.sh api-only")
    android_if_idx = text.index('if [ "$RELEASE_PLATFORM_ANDROID" = "true" ]')
    ios_if_idx = text.index('if [ "$RELEASE_PLATFORM_IOS" = "true" ]')
    assert api_idx < android_if_idx < ios_if_idx


def test_full_solution_captures_pre_and_post_build_identity():
    # Phase 4: pre-run identity is captured before any test runs; post-run
    # identity after. Consolidation compares them and BLOCKS on a change.
    text = FULL_SOLUTION_SCRIPT.read_text(encoding="utf-8")
    assert 'build-identity --run-id "$CALEE_RUN_ID" --phase pre' in text
    assert 'build-identity --run-id "$CALEE_RUN_ID" --phase post' in text
    tablet_marker = "--- Step 2: Calee Tablet ---"
    assert text.index("--phase pre") < text.index(tablet_marker)
    assert text.index("--phase post") > text.index(tablet_marker)


def test_api_only_mode_runs_api_and_records_only_the_api_report(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path, ui_recorder=True)
    run_id = "release-20260101-000000-apionly"
    _write_env_report(calee_regression_copy, run_id, fixture_status="ok", backend=DEV_BACKEND)
    order_log = tmp_path / "order.log"

    result = _run_script_args(
        calee_regression_copy, tmp_path, ["api-only"],
        env_overrides={"CALEE_RUN_ID": run_id, "FAKE_ORDER_LOG": str(order_log)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # Only the Client API suite ran, exactly once -- no UI.
    assert order_log.read_text() == "API\n"
    runs = calee_regression_copy / "reports" / "runs" / run_id
    assert (runs / "mobile-api" / "results.json").is_file()
    # api-only never produces (or touches) any per-platform UI report.
    assert not (runs / "mobile-android" / "results.json").exists()
    assert not (runs / "mobile-ios" / "results.json").exists()


def test_ui_only_mode_runs_ui_and_never_runs_or_writes_the_api_report(tmp_path):
    calee_regression_copy = _copy_calee_regression(tmp_path)
    _make_fake_sibling(tmp_path, ui_recorder=True)
    fakebin = _make_fakebin(tmp_path, with_flutter=True)
    run_id = "release-20260101-000000-uionly"
    _write_env_report(calee_regression_copy, run_id, fixture_status="ok", backend=DEV_BACKEND)
    order_log = tmp_path / "order.log"
    ui_record = tmp_path / "ui_record.json"

    result = _run_script_fakebin_args(
        calee_regression_copy, tmp_path, ["android", "--ui-only"], fakebin=fakebin,
        extra_env={
            "CALEE_RUN_ID": run_id,
            "CALEE_TEST_EMAIL": "demo@example.com",
            "CALEE_TEST_PASSWORD": "hunter2",
            "FAKE_ORDER_LOG": str(order_log),
            "FAKE_UI_RECORD": str(ui_record),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # The Android UI ran; the Client API suite never ran in this invocation.
    assert order_log.read_text() == "UI\n"
    runs = calee_regression_copy / "reports" / "runs" / run_id
    assert (runs / "mobile-android" / "results.json").is_file()
    # A --ui-only run must never write (let alone overwrite) the API report.
    assert not (runs / "mobile-api" / "results.json").exists()


# --- Workstream 1: the release-feature scope reaches run_ui_suite / Flutter ---


def test_script_populates_and_forwards_release_feature_flags():
    text = _read_script()
    # No YAML parsing in bash: the scope is sourced from the release-platforms
    # command (its exported CALEE_RELEASE_FEATURE_* lines), defaulting to
    # mandatory (true) when the parent launcher didn't already export it.
    assert "release-platforms" in text
    for var in (
        "CALEE_RELEASE_FEATURE_MEALS", "CALEE_RELEASE_FEATURE_ONBOARDING",
        "CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR", "CALEE_RELEASE_FEATURE_KIOSK_ADMIN",
    ):
        assert var in text
    # The three mobile features are forwarded to run_ui_suite.py as --feature.
    assert "--feature" in text
    assert 'meals=$CALEE_RELEASE_FEATURE_MEALS' in text
    assert 'onboarding=$CALEE_RELEASE_FEATURE_ONBOARDING' in text
    assert 'google_calendar=$CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR' in text


def test_release_feature_flags_with_explicit_values_reach_run_ui_suite(tmp_path):
    # End-to-end dry run: an explicit CALEE_RELEASE_FEATURE_* scope exported by
    # the parent launcher must arrive verbatim on run_ui_suite.py's argv.
    calee_regression_copy = _copy_calee_regression(tmp_path)
    record = tmp_path / "ui_record.json"
    _make_fake_sibling(tmp_path, ui_recorder=True)
    fakebin = _make_fakebin(tmp_path, with_flutter=True)
    call_log = tmp_path / "prepare_calls.log"
    run_id = "release-20260101-000000-feat01"

    extra_env = _prepare_env(
        calee_regression_copy, call_log,
        exit_code=0, writes_report=True,
        report={"fixtureVerificationStatus": "ok", "targetEnvironment": DEV_BACKEND},
    )
    extra_env["CALEE_RUN_ID"] = run_id
    extra_env["FAKE_UI_RECORD"] = str(record)
    # A parent launcher exported a mixed scope: meals excluded, gcal excluded.
    extra_env["CALEE_RELEASE_FEATURE_MEALS"] = "false"
    extra_env["CALEE_RELEASE_FEATURE_ONBOARDING"] = "true"
    extra_env["CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR"] = "false"

    result = _run_script_fakebin(
        calee_regression_copy, tmp_path, "android", fakebin=fakebin, extra_env=extra_env
    )

    assert record.is_file(), result.stdout + result.stderr
    argv = json.loads(record.read_text())["argv"]
    assert "meals=false" in argv
    assert "onboarding=true" in argv
    assert "google_calendar=false" in argv
    # Each rides on its own --feature flag.
    assert argv.count("--feature") == 3
    assert result.returncode == 0
