"""Priority 7 -- subscribed-fixture as a first-class run component.

Covers: registration in run_context.COMPONENT_NAMES, component_from_
subscribed_fixture_report's PASS/BLOCKED/NOT_RUN mapping, consolidate CLI
integration (auto-discovery, wrong-run/stale rejection via the same
run_context.validate_component_report machinery every other component uses),
and the promotion-driven mandatory-when-promoted behavior (Priority 11 items
"wrong-run fixture evidence rejection").
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import cli, run_context
from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_NOT_RUN,
    STATUS_PASS,
    component_from_subscribed_fixture_report,
)
from calee_regression.models import EXIT_SUCCESS


def test_subscribed_fixture_is_a_registered_component():
    assert "subscribed-fixture" in run_context.COMPONENT_NAMES


def test_component_from_report_none_is_not_run():
    c = component_from_subscribed_fixture_report("Subscribed-calendar fixture", None)
    assert c.status == STATUS_NOT_RUN
    assert c.mandatory is False  # default


def test_component_from_report_ok_status_is_pass():
    c = component_from_subscribed_fixture_report("x", {"status": "ok", "detail": ["published+observed"]}, mandatory=True)
    assert c.status == STATUS_PASS
    assert c.mandatory is True


def test_component_from_report_blocked_status_is_blocked():
    c = component_from_subscribed_fixture_report("x", {"status": "blocked", "detail": ["timeout"]})
    assert c.status == STATUS_BLOCKED


def test_component_from_report_unrecognized_status_is_blocked_never_silent_pass():
    c = component_from_subscribed_fixture_report("x", {"status": "something-weird"})
    assert c.status == STATUS_BLOCKED
    assert any("Unrecognized" in d for d in c.detail)


# ── CLI integration: auto-discovery + optional-while-draft ─────────────────


RUN_ID = "release-20260720-101010-subfix1"


def _workspace(tmp_path):
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    ws.ensure_created()
    manifest = run_context.RunManifest(run_id=RUN_ID, started_at="2020-01-01 00:00:00")
    manifest.write(ws.manifest_path)
    return ws


def _seed_minimal_passing_run(ws, *, subscribed_status=None):
    def _w(component, data):
        path = ws.component_report_path(component)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runId": RUN_ID, **data}))

    _w("environment", {"status": "pass", "detail": []})
    _w("tablet", {"passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
                  "scenarios": [{"name": "REG", "status": "passed"}]})
    _w("mobile-api", {"counts": {"PASS": 1}, "steps": [{"name": "api", "status": "PASS"}]})
    _w("manual-checks", {"checks": [
        {"title": "t", "instruction": "i", "expectedResult": "e", "status": "pass"},
    ]})
    if subscribed_status is not None:
        _w("subscribed-fixture", {"mode": "offline-only", "status": subscribed_status, "detail": ["seeded"]})


_MINIMAL_CONSOLIDATE_ARGS = [
    "--android-optional", "--ios-optional", "--sync-optional",
    "--meals-optional", "--onboarding-optional", "--google-calendar-optional", "--kiosk-admin-optional",
    "--selector-contract-optional", "--allow-unknown-build-identity",
]


def test_consolidate_shows_subscribed_fixture_as_optional_while_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws, subscribed_status="blocked")  # BLOCKED, but optional -> must not block overall
    result = CliRunner().invoke(cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS])
    assert "Subscribed-calendar fixture (optional): BLOCKED" in result.output
    assert result.exit_code == EXIT_SUCCESS, result.output  # optional component never blocks overall PASS


def test_consolidate_explicit_mandatory_flag_gates_overall_status(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws, subscribed_status="blocked")
    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS, "--subscribed-fixture-mandatory"],
    )
    assert "Subscribed-calendar fixture: BLOCKED" in result.output
    assert result.exit_code != EXIT_SUCCESS


def test_consolidate_explicit_mandatory_with_ok_status_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws, subscribed_status="ok")
    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS, "--subscribed-fixture-mandatory"],
    )
    assert "Subscribed-calendar fixture: PASS" in result.output
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_consolidate_auto_derives_optional_from_draft_promotion_file(tmp_path, monkeypatch):
    # No --subscribed-fixture-mandatory/--optional flag at all: derives from
    # the REAL scenarios/promotion/subscribed_calendar.yaml, which is
    # releaseSuiteEligible: false (draft) in this repository today.
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws, subscribed_status="blocked")
    result = CliRunner().invoke(cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS])
    assert "Subscribed-calendar fixture (optional): BLOCKED" in result.output
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_consolidate_auto_derives_mandatory_once_promotion_file_says_eligible(tmp_path, monkeypatch):
    # Simulate the scenario having been promoted: patch the promotion module's
    # PROMOTION_DIR to a temp dir containing a releaseSuiteEligible: true file.
    from calee_regression import promotion as promotion_mod

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    promo_dir = tmp_path / "promotion"
    promo_dir.mkdir()
    (promo_dir / "subscribed_calendar.yaml").write_text(
        "scenario: subscribed_calendar\n"
        "scenarioFile: scenarios/subscribed_calendar.yaml\n"
        "sourceConfirmed: true\n"
        'sourceSha: "' + "a" * 40 + '"\n'
        "offlineTestsPassed: true\n"
        "physicalConfirmation:\n"
        "  status: passed\n"
        "  requiredDevice: physical_tablet\n"
        "  evidenceRequired: [runId]\n"
        "  evidence:\n"
        "    runId: some-run\n"
        "releaseSuiteEligible: true\n"
    )
    monkeypatch.setattr(promotion_mod, "PROMOTION_DIR", promo_dir)

    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws, subscribed_status="blocked")
    result = CliRunner().invoke(cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS])
    assert "Subscribed-calendar fixture: BLOCKED" in result.output  # no longer "(optional)"
    assert result.exit_code != EXIT_SUCCESS, result.output


# ── wrong-run / stale evidence rejection (reuses run_context validation) ───


def test_consolidate_rejects_subscribed_fixture_evidence_from_a_different_run(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws)
    # Written with a DIFFERENT run id than this workspace's own.
    path = ws.component_report_path("subscribed-fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": "release-20260101-000000-wrongrun", "status": "ok"}))

    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS, "--subscribed-fixture-mandatory"],
    )
    assert "subscribed-fixture report rejected" in result.output
    # Rejected -> treated as not-run -> BLOCKS since mandatory here.
    assert result.exit_code != EXIT_SUCCESS


def test_consolidate_rejects_stale_subscribed_fixture_evidence(tmp_path, monkeypatch):
    import os
    import time as _time

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws)
    path = ws.component_report_path("subscribed-fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": RUN_ID, "status": "ok"}))
    # Backdate the file's mtime to well before this run's recorded start.
    old = _time.mktime(_time.strptime("2019-01-01 00:00:00", "%Y-%m-%d %H:%M:%S"))
    os.utime(path, (old, old))

    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS, "--subscribed-fixture-mandatory"],
    )
    assert "subscribed-fixture report rejected" in result.output
    assert "stale" in result.output.lower() or "before this run started" in result.output.lower()
    assert result.exit_code != EXIT_SUCCESS


# ── #24: no secret in commands/output/JSON, through the REAL CLI command ───
#
# Every other subscribed-publisher test (test_subscribed_publisher.py) calls
# sp.prepare_subscribed_fixture()/the adapter functions directly. Nothing
# elsewhere exercises the actual `prepare-subscribed-fixture` CLI command --
# the real credential-resolution-to-results.json path a technical owner's
# machine actually runs -- so this is the one place that wiring gets proven,
# not just the pure functions underneath it.


def test_cli_prepare_subscribed_fixture_published_mode_leaks_no_credential(tmp_path, monkeypatch):
    import urllib.request

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    fake_username = "reg-webdav-user"
    fake_password = "S3cr3t-WebDAV-P@ss-9f8e"
    monkeypatch.setenv("CALEE_SUBSCRIBED_WEBDAV_USERNAME", fake_username)
    monkeypatch.setenv("CALEE_SUBSCRIBED_WEBDAV_PASSWORD", fake_password)

    ics_bytes = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"

    class _FakeResponse:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return ics_bytes

    def _fake_urlopen(req, timeout=None):
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    config_path = tmp_path / "machine.local.yaml"
    config_path.write_text(
        "subscribed_fixture:\n"
        "  publisher: webdav\n"
        "  public_url: https://fixtures.example.invalid/calee/regression-calendar.ics\n"
        "  poll_interval_seconds: 1\n"
        "  timeout_seconds: 5\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "prepare-subscribed-fixture", "--run-id", RUN_ID, "--release-id", "2026.07.20-rc3",
            "--config", str(config_path), "--mode", "published",
        ],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output

    # Neither credential ever appears in the command's own stdout...
    assert fake_username not in result.output
    assert fake_password not in result.output

    # ...nor in the results.json it wrote...
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    report_path = ws.component_report_path("subscribed-fixture")
    report_text = report_path.read_text(encoding="utf-8")
    assert fake_username not in report_text
    assert fake_password not in report_text

    # ...nor in the ICS sidecar written next to it (regression titles only).
    ics_path = report_path.parent / "reg_sub_today_relative.ics"
    assert ics_path.is_file()
    ics_text = ics_path.read_text(encoding="utf-8")
    assert fake_username not in ics_text
    assert fake_password not in ics_text

    # Sanity: this genuinely exercised the published path (not a silent
    # short-circuit) -- publication and observation both actually succeeded.
    data = json.loads(report_text)
    assert data["status"] == "ok", report_text
    assert data["publicationStatus"] == "ok", report_text
    assert data["observationStatus"] == "ok", report_text
    assert data["publisherType"] == "webdav", report_text
