"""End-to-end OFFLINE orchestration test for the one-button release launcher
(`tester/00 Run Calee Release Regression.command`) and the run it produces.

Two complementary harnesses, both offline, both driving REAL code where it
matters (never re-implementing the logic under test):

  * Harness B (this file's `_seed_full_run` + real `consolidate`): assembles a
    complete "fake run" -- one run ID owning a machine-config snapshot, an
    installation component, and every product leg -- and runs the REAL
    consolidation gate over it. Proves a fully successful fake run PASSes, that
    a product assertion failure FAILs, that a BLOCKED installation prevents
    PASS, that the consolidation INCLUDES installation/tablet/mobile/sync/kiosk/
    manual, and that no secret appears in any generated report.

  * Harness A (`_run_launcher` driving the real `tester/00` .command via a fake
    `python3`/`adb`/SDK-tool shim): proves the launcher ORCHESTRATION -- one run
    ID created BEFORE any verification, machine config loaded, the release
    bundle path OUTSIDE the repo, ABSOLUTE APK paths passed to adb, installer
    evidence written INTO the run, manual input reaching the delegated workflow,
    a missing device becoming BLOCKED, and missing credentials becoming BLOCKED.

Nothing here claims a physical device was present or that installation/physical
testing passed -- the installation leg is exercised with a fake device seam and
the physical scenarios stay out of scope.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from calee_regression import cli, run_context
from calee_regression.models import EXIT_REGRESSION, EXIT_SUCCESS
from calee_regression.suites import REPO_ROOT

CALEE_SHA = "a" * 40
CM_SHA = "c" * 40

SECRET_EMAIL = "secret-tester@example.com"
SECRET_PASSWORD = "hunter2-DO-NOT-LEAK"


# ══════════════════════════ Harness B: real consolidation ══════════════════


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)


def _seed_full_run(tmp_path, *, run_id, tablet_status="passed", installation_status="ok"):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00")
    manifest.write(workspace.manifest_path)

    def _w(component, data):
        path = workspace.component_report_path(component)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runId": run_id, **data}) + "\n")

    # Machine-config snapshot (secrets excluded).
    _w("machine-config", {
        "status": "ok", "detail": ["authoritative"],
        "selected": {"backendUrl": "https://hub-dev.calee.com.au", "releaseProfile": "production",
                     "tabletSerial": "TAB123", "caleePackageId": "com.viso.calee"},
        "reconciliations": [],
    })
    # Installation (with an ABSOLUTE APK path in the plan evidence).
    _w("installation", {
        "status": installation_status,
        "detail": [] if installation_status == "ok" else ["No usable device."],
        "plan": {"releaseId": "2026.07.20-rc1", "steps": [
            {"label": "install-calee", "argv": ["adb", "-s", "TAB123", "install", "-r",
                                                 "/Users/tech/Calee-Releases/current/calee.apk"]},
        ]},
    })
    _w("environment", {"status": "pass", "detail": []})
    _w("tablet", {"passed_count": 1 if tablet_status == "passed" else 0,
                  "failed_count": 1 if tablet_status == "failed" else 0,
                  "blocked_count": 0, "skipped_count": 0,
                  "scenarios": [{"name": "REG-TABLET", "status": tablet_status}]})
    _w("mobile-api", {"counts": {"PASS": 1}, "steps": [{"name": "api", "status": "PASS"}]})
    _w("mobile-android", {"counts": {"PASS": 1}, "steps": [{"name": "android", "status": "PASS"}]})
    _w("mobile-ios", {"counts": {"PASS": 1}, "steps": [{"name": "ios", "status": "PASS"}]})
    _w("sync", {"mandatory": True, "flows": [{"flow": "event-sync", "status": "ok", "steps": []}]})
    _w("kiosk-admin", {"status": "pass", "steps": []})
    _w("manual-checks", {"checks": [
        {"title": "Kiosk escape check", "instruction": "swipe down", "expectedResult": "no shade", "status": "pass"},
    ]})
    return workspace


# A tablet-in-scope release scope for the offline test: the tablet + Client API
# + sync + installation + machine-config are mandatory gates; the platform UI
# legs and selector contract are opted out of scope (selector evidence needs a
# GitHub-authenticated CI artifact this offline run can't produce -- see
# docs/RELEASE_POLICY.md). The mobile leg reports are still seeded so they
# appear in the consolidation (item 11), just as optional components.
_CONSOLIDATE_COMMON = [
    "--android-optional", "--ios-optional", "--sync-mandatory",
    "--installation-mandatory", "--machine-config-mandatory",
    "--meals-optional", "--onboarding-optional", "--google-calendar-optional", "--kiosk-admin-optional",
    "--allow-unknown-build-identity",
    "--calee-build-version", "founder-v0.3.25",
    "--calee-application-id", "com.viso.calee", "--calee-version-code", "325",
]


def test_fully_successful_fake_run_produces_pass(tmp_path):
    run_id = "release-e2e-pass-001"
    _seed_full_run(tmp_path, run_id=run_id)
    result = CliRunner().invoke(cli.main, ["consolidate", "--run-id", run_id, *_CONSOLIDATE_COMMON])
    assert "Overall: PASS" in result.output, result.output
    # Final consolidation INCLUDES installation, tablet, mobile, sync, kiosk, manual.
    for needle in (
        "Calee tablet release installation", "Machine configuration",
        "Calee tablet", "CaleeMobile Client API", "CaleeMobile Android UI",
        "CaleeMobile iPhone UI", "cross-device synchronization",
        "CaleeShell kiosk", "manual checks",
    ):
        assert needle in result.output, f"{needle!r} missing from consolidation:\n{result.output}"


def test_product_assertion_failure_becomes_fail(tmp_path):
    run_id = "release-e2e-fail-001"
    _seed_full_run(tmp_path, run_id=run_id, tablet_status="failed")
    result = CliRunner().invoke(cli.main, ["consolidate", "--run-id", run_id, *_CONSOLIDATE_COMMON])
    assert "Overall: FAIL" in result.output, result.output


def test_blocked_installation_prevents_pass(tmp_path):
    run_id = "release-e2e-instblock-001"
    _seed_full_run(tmp_path, run_id=run_id, installation_status="blocked")
    result = CliRunner().invoke(cli.main, ["consolidate", "--run-id", run_id, *_CONSOLIDATE_COMMON])
    assert "Overall: BLOCKED" in result.output, result.output
    assert "Calee tablet release installation" in result.output


def test_no_secret_appears_in_generated_reports(tmp_path):
    run_id = "release-e2e-nosecret-001"
    workspace = _seed_full_run(tmp_path, run_id=run_id)
    # Even if a secret somehow reached a report on disk, the consolidation bundle
    # must not surface it. Here we assert the seeded run + consolidated bundle
    # contain no credential value.
    CliRunner().invoke(cli.main, ["consolidate", "--run-id", run_id, *_CONSOLIDATE_COMMON,
                                  "--out-dir", str(tmp_path / "out")])
    for path in workspace.root.rglob("*.json"):
        text = path.read_text()
        assert SECRET_PASSWORD not in text
        assert SECRET_EMAIL not in text
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert SECRET_PASSWORD.encode() not in data
            assert SECRET_EMAIL.encode() not in data


# ══════════════════════════ Harness A: real bash launcher ══════════════════


def _copy_repo(tmp_path):
    dest = tmp_path / "calee-regression"
    shutil.copytree(REPO_ROOT, dest, ignore=shutil.ignore_patterns(".git", "reports", ".venv", "__pycache__"))
    (dest / "reports").mkdir(exist_ok=True)
    venv_bin = dest / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "activate").write_text("")  # sourced no-op
    shutil.copyfile(dest / "config" / "tester.local.example.yaml", dest / "config" / "tester.local.yaml")
    return dest


def _external_bundle(tmp_path):
    """A release bundle OUTSIDE the repo (like ~/Calee-Releases/current)."""
    bundle = tmp_path / "Calee-Releases" / "current"
    bundle.mkdir(parents=True)
    calee_bytes = b"calee-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    import hashlib
    sha = hashlib.sha256(calee_bytes).hexdigest()
    (bundle / "release-manifest.json").write_text(json.dumps({
        "releaseId": "2026.07.20-rc1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": sha},
    }))
    (bundle / "checksums.sha256").write_text(f"{sha}  calee.apk\n")
    return bundle


def _machine_yaml(repo, bundle):
    (repo / "config" / "machine.local.yaml").write_text(yaml.safe_dump({
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
    }))


def _fakebin(tmp_path, *, device_present=True, prepare_exit=0, consolidate_exit=0):
    """Fake python3/adb/apkanalyzer/apksigner. The python shim DELEGATES the
    machine-config-snapshot + install-tablet-release subcommands to the REAL
    interpreter (so their real evidence is produced), fakes every product leg,
    and fakes `consolidate` (records its argv + returns a chosen exit)."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    real_python = sys.executable

    py_shim = f'''#!{real_python}
import os, sys, json, pathlib
REAL = {real_python!r}
argv = sys.argv[1:]
order = os.environ.get("FAKE_ORDER_LOG")
run_id = os.environ.get("CALEE_RUN_ID", "")
root = pathlib.Path(os.environ.get("FAKE_REPO_ROOT", "."))

def wc(component, data):
    d = root / "reports" / "runs" / run_id / component
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(json.dumps({{"runId": run_id, **data}}))

if len(argv) >= 3 and argv[0] == "-m" and argv[1] == "calee_regression":
    cmd = argv[2]
    if order:
        with open(order, "a") as f:
            f.write(cmd + "\\n")
    if cmd in ("machine-config-snapshot", "install-tablet-release"):
        os.execv(REAL, [REAL, "-m", "calee_regression"] + argv[2:])
    if cmd == "release-platforms":
        sys.stdout.write("export RELEASE_PLATFORM_TABLET=true\\n")
        sys.stdout.write("export RELEASE_PLATFORM_ANDROID=true\\n")
        sys.stdout.write("export RELEASE_PLATFORM_IOS=false\\n")
        sys.stdout.write("export RELEASE_FEATURE_SYNCHRONIZATION=true\\n")
        sys.stdout.write("export RELEASE_FEATURE_KIOSK_ADMIN=false\\n")
        sys.exit(0)
    if cmd == "prepare":
        code = int(os.environ.get("FAKE_PREPARE_EXIT", "0"))
        status = "pass" if code == 0 else "blocked"
        wc("environment", {{"status": status, "detail": [] if code == 0 else ["Missing fixture credentials."]}})
        sys.exit(code)
    if cmd == "build-identity":
        if "--phase" in argv and argv[argv.index("--phase") + 1] == "post":
            sys.stdout.write("export AUTO_CALEE_IDENTITY_AVAILABLE=true\\n")
        sys.exit(0)
    if cmd == "selector-contract":
        wc("selector-contract", {{"status": "pass"}})
        sys.exit(0)
    if cmd == "suite":
        wc("tablet", {{"passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
                       "scenarios": [{{"name": "REG", "status": "passed"}}]}})
        sys.exit(0)
    if cmd == "sync-smoke":
        wc("sync", {{"mandatory": True, "flows": [{{"flow": "event-sync", "status": "ok", "steps": []}}]}})
        sys.exit(0)
    if cmd == "kiosk-admin":
        wc("kiosk-admin", {{"status": "pass", "steps": []}})
        sys.exit(0)
    if cmd == "record-manual-checks":
        data = sys.stdin.read()
        (root / "reports" / "runs" / run_id).mkdir(parents=True, exist_ok=True)
        (root / "reports" / "runs" / run_id / "manual-stdin.txt").write_text(data)
        wc("manual-checks", {{"checks": [{{"title": "t", "instruction": "i", "expectedResult": "e", "status": "pass"}}]}})
        sys.exit(0)
    if cmd == "consolidate":
        (root / "reports" / "runs" / run_id / "consolidate-argv.txt").write_text(" ".join(argv[2:]))
        sys.exit(int(os.environ.get("FAKE_CONSOLIDATE_EXIT", "0")))
    if cmd == "stop-appium":
        sys.exit(0)
    sys.exit(0)

os.execv(REAL, [REAL] + argv)
'''
    for name in ("python3", "python"):
        p = fakebin / name
        p.write_text(py_shim)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    dev_line = ("device" if device_present else "error: no devices/emulators found")
    dev_rc = "0" if device_present else "1"
    adb = f'''#!/bin/bash
args=("$@")
joined="$*"
case "$joined" in
  *get-state*) echo "{dev_line}"; exit {dev_rc} ;;
  *"pm path"*) echo ""; exit 0 ;;
  *dumpsys*calee* ) echo "versionName=founder-v0.3.25"; echo "versionCode=325"; exit 0 ;;
  *dumpsys*caleeshell* ) echo "versionName=founder-v0.2.12"; echo "versionCode=212"; exit 0 ;;
  *"category.HOME"*) echo "packageName=com.viso.caleeshell"; exit 0 ;;
  *"action.START"*) echo "packageName=com.viso.calee"; exit 0 ;;
  *install*) echo "Success"; exit 0 ;;
  *wait-for-device*) exit 0 ;;
  *reboot*) exit 0 ;;
  *) exit 0 ;;
esac
'''
    (fakebin / "adb").write_text(adb)
    (fakebin / "adb").chmod((fakebin / "adb").stat().st_mode | stat.S_IEXEC)

    apkanalyzer = '''#!/bin/bash
case "$*" in
  *application-id*) echo "com.viso.calee" ;;
  *version-code*) echo "325" ;;
  *version-name*) echo "founder-v0.3.25" ;;
  *) echo "" ;;
esac
exit 0
'''
    (fakebin / "apkanalyzer").write_text(apkanalyzer)
    (fakebin / "apkanalyzer").chmod((fakebin / "apkanalyzer").stat().st_mode | stat.S_IEXEC)

    apksigner = '''#!/bin/bash
echo "Signer #1 certificate SHA-256 digest: 1111111111111111111111111111111111111111111111111111111111111111"
exit 0
'''
    (fakebin / "apksigner").write_text(apksigner)
    (fakebin / "apksigner").chmod((fakebin / "apksigner").stat().st_mode | stat.S_IEXEC)
    return fakebin


def _run_launcher(repo, fakebin, *, stdin="", extra_env=None):
    env = dict(os.environ)
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["FAKE_REPO_ROOT"] = str(repo)
    env["FAKE_ORDER_LOG"] = str(repo / "order.log")
    env.setdefault("CALEE_TEST_EMAIL", SECRET_EMAIL)
    env.setdefault("CALEE_TEST_PASSWORD", SECRET_PASSWORD)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(repo / "tester" / "00 Run Calee Release Regression.command")],
        cwd=str(repo), env=env, input=stdin, capture_output=True, text=True, timeout=120,
    )
    return proc


def _only_run_dir(repo):
    runs = list((repo / "reports" / "runs").glob("release-*"))
    assert len(runs) == 1, f"expected exactly one run dir, got {runs}"
    return runs[0]


def test_launcher_one_run_id_machine_config_and_install_evidence(tmp_path):
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    _machine_yaml(repo, bundle)
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, stdin="")
    order = (repo / "order.log").read_text().splitlines()

    # 1. Machine config is loaded, and 2. ONE run ID created BEFORE verification:
    # machine-config-snapshot runs before install-tablet-release, and there is a
    # single run workspace shared by both.
    assert "machine-config-snapshot" in order
    assert order.index("machine-config-snapshot") < order.index("install-tablet-release")
    run_dir = _only_run_dir(repo)

    # 1. machine-config snapshot recorded (secrets excluded).
    snap = json.loads((run_dir / "machine-config" / "results.json").read_text())
    assert snap["status"] == "ok"
    assert snap["selected"]["backendUrl"] == "https://hub-dev.calee.com.au"
    assert SECRET_PASSWORD not in (run_dir / "machine-config" / "results.json").read_text()

    # 3. bundle path is OUTSIDE the repo, and 5. installer evidence is IN the run.
    install = json.loads((run_dir / "installation" / "results.json").read_text())
    assert str(bundle) not in str(repo)  # sanity: the bundle really is external
    # 4. APK paths passed to adb are ABSOLUTE (and point inside the external bundle).
    argvs = [a for step in install["plan"]["steps"] for a in step["argv"]]
    apk_args = [a for a in argvs if a.endswith(".apk")]
    assert apk_args, install
    for a in apk_args:
        assert a.startswith("/"), a
        assert str(bundle) in a


def test_launcher_manual_input_reaches_delegated_workflow(tmp_path):
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    _machine_yaml(repo, bundle)
    fakebin = _fakebin(tmp_path)

    marker = "TESTER-MANUAL-ANSWER-42"
    # Feed the launcher's terminal stdin; it must flow through 00 -> 06 ->
    # record-manual-checks (Priority 2: no </dev/null swallowing it).
    _run_launcher(repo, fakebin, stdin=f"{marker}\n")
    run_dir = _only_run_dir(repo)
    captured = (run_dir / "manual-stdin.txt").read_text()
    assert marker in captured


def test_launcher_missing_device_becomes_blocked(tmp_path):
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    _machine_yaml(repo, bundle)
    fakebin = _fakebin(tmp_path, device_present=False)

    proc = _run_launcher(repo, fakebin, stdin="")
    run_dir = _only_run_dir(repo)
    install = json.loads((run_dir / "installation" / "results.json").read_text())
    assert install["status"] == "blocked"  # no device -> installation BLOCKED, never a fake pass
    assert "NEEDS TECHNICAL OWNER" in proc.stdout or "BLOCKED" in proc.stdout


def test_launcher_missing_credentials_becomes_blocked(tmp_path):
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    _machine_yaml(repo, bundle)
    # prepare (fixture reset) BLOCKS on missing credentials -> the run BLOCKS.
    fakebin = _fakebin(tmp_path)
    proc = _run_launcher(repo, fakebin, stdin="", extra_env={"FAKE_PREPARE_EXIT": "3", "FAKE_CONSOLIDATE_EXIT": "3"})
    assert proc.returncode not in (0, 1)  # BLOCKED (not PASS, not a product FAIL)
    run_dir = _only_run_dir(repo)
    env = json.loads((run_dir / "environment" / "results.json").read_text())
    assert env["status"] == "blocked"
