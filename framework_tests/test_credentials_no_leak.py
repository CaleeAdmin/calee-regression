"""Adversarial credential-leak tests (Priority 3).

Secrets (email/password) must NEVER appear on a subprocess argv, in an
exception string, in captured child output, or in a report. The bridges pass
credentials through the child ENVIRONMENT (never argv) and redact any child
output that could echo them back. These tests use fake sibling scripts that
deliberately try to leak, and prove the leak is closed.
"""

from __future__ import annotations

import json

import pytest

from calee_regression import credentials
from calee_regression.fixture_bridge import FixtureBridgeError, run_fixture_action
from calee_regression.sync_smoke_bridge import (
    SyncSmokeBridgeError,
    create_scratch_event,
    run_mobile_flow,
)

SECRET_EMAIL = "secret-user@example.com"
SECRET_PASSWORD = "hunter2-TOP-SECRET-VALUE"


def _sibling(tmp_path, rel_path: str, body: str):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    target = tmp_path / "CaleeMobile-Regression" / rel_path
    target.parent.mkdir(parents=True)
    target.write_text(body)
    return repo_root


# ── API sync-smoke actions ───────────────────────────────────────────────

# Records its own argv + the credential env vars it received into the report.
_RECORDING_API = """
import json, os, sys
args = sys.argv[1:]
report = args[args.index("--report") + 1]
json.dump({
    "found": True, "id": "evt_1", "title": "t",
    "_argv": sys.argv,
    "_env_email": os.environ.get("CALEE_TEST_EMAIL"),
    "_env_password": os.environ.get("CALEE_TEST_PASSWORD"),
}, open(report, "w"))
sys.exit(0)
"""

# A hostile/buggy child that echoes the received password to stderr then fails.
_ECHOING_API = """
import os, sys
print("could not log in as user with password " + (os.environ.get("CALEE_TEST_PASSWORD") or ""), file=sys.stderr)
sys.exit(3)
"""


def test_api_bridge_passes_secrets_via_env_not_argv(tmp_path):
    repo_root = _sibling(tmp_path, "api/sync_smoke_actions.py", _RECORDING_API)
    result = create_scratch_event(
        repo_root=repo_root, base_url="https://x",
        email=SECRET_EMAIL, password=SECRET_PASSWORD, title="REG-T",
    )
    argv = result["_argv"]
    # The command argument array carries NO secret and no --email/--password.
    assert SECRET_EMAIL not in argv
    assert SECRET_PASSWORD not in argv
    assert "--email" not in argv
    assert "--password" not in argv
    # The secrets were delivered through the environment instead.
    assert result["_env_email"] == SECRET_EMAIL
    assert result["_env_password"] == SECRET_PASSWORD


def test_api_bridge_redacts_secret_echoed_into_exception(tmp_path):
    repo_root = _sibling(tmp_path, "api/sync_smoke_actions.py", _ECHOING_API)
    with pytest.raises(SyncSmokeBridgeError) as exc_info:
        create_scratch_event(
            repo_root=repo_root, base_url="https://x",
            email=SECRET_EMAIL, password=SECRET_PASSWORD, title="REG-T",
        )
    msg = str(exc_info.value)
    assert SECRET_PASSWORD not in msg
    assert credentials._REDACTED in msg


# ── Fixture reset/verify ─────────────────────────────────────────────────

# Prints only a boolean env marker (never the secret itself) + its argv.
_RECORDING_FIXTURE = """
import os, sys
both = bool(os.environ.get("CALEE_TEST_EMAIL")) and bool(os.environ.get("CALEE_TEST_PASSWORD"))
print("ARGV=" + repr(sys.argv))
print("ENV_CREDS_PRESENT=" + ("yes" if both else "no"))
sys.exit(0)
"""

_ECHOING_FIXTURE = """
import os, sys
print("reset failed for password " + (os.environ.get("CALEE_TEST_PASSWORD") or ""), file=sys.stderr)
sys.exit(1)
"""


def test_fixture_bridge_passes_secrets_via_env_not_argv(tmp_path):
    repo_root = _sibling(tmp_path, "api/manage_fixture.py", _RECORDING_FIXTURE)
    out = run_fixture_action(
        "reset", repo_root=repo_root, base_url="https://x",
        email=SECRET_EMAIL, password=SECRET_PASSWORD,
    )
    # No secret and no secret-bearing flag ever reached the child argv.
    assert "--email" not in out
    assert "--password" not in out
    assert SECRET_EMAIL not in out
    assert SECRET_PASSWORD not in out
    # But the credentials WERE delivered via the environment.
    assert "ENV_CREDS_PRESENT=yes" in out


def test_fixture_bridge_redacts_secret_echoed_into_exception(tmp_path):
    repo_root = _sibling(tmp_path, "api/manage_fixture.py", _ECHOING_FIXTURE)
    with pytest.raises(FixtureBridgeError) as exc_info:
        run_fixture_action(
            "reset", repo_root=repo_root, base_url="https://x",
            email=SECRET_EMAIL, password=SECRET_PASSWORD,
        )
    msg = str(exc_info.value)
    assert SECRET_PASSWORD not in msg
    assert credentials._REDACTED in msg


# ── Mobile Flutter flow execution ────────────────────────────────────────

_RECORDING_UI = """
import json, os, sys
args = sys.argv[1:]
report = args[args.index("--report") + 1]
json.dump({
    "argv": sys.argv,
    "env_email": os.environ.get("CALEE_TEST_EMAIL"),
    "env_password": os.environ.get("CALEE_TEST_PASSWORD"),
}, open(report, "w"))
sys.exit(0)
"""


def test_mobile_flow_passes_secrets_via_env_not_argv(tmp_path):
    repo_root = _sibling(tmp_path, "ui/run_ui_suite.py", _RECORDING_UI)
    report_dir = tmp_path / "reports"
    ok = run_mobile_flow(
        repo_root=repo_root, target="integration_test/flows/sync_task_complete_test.dart",
        platform="android", email=SECRET_EMAIL, password=SECRET_PASSWORD, report_dir=report_dir,
    )
    assert ok is True
    written = json.loads((report_dir / "sync_task_complete_test-results.json").read_text())
    assert SECRET_EMAIL not in written["argv"]
    assert SECRET_PASSWORD not in written["argv"]
    assert "--email" not in written["argv"]
    assert "--password" not in written["argv"]
    assert written["env_email"] == SECRET_EMAIL
    assert written["env_password"] == SECRET_PASSWORD


# ── Report JSON redaction (the credentials.redact contract) ───────────────


def test_report_json_redaction_scrubs_every_secret_value():
    # A serialized report that (adversarially) contains a leaked secret is fully
    # scrubbed before being written -- the pattern the sync-smoke CLI applies to
    # results.json via credentials.redact(..., resolver.secret_values()).
    leaked = json.dumps({
        "runId": "release-x",
        "flows": [{"detail": f"login failed for {SECRET_EMAIL} / {SECRET_PASSWORD}"}],
    })
    scrubbed = credentials.redact(leaked, {SECRET_EMAIL, SECRET_PASSWORD})
    assert SECRET_EMAIL not in scrubbed
    assert SECRET_PASSWORD not in scrubbed
    assert credentials._REDACTED in scrubbed
    # Still valid JSON after redaction.
    assert json.loads(scrubbed)["runId"] == "release-x"


def test_resolver_repr_never_contains_a_secret():
    resolver = credentials.default_resolver(injected={
        "regression_username": SECRET_EMAIL, "regression_password": SECRET_PASSWORD,
    })
    resolver.require(credentials.REGRESSION_PASSWORD)
    assert SECRET_PASSWORD not in repr(resolver)
    assert SECRET_EMAIL not in repr(resolver)
