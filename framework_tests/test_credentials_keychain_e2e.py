"""Priority 5 -- Keychain-only credential flow across the whole run.

A true end-to-end proof, with NO CALEE_TEST_EMAIL / CALEE_TEST_PASSWORD in the
parent environment: a fake macOS Keychain (a fake ``security`` binary on PATH)
returns them, ``run-with-credentials`` resolves them once, and they reach the
delegated process AND its grandchild (standing in for the Bash mobile
orchestration -> Prepare / CaleeMobile API / UI / sync receivers), while never
appearing in any argv, stdout, stderr, or on-disk report. The credential
absence is NOT simulated by a forced exit code -- real resolution runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET_EMAIL = "regression-keychain@example.com"
SECRET_PASSWORD = "Keychain-Only-P@ssw0rd-77"


def _fake_security(bin_dir: Path):
    """A fake `security find-generic-password -s <svc> -a <account> -w`."""
    security = bin_dir / "security"
    security.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "acct = argv[argv.index('-a') + 1] if '-a' in argv else ''\n"
        f"vals = {{'regression-username': {SECRET_EMAIL!r}, 'regression-password': {SECRET_PASSWORD!r}}}\n"
        "if acct in vals:\n"
        "    sys.stdout.write(vals[acct] + '\\n'); sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    security.chmod(0o755)


def _receiver(path: Path, out: Path, grandchild_out: Path):
    """A delegated command that records the creds it received in its env + its
    argv, then spawns a grandchild that re-checks the env (proving inheritance
    down the process tree the Bash orchestration would create)."""
    path.write_text(
        "import os, sys, json, subprocess\n"
        "json.dump({\n"
        "    'email': os.environ.get('CALEE_TEST_EMAIL'),\n"
        "    'password': os.environ.get('CALEE_TEST_PASSWORD'),\n"
        "    'argv': sys.argv,\n"
        f"}}, open({str(out)!r}, 'w'))\n"
        "# grandchild inherits the same environment (bash -> python child chain)\n"
        "gc = 'import os,json;json.dump({\\'email\\':os.environ.get(\\'CALEE_TEST_EMAIL\\'),"
        "\\'password\\':os.environ.get(\\'CALEE_TEST_PASSWORD\\')},open(%r,\\'w\\'))' % " f"{str(grandchild_out)!r}\n"
        "subprocess.run([sys.executable, '-c', gc], check=True)\n"
    )


def test_keychain_only_full_fake_run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_security(bin_dir)

    received = tmp_path / "received.json"
    grandchild = tmp_path / "grandchild.json"
    receiver = tmp_path / "receiver.py"
    _receiver(receiver, received, grandchild)

    # 1. No credentials in the parent environment.
    env = {k: v for k, v in os.environ.items() if k not in ("CALEE_TEST_EMAIL", "CALEE_TEST_PASSWORD")}
    # 2. Fake Keychain reachable on PATH.
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    marker = "delegated-marker-arg"
    proc = subprocess.run(
        [sys.executable, "-m", "calee_regression", "run-with-credentials", "--",
         sys.executable, str(receiver), marker],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    # 3-6. The delegated command AND its grandchild received the Keychain creds.
    got = json.loads(received.read_text())
    assert got["email"] == SECRET_EMAIL
    assert got["password"] == SECRET_PASSWORD
    gc = json.loads(grandchild.read_text())
    assert gc["email"] == SECRET_EMAIL and gc["password"] == SECRET_PASSWORD

    # 7. No command argv contains the credentials.
    assert marker in got["argv"]
    assert SECRET_EMAIL not in " ".join(got["argv"])
    assert SECRET_PASSWORD not in " ".join(got["argv"])

    # 8. No log/stdout/stderr contains the credentials.
    assert SECRET_EMAIL not in proc.stdout and SECRET_EMAIL not in proc.stderr
    assert SECRET_PASSWORD not in proc.stdout and SECRET_PASSWORD not in proc.stderr
    # Nothing produced by the wrapper itself left the secret on disk. The only
    # files that legitimately contain it are the test's fake Keychain source
    # (`security`) and the receiver's own evidence files -- everything else (a
    # real report/log/temp file) must be clean.
    legit = {"received.json", "grandchild.json", "security"}
    for leftover in tmp_path.rglob("*"):
        if leftover.is_file() and leftover.name not in legit:
            text = leftover.read_text(errors="ignore")
            assert SECRET_PASSWORD not in text, f"secret leaked into {leftover}"


def test_run_with_credentials_blocks_when_no_source_has_them(tmp_path):
    # No env creds and no Keychain -> BLOCKED (never a silent empty run). This is
    # real resolution failing, not a forced exit code.
    env = {k: v for k, v in os.environ.items() if k not in ("CALEE_TEST_EMAIL", "CALEE_TEST_PASSWORD")}
    # A PATH with no `security` at all.
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    env["PATH"] = str(empty_bin)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "calee_regression", "run-with-credentials", "--", sys.executable, "-c", "print('ran')"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 3, proc.stdout  # EXIT_BLOCKED
    assert "ran" not in proc.stdout  # the delegated command never ran
    assert "could not be resolved" in proc.stderr
