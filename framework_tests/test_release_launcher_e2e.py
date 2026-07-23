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
from tablet_fixtures import TABLET_CERTIFYING_ENVELOPE as _TABLET_CERTIFYING_ENVELOPE
from calee_regression.models import EXIT_REGRESSION, EXIT_SUCCESS
from calee_regression.suites import REPO_ROOT

CALEE_SHA = "a" * 40
CM_SHA = "c" * 40
CALEESHELL_SHA = "b" * 40
# The digest the fake `apksigner` script in _fakebin() reports for ANY app
# (Priority 2: verify_tablet_solution now requires a matching signerSha256 for
# BOTH Calee and CaleeShell whenever a run-id is present -- every launcher run
# always carries one, so this must be declared and correct for a clean PASS).
FAKE_SIGNER_SHA256 = "1" * 64

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
    _w("tablet", {**_TABLET_CERTIFYING_ENVELOPE, "passed_count": 1 if tablet_status == "passed" else 0,
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
    """A release bundle OUTSIDE the repo (like ~/Calee-Releases/current). Calee
    is installed this release; CaleeShell is unchanged but -- like every real
    complete-solution manifest -- still declares its expected installed
    identity (Priority 2: BOTH apps' identity, including a trusted signer, is
    always required for verify_tablet_solution to resolve OK, whether or not
    this release touches that app)."""
    bundle = tmp_path / "Calee-Releases" / "current"
    bundle.mkdir(parents=True)
    calee_bytes = b"calee-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    import hashlib
    sha = hashlib.sha256(calee_bytes).hexdigest()
    (bundle / "release-manifest.json").write_text(json.dumps({
        "releaseId": "2026.07.20-rc1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": sha,
                  "signerSha256": FAKE_SIGNER_SHA256},
        "caleeShell": {"installArtifact": False, "expectedInstalled": {
            "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
            "versionCode": 212, "gitSha": CALEESHELL_SHA, "signerSha256": FAKE_SIGNER_SHA256,
        }},
    }))
    (bundle / "checksums.sha256").write_text(f"{sha}  calee.apk\n")
    return bundle


def _machine_yaml(repo, bundle, *, report_dir="."):
    # A release-platforms.yaml narrowed to exactly what this fake machine can
    # provide (android + tablet, no iOS device, no kiosk/technical
    # authorisation) -- without it, release-config's default "every platform
    # and feature is required" scope conflicts with this machine (missing iOS,
    # unauthorised kiosk_admin) and BLOCKS before Prepare ever runs (Priority
    # 1: this is now a hard gate, so an inconsistent fixture like the old
    # "release_profile: production" below -- with no matching
    # expected_build_identity.production -- would stop these tests dead
    # instead of silently being ignored).
    (repo / "config" / "release-platforms.yaml").write_text(yaml.safe_dump({
        "release_platforms": {"tablet": True, "mobile_android": True, "mobile_ios": False},
        "release_features": {
            "synchronization": True, "meals": True, "onboarding": True,
            "google_calendar": True, "kiosk_admin": False,
        },
    }))
    (repo / "config" / "machine.local.yaml").write_text(yaml.safe_dump({
        "tablet_serial": "TAB123",
        "expected_tablet_state": "logged_in_tablet",
        "calee_package_id": "com.viso.calee",
        "caleeshell_package_id": "com.viso.caleeshell",
        "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
        "calee_launch_action": "com.viso.calee.action.START",
        "release_bundle_dir": str(bundle),
        "backend_url": "https://hub-dev.calee.com.au",
        "release_profile": "staging",
        "report_dir": report_dir,
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
import os, sys, json, pathlib, zipfile
REAL = {real_python!r}
argv = sys.argv[1:]
order = os.environ.get("FAKE_ORDER_LOG")
run_id = os.environ.get("CALEE_RUN_ID", "")
# Priority 3: honour an already-resolved CALEE_REPORT_ROOT (as the real "00"
# launcher exports after delegating "report-root" to the real interpreter
# above) so this fake's OWN writes land in the same place as the real,
# delegated components' evidence -- falling back to FAKE_REPO_ROOT only if
# report-root was never resolved (matches this fixture's pre-Priority-3
# behaviour exactly when no custom root is configured).
root = pathlib.Path(os.environ.get("CALEE_REPORT_ROOT") or os.environ.get("FAKE_REPO_ROOT", "."))

def wc(component, data):
    d = root / "reports" / "runs" / run_id / component
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(json.dumps({{"runId": run_id, **data}}))

if len(argv) >= 3 and argv[0] == "-m" and argv[1] == "calee_regression":
    cmd = argv[2]
    if order:
        with open(order, "a") as f:
            f.write(cmd + "\\n")
    if cmd in (
        "machine-config-snapshot", "install-tablet-release", "run-with-credentials", "report-root",
        "verify-release-bundle", "release-config",
    ):
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
        # Priority 2 (this session): controllable via FAKE_SELECTOR_EXIT so
        # tests can exercise mandatory/optional PASS/FAIL launcher branching
        # without a real CaleeMobile checkout.
        exit_code = int(os.environ.get("FAKE_SELECTOR_EXIT", "0"))
        wc("selector-contract", {{"status": "pass" if exit_code == 0 else "blocked"}})
        sys.exit(exit_code)
    if cmd == "suite":
        wc("tablet", {{"reportType": "tablet-scenario-suite", "reportSchemaVersion": 1,
                       "deviceInitializationMode": "standard", "diagnosticMode": False,
                       "certificationEligible": True,
                       "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
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
        exit_code = int(os.environ.get("FAKE_CONSOLIDATE_EXIT", "0"))
        status_word = {{0: "PASS", 1: "FAIL"}}.get(exit_code, "BLOCKED")
        # A minimal but real consolidated bundle (JSON/HTML/JUnit + ZIP) and
        # latest-run pointer -- Priority 3 requires proving these land under
        # the SAME resolved report root as every component report, which a
        # bare argv-recording fake could not demonstrate. Real consolidation
        # CORRECTNESS (exact JSON shape, gating rules, ...) is exercised
        # elsewhere (Harness B in this file, test_cli_consolidate.py,
        # test_selector_contract_gate.py); this only proves root placement.
        consolidated_dir = root / "reports" / "runs" / run_id / "consolidated"
        consolidated_dir.mkdir(parents=True, exist_ok=True)
        (consolidated_dir / "consolidated-report.json").write_text(
            json.dumps({{"runId": run_id, "overallStatus": status_word}})
        )
        (consolidated_dir / "consolidated-report.html").write_text(
            f"<html><body>{{status_word}} ({{run_id}})</body></html>"
        )
        (consolidated_dir / "consolidated-report.junit.xml").write_text(
            '<?xml version="1.0"?><testsuite name="calee-regression"></testsuite>'
        )
        zip_path = consolidated_dir / f"Calee-Regression-fake-{{run_id}}-{{status_word}}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in ("consolidated-report.json", "consolidated-report.html", "consolidated-report.junit.xml"):
                zf.write(consolidated_dir / name, arcname=name)
        latest_link = root / "reports" / "latest-run"
        if latest_link.is_symlink() or latest_link.exists():
            try:
                latest_link.unlink()
            except OSError:
                pass
        try:
            latest_link.symlink_to(pathlib.Path("runs") / run_id, target_is_directory=True)
        except OSError:
            pass
        sys.exit(exit_code)
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
# Priority 1, requirement 10: every invocation of this fake adb -- mutating or
# not -- is logged when FAKE_ADB_LOG is set, so a test can prove NO adb command
# of any kind ran before release-config succeeded (install-tablet-release,
# the only step that ever invokes adb, must never even start).
if [ -n "${{FAKE_ADB_LOG:-}}" ]; then
    echo "$joined" >> "$FAKE_ADB_LOG"
fi
case "$joined" in
  *get-state*) echo "{dev_line}"; exit {dev_rc} ;;
  # Priority 2: device_installed_signer_reader needs a real "package:<path>"
  # line to proceed to the (faked) pull + apksigner read -- an empty pm path
  # reads as SIGNER_NOT_INSTALLED, which used to be masked by defect #2 never
  # letting the launcher reach this far. Not package-specific: the reader
  # doesn't cross-check the returned path against the requested package id.
  *"pm path"*) echo "package:/data/app/fake/base.apk"; exit 0 ;;
  # "caleeshell" contains "calee" as a substring, so the more specific pattern
  # MUST be checked first -- otherwise every caleeShell dumpsys query would
  # match the calee pattern above it and report Calee's version/code instead.
  *dumpsys*caleeshell* ) echo "versionName=founder-v0.2.12"; echo "versionCode=212"; exit 0 ;;
  *dumpsys*calee* ) echo "versionName=founder-v0.3.25"; echo "versionCode=325"; exit 0 ;;
  *"category.HOME"*) echo "packageName=com.viso.caleeshell"; exit 0 ;;
  *"action.START"*) echo "packageName=com.viso.calee"; exit 0 ;;
  *install*) echo "Success"; exit 0 ;;
  *wait-for-device*) exit 0 ;;
  # release_installer.execute_install_plan classifies every non-"verify" step
  # (reboot included) through classify_install_output, which requires
  # "success" in the output to resolve OK -- matching test_release_installer.py's
  # own FakeAdb, whose default for an unmatched command is Success text.
  # Without this, the real (delegated-to) execute_install_plan halts at
  # "reboot" as OUTCOME_INSTALL_FAILED and BLOCKS the whole install, which
  # Priority 1's fail-fast fix (defect #2) now correctly stops the launcher
  # on -- so this fake must actually simulate the reboot succeeding.
  *reboot*) echo "Success"; exit 0 ;;
  *) echo "Success"; exit 0 ;;
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
    # Priority 1, requirement 10: every fake-adb invocation this run makes is
    # logged here (see _fakebin's adb script) so a test can prove none at all
    # happened before release-config succeeded.
    env["FAKE_ADB_LOG"] = str(repo / "adb.log")
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
    # 4. APK paths passed to adb are ABSOLUTE. Once release-config has frozen
    # this run's approved release candidate into an immutable snapshot
    # (release_candidate.py), install-tablet-release installs ONLY from that
    # run-scoped snapshot -- never the original external drop folder, even
    # though that's what --bundle still points at -- closing the TOCTOU gap
    # between approval and installation.
    argvs = [a for step in install["plan"]["steps"] for a in step["argv"]]
    apk_args = [a for a in argvs if a.endswith(".apk")]
    assert apk_args, install
    # release-candidate is a symlink pointer (Priority 4 crash-recoverable
    # publication -- see atomic_publish.py) into a content-addressed version
    # directory; the resolved APK path lives under the pointer's real target.
    for a in apk_args:
        assert a.startswith("/"), a
        assert Path(a).is_relative_to((run_dir / "release-candidate").resolve())
    assert install.get("releaseCandidateFingerprint") is not None, install


def test_launcher_verifies_bundle_and_composes_release_config_before_installing(tmp_path):
    """Priority 1's required order: machine-config-snapshot -> verify-release-
    bundle -> release-config -> install-tablet-release."""
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    _machine_yaml(repo, bundle)
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, stdin="")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    order = (repo / "order.log").read_text().splitlines()

    for cmd in ("machine-config-snapshot", "verify-release-bundle", "release-config", "install-tablet-release"):
        assert cmd in order, f"{cmd!r} missing from order: {order}"
    assert order.index("machine-config-snapshot") < order.index("verify-release-bundle")
    assert order.index("verify-release-bundle") < order.index("release-config")
    assert order.index("release-config") < order.index("install-tablet-release")

    # release-config evidence recorded for this run, folding in the verified
    # bundle manifest (Priority 1 requirement 5).
    run_dir = _only_run_dir(repo)
    release_cfg = json.loads((run_dir / "release-config" / "results.json").read_text())
    assert release_cfg["status"] == "ok"
    assert release_cfg["releaseId"] == "2026.07.20-rc1"

    # "06" (delegated to by "00") CONSUMES the same evidence rather than
    # recomputing -- its own release-config invocation (order.log records
    # BOTH "00"'s and "06"'s calls when both actually run the real command)
    # must not have overwritten it with a different run/composition.
    assert order.count("release-config") >= 1


def _machine_yaml_with_conflict(repo, bundle, *, report_dir="."):
    """Like _machine_yaml, but the machine is NOT capable of a platform the
    release candidate requires (no iPhone device, yet release-platforms.yaml
    requires mobile_ios) -- release-config must BLOCK on this."""
    (repo / "config" / "release-platforms.yaml").write_text(yaml.safe_dump({
        "release_platforms": {"tablet": True, "mobile_android": True, "mobile_ios": True},
        "release_features": {
            "synchronization": True, "meals": True, "onboarding": True,
            "google_calendar": True, "kiosk_admin": False,
        },
    }))
    (repo / "config" / "machine.local.yaml").write_text(yaml.safe_dump({
        "tablet_serial": "TAB123",
        "expected_tablet_state": "logged_in_tablet",
        "calee_package_id": "com.viso.calee",
        "caleeshell_package_id": "com.viso.caleeshell",
        "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
        "calee_launch_action": "com.viso.calee.action.START",
        "release_bundle_dir": str(bundle),
        "backend_url": "https://hub-dev.calee.com.au",
        "release_profile": "staging",
        "report_dir": report_dir,
        "mobile_platforms": ["android"],  # no ios -- conflicts with release-platforms.yaml above
    }))


def test_launcher_release_config_conflict_blocks_before_any_adb_command(tmp_path):
    """Priority 1, requirement 10: when release-config BLOCKS (a machine/
    release conflict here), NO mutating -- or indeed ANY -- adb command may
    have occurred, install-tablet-release must never even start, and no
    product test may run. Still produces ONE consolidated BLOCKED report."""
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    _machine_yaml_with_conflict(repo, bundle)
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, stdin="", extra_env={"FAKE_CONSOLIDATE_EXIT": "3"})
    order = (repo / "order.log").read_text().splitlines()

    # release-config was attempted and blocked...
    assert "verify-release-bundle" in order
    assert "release-config" in order
    # ...but installation, and every product test, never ran.
    assert "install-tablet-release" not in order
    assert "prepare" not in order and "suite" not in order
    assert "sync-smoke" not in order and "kiosk-admin" not in order
    assert "record-manual-checks" not in order

    # No adb command of any kind ran -- the log is either empty or absent.
    adb_log = repo / "adb.log"
    adb_calls = adb_log.read_text().strip() if adb_log.is_file() else ""
    assert adb_calls == "", f"adb was invoked before release-config succeeded: {adb_calls!r}"

    # No installation evidence was ever written (nothing to mutate a device
    # for was ever attempted) -- consolidate must record it as not-run,
    # never fabricate a result.
    run_dir = _only_run_dir(repo)
    assert not (run_dir / "installation" / "results.json").is_file()

    # release-config's own evidence records the blocking conflict.
    release_cfg = json.loads((run_dir / "release-config" / "results.json").read_text())
    assert release_cfg["status"] == "blocked"
    assert any(c["blocking"] and c["field"] == "platform:ios" for c in release_cfg["conflicts"])

    # Still ONE consolidated BLOCKED report -- the run never exits silently.
    assert proc.returncode == 3
    assert "consolidate" in order


def test_launcher_invalid_bundle_blocks_before_release_config_and_any_adb_command(tmp_path):
    """An invalid bundle stops at bundle verification -- release-config is
    never even attempted, and no adb command runs."""
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    manifest = json.loads((bundle / "release-manifest.json").read_text())
    manifest["calee"]["sha256"] = "0" * 64  # corrupt checksum
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    _machine_yaml(repo, bundle)
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, stdin="", extra_env={"FAKE_CONSOLIDATE_EXIT": "3"})
    order = (repo / "order.log").read_text().splitlines()

    assert "verify-release-bundle" in order
    assert "release-config" not in order
    assert "install-tablet-release" not in order

    adb_log = repo / "adb.log"
    adb_calls = adb_log.read_text().strip() if adb_log.is_file() else ""
    assert adb_calls == "", f"adb was invoked before bundle verification passed: {adb_calls!r}"
    assert proc.returncode == 3


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


def test_launcher_invalid_bundle_still_consolidates(tmp_path):
    """Priority 7: an invalid bundle stops before the product tests, but the
    launcher STILL consolidates -- it never exits without producing one report.

    Priority 1 moved bundle verification to its own standalone step BEFORE
    release-config/installation, so a corrupted bundle is now caught there --
    install-tablet-release (and release-config) is never even attempted; see
    test_launcher_invalid_bundle_blocks_before_release_config_and_any_adb_command
    for the "no adb at all" proof. This test focuses on Priority 7's "still
    consolidates" guarantee."""
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    # Corrupt the manifest checksum so verify_release_bundle fails.
    manifest = json.loads((bundle / "release-manifest.json").read_text())
    manifest["calee"]["sha256"] = "0" * 64
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    _machine_yaml(repo, bundle)
    fakebin = _fakebin(tmp_path)

    # The fake consolidate reports BLOCKED (exit 3); the launcher must exit with it.
    proc = _run_launcher(repo, fakebin, stdin="", extra_env={"FAKE_CONSOLIDATE_EXIT": "3"})
    order = (repo / "order.log").read_text().splitlines()

    # verify-release-bundle ran (real) and caught the corruption; the launcher
    # STILL consolidated afterwards...
    assert "verify-release-bundle" in order
    assert "consolidate" in order
    assert order.index("verify-release-bundle") < order.index("consolidate")
    # ...but installation/release-config/product tests never ran.
    assert "install-tablet-release" not in order
    assert "release-config" not in order
    assert "suite" not in order and "sync-smoke" not in order and "kiosk-admin" not in order
    # No installation evidence was ever written -- nothing was attempted.
    assert not (_only_run_dir(repo) / "installation" / "results.json").is_file()
    assert proc.returncode == 3
    assert proc.returncode == 3


def test_custom_report_root_controls_every_artifact_through_the_real_launcher(tmp_path):
    """Priority 3: a configured machine report_dir must control the ENTIRE
    run, not just ReportBuilder/EffectiveReleaseConfig in isolation. Drives
    the real "00" launcher end to end (through the fake orchestration) with
    report_dir pointed at an external directory, and proves every component
    report, the run manifest, the consolidated bundle (JSON/HTML/JUnit), the
    evidence ZIP, and the latest-run pointer all land under it -- while the
    repo's own default reports/ directory is never touched at all."""
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle(tmp_path)
    custom_root = tmp_path / "custom-calee-reports"
    _machine_yaml(repo, bundle, report_dir=str(custom_root))
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, stdin="")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # The repo's own default reports/ directory must never be used for
    # anything run-scoped -- not even an empty "runs/" placeholder.
    default_runs_dir = repo / "reports" / "runs"
    assert not default_runs_dir.exists(), (
        f"the repo's default reports/runs was used despite a configured custom "
        f"report root: {list(default_runs_dir.rglob('*'))}"
    )

    # Exactly one run workspace, under the CUSTOM root.
    custom_runs = list((custom_root / "reports" / "runs").glob("release-*"))
    assert len(custom_runs) == 1, f"expected exactly one run dir under the custom root, got {custom_runs}"
    run_dir = custom_runs[0]

    # Every component this run produced is under the custom root. (This
    # harness's fake python3 shim doesn't delegate "release-config" to the
    # real interpreter -- see _fakebin() -- so it's not a component here;
    # release-config's own report-root placement is proven directly in
    # test_full_solution_fail_fast.py, which does delegate it. There's no
    # fake CaleeMobile-Regression sibling in this harness either, so
    # scripts/test_caleemobile.sh BLOCKS before mobile-api/mobile-android
    # ever get a results.json -- unrelated to report-root placement, which
    # is what this test is about.)
    for component in (
        "machine-config", "installation", "environment",
        "tablet", "sync", "kiosk-admin", "manual-checks", "selector-contract",
    ):
        report = run_dir / component / "results.json"
        assert report.is_file(), f"{component} results.json missing under the custom root: {report}"

    # The run manifest is under the custom root.
    assert (run_dir / "run-manifest.json").is_file()

    # The consolidated report (JSON/HTML/JUnit) and evidence ZIP are under
    # the custom root.
    consolidated_dir = run_dir / "consolidated"
    assert (consolidated_dir / "consolidated-report.json").is_file()
    assert (consolidated_dir / "consolidated-report.html").is_file()
    assert (consolidated_dir / "consolidated-report.junit.xml").is_file()
    zips = list(consolidated_dir.glob("*.zip"))
    assert len(zips) == 1, f"expected exactly one evidence ZIP under the custom root, got {zips}"

    # latest-run lives under the custom root and points into this same run.
    latest_link = custom_root / "reports" / "latest-run"
    assert latest_link.is_symlink(), "latest-run must be a symlink under the custom root"
    assert latest_link.resolve() == run_dir.resolve()
    assert (latest_link / "consolidated" / "consolidated-report.html").is_file()

    # "07 Open Latest Report" resolves and opens the SAME report -- it must
    # never silently fall back to the repo's own reports/ when a custom root
    # is configured.
    open_report = subprocess.run(
        ["bash", str(repo / "tester" / "07 Open Latest Report.command")],
        cwd=str(repo),
        env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}", "CALEE_REPORT_ROOT": str(custom_root)},
        input="", capture_output=True, text=True, timeout=30,
    )
    assert str(custom_root) in open_report.stdout, open_report.stdout + open_report.stderr


# ═══════════ Priority 2 (this session): selector mandatory/optional policy ═══
#
# Launcher "06" reads $RELEASE_SELECTOR_EVIDENCE_REQUIRED (emitted by
# release-config from calee_regression/release_config.py's
# resolve_selector_evidence_required) to decide --mandatory/--optional on the
# selector-contract gate, and to decide which mobile legs to skip on failure.
# These use a schema-v2 bundle so the manifest's own caleeMobile.
# selectorEvidenceRequired opinion is what's actually being exercised.


def _external_bundle_v2(tmp_path, *, selector_evidence_required=True, profile="staging"):
    bundle = tmp_path / "Calee-Releases" / "current-v2"
    bundle.mkdir(parents=True)
    calee_bytes = b"calee-apk-bytes-v2"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    import hashlib
    sha = hashlib.sha256(calee_bytes).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "releaseId": "2026.07.21-rcv2",
        "profile": profile,
        "backend": "https://hub.calee.com.au" if profile == "production" else "https://hub-dev.calee.com.au",
        "platforms": {"tablet": True, "mobileAndroid": True, "mobileIos": False},
        "features": {
            "synchronization": True, "meals": True, "onboarding": True,
            "googleCalendar": True, "kioskAdmin": False, "notifications": True,
        },
        "tabletSolution": {
            "calee": {
                "installArtifact": True, "apk": "calee.apk", "sha256": sha,
                "expectedInstalled": {
                    "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                    "versionCode": 325, "gitSha": CALEE_SHA, "signerSha256": FAKE_SIGNER_SHA256,
                },
            },
            "caleeShell": {
                "installArtifact": False,
                "expectedInstalled": {
                    "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                    "versionCode": 212, "gitSha": CALEESHELL_SHA, "signerSha256": FAKE_SIGNER_SHA256,
                },
            },
        },
        "caleeMobile": {
            "version": "0.0.24+24", "gitSha": CM_SHA,
            "selectorEvidenceRequired": selector_evidence_required,
            "distributedBuildAcceptanceRequired": True,
        },
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(f"{sha}  calee.apk\n")
    return bundle


def _machine_yaml_v2(repo, bundle, *, profile="staging", report_dir="."):
    # Schema v2 is self-contained/authoritative for scope -- no release-
    # platforms.yaml needed (and none is written here, proving it).
    (repo / "config" / "machine.local.yaml").write_text(yaml.safe_dump({
        "tablet_serial": "TAB123",
        "expected_tablet_state": "logged_in_tablet",
        "calee_package_id": "com.viso.calee",
        "caleeshell_package_id": "com.viso.caleeshell",
        "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
        "calee_launch_action": "com.viso.calee.action.START",
        "release_bundle_dir": str(bundle),
        "backend_url": "https://hub.calee.com.au" if profile == "production" else "https://hub-dev.calee.com.au",
        "release_profile": profile,
        "report_dir": report_dir,
        "mobile_platforms": ["android"],
    }))


def _credential_invocation_count(repo) -> int:
    order_log = repo / "order.log"
    if not order_log.is_file():
        return 0
    return order_log.read_text().count("run-with-credentials")


def test_mandatory_selector_pass_runs_mobile_legs_and_sync(tmp_path):
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle_v2(tmp_path, selector_evidence_required=True, profile="staging")
    _machine_yaml_v2(repo, bundle, profile="staging")
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, extra_env={"FAKE_SELECTOR_EXIT": "0"})
    run_dir = _only_run_dir(repo)
    assert json.loads((run_dir / "selector-contract" / "results.json").read_text())["status"] == "pass"
    assert (run_dir / "sync" / "results.json").is_file(), "sync-smoke must run after a passing selector gate"
    assert _credential_invocation_count(repo) >= 1, "the API leg (at least) must run after a passing selector gate: " + proc.stdout + proc.stderr


def test_mandatory_selector_failure_skips_every_mobile_leg(tmp_path):
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle_v2(tmp_path, selector_evidence_required=True, profile="staging")
    _machine_yaml_v2(repo, bundle, profile="staging")
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, extra_env={"FAKE_SELECTOR_EXIT": "3"})
    run_dir = _only_run_dir(repo)
    assert json.loads((run_dir / "selector-contract" / "results.json").read_text())["status"] == "blocked"
    assert not (run_dir / "sync" / "results.json").is_file(), "sync-smoke must not run after a failed mandatory selector gate"
    assert _credential_invocation_count(repo) == 0, (
        "no mobile leg (API or UI) may run after a failed MANDATORY selector gate: " + proc.stdout + proc.stderr
    )


def test_optional_selector_pass_runs_mobile_legs_and_sync(tmp_path):
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle_v2(tmp_path, selector_evidence_required=False, profile="staging")
    _machine_yaml_v2(repo, bundle, profile="staging")
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, extra_env={"FAKE_SELECTOR_EXIT": "0"})
    run_dir = _only_run_dir(repo)
    assert json.loads((run_dir / "selector-contract" / "results.json").read_text())["status"] == "pass"
    assert (run_dir / "sync" / "results.json").is_file()
    assert _credential_invocation_count(repo) >= 1, proc.stdout + proc.stderr


def test_optional_selector_failure_runs_api_but_skips_ui_and_sync(tmp_path):
    # Priority 2, requirement 5: a selector-OPTIONAL failure may allow the
    # device-independent API check to continue, but every selector-dependent
    # leg (Android/iOS UI, and sync -- which drives that same UI) must be
    # skipped.
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle_v2(tmp_path, selector_evidence_required=False, profile="staging")
    _machine_yaml_v2(repo, bundle, profile="staging")
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, extra_env={"FAKE_SELECTOR_EXIT": "3"})
    run_dir = _only_run_dir(repo)
    assert json.loads((run_dir / "selector-contract" / "results.json").read_text())["status"] == "blocked"
    assert not (run_dir / "sync" / "results.json").is_file(), (
        "cross-device sync drives mobile UI -- it must not run when selector evidence was not verified"
    )
    # Exactly one credential-boundary invocation: the API leg only (never the
    # Android/iOS UI legs, which depend on unverified selectors).
    assert _credential_invocation_count(repo) == 1, proc.stdout + proc.stderr


def test_production_profile_forces_selector_mandatory_despite_manifest_optional(tmp_path):
    # Priority 2, requirement (precedence): a PRODUCTION release with a
    # mobile platform in scope is unconditionally mandatory, regardless of
    # the manifest's own selectorEvidenceRequired: false.
    repo = _copy_repo(tmp_path)
    bundle = _external_bundle_v2(tmp_path, selector_evidence_required=False, profile="production")
    _machine_yaml_v2(repo, bundle, profile="production")
    fakebin = _fakebin(tmp_path)

    proc = _run_launcher(repo, fakebin, extra_env={"FAKE_SELECTOR_EXIT": "3"})
    run_dir = _only_run_dir(repo)
    assert json.loads((run_dir / "selector-contract" / "results.json").read_text())["status"] == "blocked"
    assert not (run_dir / "sync" / "results.json").is_file()
    assert _credential_invocation_count(repo) == 0, (
        "a production release must treat selector evidence as mandatory even though the manifest said "
        "selectorEvidenceRequired: false: " + proc.stdout + proc.stderr
    )
