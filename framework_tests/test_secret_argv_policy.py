"""Tests for the secret-on-argv policy (cli._enforce_secret_argv_policy):
orchestrated commands (sync-smoke) REJECT a password on the command line;
standalone commands (prepare/prepare-fixture) warn-but-work; the environment/
Keychain paths stay silent; and no doc keeps a runnable example that puts a
password on argv. CliRunner only -- no device, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from calee_regression import cli
from calee_regression.cli import main
from calee_regression.models import EXIT_BLOCKED

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET = "argv-secret-value"


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    # Keep any workspace writes under tmp_path (same pattern as
    # test_cli_sync_smoke.py) and out of this checkout.
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "_resolved_report_root", lambda *a, **k: tmp_path)


# ── orchestrated: sync-smoke rejects an argv password ──────────────────────
def test_sync_smoke_rejects_argv_password_without_echoing_it():
    result = CliRunner().invoke(main, [
        "sync-smoke", "--run-id", "release-argv-001", "--base-url", "https://x",
        "--email", "a@x", "--password", SECRET,
    ])
    assert result.exit_code == EXIT_BLOCKED
    assert "BLOCKED" in result.output and "--password" in result.output
    assert "CALEE_TEST_PASSWORD" in result.output  # points at the supported path
    assert SECRET not in result.output
    # rejected before anything ran: no run workspace was created
    # (guard fires before workspace.ensure_created)


def test_sync_smoke_env_password_is_not_rejected():
    # With the password in the environment the argv guard stays silent; the
    # invocation then proceeds to the normal missing-base-url/credentials
    # handling (still BLOCKED here, but for the ordinary reason).
    result = CliRunner().invoke(
        main, ["sync-smoke", "--run-id", "release-argv-002"],
        env={"CALEE_TEST_PASSWORD": SECRET},
    )
    assert "deprecated" not in result.output.lower()
    assert "must not be passed on the command line" not in result.output
    assert SECRET not in result.output


# ── standalone: prepare-fixture warns but works ────────────────────────────
def test_prepare_fixture_argv_password_warns_but_still_works(tmp_path):
    result = CliRunner().invoke(main, [
        "prepare-fixture", "--fixture-password", SECRET, "--run-id", "run-argv-003",
    ])
    assert "--fixture-password on the command line is deprecated" in result.output
    assert "CALEE_TEST_PASSWORD" in result.output
    assert SECRET not in result.output
    # still works: the command proceeded past the guard into the normal flow
    # (BLOCKED on the missing base URL/email, not on the argv password).
    assert "must not be passed" not in result.output
    assert "Run ID: run-argv-003" in result.output


def test_prepare_fixture_env_password_produces_no_warning():
    result = CliRunner().invoke(
        main, ["prepare-fixture", "--run-id", "run-argv-004"],
        env={"CALEE_TEST_PASSWORD": SECRET},
    )
    assert "deprecated" not in result.output.lower()
    assert SECRET not in result.output


# ── docs: no runnable argv-password examples remain ────────────────────────
# A --password/--fixture-password/-p style option followed by a value or
# placeholder, e.g. `--password hunter2`, `--password <...>`, `--password=$X`.
_ARGV_SECRET_RE = re.compile(r"--(?:fixture-)?password(?:=|\s+)\S")


def _code_lines(markdown: str):
    """Every line that is part of a fenced code block, plus every inline
    backtick span -- the 'runnable example' surface of a Markdown doc.
    Prose that merely names the option (e.g. a deprecation explanation)
    is deliberately not scanned."""
    fenced = False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            yield line
        else:
            for span in re.findall(r"`([^`]+)`", line):
                yield span


def test_docs_have_no_argv_password_examples():
    offenders = []
    for doc in sorted((REPO_ROOT / "docs").rglob("*.md")):
        for line in _code_lines(doc.read_text(encoding="utf-8")):
            if _ARGV_SECRET_RE.search(line):
                offenders.append(f"{doc.relative_to(REPO_ROOT)}: {line.strip()}")
    assert offenders == [], (
        "Runnable doc examples must not place a password on argv -- use "
        "CALEE_TEST_PASSWORD / the macOS Keychain instead:\n" + "\n".join(offenders)
    )
