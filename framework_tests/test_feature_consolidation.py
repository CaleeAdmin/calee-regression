"""Independent release-feature consolidation gating (Workstream 3).

Locks in that each declared release feature (Meals, onboarding + display/mobile
handoff, Google Calendar, CaleeShell kiosk/admin) is a REAL, independent
release-gating component built strictly from its own feature-tagged step
evidence -- never inferred from the broad Android/iOS (or tablet) component
passing:

  * a mandatory feature with matching PASS evidence      -> PASS;
  * a mandatory feature whose tagged step FAILED         -> FAIL;
  * a mandatory feature whose tagged step is BLOCKED / a
    mandatory tagged SKIP                                -> BLOCKED;
  * a mandatory feature with NO tagged evidence at all   -> NOT_RUN (blocks);
  * an optional/excluded feature with no evidence        -> shown, never gates;
  * a feature report from the wrong run ID               -> rejected (its
    evidence vanishes -> a mandatory feature then BLOCKS);
  * a feature result carried on the wrong CaleeMobile SHA -> the SHA-agreement
    gate BLOCKS the release.

Two layers are exercised: component_from_feature_evidence (unit, synthetic
dicts) and the `consolidate` CLI (run-scoped, end to end).
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from calee_regression import run_context
from calee_regression.cli import main
from calee_regression.consolidated_report import (
    FEATURE_COMPONENT_NAMES,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_NOT_RUN,
    STATUS_PASS,
    component_from_feature_evidence,
)
from calee_regression.models import EXIT_BLOCKED, EXIT_REGRESSION, EXIT_SUCCESS

RUN_ID = "release-test-feature-001"
SHA_A = "a" * 40
SHA_B = "b" * 40


def _ui_report(*steps, run_id=RUN_ID, platform="android", git_sha=SHA_A):
    """A mobile UI report shaped like run_ui_suite.py's _write_report output
    (counts are derived from the steps, exactly as the real writer does)."""
    counts: dict = {}
    for step in steps:
        counts[step["status"]] = counts.get(step["status"], 0) + 1
    return {
        "runId": run_id,
        "releaseRunId": run_id,
        "platform": platform,
        "deviceId": f"{platform}-device-1",
        "backend": {"requested": "https://hub-dev.calee.com.au",
                    "resolved": "https://hub-dev.calee.com.au",
                    "fixture": "https://hub-dev.calee.com.au"},
        "buildIdentity": {"available": True, "buildVersion": "0.0.22+22", "gitSha": git_sha, "dirty": False},
        "counts": counts,
        "steps": list(steps),
    }


def _step(name, status, feature, *, mandatory=True, skip_category=None, detail=""):
    return {
        "name": name, "status": status, "mandatory": mandatory,
        "skipCategory": skip_category, "feature": feature, "detail": detail,
    }


MEALS = "meals"
MEALS_NAME = FEATURE_COMPONENT_NAMES["meals"]


# ── component_from_feature_evidence: the status mapping ───────────────────────


def test_matching_pass_evidence_is_pass():
    report = _ui_report(_step("meals: add", "PASS", MEALS))
    c = component_from_feature_evidence(MEALS, MEALS_NAME, mandatory=True, sources=[(report, "android")])
    assert c.status == STATUS_PASS
    assert c.mandatory is True
    assert c.passed == 1
    # Evidence records applicability, the exact step, device/platform, SHA, backend.
    assert c.evidence["applicability"] == "mandatory"
    assert c.evidence["steps"][0]["name"] == "meals: add"
    assert "android" in c.evidence["platforms"]
    assert SHA_A in c.evidence["buildShas"]
    assert "https://hub-dev.calee.com.au" in c.evidence["backends"]


def test_failed_tagged_step_is_fail():
    report = _ui_report(_step("meals: add", "FAIL", MEALS, detail="delete button never appeared"))
    c = component_from_feature_evidence(MEALS, MEALS_NAME, mandatory=True, sources=[(report, "android")])
    assert c.status == STATUS_FAIL
    assert c.failed == 1


def test_blocked_tagged_step_is_blocked():
    report = _ui_report(_step("meals: add", "BLOCKED", MEALS, detail="no meals service"))
    c = component_from_feature_evidence(MEALS, MEALS_NAME, mandatory=True, sources=[(report, "android")])
    assert c.status == STATUS_BLOCKED
    assert c.evidence["blockedPrerequisite"]


def test_mandatory_skip_is_blocked():
    # A mandatory feature reported ENVIRONMENT_BLOCKED (a mandatory SKIP) must
    # never read as an optional skip -- it blocks.
    report = _ui_report(_step("meals: add", "SKIP", MEALS, mandatory=True,
                              skip_category="environment_blocked", detail="ENVIRONMENT_BLOCKED: no service"))
    c = component_from_feature_evidence(MEALS, MEALS_NAME, mandatory=True, sources=[(report, "android")])
    assert c.status == STATUS_BLOCKED


def test_optional_skip_does_not_pass_a_mandatory_feature():
    # Even if the Dart erroneously emitted an OPTIONAL skip, a mandatory feature
    # whose only evidence is a non-passing step must NOT read as PASS -- it is
    # NOT_RUN (the feature never actually ran), which still blocks for a
    # mandatory feature.
    report = _ui_report(_step("meals: add", "SKIP", MEALS, mandatory=False,
                              skip_category="optional_feature", detail="OPTIONAL: no service"))
    c = component_from_feature_evidence(MEALS, MEALS_NAME, mandatory=True, sources=[(report, "android")])
    assert c.status != STATUS_PASS
    assert c.status in (STATUS_BLOCKED, STATUS_NOT_RUN)  # nothing passed -> blocks


def test_no_evidence_mandatory_is_not_run():
    # The broad platform report passed, but nothing is tagged for this feature:
    # a mandatory feature with no evidence is NOT_RUN, never inferred PASS.
    report = _ui_report(_step("some other flow", "PASS", None))
    c = component_from_feature_evidence(MEALS, MEALS_NAME, mandatory=True, sources=[(report, "android")])
    assert c.status == STATUS_NOT_RUN
    assert c.passed == 0


def test_no_evidence_optional_is_not_run_but_not_mandatory():
    report = _ui_report(_step("some other flow", "PASS", None))
    c = component_from_feature_evidence(MEALS, MEALS_NAME, mandatory=False, sources=[(report, "android")])
    assert c.status == STATUS_NOT_RUN
    assert c.mandatory is False
    assert c.evidence["applicability"] == "optional"


def test_evidence_gathered_across_both_platforms():
    android = _ui_report(_step("meals: add", "PASS", MEALS), platform="android")
    ios = _ui_report(_step("meals: add", "PASS", MEALS), platform="ios")
    c = component_from_feature_evidence(
        MEALS, MEALS_NAME, mandatory=True, sources=[(android, "android"), (ios, "ios")]
    )
    assert c.status == STATUS_PASS
    assert c.passed == 2
    assert set(c.evidence["platforms"]) == {"android", "ios"}


# ── consolidate CLI: run-scoped feature gating end to end ─────────────────────


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00").write(workspace.manifest_path)
    return workspace


def _write(workspace, component, data):
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _write_passing_base(workspace, run_id=RUN_ID):
    _write(workspace, "environment", {"runId": run_id, "status": "pass", "detail": []})
    _write(workspace, "tablet", {"runId": run_id, "passed_count": 1, "failed_count": 0,
                                 "blocked_count": 0, "skipped_count": 0,
                                 "scenarios": [{"name": "a", "status": "passed"}]})
    _write(workspace, "mobile-api", {"runId": run_id, "counts": {"PASS": 1},
                                     "steps": [{"name": "x", "status": "PASS"}]})
    _write(workspace, "manual-checks", {"runId": run_id,
                                        "checks": [{"title": "t", "instruction": "i",
                                                    "expectedResult": "e", "status": "pass"}]})


# Isolate the Meals feature: everything else opted out so only Meals gates.
# The CaleeMobile selector contract is mandatory for any mobile release
# (Priority 2); opt it out via the named waiver the diagnostic path requires so
# it doesn't confound the Meals assertions (selector evidence has its own tests).
_ISOLATE = (
    "--android-mandatory", "--ios-optional", "--allow-unknown-build-identity",
    "--sync-optional", "--onboarding-optional",
    "--google-calendar-optional", "--kiosk-admin-optional",
    "--selector-contract-optional",
    "--waiver-reason", "unit test: Meals feature gating only",
    "--waiver-approver", "framework-tests",
    "--waiver-timestamp", "2026-07-18T00:00:00Z",
)


def _consolidate(tmp_path, *extra, run_id=RUN_ID):
    return CliRunner().invoke(
        main,
        ["consolidate", "--run-id", run_id, *_ISOLATE,
         "--out-dir", str(tmp_path / "out"), *extra],
    )


def test_cli_mandatory_meals_pass(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "mobile-android", _ui_report(_step("meals: add", "PASS", MEALS)))
    result = _consolidate(tmp_path, "--meals-mandatory")
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert f"{MEALS_NAME}: PASS" in result.output


def test_cli_mandatory_meals_fail(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "mobile-android", _ui_report(_step("meals: add", "FAIL", MEALS)))
    result = _consolidate(tmp_path, "--meals-mandatory")
    assert result.exit_code == EXIT_REGRESSION, result.output
    assert f"{MEALS_NAME}: FAIL" in result.output


def test_cli_mandatory_meals_blocked(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "mobile-android",
           _ui_report(_step("meals: add", "BLOCKED", MEALS, detail="no meals service")))
    result = _consolidate(tmp_path, "--meals-mandatory")
    assert result.exit_code == EXIT_BLOCKED, result.output


def test_cli_mandatory_meals_absent_blocks_even_though_android_passed(tmp_path):
    # The broad Android UI component passes, but has NO meals-tagged step.
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "mobile-android", _ui_report(_step("navigation", "PASS", None)))
    result = _consolidate(tmp_path, "--meals-mandatory")
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert f"{MEALS_NAME}: NOT_RUN" in result.output


def test_cli_optional_meals_absent_passes(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "mobile-android", _ui_report(_step("navigation", "PASS", None)))
    result = _consolidate(tmp_path, "--meals-optional")
    assert result.exit_code == EXIT_SUCCESS, result.output
    # Still shown as an explicit optional component -- never silently omitted.
    assert MEALS_NAME in result.output


def test_cli_meals_report_from_wrong_run_id_is_rejected_and_blocks(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    # A meals PASS, but the report carries a DIFFERENT run ID -> validation
    # rejects it -> the mandatory Meals feature has no evidence -> BLOCKED.
    _write(workspace, "mobile-android",
           _ui_report(_step("meals: add", "PASS", MEALS), run_id="some-other-run"))
    result = _consolidate(tmp_path, "--meals-mandatory")
    assert result.exit_code == EXIT_BLOCKED, result.output


def test_cli_meals_result_on_wrong_caleemobile_sha_blocks(tmp_path):
    # A real meals PASS, but carried on a CaleeMobile SHA that disagrees with the
    # expected release SHA -> the SHA-agreement gate BLOCKS: a feature result on
    # the wrong build can never certify the release.
    workspace = _make_workspace(tmp_path)
    _write_passing_base(workspace)
    _write(workspace, "mobile-android", _ui_report(_step("meals: add", "PASS", MEALS), git_sha=SHA_A))
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID,
         "--android-mandatory", "--ios-optional",
         "--sync-optional", "--meals-mandatory",
         "--onboarding-optional", "--google-calendar-optional", "--kiosk-admin-optional",
         "--caleemobile-git-sha", SHA_A,
         "--expected-caleemobile-git-sha", SHA_B,
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert "SHA" in result.output or "sha" in result.output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
