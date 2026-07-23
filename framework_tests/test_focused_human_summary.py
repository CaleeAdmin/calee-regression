"""Tests for the plain-language focused summary (Phase 8,
focused_human_summary.py): consistency with the machine summary, whitelisted-
field-only rendering (no secret leakage), no release-readiness claim, real
pastable next commands (no angle brackets), and immutable writing.
"""

from __future__ import annotations

import stat

import pytest

from calee_regression import focused_human_summary as fhs

SECRET = "s3cret-keychain-pw"


def make_summary(**overrides):
    summary = {
        "reportType": "focused-verify-summary",
        "reportSchemaVersion": 2,
        "runId": "run-20260723-101500-abc123",
        "invocationId": "inv-20260723T101500-000001",
        "status": "blocked",
        "verifiedBackend": "https://staging.calee.invalid",
        "fixtureVersion": "REG-9",
        "regressionShas": {"calee-regression": "aa" * 20, "caleemobile-regression": "bb" * 20},
        "productBuild": {"caleeMobileSha": "c0ffee00" * 5},
        "deviceIds": {"tablet": "TABLET-1", "ios": "IPHONE-1"},
        "installedArtifactIdentity": {"status": "unproven", "reason": "no APK configured"},
        "steps": [
            {"id": "fixture", "title": "Fixture preparation", "status": "pass",
             "reportPath": "/runs/x/environment/results.json", "evidence": "executed"},
            {"id": "tablet-standard", "title": "Standard tablet scenario", "status": "fail",
             "mode": "standard", "detail": "recurring event not shown",
             "reportPath": "/runs/x/tablet-targeted/standard/results.json"},
            {"id": "tablet-diagnostic", "title": "Diagnostic tablet scenario", "status": "blocked",
             "mode": "diagnostic", "blockedBy": "appium",
             "detail": "Appium endpoint unavailable -- step not started."},
            {"id": "api-1", "title": "API attempt 1", "status": "invalid_config",
             "detail": "child exited 2"},
            {"id": "ios", "title": "iPhone environment check", "status": "blocked_not_run",
             "blockedBy": "fixture", "detail": "prerequisite step 'fixture' did not pass"},
        ],
    }
    summary.update(overrides)
    return summary


def test_every_step_appears_and_counts_agree():
    summary = make_summary()
    text = fhs.render(summary)
    for step in summary["steps"]:
        assert step["title"] in text
    # each status lands in exactly its section
    assert "What passed?" in text
    assert "What failed?" in text
    assert "What was blocked?" in text
    assert "What did not run?" in text
    passed_section = text.split("What passed?")[1].split("What failed?")[0]
    assert "Fixture preparation" in passed_section
    failed_section = text.split("What failed?")[1].split("What was blocked?")[0]
    assert "Standard tablet scenario" in failed_section
    assert "recurring event not shown" in failed_section
    not_run_section = text.split("What did not run?")[1]
    assert "iPhone environment check" in not_run_section


def test_blockers_carry_reason_codes_and_distinguish_kinds():
    text = fhs.render(make_summary())
    assert "reason code: appium (framework/tooling blocker)" in text
    assert "invalid invocation/configuration" in text
    assert "reason code: fixture" in text
    # product failures are labelled as such, apart from framework blockers
    assert "product failures" in text
    assert "NOT product failures" in text


def test_identity_section_is_complete():
    text = fhs.render(make_summary())
    assert "https://staging.calee.invalid" in text
    assert "REG-9" in text
    assert "TABLET-1" in text and "IPHONE-1" in text
    assert "aa" * 20 in text and "bb" * 20 in text
    assert "c0ffee00" * 5 in text
    assert "unproven" in text and "no APK configured" in text


def test_exact_report_paths_are_preserved():
    text = fhs.render(make_summary())
    assert "/runs/x/environment/results.json" in text
    assert "/runs/x/tablet-targeted/standard/results.json" in text


def test_never_claims_release_readiness():
    for status in ("pass", "fail", "blocked"):
        text = fhs.render(make_summary(status=status))
        assert fhs.NON_CERTIFICATION_STATEMENT in text
        assert "DIAGNOSTIC ONLY" in text
        assert "still needs certification" in text.lower()


def test_unexpected_credential_adjacent_keys_never_leak():
    # render uses explicit whitelisted field access only: a secret smuggled
    # into unexpected keys (top-level or per-step) must never appear.
    summary = make_summary(password=SECRET, credentialValue=SECRET)
    summary["steps"][0]["password"] = SECRET
    summary["credentialSource"] = {"regression_password": SECRET}
    text = fhs.render(summary)
    assert SECRET not in text
    assert "password" not in text.lower()


def test_next_command_on_pass_is_pastable_with_real_run_id():
    summary = make_summary(status="pass")
    text = fhs.render(summary)
    command, _explanation = fhs.next_command(summary)
    assert command in text
    assert "release-remediation-plan" in command
    assert "--focused-run run-20260723-101500-abc123" in command
    assert "RELEASE_RUN_ID" in command
    assert "replacing the literal token RELEASE_RUN_ID" in text
    assert "<" not in command and ">" not in command


def test_next_command_on_blockers_suggests_preflight():
    summary = make_summary(status="blocked")
    text = fhs.render(summary)
    command, _explanation = fhs.next_command(summary)
    assert command in text
    assert "--preflight-only" in command
    assert "<" not in command and ">" not in command


def test_write_is_immutable(tmp_path):
    path = tmp_path / "summary.txt"
    fhs.write(make_summary(), path)
    assert path.read_text() == fhs.render(make_summary())
    assert not (path.stat().st_mode & stat.S_IWUSR)  # read-only on disk
    with pytest.raises(FileExistsError):
        fhs.write(make_summary(), path)
    # original content untouched
    assert path.read_text() == fhs.render(make_summary())
