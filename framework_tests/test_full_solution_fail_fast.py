"""Phase 1: fail fast when Prepare fails.

`tester/06 Test Full Calee Solution.command` must NOT run any downstream
functional test (Calee tablet suite, CaleeMobile Client API, Android UI,
iPhone UI, cross-device synchronization, manual functional checks) once
Prepare has reported anything other than READY. When Prepare is not ready the
launcher still has to:

  * preserve the environment report Prepare wrote,
  * collect the safe (read-only) build identity,
  * produce ONE consolidated BLOCKED bundle,
  * stop Appium if this run started it,
  * print the run ID, the bundle workspace, and the exact Prepare problem.

These tests dry-run the real launcher against a fake `python`/`python3` (so
`python -m calee_regression <cmd>` is intercepted, never touching a real
backend/Appium) and a fake `scripts/test_caleemobile.sh`, recording the exact
order in which the orchestrated steps ran. That lets us assert -- against the
real script, not a copy of its logic -- that no downstream test command runs
after a failed Prepare, while consolidation + Appium teardown still do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from calee_regression.suites import REPO_ROOT

FULL_SOLUTION_REL = "tester/06 Test Full Calee Solution.command"

# The calee_regression subcommands / launcher steps that only ever run when the
# environment is actually prepared. If any of these appear in the order log
# after a failed Prepare, the fail-fast gate has regressed. ("sync-smoke" is the
# cross-device synchronization step, Workstream 1 -- it must be gated behind
# Prepare exactly like the tablet suite and manual checks.)
DOWNSTREAM_TEST_STEPS = ("suite", "record-manual-checks", "sync-smoke", "kiosk-admin")


def _copy_repo(tmp_path: Path) -> Path:
    """A throwaway copy of calee-regression that ensure_environment.sh treats
    as already-bootstrapped: a no-op .venv/bin/activate (so it never tries to
    create a venv or pip-install) and a tester config present."""
    dest = tmp_path / "calee-regression"
    shutil.copytree(
        REPO_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", "reports", ".venv", "__pycache__"),
    )
    (dest / "reports").mkdir(exist_ok=True)
    venv_bin = dest / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "activate").write_text("")  # sourced no-op
    shutil.copyfile(
        dest / "config" / "tester.local.example.yaml",
        dest / "config" / "tester.local.yaml",
    )
    return dest


def _install_fakes(repo: Path, tmp_path: Path) -> Path:
    """Put a fake `python`/`python3` first on PATH and replace
    scripts/test_caleemobile.sh with a recorder, so the launcher can be dry-run
    end to end. Returns the fakebin dir to prepend to PATH."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    shim = (
        "#!/bin/bash\n"
        'if [ "$1" = "-m" ] && [ "$2" = "calee_regression" ]; then\n'
        '    cmd="$3"\n'
        '    printf "%s\\n" "$cmd" >> "$FAKE_ORDER_LOG"\n'
        '    case "$cmd" in\n'
        "        release-platforms)\n"
        '            printf "export RELEASE_PLATFORM_TABLET=%s\\n" "${FAKE_TABLET:-true}"\n'
        '            printf "export RELEASE_PLATFORM_ANDROID=%s\\n" "${FAKE_ANDROID:-true}"\n'
        '            printf "export RELEASE_PLATFORM_IOS=%s\\n" "${FAKE_IOS:-true}"\n'
        '            printf "export RELEASE_FEATURE_SYNCHRONIZATION=%s\\n" "${FAKE_SYNC:-true}"\n'
        '            printf "export RELEASE_FEATURE_KIOSK_ADMIN=%s\\n" "${FAKE_KIOSK:-true}"\n'
        "            exit 0 ;;\n"
        "        prepare)\n"
        '            run_id=""\n'
        "            shift 3\n"
        '            while [ "$#" -gt 0 ]; do\n'
        '                if [ "$1" = "--run-id" ]; then run_id="$2"; fi\n'
        "                shift\n"
        "            done\n"
        '            d="$FAKE_REPO_ROOT/reports/runs/$run_id/environment"\n'
        '            mkdir -p "$d"\n'
        '            printf "%s" "$FAKE_PREPARE_REPORT" > "$d/results.json"\n'
        '            exit "${FAKE_PREPARE_EXIT:-0}" ;;\n'
        "        consolidate)\n"
        '            exit "${FAKE_CONSOLIDATE_EXIT:-3}" ;;\n'
        "        sync-smoke)\n"
        # Record the platform sync was asked to drive so a test can prove the
        # launcher selected the correct in-scope mobile platform (Workstream 1).
        '            plat=""\n'
        "            shift 3\n"
        '            while [ "$#" -gt 0 ]; do\n'
        '                if [ "$1" = "--platform" ]; then plat="$2"; fi\n'
        "                shift\n"
        "            done\n"
        '            printf "sync-smoke:%s\\n" "$plat" >> "$FAKE_ORDER_LOG"\n'
        "            exit 0 ;;\n"
        # The credential wrapper (Priority 5) just execs its delegated command in
        # this dry-run; real credential resolution is exercised by its own tests.
        "        run-with-credentials)\n"
        "            shift 3\n"
        '            if [ "$1" = "--" ]; then shift; fi\n'
        '            exec "$@" ;;\n'
        "        *)\n"
        "            exit 0 ;;\n"
        "    esac\n"
        "fi\n"
        'exec "$REAL_PYTHON" "$@"\n'
    )
    for name in ("python", "python3"):
        p = fakebin / name
        p.write_text(shim)
        p.chmod(0o755)
    # Recorder standing in for the CaleeMobile launcher: records exactly which
    # invocation it received so a test can prove it never ran after a failed
    # Prepare -- and never actually shells out to flutter/adb.
    (repo / "scripts" / "test_caleemobile.sh").write_text(
        "#!/bin/bash\n"
        'printf "caleemobile:%s\\n" "$*" >> "$FAKE_ORDER_LOG"\n'
        "exit 0\n"
    )
    return fakebin


def _run(repo: Path, fakebin: Path, order_log: Path, prepare_report: dict, *, prepare_exit: int,
         android: str = "true", ios: str = "true", sync: str = "true") -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "HOME": str(repo),
        "REAL_PYTHON": sys.executable,
        "FAKE_ORDER_LOG": str(order_log),
        "FAKE_REPO_ROOT": str(repo),
        "FAKE_PREPARE_REPORT": json.dumps(prepare_report),
        "FAKE_PREPARE_EXIT": str(prepare_exit),
        "FAKE_ANDROID": android,
        "FAKE_IOS": ios,
        "FAKE_SYNC": sync,
    }
    return subprocess.run(
        ["bash", str(repo / FULL_SOLUTION_REL)],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=90,
    )


def _order(order_log: Path) -> list[str]:
    if not order_log.exists():
        return []
    return [ln for ln in order_log.read_text().splitlines() if ln]


# --- Fail-fast behavior (BLOCKED / non-ready Prepare) --------------------


def test_blocked_prepare_skips_every_downstream_test(tmp_path):
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {
        "status": "blocked",
        "detail": ["Appium could not be started or reached."],
    }
    result = _run(repo, fakebin, order_log, report, prepare_exit=3)
    steps = _order(order_log)

    # Prepare ran; the safe identity snapshots + consolidation + teardown ran.
    assert "prepare" in steps
    assert "consolidate" in steps
    assert "stop-appium" in steps
    # NO downstream functional test ran.
    for forbidden in DOWNSTREAM_TEST_STEPS:
        assert forbidden not in steps, f"{forbidden!r} ran after a failed Prepare: {steps}"
    assert not any(s.startswith("caleemobile:") for s in steps), steps
    # Consolidation still produced its (BLOCKED) exit code.
    assert result.returncode == 3


def test_blocked_prepare_consolidates_after_prepare_and_before_teardown(tmp_path):
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {"status": "blocked", "detail": ["Fixture verification failed: 42 != 43"]}
    _run(repo, fakebin, order_log, report, prepare_exit=3)
    steps = _order(order_log)

    assert steps.index("prepare") < steps.index("consolidate") < steps.index("stop-appium")


def test_blocked_prepare_prints_run_id_bundle_and_exact_problem(tmp_path):
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {
        "status": "blocked",
        "detail": ["Fixture credentials are not configured (set CALEE_API_BASE, ...)."],
    }
    result = _run(repo, fakebin, order_log, report, prepare_exit=3)

    assert "FAIL FAST" in result.stdout
    # The exact Prepare problem is surfaced verbatim from the environment report.
    assert "Fixture credentials are not configured" in result.stdout
    # Run ID + bundle workspace pointer are printed for the tester.
    assert "Run ID:" in result.stdout
    assert "reports/runs/" in result.stdout
    # The downstream step headers never printed (they live inside the gate).
    assert "Step 2: Calee Tablet" not in result.stdout


def test_any_nonzero_prepare_exit_triggers_fail_fast(tmp_path):
    # Prepare only ever exits 0 (READY) or non-zero (BLOCKED), but the gate must
    # trip for ANY non-zero code, not just the canonical BLOCKED (3).
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {"status": "blocked", "detail": ["Environment is not ready."]}
    _run(repo, fakebin, order_log, report, prepare_exit=2)
    steps = _order(order_log)

    for forbidden in DOWNSTREAM_TEST_STEPS:
        assert forbidden not in steps
    assert not any(s.startswith("caleemobile:") for s in steps)
    assert "consolidate" in steps


def test_blocked_prepare_preserves_environment_report(tmp_path):
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    detail = ["Appium could not be started or reached."]
    report = {"status": "blocked", "detail": detail}
    _run(repo, fakebin, order_log, report, prepare_exit=3)

    # Nothing after Prepare overwrote the environment report it wrote.
    env_reports = list((repo / "reports" / "runs").glob("*/environment/results.json"))
    assert len(env_reports) == 1
    saved = json.loads(env_reports[0].read_text())
    assert saved["status"] == "blocked"
    assert saved["detail"] == detail


def test_blocked_prepare_still_collects_safe_build_identity(tmp_path):
    # The read-only pre/post identity snapshots are safe to collect even when
    # we fail fast, and the consolidated bundle needs BOTH (exactly one is an
    # "incomplete capture" BLOCK). Both build-identity calls must still happen.
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {"status": "blocked", "detail": ["nope"]}
    _run(repo, fakebin, order_log, report, prepare_exit=3)
    steps = _order(order_log)

    assert steps.count("build-identity") == 2  # pre and post


# --- Positive path (READY Prepare still runs everything) -----------------


def test_ready_prepare_runs_every_downstream_step(tmp_path):
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {"status": "pass", "detail": ["Environment and fixture ready."]}
    result = _run(repo, fakebin, order_log, report, prepare_exit=0)
    steps = _order(order_log)

    # The full functional sweep ran, in order, after a ready Prepare.
    assert "suite" in steps  # Calee tablet
    assert "caleemobile:api-only" in steps  # Client API, once
    assert "caleemobile:android --ui-only" in steps
    assert "caleemobile:ios --ui-only" in steps
    # Cross-device synchronization ran (Workstream 1) -- the positive path must
    # prove sync is actually invoked, not just gated behind Prepare.
    assert "sync-smoke" in steps
    # Kiosk/admin ran (Workstream 4) -- its own gating step after sync.
    assert "kiosk-admin" in steps
    assert "record-manual-checks" in steps
    assert "consolidate" in steps
    assert "stop-appium" in steps
    # Client API runs exactly once, and strictly before the platform UI runs.
    assert steps.count("caleemobile:api-only") == 1
    assert steps.index("caleemobile:api-only") < steps.index("caleemobile:android --ui-only")
    # Full ordering: Prepare -> tablet -> API -> Android/iOS UI -> sync ->
    # manual checks -> consolidate. Sync runs AFTER both mobile UI legs and
    # BEFORE manual checks and consolidation.
    assert (
        steps.index("prepare")
        < steps.index("suite")
        < steps.index("caleemobile:api-only")
        < steps.index("caleemobile:android --ui-only")
        < steps.index("caleemobile:ios --ui-only")
        < steps.index("sync-smoke")
        < steps.index("kiosk-admin")
        < steps.index("record-manual-checks")
        < steps.index("consolidate")
    )
    # Sync drove the preferred in-scope mobile platform (Android).
    assert "sync-smoke:android" in steps
    # No fail-fast messaging on the happy path.
    assert "FAIL FAST" not in result.stdout


def test_ready_prepare_honors_release_platform_scope(tmp_path):
    # When a platform is out of release scope its UI step must not run even
    # though Prepare is ready -- the gate is Prepare-readiness AND scope.
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {"status": "pass", "detail": ["ready"]}
    _run(repo, fakebin, order_log, report, prepare_exit=0, ios="false")
    steps = _order(order_log)

    assert "caleemobile:android --ui-only" in steps
    assert "caleemobile:ios --ui-only" not in steps


def test_ready_prepare_sync_uses_remaining_platform_when_android_excluded(tmp_path):
    # A platform-excluded release must still run sync on the correct REMAINING
    # mobile platform: with Android out of scope, sync drives iOS (Workstream 1).
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {"status": "pass", "detail": ["ready"]}
    _run(repo, fakebin, order_log, report, prepare_exit=0, android="false", ios="true")
    steps = _order(order_log)

    assert "sync-smoke" in steps
    assert "sync-smoke:ios" in steps
    assert "sync-smoke:android" not in steps
    # The excluded Android UI leg still didn't run.
    assert "caleemobile:android --ui-only" not in steps


def test_ready_prepare_sync_has_no_platform_when_no_mobile_in_scope(tmp_path):
    # No in-scope CaleeMobile platform (tablet-only mobile scope): a mandatory
    # sync has no mobile surface to verify against. The launcher must invoke
    # sync with --platform none (the sync-smoke command then records BLOCKED --
    # verified in the sync consolidation tests), never silently skip it.
    repo = _copy_repo(tmp_path)
    fakebin = _install_fakes(repo, tmp_path)
    order_log = tmp_path / "order.log"
    report = {"status": "pass", "detail": ["ready"]}
    _run(repo, fakebin, order_log, report, prepare_exit=0, android="false", ios="false")
    steps = _order(order_log)

    assert "sync-smoke" in steps  # not silently skipped
    assert "sync-smoke:none" in steps
    assert "sync-smoke:android" not in steps
    assert "sync-smoke:ios" not in steps


# --- Structural guard (protects the gate against a future refactor) ------


def test_script_gates_downstream_steps_behind_prepare_status(tmp_path):
    text = (REPO_ROOT / FULL_SOLUTION_REL).read_text(encoding="utf-8")
    gate = 'if [ "$PREPARE_STATUS" -eq 0 ]; then'
    assert gate in text
    gate_idx = text.index(gate)
    # Every downstream functional test command must live AFTER the gate opens.
    for needle in (
        'suite --config "$CALEE_TEST_CONFIG" --suite full-tester',
        "bash scripts/test_caleemobile.sh api-only",
        "bash scripts/test_caleemobile.sh android --ui-only",
        "bash scripts/test_caleemobile.sh ios --ui-only",
        "sync-smoke",
        "record-manual-checks --run-id",
    ):
        assert needle in text
        assert text.index(needle) > gate_idx, f"{needle!r} is not gated behind PREPARE_STATUS"
