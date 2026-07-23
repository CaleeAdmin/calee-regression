"""Tests for release remediation planning (Phase 6): the pure
remediation_plan module plus the `release-remediation-plan` CLI command --
identity comparison, per-component classification, the always-present
NO_RELEASE_PROMOTION_ALLOWED decision, Android-in-scope-unqualified handling,
immutability, and the guarantee that release results are never modified.
"""

from __future__ import annotations

import hashlib
import json
import stat

import pytest
from click.testing import CliRunner

from calee_regression import cli, models, remediation_plan as rp, run_context

FOCUSED_RUN = "focused-20260723-101500-abc123"
RELEASE_RUN = "release-20260720-090000-def456"
BACKEND = "https://staging.calee.invalid"
PRODUCT_SHA = "c0ffee00" * 5


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_focused_summary(**overrides):
    summary = {
        "reportType": "focused-verify-summary",
        "reportSchemaVersion": 2,
        "runId": FOCUSED_RUN,
        "invocationId": "inv-20260723T101500-000001",
        "releaseId": f"focused-diagnostic-{FOCUSED_RUN}",
        "status": "pass",
        "certificationEligible": False,
        "verifiedBackend": BACKEND,
        "fixtureVersion": "REG-9",
        "regressionShas": {"calee-regression": "aa" * 20, "caleemobile-regression": "bb" * 20},
        "productBuild": {"caleeMobileSha": PRODUCT_SHA},
        "deviceIds": {"tablet": "TABLET-1", "ios": "IPHONE-1"},
        "installedArtifactIdentity": {"status": "verified"},
        "steps": [
            {"id": "fixture", "status": "pass"},
            {"id": "tablet-standard", "status": "pass", "mode": "standard"},
            {"id": "tablet-diagnostic", "status": "pass", "mode": "diagnostic"},
            {"id": "api-1", "status": "pass"},
            {"id": "api-2", "status": "pass"},
            {"id": "ios", "status": "pass"},
        ],
    }
    summary.update(overrides)
    return summary


def make_release_manifest(**overrides):
    data = {
        "runId": RELEASE_RUN,
        "startedAt": "2026-07-20 09:00:00",
        "expectedComponents": ["environment", "tablet", "mobile-api", "mobile-android",
                               "mobile-ios", "kiosk-admin"],
        "releasePlatformProfile": {"tablet": True, "mobile_android": True, "mobile_ios": True,
                                   "kiosk_admin": True},
        "exitCodes": {"environment": 0, "tablet": 3, "mobile-api": 0, "mobile-ios": 3},
        "targetBackend": BACKEND,
        "fixtureVersion": "REG-9",
        "gitShas": {"caleeMobile": PRODUCT_SHA},
        "deviceIds": {},
    }
    data.update(overrides)
    return data


# ── pure module ────────────────────────────────────────────────────────────
def test_matching_identities_have_no_hard_mismatch():
    comparison = rp.compare_identities(make_focused_summary(), make_release_manifest())
    assert rp.hard_mismatches(comparison) == []


@pytest.mark.parametrize("summary_overrides, field", [
    ({"verifiedBackend": "https://other.invalid"}, "backend"),
    ({"productBuild": {"caleeMobileSha": "deadbeef"}}, "productSha"),
    ({"fixtureVersion": "REG-8"}, "fixtureVersion"),
])
def test_hard_mismatch_forces_fresh_release_run(summary_overrides, field):
    summary = make_focused_summary(**summary_overrides)
    comparison = rp.compare_identities(summary, make_release_manifest())
    assert [m["field"] for m in rp.hard_mismatches(comparison)] == [field]
    decisions = rp.decide(comparison, rp.classify_components(summary, make_release_manifest()))
    assert rp.DECISION_RELEASE_INPUT_MISMATCH in decisions
    assert rp.DECISION_START_FRESH_RELEASE_RUN in decisions
    assert rp.DECISION_NO_RELEASE_PROMOTION_ALLOWED in decisions


def test_unknown_side_is_neither_match_nor_hard_mismatch():
    summary = make_focused_summary(fixtureVersion=None)
    comparison = rp.compare_identities(summary, make_release_manifest())
    entry = next(e for e in comparison if e["field"] == "fixtureVersion")
    assert entry["match"] is None
    assert rp.hard_mismatches(comparison) == []


def test_blocked_tablet_with_focused_pass_is_framework_fixed_resumable():
    classifications = rp.classify_components(make_focused_summary(), make_release_manifest())
    by_component = {c["component"]: c for c in classifications}
    assert by_component["tablet"]["classification"] == rp.CLASS_FRAMEWORK_FIXED_RESUMABLE
    assert by_component["mobile-ios"]["classification"] == rp.CLASS_FRAMEWORK_FIXED_RESUMABLE
    decisions = rp.decide(rp.compare_identities(make_focused_summary(), make_release_manifest()),
                          classifications)
    assert rp.DECISION_RESUME_BLOCKED_COMPONENTS in decisions
    assert rp.DECISION_RERUN_TABLET_STANDARD in decisions
    assert rp.DECISION_RERUN_IOS in decisions


def test_blocked_component_without_focused_pass_stays_blocked():
    summary = make_focused_summary(steps=[
        {"id": "fixture", "status": "pass"},
        {"id": "tablet-standard", "status": "blocked"},
    ])
    by_component = {c["component"]: c
                    for c in rp.classify_components(summary, make_release_manifest())}
    assert by_component["tablet"]["classification"] == rp.CLASS_BLOCKED_UNRESOLVED


def test_android_in_scope_but_unqualified_is_never_silently_excluded():
    classifications = rp.classify_components(make_focused_summary(), make_release_manifest())
    by_component = {c["component"]: c for c in classifications}
    assert by_component["mobile-android"]["classification"] == rp.CLASS_ANDROID_UNQUALIFIED
    decisions = rp.decide([], classifications)
    assert rp.DECISION_ANDROID_DEVICE_REQUIRED in decisions


def test_android_out_of_scope_is_just_untested():
    manifest = make_release_manifest(
        releasePlatformProfile={"tablet": True, "mobile_android": False, "mobile_ios": True})
    by_component = {c["component"]: c
                    for c in rp.classify_components(make_focused_summary(), manifest)}
    assert by_component["mobile-android"]["classification"] == rp.CLASS_UNTESTED


def test_kiosk_blocked_requires_authorization():
    manifest = make_release_manifest(
        exitCodes={"environment": 0, "kiosk-admin": 3})
    classifications = rp.classify_components(make_focused_summary(), manifest)
    by_component = {c["component"]: c for c in classifications}
    assert by_component["kiosk-admin"]["classification"] == rp.CLASS_KIOSK_AUTHORIZATION_REQUIRED
    assert rp.DECISION_KIOSK_AUTHORIZATION_REQUIRED in rp.decide([], classifications)


def test_prior_fail_and_pass_and_untested_classifications():
    manifest = make_release_manifest(exitCodes={"environment": 0, "mobile-api": 1})
    by_component = {c["component"]: c
                    for c in rp.classify_components(make_focused_summary(), manifest)}
    assert by_component["environment"]["classification"] == rp.CLASS_PASSED
    assert by_component["mobile-api"]["classification"] == rp.CLASS_FAILED_RERUN_REQUIRED
    assert by_component["tablet"]["classification"] == rp.CLASS_UNTESTED


def test_no_release_promotion_allowed_is_always_present():
    # Even a fully clean comparison with nothing to do keeps the invariant.
    for classifications in ([], rp.classify_components(make_focused_summary(), make_release_manifest())):
        assert rp.DECISION_NO_RELEASE_PROMOTION_ALLOWED in rp.decide([], classifications)


def test_build_plan_shape_and_evidence_binding():
    supporting = [{"role": "focused-verify-summary", "path": "/x/summary.json", "sha256": "ab" * 32}]
    plan = rp.build_plan(
        focused_summary=make_focused_summary(), release_manifest=make_release_manifest(),
        focused_run_id=FOCUSED_RUN, release_run_id=RELEASE_RUN,
        planned_from_invocation="inv-1", all_invocations=["inv-0", "inv-1"],
        supporting_reports=supporting,
    )
    assert plan["reportType"] == rp.REPORT_TYPE
    assert plan["reportSchemaVersion"] == rp.SCHEMA_VERSION
    assert plan["certificationEligible"] is False
    assert "diagnostic planning evidence only" in plan["evidenceRole"]
    assert plan["focusedRun"]["runId"] == FOCUSED_RUN
    assert plan["releaseRun"]["runId"] == RELEASE_RUN
    assert plan["focusedRun"]["allInvocations"] == ["inv-0", "inv-1"]
    assert plan["supportingFocusedReports"] == supporting
    assert plan["decisions"][-1] == rp.DECISION_NO_RELEASE_PROMOTION_ALLOWED


# ── CLI command ────────────────────────────────────────────────────────────
@pytest.fixture
def cli_root(monkeypatch, tmp_path):
    root = tmp_path / "root"
    monkeypatch.setattr(cli, "_resolved_report_root", lambda *a, **k: root)
    return root


def write_focused_run(root, summary=None, with_manifest=True, child_reports=()):
    summary = summary if summary is not None else make_focused_summary()
    invocation = summary.get("invocationId", "inv-20260723T101500-000001")
    focused_dir = root / "reports" / "runs" / FOCUSED_RUN / "focused-verify" / invocation
    focused_dir.mkdir(parents=True, exist_ok=True)
    steps = summary.get("steps", [])
    for step_id, content in child_reports:
        child_path = focused_dir / f"{step_id}.json"
        child_path.write_text(json.dumps(content) + "\n", encoding="utf-8")
        for step in steps:
            if step["id"] == step_id:
                step["reportPath"] = str(child_path)
                step["reportSha256"] = _sha(child_path)
    summary_path = focused_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if with_manifest:
        workspace = run_context.RunWorkspace(root, FOCUSED_RUN)
        manifest = run_context.RunManifest(run_id=FOCUSED_RUN, started_at="2026-07-23 10:15:00")
        manifest.record_component("focused-verify", report_path=str(summary_path), exit_code=0,
                                  invocation_id=invocation, invocation_path=str(focused_dir))
        manifest.write(workspace.manifest_path)
    return summary_path


def write_release_run(root, manifest_dict=None):
    workspace = run_context.RunWorkspace(root, RELEASE_RUN)
    manifest = run_context.RunManifest.from_dict(manifest_dict or make_release_manifest())
    manifest.write(workspace.manifest_path)
    return workspace


def invoke(*args):
    return CliRunner().invoke(cli.main, ["release-remediation-plan", *args])


def _plan_paths(root):
    return list((root / "reports" / "runs" / RELEASE_RUN / "remediation").glob("*/remediation.json"))


def test_cli_produces_immutable_plan_and_records_manifest(cli_root):
    write_focused_run(cli_root)
    workspace = write_release_run(cli_root)
    before = workspace.manifest_path.read_text()
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    assert "NO_RELEASE_PROMOTION_ALLOWED" in result.output
    paths = _plan_paths(cli_root)
    assert len(paths) == 1
    plan = json.loads(paths[0].read_text())
    assert plan["reportType"] == "release-remediation-plan"
    assert rp.DECISION_RESUME_BLOCKED_COMPONENTS in plan["decisions"]
    assert rp.DECISION_RERUN_TABLET_STANDARD in plan["decisions"]
    assert rp.DECISION_ANDROID_DEVICE_REQUIRED in plan["decisions"]
    assert not (paths[0].stat().st_mode & stat.S_IWUSR)  # read-only on disk
    # supporting evidence links summary path + digest
    supporting = plan["supportingFocusedReports"]
    assert supporting[0]["role"] == "focused-verify-summary"
    assert supporting[0]["sha256"]
    # release results never modified: only the remediation-plan component is new
    after = run_context.RunManifest.load(workspace.manifest_path)
    original = json.loads(before)
    assert {k: v for k, v in after.exit_codes.items() if k != "remediation-plan"} \
        == original["exitCodes"]
    assert after.exit_codes["remediation-plan"] == 0


def test_cli_identity_mismatch_plan_says_start_fresh(cli_root):
    write_focused_run(cli_root, summary=make_focused_summary(verifiedBackend="https://other.invalid"))
    write_release_run(cli_root)
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    plan = json.loads(_plan_paths(cli_root)[0].read_text())
    assert rp.DECISION_RELEASE_INPUT_MISMATCH in plan["decisions"]
    assert rp.DECISION_START_FRESH_RELEASE_RUN in plan["decisions"]
    assert "RELEASE_INPUT_MISMATCH" in result.output


def test_cli_refuses_to_overwrite_existing_plan(cli_root):
    write_focused_run(cli_root)
    write_release_run(cli_root)
    first = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert first.exit_code == models.EXIT_SUCCESS
    content = _plan_paths(cli_root)[0].read_text()
    second = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert second.exit_code == models.EXIT_BLOCKED
    assert "refusing to overwrite" in second.output.lower()
    assert _plan_paths(cli_root)[0].read_text() == content  # untouched


def test_cli_child_report_digest_mismatch_blocks(cli_root):
    child = {"reportType": "tablet-targeted-repeat", "status": "pass"}
    summary_path = write_focused_run(
        cli_root, child_reports=[("tablet-standard", child)])
    # tamper the child report AFTER its digest was recorded in the summary
    tampered = summary_path.parent / "tablet-standard.json"
    tampered.write_text(json.dumps({"status": "pass", "tampered": True}), encoding="utf-8")
    write_release_run(cli_root)
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_BLOCKED
    assert "digest mismatch" in result.output
    assert _plan_paths(cli_root) == []


def test_cli_invalid_run_ids_exit_2(cli_root):
    result = invoke("--focused-run", "bad id!", "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_INVALID_CONFIG
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", "!!")
    assert result.exit_code == models.EXIT_INVALID_CONFIG


def test_cli_missing_focused_summary_blocks(cli_root):
    write_release_run(cli_root)
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_BLOCKED
    assert "no focused-verify summary" in result.output


def test_cli_missing_release_manifest_blocks(cli_root):
    write_focused_run(cli_root)
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_BLOCKED
    assert "manifest" in result.output


def test_cli_summary_run_id_mismatch_blocks(cli_root):
    write_focused_run(cli_root, summary=make_focused_summary(runId="some-other-run"))
    write_release_run(cli_root)
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_BLOCKED
    assert "failed validation" in result.output


def test_cli_plans_from_newest_invocation_but_validates_all(cli_root):
    older = make_focused_summary(invocationId="inv-20260722T090000-000001", status="fail")
    newer = make_focused_summary(invocationId="inv-20260723T101500-000002")
    write_focused_run(cli_root, summary=older)
    write_focused_run(cli_root, summary=newer)
    write_release_run(cli_root)
    result = invoke("--focused-run", FOCUSED_RUN, "--release-run", RELEASE_RUN)
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    plan = json.loads(_plan_paths(cli_root)[0].read_text())
    assert plan["focusedRun"]["invocationPlannedFrom"] == "inv-20260723T101500-000002"
    assert plan["focusedRun"]["allInvocations"] == [
        "inv-20260722T090000-000001", "inv-20260723T101500-000002"]
    assert plan["focusedRun"]["status"] == "pass"
