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
from tablet_fixtures import TABLET_CERTIFYING_ENVELOPE as _TABLET_CERTIFYING_ENVELOPE
from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_NOT_RUN,
    STATUS_PASS,
    component_from_subscribed_fixture_report,
)
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS


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


# ── release-identity binding (Priority 7) ───────────────────────────────────


def test_component_from_report_accepts_matching_release_id():
    c = component_from_subscribed_fixture_report(
        "x", {"status": "ok", "releaseId": "2026.07.20-rc1"}, mandatory=True,
        expected_release_id="2026.07.20-rc1",
    )
    assert c.status == STATUS_PASS


def test_component_from_report_rejects_missing_release_id_when_expected():
    c = component_from_subscribed_fixture_report(
        "x", {"status": "ok"}, mandatory=True, expected_release_id="2026.07.20-rc1",
    )
    assert c.status == STATUS_BLOCKED
    assert any("has none" in d for d in c.detail)


def test_component_from_report_rejects_release_id_mismatch():
    c = component_from_subscribed_fixture_report(
        "x", {"status": "ok", "releaseId": "some-other-release"}, mandatory=True,
        expected_release_id="2026.07.20-rc1",
    )
    assert c.status == STATUS_BLOCKED
    joined = " ".join(c.detail)
    assert "some-other-release" in joined and "2026.07.20-rc1" in joined


def test_component_from_report_published_ok_requires_public_read_verification_ok():
    # A tampered/inconsistent report claiming overall success while the
    # Priority 5 public-read phase never actually verified must still BLOCK.
    c = component_from_subscribed_fixture_report(
        "x", {
            "status": "ok", "mode": "published",
            "publicReadVerificationStatus": "blocked-mismatch", "ingestionStatus": "ok",
        }, mandatory=True,
    )
    assert c.status == STATUS_BLOCKED
    assert any("publicReadVerificationStatus" in d for d in c.detail)


def test_component_from_report_published_ok_requires_ingestion_status_ok():
    # Same, for the Priority 6 ingestion phase.
    c = component_from_subscribed_fixture_report(
        "x", {
            "status": "ok", "mode": "published",
            "publicReadVerificationStatus": "ok", "ingestionStatus": "blocked",
        }, mandatory=True,
    )
    assert c.status == STATUS_BLOCKED
    assert any("ingestionStatus" in d for d in c.detail)


def test_component_from_report_published_ok_with_both_phases_verified_passes():
    c = component_from_subscribed_fixture_report(
        "x", {
            "status": "ok", "mode": "published",
            "publicReadVerificationStatus": "ok", "ingestionStatus": "ok",
        }, mandatory=True,
    )
    assert c.status == STATUS_PASS


def test_component_from_report_fixed_date_ok_unaffected_by_phase_checks():
    # fixed-date/offline-only never set publicReadVerificationStatus/
    # ingestionStatus at all (Priority 6: never faked for those modes) -- the
    # phase-consistency check applies only to mode == "published" reports.
    c = component_from_subscribed_fixture_report(
        "x", {"status": "ok", "mode": "offline-only"}, mandatory=True,
    )
    assert c.status == STATUS_PASS


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
    _w("tablet", {**_TABLET_CERTIFYING_ENVELOPE,
                  "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
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


# ── release-identity binding at consolidation (Priority 7) ─────────────────


def _seed_release_config(ws, *, release_id):
    ws.component_report_path("release-config").write_text(json.dumps({
        "runId": RUN_ID, "status": "ok", "releaseId": release_id, "schemaVersion": 2,
        "machineSelections": {}, "deviceIds": {},
        "releaseSelections": {
            "profile": "staging", "selectedBackend": "https://hub-dev.calee.com.au",
            "enabledPlatforms": [], "enabledFeatures": [],
            "expectedIdentities": {"calee": {}, "caleeShell": {}, "caleeMobile": {}},
        },
        "conflicts": [],
    }))


def _component_detail(out_dir, name):
    report = json.loads((out_dir / "consolidated-report.json").read_text(encoding="utf-8"))
    for c in report["components"]:
        if c["name"] == name:
            return " ".join(c["detail"])
    raise AssertionError(f"no component named {name!r} in consolidated report")


def test_consolidate_rejects_subscribed_fixture_evidence_for_a_different_release(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws)
    _seed_release_config(ws, release_id="2026.07.20-rc1")
    path = ws.component_report_path("subscribed-fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": RUN_ID, "status": "ok", "releaseId": "some-other-release"}))

    out_dir = tmp_path / "out"
    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS,
                   "--subscribed-fixture-mandatory", "--allow-unknown-build-identity", "--out-dir", str(out_dir)],
    )
    assert "Subscribed-calendar fixture: BLOCKED" in result.output
    detail = _component_detail(out_dir, "Subscribed-calendar fixture")
    assert "some-other-release" in detail and "2026.07.20-rc1" in detail
    assert result.exit_code != EXIT_SUCCESS


def test_consolidate_rejects_subscribed_fixture_evidence_with_no_release_id_when_release_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws)
    _seed_release_config(ws, release_id="2026.07.20-rc1")
    path = ws.component_report_path("subscribed-fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": RUN_ID, "status": "ok"}))  # no releaseId at all

    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS,
                   "--subscribed-fixture-mandatory", "--allow-unknown-build-identity"],
    )
    assert "Subscribed-calendar fixture: BLOCKED" in result.output
    assert result.exit_code != EXIT_SUCCESS


def test_consolidate_accepts_subscribed_fixture_evidence_for_the_matching_release(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = _workspace(tmp_path)
    _seed_minimal_passing_run(ws)
    _seed_release_config(ws, release_id="2026.07.20-rc1")
    path = ws.component_report_path("subscribed-fixture")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": RUN_ID, "status": "ok", "releaseId": "2026.07.20-rc1"}))

    result = CliRunner().invoke(
        cli.main, ["consolidate", "--run-id", RUN_ID, *_MINIMAL_CONSOLIDATE_ARGS,
                   "--subscribed-fixture-mandatory", "--allow-unknown-build-identity"],
    )
    assert "Subscribed-calendar fixture: PASS" in result.output
    assert result.exit_code == EXIT_SUCCESS, result.output


# ── prepare-subscribed-fixture adopts this run's own release-config releaseId
# (Priority 7), exactly like selector-contract's Priority 8 adoption ────────


def test_cli_prepare_subscribed_fixture_adopts_release_id_from_this_runs_release_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    ws.ensure_created()
    _seed_release_config(ws, release_id="2026.07.20-rc9")

    result = CliRunner().invoke(cli.main, ["prepare-subscribed-fixture", "--run-id", RUN_ID])
    assert result.exit_code == EXIT_SUCCESS, result.output

    data = json.loads(ws.component_report_path("subscribed-fixture").read_text(encoding="utf-8"))
    assert data["releaseId"] == "2026.07.20-rc9", data


def test_cli_prepare_subscribed_fixture_explicit_release_id_wins_over_release_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    ws.ensure_created()
    _seed_release_config(ws, release_id="2026.07.20-rc9")

    result = CliRunner().invoke(
        cli.main, ["prepare-subscribed-fixture", "--run-id", RUN_ID, "--release-id", "explicit-override"],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output

    data = json.loads(ws.component_report_path("subscribed-fixture").read_text(encoding="utf-8"))
    assert data["releaseId"] == "explicit-override", data


def test_cli_prepare_subscribed_fixture_no_release_config_leaves_release_id_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)

    result = CliRunner().invoke(cli.main, ["prepare-subscribed-fixture", "--run-id", RUN_ID])
    assert result.exit_code == EXIT_SUCCESS, result.output

    data = json.loads(ws.component_report_path("subscribed-fixture").read_text(encoding="utf-8"))
    assert data["releaseId"] is None, data


def test_cli_prepare_subscribed_fixture_records_generated_at(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(cli.main, ["prepare-subscribed-fixture", "--run-id", RUN_ID])
    assert result.exit_code == EXIT_SUCCESS, result.output

    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    data = json.loads(ws.component_report_path("subscribed-fixture").read_text(encoding="utf-8"))
    assert data["generatedAt"], data


# ── Priority 6 (this session): explicit --gate/--non-gating execution policy ──


def _blocked_published_run(tmp_path, monkeypatch, *, extra_args=()):
    """published mode with no publisher configured at all -- BLOCKS honestly
    (build_publisher_from_config returns (None, None, None))."""
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    return CliRunner().invoke(
        cli.main, ["prepare-subscribed-fixture", "--run-id", RUN_ID, "--mode", "published", *extra_args],
    )


def test_gate_flag_exits_blocked_on_a_failed_published_attempt(tmp_path, monkeypatch):
    result = _blocked_published_run(tmp_path, monkeypatch, extra_args=["--gate"])
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "BLOCKED" in result.output
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    data = json.loads(ws.component_report_path("subscribed-fixture").read_text(encoding="utf-8"))
    assert data["status"] != "ok"


def test_non_gating_flag_still_exits_success_on_a_failed_published_attempt(tmp_path, monkeypatch):
    result = _blocked_published_run(tmp_path, monkeypatch, extra_args=["--non-gating"])
    assert result.exit_code == EXIT_SUCCESS, result.output
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    data = json.loads(ws.component_report_path("subscribed-fixture").read_text(encoding="utf-8"))
    assert data["status"] != "ok"  # the failure is still fully recorded


def test_default_gate_is_derived_from_scenario_promotion_state_not_promoted(tmp_path, monkeypatch):
    # No promotion file at all (or draft) -- default is non-gating, matching
    # the exact derivation 'consolidate' uses for this component's
    # mandatory-ness.
    result = _blocked_published_run(tmp_path, monkeypatch)
    assert result.exit_code == EXIT_SUCCESS, result.output


_PROMOTED_SUBSCRIBED_CALENDAR_YAML = (
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


def test_default_gate_follows_promotion_once_scenario_is_promoted(tmp_path, monkeypatch):
    import calee_regression.promotion as promotion_mod

    promoted_dir = tmp_path / "promotion"
    promoted_dir.mkdir(parents=True)
    (promoted_dir / "subscribed_calendar.yaml").write_text(_PROMOTED_SUBSCRIBED_CALENDAR_YAML)
    monkeypatch.setattr(promotion_mod, "PROMOTION_DIR", promoted_dir)
    result = _blocked_published_run(tmp_path, monkeypatch)
    assert result.exit_code == EXIT_BLOCKED, result.output


def test_explicit_gate_flag_overrides_promotion_derived_default(tmp_path, monkeypatch):
    import calee_regression.promotion as promotion_mod

    promoted_dir = tmp_path / "promotion"
    promoted_dir.mkdir(parents=True)
    (promoted_dir / "subscribed_calendar.yaml").write_text(_PROMOTED_SUBSCRIBED_CALENDAR_YAML)
    monkeypatch.setattr(promotion_mod, "PROMOTION_DIR", promoted_dir)
    result = _blocked_published_run(tmp_path, monkeypatch, extra_args=["--non-gating"])
    assert result.exit_code == EXIT_SUCCESS, result.output


def test_gate_flag_with_a_passing_result_still_exits_success(tmp_path, monkeypatch):
    # offline-only mode with --gate: nothing to block on, gate never fires
    # against a genuinely passing result.
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = CliRunner().invoke(cli.main, ["prepare-subscribed-fixture", "--run-id", RUN_ID, "--gate"])
    assert result.exit_code == EXIT_SUCCESS, result.output


# ── Priority 6 (this session): safe_scenario_variables_from_report guard ────


def test_safe_scenario_variables_returns_none_for_non_published_mode():
    from calee_regression import subscribed_publisher as sp

    report = {"mode": "offline-only", "status": "ok", "generatedTitles": {"REG_SUB_TIMED_TITLE": "x"}}
    assert sp.safe_scenario_variables_from_report(report) is None


def test_safe_scenario_variables_returns_none_when_any_phase_not_ok():
    from calee_regression import subscribed_publisher as sp

    base = {
        "mode": "published", "status": "ok", "generatedTitles": {"REG_SUB_TIMED_TITLE": "x"},
        "publicationStatus": "ok", "publicReadVerificationStatus": "ok", "ingestionStatus": "ok",
    }
    assert sp.safe_scenario_variables_from_report(dict(base, publicationStatus="blocked")) is None
    assert sp.safe_scenario_variables_from_report(dict(base, publicReadVerificationStatus="blocked-mismatch")) is None
    assert sp.safe_scenario_variables_from_report(dict(base, ingestionStatus="blocked")) is None


def test_safe_scenario_variables_returns_titles_when_fully_verified():
    from calee_regression import subscribed_publisher as sp

    report = {
        "mode": "published", "status": "ok", "runId": "r1", "releaseId": "rel1",
        "generatedTitles": {"REG_SUB_TIMED_TITLE": "x", "REG_SUB_ALLDAY_TITLE": "y", "REG_SUB_DATE": "2026-01-01"},
        "publicationStatus": "ok", "publicReadVerificationStatus": "ok", "ingestionStatus": "ok",
    }
    assert sp.safe_scenario_variables_from_report(report, expected_run_id="r1", expected_release_id="rel1") == report["generatedTitles"]


def test_safe_scenario_variables_rejects_wrong_run_or_release():
    from calee_regression import subscribed_publisher as sp

    report = {
        "mode": "published", "status": "ok", "runId": "r1", "releaseId": "rel1",
        "generatedTitles": {"REG_SUB_TIMED_TITLE": "x"},
        "publicationStatus": "ok", "publicReadVerificationStatus": "ok", "ingestionStatus": "ok",
    }
    assert sp.safe_scenario_variables_from_report(report, expected_run_id="different-run") is None
    assert sp.safe_scenario_variables_from_report(report, expected_release_id="different-release") is None


def test_safe_scenario_variables_returns_none_for_missing_or_non_dict_report():
    from calee_regression import subscribed_publisher as sp

    assert sp.safe_scenario_variables_from_report(None) is None
    assert sp.safe_scenario_variables_from_report({}) is None
    assert sp.safe_scenario_variables_from_report("not-a-dict") is None


# ── #24: no secret in commands/output/JSON, through the REAL CLI command ───
#
# Every other subscribed-publisher test (test_subscribed_publisher.py) calls
# sp.prepare_subscribed_fixture()/the adapter functions directly. Nothing
# elsewhere exercises the actual `prepare-subscribed-fixture` CLI command --
# the real credential-resolution-to-results.json path a technical owner's
# machine actually runs -- so this is the one place that wiring gets proven,
# not just the pure functions underneath it.


def test_cli_prepare_subscribed_fixture_published_mode_leaks_no_credential(tmp_path, monkeypatch):
    import stat
    import urllib.request

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    fake_username = "reg-webdav-user"
    fake_password = "S3cr3t-WebDAV-P@ss-9f8e"
    monkeypatch.setenv("CALEE_SUBSCRIBED_WEBDAV_USERNAME", fake_username)
    monkeypatch.setenv("CALEE_SUBSCRIBED_WEBDAV_PASSWORD", fake_password)

    # Priority 6: the second, ingestion phase needs its own credentials
    # (also never allowed to leak) and a sibling CaleeMobile-Regression
    # checkout exposing find-event-by-title.
    reg_email = "reg-tester@example.invalid"
    reg_password = "hunter2-DO-NOT-LEAK-either"
    monkeypatch.setenv("CALEE_TEST_EMAIL", reg_email)
    monkeypatch.setenv("CALEE_TEST_PASSWORD", reg_password)

    sibling_api_dir = tmp_path.parent / "CaleeMobile-Regression" / "api"
    sibling_api_dir.mkdir(parents=True, exist_ok=True)
    fake_ingestion_script = sibling_api_dir / "sync_smoke_actions.py"
    fake_ingestion_script.write_text(
        "import argparse, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('action')\n"
        "p.add_argument('--base-url'); p.add_argument('--email'); p.add_argument('--password')\n"
        "p.add_argument('--title', default=None); p.add_argument('--calendar-id', default=None)\n"
        "p.add_argument('--report')\n"
        "args = p.parse_args()\n"
        "payload = {'found': True, 'id': 'evt_ingested', 'title': args.title, 'calendarId': args.calendar_id}\n"
        "open(args.report, 'w').write(json.dumps(payload))\n"
        "sys.exit(0)\n"
    )
    fake_ingestion_script.chmod(fake_ingestion_script.stat().st_mode | stat.S_IEXEC)

    # A real WebDAV server serves back exactly the bytes it was PUT with --
    # this fake does the same (Priority 5: the published-mode verification
    # now checks byte SHA-256 + both run-specific titles + the target date
    # against the exact generated ICS, so a fake server returning unrelated
    # placeholder bytes would -- correctly -- fail verification).
    published = {}

    class _FakeResponse:
        status = 201

        def __init__(self, body=b""):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._body

    def _fake_urlopen(req, timeout=None):
        # The publisher passes a urllib.request.Request (PUT); the CLI's
        # own read-back poll passes a bare URL string (GET).
        if hasattr(req, "get_method") and req.get_method() == "PUT":
            published["ics"] = req.data
            return _FakeResponse()
        return _FakeResponse(published.get("ics", b""))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    config_path = tmp_path / "machine.local.yaml"
    config_path.write_text(
        "backend_url: https://hub-dev.calee.com.au\n"
        "subscribed_fixture:\n"
        "  publisher: webdav\n"
        "  public_url: https://fixtures.example.invalid/calee/regression-calendar.ics\n"
        "  poll_interval_seconds: 1\n"
        "  timeout_seconds: 5\n"
        "  ingestion_poll_interval_seconds: 1\n"
        "  ingestion_timeout_seconds: 5\n",
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
    assert reg_email not in result.output
    assert reg_password not in result.output

    # ...nor in the results.json it wrote...
    ws = run_context.RunWorkspace(tmp_path, RUN_ID)
    report_path = ws.component_report_path("subscribed-fixture")
    report_text = report_path.read_text(encoding="utf-8")
    assert fake_username not in report_text
    assert fake_password not in report_text
    assert reg_email not in report_text
    assert reg_password not in report_text

    # ...nor in the ICS sidecar written next to it (regression titles only).
    ics_path = report_path.parent / "reg_sub_today_relative.ics"
    assert ics_path.is_file()
    ics_text = ics_path.read_text(encoding="utf-8")
    assert fake_username not in ics_text
    assert fake_password not in ics_text

    # Sanity: this genuinely exercised the FULL two-phase published path
    # (Priority 5 public-read verification AND Priority 6 Calee-ingestion
    # verification via the bridged find-event-by-title action) -- not a
    # silent short-circuit.
    data = json.loads(report_text)
    assert data["status"] == "ok", report_text
    assert data["publicationStatus"] == "ok", report_text
    assert data["observationStatus"] == "ok", report_text
    assert data["publisherType"] == "webdav", report_text
    assert data["publicReadVerificationStatus"] == "ok", report_text
    assert data["ingestionStatus"] == "ok", report_text
    assert data["ingestionObservedEvent"]["id"] == "evt_ingested", report_text
