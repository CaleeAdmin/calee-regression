import json

import pytest
from click.testing import CliRunner

from calee_regression import run_context
from calee_regression.cli import main
from calee_regression.models import (
    EXIT_BLOCKED,
    EXIT_INVALID_CONFIG,
    EXIT_REGRESSION,
    EXIT_SUCCESS,
)

RUN_ID = "release-test-consolidate-001"


@pytest.fixture(autouse=True)
def _isolate_repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path)


def _make_workspace(tmp_path, run_id=RUN_ID):
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    manifest = run_context.RunManifest(run_id=run_id, started_at="2020-01-01 00:00:00")
    manifest.write(workspace.manifest_path)
    return workspace


def _write_component(workspace, component, data):
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_consolidate_requires_run_id():
    runner = CliRunner()
    result = runner.invoke(main, ["consolidate"])
    assert result.exit_code != EXIT_SUCCESS


def test_consolidate_rejects_unknown_run_id(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["consolidate", "--run-id", "release-never-created"])
    assert result.exit_code == EXIT_INVALID_CONFIG
    assert "no run workspace found" in result.output.lower()


def test_consolidate_blocks_without_manual_checks(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "environment", {"runId": RUN_ID, "status": "pass", "detail": []})
    _write_component(workspace, "tablet", {
        "runId": RUN_ID,
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    _write_component(workspace, "mobile-api", {
        "runId": RUN_ID, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}],
    })

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "BLOCKED" in result.output


def test_consolidate_passes_when_everything_is_provided_and_clean(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "environment", {"runId": RUN_ID, "status": "pass", "detail": []})
    _write_component(workspace, "tablet", {
        "runId": RUN_ID,
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    _write_component(workspace, "mobile-api", {
        "runId": RUN_ID, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}],
    })
    _write_component(workspace, "manual-checks", {
        "runId": RUN_ID,
        "checks": [{"title": "Kiosk escape check", "instruction": "swipe down", "expectedResult": "no shade", "status": "pass"}],
    })

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "consolidate", "--run-id", RUN_ID,
            "--build-version", "9.9.9",
            # This release doesn't include mobile UI results at all (a
            # tablet-only scope for this test) -- without an explicit
            # opt-out, Android/iOS UI default to mandatory=True and a
            # missing report would correctly BLOCK. See
            # test_release_platforms.py for the platform-driven cases.
            "--android-optional", "--ios-optional",
            # The tablet IS in scope, so its build identity is mandatory to
            # know (Phase 3): provide the detected version so identity is
            # available and clean. CaleeMobile identity is not required here
            # (no mobile platform in scope).
            "--calee-build-version", "0.3.22",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "PASS" in result.output

    bundles = list((tmp_path / "out").glob("**/*.zip"))
    assert len(bundles) == 1
    assert "9.9.9" in bundles[0].name
    assert bundles[0].name.endswith("-PASS.zip")

    manifest = run_context.RunManifest.load(workspace.manifest_path)
    assert manifest.finished_at
    assert manifest.exit_codes["consolidated"] == EXIT_SUCCESS

    latest_link = tmp_path / "reports" / "latest-run"
    assert latest_link.is_symlink()
    assert latest_link.resolve() == workspace.root.resolve()


def _write_full_tablet_only_components(workspace, run_id=RUN_ID):
    _write_component(workspace, "environment", {"runId": run_id, "status": "pass", "detail": []})
    _write_component(workspace, "tablet", {
        "runId": run_id,
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    _write_component(workspace, "mobile-api", {
        "runId": run_id, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}],
    })
    _write_component(workspace, "manual-checks", {
        "runId": run_id,
        "checks": [{"title": "Kiosk escape check", "instruction": "swipe down", "expectedResult": "no shade", "status": "pass"}],
    })


def test_consolidate_blocks_when_required_tablet_identity_is_unknown(tmp_path):
    # Tablet is in scope (default mandatory) but no --calee-build-version is
    # given and identity is required -> BLOCKED (never certify an unknown build).
    workspace = _make_workspace(tmp_path)
    _write_full_tablet_only_components(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "Calee tablet build identity: BLOCKED" in result.output


def test_consolidate_blocks_on_dirty_build_without_approval(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_full_tablet_only_components(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional",
         "--calee-build-version", "0.3.22", "--calee-dirty",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "Calee tablet build identity: BLOCKED" in result.output


def test_consolidate_passes_on_dirty_build_when_explicitly_approved(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_full_tablet_only_components(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional",
         "--calee-build-version", "0.3.22", "--calee-dirty", "--allow-dirty",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "PASS" in result.output


def test_consolidate_blocks_on_expected_build_mismatch(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_full_tablet_only_components(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional",
         "--calee-build-version", "0.3.21", "--expected-calee-build-version", "0.3.22",
         "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "Calee tablet build identity: BLOCKED" in result.output


def test_consolidate_allow_unknown_build_identity_opts_out(tmp_path):
    # The escape hatch: explicitly allow an unknown identity (e.g. a
    # diagnostic run) -> the identity no longer gates.
    workspace = _make_workspace(tmp_path)
    _write_full_tablet_only_components(workspace)
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional",
         "--allow-unknown-build-identity", "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_SUCCESS
    assert "PASS" in result.output


def test_consolidate_rejects_report_with_wrong_run_id(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "environment", {"runId": RUN_ID, "status": "pass", "detail": []})
    # tablet report claims a different run -- must be rejected, not
    # silently treated as belonging to this run.
    _write_component(workspace, "tablet", {
        "runId": "release-some-other-run",
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    _write_component(workspace, "mobile-api", {
        "runId": RUN_ID, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}],
    })

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional", "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "different run" in result.output.lower()
    # Rejected, so the tablet component must read as not-executed, not
    # silently pass through as if the mismatched report were valid.
    assert "Calee tablet: BLOCKED" in result.output or "Calee tablet: NOT_RUN" in result.output


def test_consolidate_rejects_report_with_no_run_id(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "environment", {"runId": RUN_ID, "status": "pass", "detail": []})
    _write_component(workspace, "tablet", {
        # No "runId" key at all.
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional", "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "no run id" in result.output.lower() or "has no run id" in result.output.lower()


def test_consolidate_rejects_report_path_outside_workspace(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "environment", {"runId": RUN_ID, "status": "pass", "detail": []})
    _write_component(workspace, "mobile-api", {
        "runId": RUN_ID, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}],
    })
    # A tablet report that carries a matching run ID but lives *outside*
    # this run's workspace -- e.g. a stale path left over in shell history
    # from a previous invocation. Must still be rejected.
    outside = tmp_path / "outside" / "tablet.json"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(json.dumps({
        "runId": RUN_ID,
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    }))

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "consolidate", "--run-id", RUN_ID, "--tablet-report", str(outside),
            "--android-optional", "--ios-optional", "--out-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "outside the current run's workspace" in result.output.lower()


def test_consolidate_rejects_report_older_than_run_start(tmp_path):
    import os
    import time as time_mod

    workspace = _make_workspace(tmp_path)
    _write_component(workspace, "environment", {"runId": RUN_ID, "status": "pass", "detail": []})
    _write_component(workspace, "mobile-api", {
        "runId": RUN_ID, "counts": {"PASS": 1}, "steps": [{"name": "x", "status": "PASS"}],
    })
    tablet_path = _write_component(workspace, "tablet", {
        "runId": RUN_ID,
        "passed_count": 1, "failed_count": 0, "blocked_count": 0, "skipped_count": 0,
        "scenarios": [{"name": "a", "status": "passed"}],
    })
    # Backdate the file's mtime to well before the manifest's started_at
    # ("2020-01-01") -- simulates a leftover results.json from a previous
    # run whose workspace directory got reused.
    old = time_mod.mktime(time_mod.strptime("2019-01-01 00:00:00", "%Y-%m-%d %H:%M:%S"))
    os.utime(tablet_path, (old, old))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional", "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "before this run started" in result.output.lower()


# --- Phase 3: an overwritten API report can't launder a FAIL into a PASS --


def test_consolidate_keeps_api_fail_after_report_overwritten_with_pass(tmp_path):
    # The manifest recorded that the Client API FAILed on its one run; a later
    # step then overwrote mobile-api/results.json with a PASS. Consolidation
    # must use the recorded FAIL -- the report overwrite can't launder it.
    workspace = _make_workspace(tmp_path)
    _write_full_tablet_only_components(workspace)  # mobile-api report says PASS
    manifest = run_context.RunManifest.load(workspace.manifest_path)
    manifest.record_component("mobile-api", report_path="mobile-api/results.json", exit_code=1)
    manifest.write(workspace.manifest_path)

    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional",
         "--calee-build-version", "0.3.22", "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_REGRESSION
    assert "CaleeMobile Client API: FAIL" in result.output
    bundles = list((tmp_path / "out").glob("**/*.zip"))
    assert len(bundles) == 1 and bundles[0].name.endswith("-FAIL.zip")


# --- Phase 4: a pre/post identity change BLOCKS the release ---------------


def _write_identity_snapshots(workspace, *, pre, post):
    identity_dir = workspace.root / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "pre.json").write_text(json.dumps(pre))
    (identity_dir / "post.json").write_text(json.dumps(post))


def test_consolidate_blocks_on_pre_post_tablet_identity_mismatch(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_full_tablet_only_components(workspace)
    # The installed tablet package's versionCode changed between the pre-run
    # and post-run snapshots -- what was tested is not what is being certified.
    base_tablet = {"applicationId": "com.viso.calee", "buildVersion": "0.3.22", "gitSha": "t1"}
    _write_identity_snapshots(
        workspace,
        pre={"tablet": {**base_tablet, "versionCode": "322"}, "caleemobile": {}},
        post={"tablet": {**base_tablet, "versionCode": "999"}, "caleemobile": {}},
    )
    result = CliRunner().invoke(
        main,
        ["consolidate", "--run-id", RUN_ID, "--android-optional", "--ios-optional",
         "--calee-build-version", "0.3.22", "--out-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == EXIT_BLOCKED
    assert "Build identity stability (pre/post run): BLOCKED" in result.output
