"""Unit tests for run_context.py -- the single-release-run-ID/workspace
primitives (Workstream 3). See test_cli_consolidate.py and
test_release_platforms.py for the CLI-level integration tests that build
on these.
"""

from __future__ import annotations

import os
import time

import pytest

from calee_regression import run_context


def test_generate_run_id_is_unique_and_matches_expected_shape():
    ids = {run_context.generate_run_id() for _ in range(20)}
    assert len(ids) == 20  # no collisions across 20 draws
    for run_id in ids:
        assert run_id.startswith("release-")
        assert run_context.is_valid_run_id(run_id)


def test_generate_run_id_respects_custom_prefix():
    run_id = run_context.generate_run_id(prefix="sync")
    assert run_id.startswith("sync-")


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("release-20260716-153012-abc123", True),
        ("a", True),
        ("", False),
        (None, False),
        ("has a space", False),
        ("../escape-attempt", False),
        ("/absolute-path", False),
    ],
)
def test_is_valid_run_id(candidate, expected):
    assert run_context.is_valid_run_id(candidate) is expected


def test_workspace_paths_are_fixed_and_predictable(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-001")
    assert workspace.root == tmp_path / "reports" / "runs" / "release-test-001"
    assert workspace.component_report_path("tablet") == workspace.root / "tablet" / "results.json"
    assert workspace.component_report_path("mobile-android") == workspace.root / "mobile-android" / "results.json"
    assert workspace.consolidated_dir == workspace.root / "consolidated"
    assert workspace.manifest_path == workspace.root / "run-manifest.json"


def test_ensure_created_makes_every_component_directory(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-002")
    workspace.ensure_created()
    for component in run_context.COMPONENT_NAMES:
        assert workspace.component_dir(component).is_dir()
    assert workspace.consolidated_dir.is_dir()


def test_is_within_accepts_paths_inside_workspace_and_rejects_outside(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-003")
    workspace.ensure_created()
    assert workspace.is_within(workspace.component_report_path("tablet")) is True
    assert workspace.is_within(tmp_path / "reports" / "runs" / "release-other-run" / "tablet" / "results.json") is False
    assert workspace.is_within(tmp_path / "elsewhere.json") is False


def test_manifest_round_trips_through_json(tmp_path):
    manifest = run_context.RunManifest(
        run_id="release-test-004",
        started_at="2026-07-16 15:30:12",
        expected_components=["environment", "tablet"],
        release_platform_profile={"tablet": True, "mobile_android": True, "mobile_ios": False},
        tester="jane",
        target_backend="https://hub-dev.calee.com.au",
        fixture_version="regression-fixture-v3",
    )
    manifest.record_component("tablet", report_path="reports/runs/release-test-004/tablet/results.json", exit_code=0, device_id="emulator-5554")
    path = tmp_path / "run-manifest.json"
    manifest.write(path)

    loaded = run_context.RunManifest.load(path)
    assert loaded.run_id == "release-test-004"
    assert loaded.started_at == "2026-07-16 15:30:12"
    assert loaded.tester == "jane"
    assert loaded.fixture_version == "regression-fixture-v3"
    assert loaded.device_ids["tablet"] == "emulator-5554"
    assert loaded.exit_codes["tablet"] == 0
    assert "tablet" in loaded.report_paths


def test_extract_report_run_id_prefers_release_run_id_over_run_id():
    # CaleeMobile-Regression's api/ui reports carry their own per-invocation
    # "runId" (backend-object-isolation ID / local report ID) distinct from
    # the shared release run ID -- see run_context.py's module docstring.
    assert run_context.extract_report_run_id({"runId": "mobile-ui-local", "releaseRunId": "release-shared"}) == "release-shared"
    assert run_context.extract_report_run_id({"runId": "release-shared"}) == "release-shared"
    assert run_context.extract_report_run_id({}) is None
    assert run_context.extract_report_run_id(None) is None
    assert run_context.extract_report_run_id("not a dict") is None


def test_validate_component_report_accepts_matching_report(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-005")
    workspace.ensure_created()
    report_path = workspace.component_report_path("tablet")
    report_path.write_text("{}")
    run_context.validate_component_report(
        {"runId": "release-test-005"}, report_path=report_path, run_id="release-test-005",
        workspace=workspace, component="tablet",
    )  # must not raise


def test_validate_component_report_rejects_missing_run_id(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-006")
    workspace.ensure_created()
    report_path = workspace.component_report_path("tablet")
    report_path.write_text("{}")
    with pytest.raises(run_context.RunIdError, match="no run ID"):
        run_context.validate_component_report(
            {}, report_path=report_path, run_id="release-test-006", workspace=workspace, component="tablet",
        )


def test_validate_component_report_rejects_mismatched_run_id(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-007")
    workspace.ensure_created()
    report_path = workspace.component_report_path("tablet")
    report_path.write_text("{}")
    with pytest.raises(run_context.RunIdError, match="different run"):
        run_context.validate_component_report(
            {"runId": "release-wrong"}, report_path=report_path, run_id="release-test-007",
            workspace=workspace, component="tablet",
        )


def test_validate_component_report_rejects_path_outside_workspace(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-008")
    workspace.ensure_created()
    outside_path = tmp_path / "elsewhere" / "tablet.json"
    outside_path.parent.mkdir(parents=True, exist_ok=True)
    outside_path.write_text("{}")
    with pytest.raises(run_context.RunIdError, match="outside the current run's workspace"):
        run_context.validate_component_report(
            {"runId": "release-test-008"}, report_path=outside_path, run_id="release-test-008",
            workspace=workspace, component="tablet",
        )


def test_validate_component_report_rejects_report_older_than_run_start(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-009")
    workspace.ensure_created()
    report_path = workspace.component_report_path("tablet")
    report_path.write_text("{}")
    old = time.time() - 3600  # an hour before "now" easily clears the 30s grace window
    os.utime(report_path, (old, old))
    with pytest.raises(run_context.RunIdError, match="before this run started"):
        run_context.validate_component_report(
            {"runId": "release-test-009"}, report_path=report_path, run_id="release-test-009",
            workspace=workspace, component="tablet", run_started_at_epoch=time.time(),
        )


def test_validate_component_report_allows_small_clock_skew(tmp_path):
    workspace = run_context.RunWorkspace(tmp_path, "release-test-010")
    workspace.ensure_created()
    report_path = workspace.component_report_path("tablet")
    report_path.write_text("{}")
    # Written 5s "before" started_at -- inside the 30s grace window, must
    # not be treated as stale.
    run_context.validate_component_report(
        {"runId": "release-test-010"}, report_path=report_path, run_id="release-test-010",
        workspace=workspace, component="tablet", run_started_at_epoch=time.time() + 5,
    )


# --- Phase 3: worst-wins recording (a result can never improve) ----------


def test_worst_exit_code_prefers_fail_then_blocked_then_pass():
    # FAIL (1) is the most severe, then BLOCKED / other non-zero, then PASS (0).
    assert run_context.worst_exit_code([0, 1]) == 1  # a later PASS can't clear a FAIL
    assert run_context.worst_exit_code([1, 0]) == 1  # an earlier FAIL survives a later PASS
    assert run_context.worst_exit_code([0, 3]) == 3  # BLOCKED beats PASS
    assert run_context.worst_exit_code([3, 1]) == 1  # FAIL escalates BLOCKED
    assert run_context.worst_exit_code([0, 0]) == 0  # all clean stays PASS
    assert run_context.worst_exit_code([None, None]) is None
    assert run_context.worst_exit_code([None, 0]) == 0


def _fresh_manifest():
    return run_context.RunManifest(run_id="release-test-attempts", started_at="2020-01-01 00:00:00")


def test_record_component_fail_survives_a_later_pass():
    # Phase 3 requirement 2: an initial API FAIL cannot be replaced by a later
    # PASS. The Client API suite runs once now, but even a stray second
    # recording must never launder the FAIL into a PASS.
    manifest = _fresh_manifest()
    manifest.record_component("mobile-api", report_path="a.json", exit_code=1)
    manifest.record_component("mobile-api", report_path="b.json", exit_code=0)
    assert manifest.exit_codes["mobile-api"] == 1
    assert manifest.effective_exit_code("mobile-api") == 1


def test_record_component_keeps_an_auditable_attempt_history():
    manifest = _fresh_manifest()
    manifest.record_component("mobile-api", report_path="a.json", exit_code=1)
    manifest.record_component("mobile-api", report_path="b.json", exit_code=0)
    attempts = manifest.component_attempts["mobile-api"]
    assert [a["exitCode"] for a in attempts] == [1, 0]
    assert [a["reportPath"] for a in attempts] == ["a.json", "b.json"]
    # Round-trips through the manifest JSON.
    restored = run_context.RunManifest.from_dict(manifest.to_dict())
    assert restored.component_attempts["mobile-api"] == attempts
    assert restored.exit_codes["mobile-api"] == 1


def test_record_component_isolates_distinct_components():
    # Phase 3 requirements 3 & 4: a platform failure cannot overwrite another
    # component's recorded evidence -- each component is recorded independently.
    manifest = _fresh_manifest()
    manifest.record_component("mobile-api", exit_code=0)
    manifest.record_component("mobile-ios", exit_code=0)
    manifest.record_component("mobile-android", exit_code=1)  # Android fails
    assert manifest.exit_codes["mobile-api"] == 0  # API evidence intact
    assert manifest.exit_codes["mobile-ios"] == 0  # iOS evidence intact
    assert manifest.exit_codes["mobile-android"] == 1

    # And symmetrically, a later iOS failure leaves Android/API alone.
    manifest2 = _fresh_manifest()
    manifest2.record_component("mobile-api", exit_code=0)
    manifest2.record_component("mobile-android", exit_code=0)
    manifest2.record_component("mobile-ios", exit_code=1)
    assert manifest2.exit_codes["mobile-api"] == 0
    assert manifest2.exit_codes["mobile-android"] == 0
    assert manifest2.exit_codes["mobile-ios"] == 1
