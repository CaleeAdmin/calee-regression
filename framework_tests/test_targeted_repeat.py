"""Permanent targeted scenario-repeat runner (Workstream 7).

Exercises profile loading, attempt planning, per-attempt status mapping,
aggregate status precedence, evidence preservation across attempts,
configurable stop-on-failure (default off), certification metadata, and the
guarantee that the targeted report never overwrites the full-suite report --
all with a fake run_once (no device).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from calee_regression import targeted_repeat as tr
from calee_regression.models import DEVICE_INIT_SKIP, DEVICE_INIT_STANDARD

REPO_ROOT = Path(__file__).resolve().parent.parent


def _suite(passed=1, failed=0, blocked=0, skipped=0, mandatory_skipped=0, scenarios=None):
    return {
        "passed_count": passed,
        "failed_count": failed,
        "blocked_count": blocked,
        "skipped_count": skipped,
        "mandatory_skipped_count": mandatory_skipped,
        "scenarios": scenarios if scenarios is not None else [{"name": "s", "status": "passed", "mandatory": True}],
    }


def make_run_once(status_by_scenario):
    """status_by_scenario: {scenario_str: suite_dict or [suite_dict per attempt]}.
    Writes a fixture results.json into each attempt_dir and records calls."""
    calls = []

    def run_once(scenario, attempt_dir):
        entry = status_by_scenario.get(scenario, _suite())
        idx = sum(1 for c in calls if c[0] == scenario)
        suite = entry[min(idx, len(entry) - 1)] if isinstance(entry, list) else entry
        (Path(attempt_dir) / "results.json").write_text(json.dumps(suite), encoding="utf-8")
        calls.append((scenario, str(attempt_dir)))
        return suite

    return run_once, calls


# ── profile loading ─────────────────────────────────────────────────────────


def test_load_checked_in_profile_has_four_scenarios():
    scenarios = tr.load_profile(REPO_ROOT / tr.DEFAULT_TARGETED_PROFILE)
    assert scenarios == [
        "scenarios/home_navigation.yaml",
        "scenarios/tasks_smoke.yaml",
        "scenarios/settings_smoke.yaml",
        "scenarios/calendar_recurring_events.yaml",
    ]


def test_load_profile_bare_list(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("- scenarios/a.yaml\n- scenarios/b.yaml\n", encoding="utf-8")
    assert tr.load_profile(p) == ["scenarios/a.yaml", "scenarios/b.yaml"]


def test_load_profile_rejects_non_list(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("scenarios: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        tr.load_profile(p)


# ── planning ────────────────────────────────────────────────────────────────


def test_plan_attempts_order_and_distinct_dirs():
    planned = tr.plan_attempts(["scenarios/a.yaml", "scenarios/b.yaml"], 2)
    assert [(p["stem"], p["repetition"]) for p in planned] == [
        ("a", 1), ("a", 2), ("b", 1), ("b", 2),
    ]
    assert len({p["dirName"] for p in planned}) == 4


def test_plan_attempts_rejects_zero():
    with pytest.raises(ValueError):
        tr.plan_attempts(["scenarios/a.yaml"], 0)


# ── status mapping ──────────────────────────────────────────────────────────


def test_attempt_status():
    assert tr.attempt_status(_suite(passed=2)) == "pass"
    assert tr.attempt_status(_suite(passed=1, failed=1)) == "fail"
    assert tr.attempt_status(_suite(passed=1, blocked=1)) == "blocked"
    # A mandatory skipped scenario folds into blocked.
    assert tr.attempt_status(
        _suite(passed=0, mandatory_skipped=1, scenarios=[{"name": "s", "status": "skipped", "mandatory": True}])
    ) == "blocked"


def test_aggregate_status_precedence():
    assert tr.aggregate_status(["pass", "blocked", "fail"]) == "fail"
    assert tr.aggregate_status(["pass", "blocked", "pass"]) == "blocked"
    assert tr.aggregate_status(["pass", "pass"]) == "pass"
    assert tr.aggregate_status([]) == "blocked"


# ── run_targeted ────────────────────────────────────────────────────────────


def test_run_targeted_preserves_every_attempt(tmp_path):
    run_once, calls = make_run_once({})
    report, status = tr.run_targeted(
        scenarios=["scenarios/a.yaml", "scenarios/b.yaml"],
        repeat_count=2,
        out_dir=tmp_path,
        run_once=run_once,
    )
    assert status == "pass"
    assert len(report["attempts"]) == 4
    # Distinct attempt directories, all preserved on disk.
    paths = [Path(a["reportPath"]) for a in report["attempts"]]
    assert len({p.parent for p in paths}) == 4
    assert all(p.exists() for p in paths)
    # Aggregate written separately from any per-attempt report.
    assert (tmp_path / "results.json").exists()


def test_run_targeted_default_does_not_hide_later_failures(tmp_path):
    # First scenario fails; default (no stop) must still run the rest so a later
    # failure is visible.
    run_once, calls = make_run_once({"scenarios/a.yaml": _suite(passed=0, failed=1)})
    report, status = tr.run_targeted(
        scenarios=["scenarios/a.yaml", "scenarios/b.yaml"],
        repeat_count=2,
        out_dir=tmp_path,
        run_once=run_once,
    )
    assert status == "fail"
    assert len(report["attempts"]) == 4  # nothing skipped
    assert report["stoppedEarly"] is False


def test_run_targeted_stop_on_failure(tmp_path):
    run_once, calls = make_run_once({"scenarios/a.yaml": _suite(passed=0, failed=1)})
    report, status = tr.run_targeted(
        scenarios=["scenarios/a.yaml", "scenarios/b.yaml"],
        repeat_count=2,
        out_dir=tmp_path,
        run_once=run_once,
        stop_on_failure=True,
    )
    assert status == "fail"
    assert report["stoppedEarly"] is True
    assert len(report["attempts"]) == 1  # stopped after first FAIL


def test_run_targeted_embeds_standard_certification(tmp_path):
    run_once, _ = make_run_once({})
    report, _ = tr.run_targeted(
        scenarios=["scenarios/a.yaml"], repeat_count=1, out_dir=tmp_path, run_once=run_once,
    )
    assert report["diagnosticMode"] is False
    assert report["certificationEligible"] is True
    assert report["reportType"] == tr.TARGETED_REPORT_TYPE


def test_run_targeted_embeds_diagnostic_certification(tmp_path):
    run_once, _ = make_run_once({})
    report, _ = tr.run_targeted(
        scenarios=["scenarios/a.yaml"], repeat_count=1, out_dir=tmp_path, run_once=run_once,
        device_initialization_mode=DEVICE_INIT_SKIP,
    )
    assert report["diagnosticMode"] is True
    assert report["certificationEligible"] is False


def test_same_stem_scenarios_do_not_collide(tmp_path):
    # Two scenarios sharing a filename stem in different dirs must get DISTINCT
    # attempt directories (Workstream 6).
    planned = tr.plan_attempts(["scenarios/a/home.yaml", "scenarios/b/home.yaml"], 2)
    assert len({p["dirName"] for p in planned}) == 4
    run_once, _ = make_run_once({})
    report, _ = tr.run_targeted(
        scenarios=["scenarios/a/home.yaml", "scenarios/b/home.yaml"],
        repeat_count=2, out_dir=tmp_path, run_once=run_once,
    )
    attempt_dirs = {Path(a["attemptDir"]) for a in report["attempts"]}
    assert len(attempt_dirs) == 4  # no collision
    assert all(d.exists() for d in attempt_dirs)


def test_second_invocation_cannot_overwrite_first(tmp_path):
    run_once, _ = make_run_once({})
    r1, _ = tr.run_targeted(
        scenarios=["scenarios/a.yaml"], repeat_count=1, out_dir=tmp_path, run_once=run_once,
        invocation_id="inv-A",
    )
    r2, _ = tr.run_targeted(
        scenarios=["scenarios/a.yaml"], repeat_count=1, out_dir=tmp_path, run_once=run_once,
        invocation_id="inv-B",
    )
    # Both invocations' immutable evidence survives.
    assert (tmp_path / "invocations" / "inv-A" / "results.json").exists()
    assert (tmp_path / "invocations" / "inv-B" / "results.json").exists()
    # The canonical index records BOTH invocations, and re-using an id is refused.
    index = json.loads((tmp_path / "results.json").read_text())
    ids = {e["invocationId"] for e in index["invocations"]}
    assert ids == {"inv-A", "inv-B"}
    assert index["selectedInvocationId"] == "inv-B"
    with pytest.raises(FileExistsError):
        tr.run_targeted(
            scenarios=["scenarios/a.yaml"], repeat_count=1, out_dir=tmp_path, run_once=run_once,
            invocation_id="inv-A",
        )


def test_interrupted_attempt_is_blocked_and_aggregate_preserved(tmp_path):
    calls = {"n": 0}

    def run_once(scenario, attempt_dir):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("device wedged mid-run")
        (Path(attempt_dir) / "results.json").write_text(json.dumps(_suite()), encoding="utf-8")
        return _suite()

    report, status = tr.run_targeted(
        scenarios=["scenarios/a.yaml", "scenarios/b.yaml"],
        repeat_count=1, out_dir=tmp_path, run_once=run_once,
    )
    # The aggregate is still written despite the interruption.
    assert (tmp_path / "results.json").exists()
    assert report["interrupted"] is True
    # The interrupted attempt is BLOCKED (never a missing report), so the run blocks.
    assert status == "blocked"
    interrupted = [a for a in report["attempts"] if a.get("interrupted")]
    assert len(interrupted) == 1
    assert interrupted[0]["status"] == "blocked"
    assert "wedged" in interrupted[0]["error"]


def test_report_carries_full_provenance(tmp_path):
    run_once, _ = make_run_once({})
    report, _ = tr.run_targeted(
        scenarios=["scenarios/a.yaml"], repeat_count=1, out_dir=tmp_path, run_once=run_once,
        invocation_id="inv-1", release_id="2026.07.20-rc1", profile_path="p.yaml",
        profile_digest="sha256:deadbeef",
        provenance={"deviceId": "TAB123", "backend": "https://hub", "fixtureVersion": "f7",
                    "tabletBuildIdentity": {"applicationId": "com.viso.calee"}, "apkSha256": "sha256:abc"},
    )
    for field in ("producer", "producerGitSha", "invocationId", "releaseId", "profilePath",
                  "profileDigest", "deviceId", "tabletBuildIdentity", "apkSha256", "backend",
                  "fixtureVersion", "startedAt", "finishedAt", "scenarios", "repeatCount",
                  "deviceInitializationMode", "attempts"):
        assert field in report, f"missing provenance field {field!r}"
    assert report["invocationId"] == "inv-1"
    assert report["releaseId"] == "2026.07.20-rc1"
    assert report["deviceId"] == "TAB123"
    assert report["attempts"][0]["reportPath"].endswith("results.json")


def test_run_targeted_does_not_touch_a_sibling_full_report(tmp_path):
    # Simulate the full-suite report living next to the targeted output.
    full = tmp_path / "tablet"
    full.mkdir()
    (full / "results.json").write_text('{"full": true}', encoding="utf-8")
    targeted = tmp_path / "tablet-targeted"
    targeted.mkdir()
    run_once, _ = make_run_once({})
    tr.run_targeted(
        scenarios=["scenarios/a.yaml"], repeat_count=1, out_dir=targeted, run_once=run_once,
    )
    # The full-suite report is untouched.
    assert json.loads((full / "results.json").read_text()) == {"full": True}
