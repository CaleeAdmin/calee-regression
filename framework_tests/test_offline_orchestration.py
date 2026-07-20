"""Priority 9 -- offline orchestration tests that span the launcher/bridge
layer, complementing the per-priority tests:

  * #5  the configured iPhone device id reaches run_ui_suite.py (--device-id
        and CALEE_UI_DEVICE_ID), via scripts/test_caleemobile.sh;
  * #7  a custom report root is honoured by the reporting + effective-config
        layers;
  * a credential-leak sweep across command arrays, child environments, reports,
        manifests and ZIP contents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_CALEEMOBILE = REPO_ROOT / "scripts" / "test_caleemobile.sh"


# ── #5: iPhone device id reaches run_ui_suite ──────────────────────────────


def test_iphone_device_id_reaches_run_ui_suite(tmp_path):
    # A minimal calee-regression + sibling layout so test_caleemobile.sh reaches
    # the UI leg: a pre-seeded, verified, same-run environment report (so Prepare
    # is skipped), a fake `flutter`, and a fake sibling run_ui_suite.py that
    # records the argv + CALEE_UI_DEVICE_ID it was invoked with.
    repo = tmp_path / "calee-regression"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "test_caleemobile.sh").write_text(TEST_CALEEMOBILE.read_text())

    run_id = "release-20260720-101010-uidev1"
    env_dir = repo / "reports" / "runs" / run_id / "environment"
    env_dir.mkdir(parents=True)
    (env_dir / "results.json").write_text(json.dumps({
        "runId": run_id, "fixtureVerificationStatus": "ok",
        "targetEnvironment": "https://hub-staging.calee.com.au",
    }))

    sibling = tmp_path / "CaleeMobile-Regression"
    (sibling / "api").mkdir(parents=True)
    (sibling / "ui").mkdir(parents=True)
    recorded = tmp_path / "run_ui_suite_invocation.json"
    (sibling / "ui" / "run_ui_suite.py").write_text(
        "import sys, os, json\n"
        f"json.dump({{'argv': sys.argv, 'uiDeviceId': os.environ.get('CALEE_UI_DEVICE_ID')}}, open({str(recorded)!r}, 'w'))\n"
    )

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "flutter").write_text("#!/bin/bash\nexit 0\n")  # pub get / presence check
    (fakebin / "flutter").chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["CALEE_RUN_ID"] = run_id
    # Priority 3: simulate report root already resolved+exported by an
    # orchestrating "06" (the realistic case for this delegated script) --
    # this test's `repo` is an isolated scratch copy, not the actual
    # installed package location `python3 -m calee_regression report-root`
    # would resolve to, so it must be supplied rather than re-derived.
    env["CALEE_REPORT_ROOT"] = str(repo)
    env["CALEE_IPHONE_DEVICE"] = "00008110-CONFIGURED-IPHONE"
    env["CALEE_TEST_EMAIL"] = "reg@example.com"
    env["CALEE_TEST_PASSWORD"] = "pw"
    # Skip the release-platforms python call (feature scope already set).
    for feat in ("MEALS", "ONBOARDING", "GOOGLE_CALENDAR", "KIOSK_ADMIN"):
        env[f"CALEE_RELEASE_FEATURE_{feat}"] = "true"

    proc = subprocess.run(
        ["bash", str(repo / "scripts" / "test_caleemobile.sh"), "ios", "--ui-only"],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=60,
    )
    assert recorded.is_file(), f"run_ui_suite.py was not invoked.\nstdout={proc.stdout}\nstderr={proc.stderr}"
    got = json.loads(recorded.read_text())
    # The configured iPhone reached the UI suite both ways.
    assert "--device-id" in got["argv"]
    assert got["argv"][got["argv"].index("--device-id") + 1] == "00008110-CONFIGURED-IPHONE"
    assert got["uiDeviceId"] == "00008110-CONFIGURED-IPHONE"
    assert "--platform" in got["argv"] and got["argv"][got["argv"].index("--platform") + 1] == "ios"


# ── #7: custom report root is honoured ─────────────────────────────────────


def _config(report_dir):
    from calee_regression.config import Config
    return Config(
        appium_url="http://x", device_name="d", udid="emulator-5554", apk_path="/tmp/a.apk",
        app_package="com.viso.calee", app_activity=".Home", shell_package="com.viso.caleeshell",
        shell_activity=".Launcher", launch_strategy="direct_activity", start_action="com.viso.calee.action.START",
        report_dir=str(report_dir),
    )


def test_custom_report_root_is_honoured_by_reporting(tmp_path):
    from calee_regression import reporting
    from calee_regression.models import SuiteResult
    custom = tmp_path / "my-custom-reports"
    rb = reporting.ReportBuilder(_config(custom), run_name="calee-smoke")
    # The report directory lives under the configured custom root.
    assert str(rb.dir).startswith(str(custom))
    out = rb.write(SuiteResult(name="calee-smoke"))
    assert Path(out).is_file() or Path(out).is_dir()
    assert str(Path(out).resolve()).startswith(str(custom.resolve()))


def test_custom_report_root_reaches_effective_release_config():
    from calee_regression import release_config as rc
    from calee_regression.machine_config import MachineConfig
    from calee_regression.release_platforms import ExpectedBuildIdentity, ReleaseFeatures, ReleasePlatforms
    machine = MachineConfig(
        tablet_serial="TAB1", expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee", caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir="~/x", backend_url="https://hub-staging.calee.com.au",
        release_profile="staging", report_dir="/Volumes/Evidence/calee-reports",
        mobile_platforms=["android", "ios"], iphone_device="IPH", android_device="AND",
        allow_caleeshell_technical=True,
    )
    cfg = rc.compose_effective_release_config(
        machine, ReleasePlatforms(), ReleaseFeatures(), ExpectedBuildIdentity(),
    )
    assert cfg.report_root == "/Volumes/Evidence/calee-reports"
    assert cfg.to_dict()["machineSelections"]["reportRoot"] == "/Volumes/Evidence/calee-reports"


# ── credential-leak sweep across arrays / env / reports / manifest / zip ────


def test_keychain_only_run_leaks_no_secret_across_any_artifact(tmp_path):
    """A Keychain-only credential-wrapped run must leave no secret in a delegated
    command's argv, its child environment, or any file it produced. This sweeps
    the whole delegated process tree's output (the launcher output analogue)."""
    email = "sweep-user@example.com"
    password = "Sweep-P@ssw0rd-ZZ9"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "security").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "acct = sys.argv[sys.argv.index('-a') + 1] if '-a' in sys.argv else ''\n"
        f"v = {{'regression-username': {email!r}, 'regression-password': {password!r}}}\n"
        "sys.stdout.write(v[acct] + '\\n') if acct in v else sys.exit(1)\n"
    )
    (bin_dir / "security").chmod(0o755)

    # A delegated command that writes a "report", a "manifest", and a "command
    # array" record, then a ZIP -- exactly the artifact kinds a real run makes.
    outdir = tmp_path / "artifacts"
    outdir.mkdir()
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import os, sys, json, zipfile, pathlib\n"
        f"out = pathlib.Path({str(outdir)!r})\n"
        "email = os.environ.get('CALEE_TEST_EMAIL'); pw = os.environ.get('CALEE_TEST_PASSWORD')\n"
        "assert email and pw, 'creds not in child env'\n"
        "# a report/manifest that must NOT echo the secret (redacts if it did)\n"
        "(out/'report.json').write_text(json.dumps({'status':'ok','user':email,'auth':'***'}))\n"
        "(out/'manifest.json').write_text(json.dumps({'runId':'r','commands':[['adb','install','-r','/x.apk']]}))\n"
        "z = zipfile.ZipFile(out/'evidence.zip','w'); z.write(out/'report.json','report.json'); z.close()\n"
        "(out/'argv.json').write_text(json.dumps({'argv': sys.argv}))\n"
    )

    env = {k: v for k, v in os.environ.items() if k not in ("CALEE_TEST_EMAIL", "CALEE_TEST_PASSWORD")}
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-m", "calee_regression", "run-with-credentials", "--",
         sys.executable, str(worker), "--flag", "value"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    # Launcher output (stdout/stderr) is clean.
    assert email not in proc.stdout and email not in proc.stderr
    assert password not in proc.stdout and password not in proc.stderr

    # Command arrays (argv.json) never carry the secret...
    argv = json.loads((outdir / "argv.json").read_text())["argv"]
    assert password not in " ".join(argv) and email not in " ".join(argv)
    assert "--flag" in argv and "value" in argv  # the real args did pass through

    # ...and neither do the report, manifest, or ZIP contents.
    for name in ("report.json", "manifest.json"):
        text = (outdir / name).read_text()
        assert password not in text, f"secret leaked into {name}"
    import zipfile
    with zipfile.ZipFile(outdir / "evidence.zip") as z:
        for entry in z.namelist():
            assert password.encode() not in z.read(entry), f"secret leaked into zip:{entry}"
