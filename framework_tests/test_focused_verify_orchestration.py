"""End-to-end orchestration tests for the refined `focused-verify` CLI (this
session): fixture-first preparation, same-run verified context, explicit
dependencies, credential injection/redaction, child-report validation,
bounded supervision wiring, exit-code precedence, plan/preflight modes, and
immutable manifest-referenced summaries. Everything faked -- no device, no
Appium, no network, no real subprocess.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from calee_regression import cli, credentials as credentials_mod, focused_supervision, models

BACKEND = "https://staging.calee.invalid"
EMAIL = "reg-user@example.com"
PASSWORD = "s3cret-keychain-pw"


class FakeChildren:
    """A fake supervised-child runner that writes the reports a real child
    would, driven by per-step behavior overrides."""

    def __init__(self, report_root: Path):
        self.report_root = report_root
        self.calls = []  # (command, env, cwd, timeout)
        self.behavior = {}  # step key -> dict(exit_code=..., report=..., no_report=...)

    def key_for(self, command):
        joined = " ".join(str(c) for c in command)
        if "prepare-fixture" in joined:
            return "fixture"
        if "run-repeat" in joined:
            return "tablet-diagnostic" if "--device-initialization skip" in joined else "tablet-standard"
        if "caleemobile_regression" in joined:
            return "api-2" if "attempt-2" in joined else "api-1"
        if "run_ui_suite.py" in joined:
            return "ios"
        return "unknown"

    def _arg(self, command, flag):
        command = [str(c) for c in command]
        return command[command.index(flag) + 1] if flag in command else None

    def __call__(self, command, *, env=None, cwd=None, timeout_seconds=None):
        self.calls.append((list(map(str, command)), dict(env or {}), cwd, timeout_seconds))
        key = self.key_for(command)
        behavior = self.behavior.get(key, self.behavior.get(key.split("-")[0], {}))
        exit_code = behavior.get("exit_code", 0)
        run_id = self._arg(command, "--run-id") or (env or {}).get("CALEE_RUN_ID")
        if not behavior.get("no_report"):
            report = self._default_report(key, command, env or {}, run_id)
            if report is not None:
                overrides = behavior.get("report", {})
                report.update(overrides)
                path = self._report_path(key, command, run_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(report) + "\n", encoding="utf-8")
        return focused_supervision.SupervisedOutcome(exit_code=exit_code)

    def _report_path(self, key, command, run_id) -> Path:
        if key == "fixture":
            return self.report_root / "reports" / "runs" / run_id / "environment" / "results.json"
        if key.startswith("tablet"):
            mode = "diagnostic" if key.endswith("diagnostic") else "standard"
            return self.report_root / "reports" / "runs" / run_id / "tablet-targeted" / mode / "results.json"
        return Path(self._arg(command, "--report"))

    def _default_report(self, key, command, env, run_id):
        if key.startswith("api"):
            key = "api"
        if key == "fixture":
            return {
                "reportType": "fixture-preparation", "reportSchemaVersion": 1,
                "runId": run_id, "status": "pass",
                "targetEnvironment": env.get("CALEE_API_BASE"),
                "fixtureVersion": "REG-9", "fixtureResetStatus": "ok",
                "fixtureVerificationStatus": "ok",
            }
        if key.startswith("tablet"):
            return {
                "reportType": "tablet-targeted-repeat", "reportSchemaVersion": 1,
                "runId": run_id, "status": "pass",
            }
        if key == "api":
            return {
                "reportType": "mobile-api-suite", "reportSchemaVersion": 1,
                "runId": "mobile-local", "releaseRunId": run_id,
                "releaseId": self._arg(command, "--release-id"),
                "backend": {"requested": self._arg(command, "--base-url")},
                "fixtureVersion": self._arg(command, "--fixture-version"),
                "executionPurpose": self._arg(command, "--execution-purpose"),
                "certificationEligible": False,
                "counts": {"PASS": 5}, "steps": [], "status": "PASS",
            }
        if key == "ios":
            return {
                "reportType": "mobile-ui-file", "reportSchemaVersion": 1,
                "runId": "mobile-ui-local", "releaseRunId": run_id,
                "releaseId": self._arg(command, "--release-id"),
                "backend": {"requested": self._arg(command, "--mobile-backend")},
                "fixtureVersion": self._arg(command, "--fixture-version"),
                "executionPurpose": self._arg(command, "--execution-purpose"),
                "certificationEligible": False,
                "counts": {"PASS": 1}, "steps": [], "status": "PASS",
            }
        return None


@pytest.fixture
def harness(monkeypatch, tmp_path):
    report_root = tmp_path / "reports"
    monkeypatch.setattr(cli, "_load_config_or_exit", lambda p: SimpleNamespace(
        appium_url="http://127.0.0.1:4723/wd/hub", device_initialization_mode="standard",
        udid="TABLET-1", device_name="tablet", app_package="au.com.calee.shell",
        apk_path=None, report_dir=str(tmp_path / "standalone")))
    monkeypatch.setattr(cli, "_resolved_report_root", lambda *a, **k: report_root)
    monkeypatch.setattr(
        cli, "_ensure_appium_for_command",
        lambda cfg, **k: cli.AppiumLifecycleState(True, "started", "u"))
    stops = []
    monkeypatch.setattr(cli.appium_lifecycle, "stop_appium_from_pid_file", lambda p: stops.append(p))

    resolver = credentials_mod.default_resolver(
        environ={}, keychain_runner=lambda argv: (0, PASSWORD if "regression-password" in argv else EMAIL))
    monkeypatch.setattr(
        cli, "_fill_credentials_from_providers",
        lambda e, p: (resolver.get(credentials_mod.REGRESSION_USERNAME),
                      resolver.get(credentials_mod.REGRESSION_PASSWORD), resolver))
    monkeypatch.setenv("CALEE_API_BASE", BACKEND)
    monkeypatch.delenv("CALEE_UI_DEVICE_ID", raising=False)

    children = FakeChildren(report_root)
    monkeypatch.setattr(cli, "_supervised_runner", children)

    # A CaleeMobile-Regression checkout shape for the static validation.
    mobile = tmp_path / "CaleeMobile-Regression"
    (mobile / "api" / "caleemobile_regression").mkdir(parents=True)
    (mobile / "ui").mkdir(parents=True)
    (mobile / "ui" / "run_ui_suite.py").write_text("# stub\n")

    def invoke(*extra_args):
        return CliRunner().invoke(cli.main, [
            "focused-verify", "--config", "x",
            "--mobile-regression-repo", str(mobile), *extra_args,
        ])

    return SimpleNamespace(
        invoke=invoke, children=children, stops=stops, report_root=report_root, mobile=mobile)


def _summary(harness):
    paths = list(harness.report_root.glob("reports/runs/*/focused-verify/*/summary.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text()), paths[0]


# ── happy path ─────────────────────────────────────────────────────────────
def test_full_pass_binds_context_and_validates_reports(harness):
    result = harness.invoke()
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    summary, summary_path = _summary(harness)
    assert summary["status"] == "pass"
    assert summary["certificationEligible"] is False
    assert summary["verifiedBackend"] == BACKEND
    assert summary["fixtureVersion"] == "REG-9"
    assert summary["credentialSource"] == {
        "regression_username": "keychain", "regression_password": "keychain"}
    # every step validated + digest-bound
    by_id = {s["id"]: s for s in summary["steps"]}
    assert set(by_id) == {"fixture", "tablet-standard", "tablet-diagnostic", "api-1", "api-2", "ios"}
    for step in by_id.values():
        assert step["status"] == "pass"
        assert step["reportSha256"], step
    # Appium stopped exactly once, at the very end
    assert len(harness.stops) == 1
    # summary is read-only on disk
    assert not (summary_path.stat().st_mode & stat.S_IWUSR)


def test_children_get_credentials_via_env_never_argv(harness):
    result = harness.invoke()
    assert result.exit_code == 0, result.output
    for command, env, _cwd, _timeout in harness.children.calls:
        joined = " ".join(command)
        assert PASSWORD not in joined and EMAIL not in joined
        assert env.get("CALEE_TEST_EMAIL") == EMAIL
        assert env.get("CALEE_TEST_PASSWORD") == PASSWORD
    # and never in any report/summary text
    summary_text = _summary(harness)[1].read_text()
    assert PASSWORD not in summary_text and EMAIL not in summary_text


def test_api_and_ios_commands_carry_full_explicit_context(harness):
    harness.invoke()
    api_calls = [c for c, *_ in harness.children.calls if harness.children.key_for(c).startswith("api")]
    assert len(api_calls) == 2
    for command in api_calls:
        joined = " ".join(command)
        assert "--require-explicit-context" in joined
        assert f"--base-url {BACKEND}" in joined
        assert "--fixture-version REG-9" in joined
        assert "--execution-purpose focused-post-fix-verification" in joined
    ios = [c for c, *_ in harness.children.calls if harness.children.key_for(c) == "ios"][0]
    joined = " ".join(ios)
    assert f"--expected-backend {BACKEND}" in joined
    assert f"--mobile-backend {BACKEND}" in joined
    assert "--fixture-status ok" in joined
    assert "--execution-purpose focused-environment-check" in joined
    assert "--release-run-id" in joined and "--release-id" in joined
    assert "--no-handoff-gate" not in joined


def test_manifest_preserves_every_focused_invocation(harness):
    harness.invoke()
    harness.invoke()
    manifests = list(harness.report_root.glob("reports/runs/*/run-manifest.json"))
    assert len(manifests) == 2  # fresh run id per invocation, each preserved
    for path in manifests:
        manifest = json.loads(path.read_text())
        attempts = manifest["componentAttempts"]["focused-verify"]
        assert len(attempts) == 1 and attempts[0]["invocationId"]


# ── dependency gating ──────────────────────────────────────────────────────
def test_fixture_failure_blocks_all_dependents_without_running_them(harness):
    harness.children.behavior["fixture"] = {"exit_code": 3, "no_report": True}
    result = harness.invoke()
    assert result.exit_code == models.EXIT_BLOCKED
    keys = [harness.children.key_for(c) for c, *_ in harness.children.calls]
    assert keys == ["fixture"]  # nothing else was started
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    for dependent in ("tablet-standard", "tablet-diagnostic", "api-1", "api-2", "ios"):
        assert by_id[dependent]["status"] == "blocked_not_run"
        assert by_id[dependent]["blockedBy"] == "fixture"
        assert "fixture" in by_id[dependent]["detail"]


def test_appium_unavailable_still_runs_fixture_api_and_ios(harness, monkeypatch):
    monkeypatch.setattr(
        cli, "_ensure_appium_for_command",
        lambda cfg, **k: cli.AppiumLifecycleState(False, "unavailable", "u"))
    result = harness.invoke()
    keys = [harness.children.key_for(c) for c, *_ in harness.children.calls]
    assert keys == ["fixture", "api-1", "api-2", "ios"]
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["tablet-standard"]["status"] == "blocked"
    assert by_id["api-1"]["status"] == "pass"
    assert by_id["ios"]["status"] == "pass"
    assert result.exit_code == models.EXIT_BLOCKED  # tablet blockers stay visible


def test_api_attempt_1_failure_never_suppresses_attempt_2(harness):
    harness.children.behavior["api-1"] = {
        "exit_code": 1, "report": {"status": "FAIL", "counts": {"FAIL": 1}}}
    result = harness.invoke()
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["api-1"]["status"] == "fail"
    assert by_id["api-2"]["status"] == "pass"
    assert result.exit_code == models.EXIT_REGRESSION  # product FAIL stays visible


# ── report validation + exit precedence ────────────────────────────────────
def test_missing_report_after_exit_0_blocks(harness):
    harness.children.behavior["api"] = {"exit_code": 0, "no_report": True}
    result = harness.invoke()
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["api-1"]["status"] == "blocked"
    assert "does not exist" in " ".join(by_id["api-1"]["validationProblems"])
    assert result.exit_code == models.EXIT_BLOCKED


def test_exit_report_disagreement_blocks(harness):
    harness.children.behavior["ios"] = {"exit_code": 0, "report": {"status": "FAIL"}}
    harness.invoke()
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["ios"]["status"] == "blocked"
    assert any("disagreement" in p for p in by_id["ios"]["validationProblems"])


def test_run_id_mismatch_blocks(harness):
    harness.children.behavior["api"] = {"report": {"releaseRunId": "some-other-run"}}
    harness.invoke()
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["api-1"]["status"] == "blocked"


def test_certification_claim_blocks(harness):
    harness.children.behavior["api"] = {"report": {"certificationEligible": True}}
    harness.invoke()
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["api-1"]["status"] == "blocked"


def test_child_exit_2_stays_invalid_config_and_aggregate_exits_2(harness):
    harness.children.behavior["api"] = {"exit_code": 2, "no_report": True}
    result = harness.invoke()
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["api-1"]["status"] == "invalid_config"
    assert result.exit_code == models.EXIT_INVALID_CONFIG


# ── static validation / plan / preflight ───────────────────────────────────
def test_unsupported_api_suite_is_invalid_invocation(harness):
    result = harness.invoke("--api-suite", "renamed-suite")
    assert result.exit_code == models.EXIT_INVALID_CONFIG
    assert harness.children.calls == []  # nothing started


def test_bad_step_timeout_is_invalid_invocation(harness):
    result = harness.invoke("--step-timeout", "tablet=zero")
    assert result.exit_code == models.EXIT_INVALID_CONFIG
    assert harness.children.calls == []


def test_plan_mode_prints_plan_without_running_anything(harness):
    result = harness.invoke("--plan")
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    assert harness.children.calls == []
    plan = json.loads(result.output[result.output.index("{"):])
    assert plan["dependencies"]["ios"] == ["fixture"]
    assert PASSWORD not in result.output
    assert "summary" in plan["reportDestinations"]


def test_preflight_only_validates_without_mutating(harness):
    result = harness.invoke("--preflight-only")
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    assert harness.children.calls == []  # no fixture reset, no product test
    assert "no fixture reset" in result.output.lower()


def test_missing_backend_blocks_before_any_mutation(harness, monkeypatch):
    monkeypatch.delenv("CALEE_API_BASE")
    result = harness.invoke()
    assert result.exit_code == models.EXIT_BLOCKED
    assert harness.children.calls == []
    assert "production default" in result.output


# ── fixture ownership lock (fixture_ownership.py integration) ──────────────
def _lock_scope():
    from calee_regression import fixture_ownership as fo

    return fo.LockScope(
        backend=BACKEND, account_fingerprint=fo.account_fingerprint(EMAIL),
        fixture_version="unknown",
    )


def test_summary_records_fixture_ownership_acquired_and_released(harness):
    result = harness.invoke()
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    summary, _ = _summary(harness)
    ownership = summary["fixtureOwnership"]
    assert ownership["exclusivityScope"] == "host-local"
    assert "cross-host" in ownership["limitation"].lower()
    assert ownership["acquisition"]["state"] == "acquired"
    assert ownership["acquisition"]["owner"]["runId"] == summary["runId"]
    assert ownership["release"]["state"] == "released"
    # the lock is gone after the run
    from calee_regression import fixture_ownership as fo

    lock_root = harness.report_root / "reports" / "locks"
    assert fo.status(lock_root, _lock_scope()).state == fo.STATE_NOT_HELD
    # and the fingerprint never exposes the email
    assert EMAIL not in json.dumps(ownership)


def test_active_owner_lock_blocks_before_any_mutation(harness):
    from calee_regression import fixture_ownership as fo

    lock_root = harness.report_root / "reports" / "locks"
    held = fo.acquire(lock_root, _lock_scope(), run_id="other-run", now=lambda: "t")
    assert held.state == fo.STATE_ACQUIRED  # this pid is alive -> active owner
    result = harness.invoke()
    assert result.exit_code == models.EXIT_BLOCKED
    assert "active_owner" in result.output
    assert harness.children.calls == []  # nothing product-mutating ran
    assert not list(harness.report_root.glob("reports/runs/*/focused-verify/*/summary.json"))
    # the other run's lock is untouched
    assert fo.status(lock_root, _lock_scope()).owner["runId"] == "other-run"


def test_stale_lock_blocks_and_names_the_recovery_command(harness, monkeypatch):
    from calee_regression import fixture_ownership as fo

    lock_root = harness.report_root / "reports" / "locks"
    fo.acquire(lock_root, _lock_scope(), run_id="dead-run", pid=2, now=lambda: "t")
    monkeypatch.setattr(fo, "default_pid_alive", lambda pid: False)
    result = harness.invoke()
    assert result.exit_code == models.EXIT_BLOCKED
    assert "stale_lock" in result.output
    assert "fixture-lock recover-stale" in result.output
    assert harness.children.calls == []


# ── installed-artifact identity attestation (installed_artifact.py) ────────
def test_summary_records_unproven_attestation_without_blocking(harness):
    # The harness cfg has no apk_path -> attestation is unproven; focused-verify
    # is non-certifying, so every step still runs.
    result = harness.invoke()
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    summary, _ = _summary(harness)
    attestation = summary["installedArtifactIdentity"]
    assert attestation["status"] == "unproven"
    assert attestation["reason"]
    by_id = {s["id"]: s for s in summary["steps"]}
    assert by_id["tablet-standard"]["status"] == "pass"


def test_identity_mismatch_blocks_tablet_steps_but_not_api_or_ios(harness, monkeypatch):
    from calee_regression import installed_artifact as ia

    monkeypatch.setattr(
        cli, "_reconcile_installed_artifact",
        lambda cfg, repo: ia.ReconcileResult(
            status=ia.STATUS_MISMATCH, mismatched_fields=["versionCode"],
            expected={"versionCode": "25"}, installed={"versionCode": "24"},
            reason="installed app identity differs from the configured APK.",
        ).to_dict(),
    )
    result = harness.invoke()
    keys = [harness.children.key_for(c) for c, *_ in harness.children.calls]
    assert keys == ["fixture", "api-1", "api-2", "ios"]  # tablet never started
    summary, _ = _summary(harness)
    by_id = {s["id"]: s for s in summary["steps"]}
    for step_id in ("tablet-standard", "tablet-diagnostic"):
        assert by_id[step_id]["status"] == "blocked"
        assert by_id[step_id]["blockedBy"] == "installed-artifact-identity"
        assert "versionCode" in by_id[step_id]["detail"]
    assert by_id["api-1"]["status"] == "pass"
    assert summary["installedArtifactIdentity"]["mismatchedFields"] == ["versionCode"]
    assert result.exit_code == models.EXIT_BLOCKED


# ── plain-language summary companion (focused_human_summary.py) ────────────
def test_summary_txt_written_alongside_machine_summary(harness):
    result = harness.invoke()
    assert result.exit_code == models.EXIT_SUCCESS, result.output
    summary, summary_path = _summary(harness)
    txt_path = summary_path.parent / "summary.txt"
    assert txt_path.is_file()
    assert str(txt_path) in result.output
    text = txt_path.read_text()
    from calee_regression import focused_human_summary

    assert text == focused_human_summary.render(json.loads(summary_path.read_text()))
    assert focused_human_summary.NON_CERTIFICATION_STATEMENT in text
    for step in summary["steps"]:
        assert step["title"] in text
    assert PASSWORD not in text and EMAIL not in text
    assert not (txt_path.stat().st_mode & stat.S_IWUSR)  # immutable like the JSON


# ── safe resume (--resume-run-id, focused_resume.py) ───────────────────────
@pytest.fixture
def resume_harness(harness, monkeypatch):
    """The orchestration harness with a fully verifiable identity: stable git
    SHAs and a VERIFIED installed-artifact attestation, so every resume
    criterion can be positively verified across invocations."""
    monkeypatch.setattr(cli, "_repo_head_sha", lambda path: "fake-sha")
    monkeypatch.setattr(cli, "_repo_dirty", lambda path: False)
    monkeypatch.setattr(
        cli, "_reconcile_installed_artifact",
        lambda cfg, repo: {"status": "verified",
                           "installed": {"versionName": "2.5.0", "versionCode": "25"}})
    return harness


def _run_id(harness):
    runs = list(harness.report_root.glob("reports/runs/*"))
    assert len(runs) == 1
    return runs[0].name


def _summaries(harness):
    paths = sorted(
        harness.report_root.glob("reports/runs/*/focused-verify/*/summary.json"),
        key=lambda p: p.parent.name)
    return [(json.loads(p.read_text()), p) for p in paths]


def test_resume_reuses_every_prior_pass_without_rerunning(resume_harness):
    first = resume_harness.invoke()
    assert first.exit_code == models.EXIT_SUCCESS, first.output
    run_id = _run_id(resume_harness)
    first_summary, first_path = _summary(resume_harness)
    resume_harness.children.calls.clear()
    second = resume_harness.invoke("--resume-run-id", run_id)
    assert second.exit_code == models.EXIT_SUCCESS, second.output
    assert resume_harness.children.calls == []  # nothing re-executed, not even fixture
    summaries = _summaries(resume_harness)
    assert len(summaries) == 2  # SAME run id, NEW invocation dir
    resumed, resumed_path = summaries[-1]
    assert resumed["runId"] == run_id
    assert resumed_path.parent != first_path.parent
    by_id = {s["id"]: s for s in resumed["steps"]}
    for step in by_id.values():
        assert step["evidence"] == "reused", step
        assert step["status"] == "pass"
    # reused steps reference the ORIGINAL reports by path + digest
    first_by_id = {s["id"]: s for s in first_summary["steps"]}
    for step_id in ("tablet-standard", "api-1", "ios"):
        assert by_id[step_id]["reportPath"] == first_by_id[step_id]["reportPath"]
        assert by_id[step_id]["reportSha256"] == first_by_id[step_id]["reportSha256"]
    assert resumed["resume"]["resumedRunId"] == run_id
    assert resumed["resume"]["executedSteps"] == []
    # prior evidence is untouched
    assert json.loads(first_path.read_text()) == first_summary


def test_resume_refuses_when_identity_cannot_be_verified(resume_harness, monkeypatch):
    first = resume_harness.invoke()
    assert first.exit_code == models.EXIT_SUCCESS, first.output
    run_id = _run_id(resume_harness)
    # the installed artifact can no longer be positively verified
    monkeypatch.setattr(
        cli, "_reconcile_installed_artifact",
        lambda cfg, repo: {"status": "unproven", "reason": "adb unavailable"})
    resume_harness.children.calls.clear()
    result = resume_harness.invoke("--resume-run-id", run_id)
    assert result.exit_code == models.EXIT_BLOCKED
    assert "installed build identity" in result.output
    assert "focused-verify --config config/tester.local.yaml" in result.output
    assert "<" not in result.output.split("focused-verify --config")[1].splitlines()[0]
    assert resume_harness.children.calls == []  # nothing ran
    assert len(_summaries(resume_harness)) == 1  # no new summary was written


def test_resume_retains_prior_fail_unless_retry_failed(resume_harness):
    resume_harness.children.behavior["api-1"] = {
        "exit_code": 1, "report": {"status": "FAIL", "counts": {"FAIL": 1}}}
    first = resume_harness.invoke()
    assert first.exit_code == models.EXIT_REGRESSION
    run_id = _run_id(resume_harness)
    resume_harness.children.calls.clear()
    second = resume_harness.invoke("--resume-run-id", run_id)
    assert second.exit_code == models.EXIT_REGRESSION  # retained FAIL stays visible
    assert resume_harness.children.calls == []  # NOT rerun by default
    resumed, _ = _summaries(resume_harness)[-1]
    by_id = {s["id"]: s for s in resumed["steps"]}
    assert by_id["api-1"]["status"] == "fail"
    assert by_id["api-1"]["evidence"] == "reused"
    assert "never automatically rerun" in by_id["api-1"]["detail"]
    # --retry-failed reruns it as a NEW attempt (old evidence preserved)
    resume_harness.children.behavior["api-1"] = {}
    third = resume_harness.invoke("--resume-run-id", run_id, "--retry-failed")
    assert third.exit_code == models.EXIT_SUCCESS, third.output
    keys = [resume_harness.children.key_for(c) for c, *_ in resume_harness.children.calls]
    assert keys == ["api-1"]
    retried, _ = _summaries(resume_harness)[-1]
    by_id = {s["id"]: s for s in retried["steps"]}
    assert by_id["api-1"]["status"] == "pass"
    assert by_id["api-1"]["evidence"] == "executed"


def test_resume_reexecutes_blocked_steps_only(resume_harness, monkeypatch):
    monkeypatch.setattr(
        cli, "_ensure_appium_for_command",
        lambda cfg, **k: cli.AppiumLifecycleState(False, "unavailable", "u"))
    first = resume_harness.invoke()
    assert first.exit_code == models.EXIT_BLOCKED  # tablet steps blocked
    run_id = _run_id(resume_harness)
    monkeypatch.setattr(
        cli, "_ensure_appium_for_command",
        lambda cfg, **k: cli.AppiumLifecycleState(True, "started", "u"))
    resume_harness.children.calls.clear()
    second = resume_harness.invoke("--resume-run-id", run_id)
    assert second.exit_code == models.EXIT_SUCCESS, second.output
    keys = sorted(resume_harness.children.key_for(c) for c, *_ in resume_harness.children.calls)
    assert keys == ["tablet-diagnostic", "tablet-standard"]  # only the blocked steps
    resumed, _ = _summaries(resume_harness)[-1]
    by_id = {s["id"]: s for s in resumed["steps"]}
    assert by_id["tablet-standard"]["evidence"] == "executed"
    assert by_id["api-1"]["evidence"] == "reused"


def test_resume_without_prior_summary_refuses(resume_harness):
    result = resume_harness.invoke("--resume-run-id", "run-that-never-existed")
    assert result.exit_code == models.EXIT_BLOCKED
    assert "no prior focused-verify summary" in result.output
    assert resume_harness.children.calls == []


def test_invalid_resume_options_are_invalid_invocation(resume_harness):
    result = resume_harness.invoke("--resume-run-id", "bad id!")
    assert result.exit_code == models.EXIT_INVALID_CONFIG
    result = resume_harness.invoke("--retry-failed")
    assert result.exit_code == models.EXIT_INVALID_CONFIG
    assert resume_harness.children.calls == []
