"""Offline tests for resume_release.py -- resuming a blocked release
qualification without repeating already-passed destructive/disruptive steps.

See docs/RELEASE_POLICY.md's "Resuming a blocked run" section for the policy
under test. Every test here is fully offline: no real adb/device/Appium/
network is ever touched -- a fake AdbRunner (mirroring release_installer's
own FakeAdb test convention) stands in for the tablet, and Prepare
re-execution is injected via `prepare_runner` rather than shelling out.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from calee_regression import consolidated_report as cr
from calee_regression import release_candidate as release_candidate_mod
from calee_regression import release_installer as ri
from calee_regression import resume_release as rr
from calee_regression import run_context
from calee_regression.models import EXIT_BLOCKED, EXIT_REGRESSION, EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


RUN_ID = "release-resume-test-000001"


def _workspace(tmp_path: Path, run_id: str = RUN_ID) -> run_context.RunWorkspace:
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()
    return workspace


def _manifest(workspace: run_context.RunWorkspace, **kwargs) -> run_context.RunManifest:
    manifest = run_context.RunManifest(
        run_id=workspace.run_id, started_at=kwargs.pop("started_at", "2026-07-20 08:00:00"), **kwargs
    )
    manifest.write(workspace.manifest_path)
    return manifest


def _release_config_report(
    *,
    run_id: str = RUN_ID,
    release_id: str = "r1",
    schema_version: int = 2,
    backend: str = "https://hub-dev.calee.com.au",
    profile: str = "staging",
    platforms=("tablet",),
    features=(),
    calee_app: "dict | None" = None,
    caleeshell_app: "dict | None" = None,
    caleemobile: "dict | None" = None,
    release_config_digest: str = "sha256:" + "a" * 64,
    fingerprint: "dict | None" = None,
) -> dict:
    calee_app = calee_app if calee_app is not None else {
        "applicationId": "com.viso.calee", "versionName": "1.0.0", "versionCode": "100",
        "signerSha256": "s" * 64, "gitSha": "c" * 40,
    }
    caleeshell_app = caleeshell_app if caleeshell_app is not None else {}
    caleemobile = caleemobile if caleemobile is not None else {
        "gitSha": "m" * 40, "buildVersion": "0.0.1+1",
        "selectorEvidenceRequired": True, "distributedBuildAcceptanceRequired": False,
    }
    report = {
        "runId": run_id,
        "status": "ok",
        "schemaVersion": schema_version,
        "releaseId": release_id,
        "releaseConfigDigest": release_config_digest,
        "releaseSelections": {
            "selectedBackend": backend,
            "enabledPlatforms": list(platforms),
            "enabledFeatures": list(features),
            "profile": profile,
            "distributedBuildRequired": False,
            "expectedIdentities": {"calee": calee_app, "caleeShell": caleeshell_app, "caleeMobile": caleemobile},
        },
    }
    if fingerprint is not None:
        report["releaseCandidateFingerprint"] = fingerprint
    return report


def _write_component(workspace: run_context.RunWorkspace, component: str, payload: dict) -> Path:
    path = workspace.component_report_path(component)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _passing_installation_report(*, run_id: str = RUN_ID, release_id: str = "r1", tablet_identity=None) -> dict:
    tablet_identity = tablet_identity if tablet_identity is not None else {
        "configuredTransport": "TAB1", "serialno": "SERIAL1", "manufacturer": "Google",
        "model": "Pixel Tablet", "product": "tangorpro", "transportType": "usb",
        "wirelessHost": None, "wirelessPort": None,
    }
    return {
        "runId": run_id, "status": "ok", "releaseId": release_id,
        "tabletStableIdentity": tablet_identity,
        "execution": {"installed": [
            {"packageId": "com.viso.calee", "present": True, "versionName": "1.0.0", "versionCode": "100"},
        ]},
    }


def _fake_adb(*, serialno="SERIAL1", manufacturer="Google", model="Pixel Tablet", product="tangorpro",
              calee_version_name="1.0.0", calee_version_code="100", device_present=True):
    """A scriptable fake AdbRunner -- mirrors release_installer's own FakeAdb
    test convention (framework_tests/test_release_installer.py)."""
    def runner(argv):
        if "getprop" in argv:
            if not device_present:
                return ri.AdbResult(1, "", "error: no devices/emulators found")
            return ri.AdbResult(
                0,
                f"[ro.serialno]: [{serialno}]\n[ro.product.manufacturer]: [{manufacturer}]\n"
                f"[ro.product.model]: [{model}]\n[ro.build.product]: [{product}]\n",
            )
        if "get-state" in argv:
            return ri.AdbResult(0, "device\n") if device_present else ri.AdbResult(1, "", "error: no devices/emulators found")
        if "dumpsys" in argv and "com.viso.calee" in argv and "caleeshell" not in " ".join(argv):
            return ri.AdbResult(0, f"versionName={calee_version_name}\nversionCode={calee_version_code}")
        if "dumpsys" in argv:
            return ri.AdbResult(0, "")
        if "resolve-activity" in argv:
            return ri.AdbResult(0, "packageName=com.viso.caleeshell")
        return ri.AdbResult(0, "")
    return runner


def _pass_prepare():
    return rr.PrepareOutcome(status="pass", exit_code=EXIT_SUCCESS, detail=["ready"])


def _blocked_prepare():
    return rr.PrepareOutcome(status="blocked", exit_code=EXIT_BLOCKED, detail=["fixture unreachable"])


# ---------------------------------------------------------------------------
# ImmutableInputs: collection, digest, round-trip
# ---------------------------------------------------------------------------


class TestImmutableInputs:
    def test_collect_from_release_config_report(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report())
        inputs = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        assert inputs.release_id == "r1"
        assert inputs.release_manifest_schema_version == 2
        assert inputs.target_backend == "https://hub-dev.calee.com.au"
        assert inputs.release_profile == "staging"
        assert inputs.platform_scope == ["tablet"]
        assert inputs.expected_package_ids["calee"] == "com.viso.calee"
        assert inputs.expected_version_codes["calee"] == "100"
        assert inputs.expected_signer_fingerprints["calee"] == "s" * 64
        assert inputs.expected_git_shas["calee"] == "c" * 40
        assert inputs.caleemobile_expected_sha == "m" * 40
        assert inputs.selector_evidence_required is True
        assert inputs.distributed_build_evidence_required is False

    def test_missing_release_config_is_unavailable_not_a_crash(self, tmp_path):
        workspace = _workspace(tmp_path)
        inputs = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        assert inputs.release_id is None
        assert "releaseConfig" in inputs.unavailable_fields

    def test_digest_is_stable_and_order_independent(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report())
        a = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        b = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        assert a.digest() == b.digest()

    def test_digest_changes_when_release_id_changes(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report(release_id="r1"))
        a = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        _write_component(workspace, "release-config", _release_config_report(release_id="r2"))
        b = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        assert a.digest() != b.digest()

    def test_round_trip_to_dict_from_dict(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report())
        original = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        restored = rr.ImmutableInputs.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()

    def test_tablet_identity_from_live_probe_overrides_installation_report(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        adb = _fake_adb(serialno="LIVE-SERIAL")
        inputs = rr.collect_immutable_inputs(workspace, repo_root=tmp_path, adb_runner=adb, tablet_serial="TAB1")
        assert inputs.tablet_stable_identity["serialno"] == "LIVE-SERIAL"

    def test_tablet_identity_falls_back_to_installation_report_without_adb(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        inputs = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        assert inputs.tablet_stable_identity["serialno"] == "SERIAL1"

    def test_manual_check_definition_version_from_config_file(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "manual-checks.json").write_text('{"checks": []}')
        workspace = _workspace(tmp_path)
        inputs = rr.collect_immutable_inputs(workspace, repo_root=tmp_path)
        assert inputs.manual_check_definition_version is not None
        assert inputs.manual_check_definition_version.startswith("sha256:")


# ---------------------------------------------------------------------------
# diff_immutable_inputs: every enumerated immutable field, individually
# ---------------------------------------------------------------------------


class TestDiffImmutableInputs:
    def _baseline(self):
        return rr.ImmutableInputs(
            release_id="r1", release_manifest_schema_version=2, candidate_fingerprint_digest="fp1",
            apk_sha256={"calee": "aa"}, expected_package_ids={"calee": "com.viso.calee"},
            expected_version_names={"calee": "1.0.0"}, expected_version_codes={"calee": "100"},
            expected_signer_fingerprints={"calee": "s" * 64}, expected_git_shas={"calee": "c" * 40},
            release_config_digest="sha256:cfg1", target_backend="https://hub-dev", release_profile="staging",
            platform_scope=["tablet"], feature_scope=["sync"], regression_sha="r" * 40,
            caleemobile_regression_sha="cr" * 20, caleemobile_expected_sha="cm" * 20,
            caleemobile_expected_version="0.0.1+1", manual_check_definition_version="sha256:mc1",
            selector_evidence_required=True, distributed_build_evidence_required=False,
        )

    def test_identical_inputs_have_no_mismatches(self):
        baseline = self._baseline()
        assert rr.diff_immutable_inputs(baseline, self._baseline()) == []

    @pytest.mark.parametrize("field_name,new_value", [
        ("release_id", "r2"),
        ("release_manifest_schema_version", 1),
        ("candidate_fingerprint_digest", "fp2-DIFFERENT"),
        ("release_config_digest", "sha256:cfg2-DIFFERENT"),
        ("target_backend", "https://hub-prod"),
        ("release_profile", "production"),
        ("regression_sha", "z" * 40),
        ("caleemobile_regression_sha", "zz" * 20),
        ("caleemobile_expected_sha", "zzz" * 20),
        ("caleemobile_expected_version", "0.0.2+2"),
        ("manual_check_definition_version", "sha256:mc2-DIFFERENT"),
        ("selector_evidence_required", False),
        ("distributed_build_evidence_required", True),
    ])
    def test_scalar_field_mismatch_is_reported(self, field_name, new_value):
        baseline = self._baseline()
        current = self._baseline()
        setattr(current, field_name, new_value)
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any(field_name.replace("_", "") for _ in [1]), "sanity"
        assert problems, f"expected a mismatch for {field_name}"

    def test_platform_scope_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.platform_scope = ["tablet", "android"]
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("platformScope" in p for p in problems)

    def test_feature_scope_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.feature_scope = ["sync", "meals"]
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("featureScope" in p for p in problems)

    def test_apk_sha256_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.apk_sha256 = {"calee": "bb-DIFFERENT"}
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("apkSha256" in p for p in problems)

    def test_expected_package_id_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.expected_package_ids = {"calee": "com.viso.other"}
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("expectedPackageIds" in p for p in problems)

    def test_expected_version_name_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.expected_version_names = {"calee": "2.0.0"}
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("expectedVersionNames" in p for p in problems)

    def test_expected_version_code_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.expected_version_codes = {"calee": "200"}
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("expectedVersionCodes" in p for p in problems)

    def test_expected_signer_fingerprint_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.expected_signer_fingerprints = {"calee": "d" * 64}
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("expectedSignerFingerprints" in p for p in problems)

    def test_expected_git_sha_mismatch_is_reported(self):
        baseline = self._baseline()
        current = self._baseline()
        current.expected_git_shas = {"calee": "e" * 40}
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("expectedGitShas" in p for p in problems)

    def test_tablet_identity_mismatch_when_both_sides_known(self):
        baseline = self._baseline()
        baseline.tablet_stable_identity = {"serialno": "SERIAL1", "manufacturer": "Google", "model": "Pixel", "product": "p"}
        current = self._baseline()
        current.tablet_stable_identity = {"serialno": "SERIAL2-DIFFERENT", "manufacturer": "Google", "model": "Pixel", "product": "p"}
        problems = rr.diff_immutable_inputs(baseline, current)
        assert any("tabletStableIdentity" in p for p in problems)

    def test_tablet_identity_unavailable_on_one_side_is_not_a_mismatch(self):
        baseline = self._baseline()
        baseline.tablet_stable_identity = {"serialno": "SERIAL1", "manufacturer": "Google", "model": "Pixel", "product": "p"}
        current = self._baseline()
        current.tablet_stable_identity = None  # no device attached this invocation
        problems = rr.diff_immutable_inputs(baseline, current)
        assert problems == []

    def test_both_none_never_mismatches(self):
        baseline = rr.ImmutableInputs()
        current = rr.ImmutableInputs()
        assert rr.diff_immutable_inputs(baseline, current) == []


# ---------------------------------------------------------------------------
# evaluate_component_reuse: the generic per-component reuse policy
# ---------------------------------------------------------------------------


class TestEvaluateComponentReuse:
    def _evaluate(self, workspace, component, *, release_id="r1", current_fixture_version=None):
        return rr.evaluate_component_reuse(
            component, workspace=workspace, run_id=RUN_ID, release_id=release_id,
            baseline_digest="sha256:baseline", run_started_at_epoch=None,
            current_fixture_version=current_fixture_version,
        )

    def test_no_report_is_execute(self, tmp_path):
        workspace = _workspace(tmp_path)
        decision = self._evaluate(workspace, "tablet")
        assert decision.decision == rr.DECISION_EXECUTE

    def test_malformed_json_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        path = workspace.component_report_path("tablet")
        path.write_text("{not json", encoding="utf-8")
        decision = self._evaluate(workspace, "tablet")
        assert decision.decision == rr.DECISION_REFUSED
        assert "malformed" in decision.reason or "stale" in decision.reason

    def test_report_not_a_json_object_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        path = workspace.component_report_path("tablet")
        path.write_text("[1, 2, 3]", encoding="utf-8")
        decision = self._evaluate(workspace, "tablet")
        assert decision.decision == rr.DECISION_REFUSED

    def test_wrong_run_id_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report(run_id="a-DIFFERENT-run"))
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REFUSED
        assert "run" in decision.reason.lower()

    def test_missing_run_id_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        payload = _release_config_report()
        del payload["runId"]
        _write_component(workspace, "release-config", payload)
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REFUSED

    def test_stale_report_predating_run_start_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        path = _write_component(workspace, "release-config", _release_config_report())
        old_time = time.time() - 3600
        os.utime(path, (old_time, old_time))
        run_started_at_epoch = time.time()
        decision = rr.evaluate_component_reuse(
            "release-config", workspace=workspace, run_id=RUN_ID, release_id="r1",
            baseline_digest="sha256:baseline", run_started_at_epoch=run_started_at_epoch,
        )
        assert decision.decision == rr.DECISION_REFUSED

    def test_release_id_mismatch_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report(release_id="r1"))
        decision = self._evaluate(workspace, "release-config", release_id="r2-DIFFERENT")
        assert decision.decision == rr.DECISION_REFUSED
        assert "release" in decision.reason.lower()

    def test_prior_fail_is_never_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "mobile-api", {
            "runId": RUN_ID, "passed_count": 3, "failed_count": 1, "blocked_count": 0, "mandatory_skipped_count": 0,
        })
        decision = self._evaluate(workspace, "mobile-api")
        assert decision.decision == rr.DECISION_REFUSED
        assert "FAIL" in decision.reason

    def test_prior_blocked_is_never_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "environment", {"runId": RUN_ID, "status": "blocked", "detail": ["x"]})
        decision = self._evaluate(workspace, "environment")
        assert decision.decision == rr.DECISION_REFUSED
        assert "BLOCKED" in decision.reason

    def test_not_run_is_executed(self, tmp_path):
        workspace = _workspace(tmp_path)
        decision = self._evaluate(workspace, "tablet")
        assert decision.decision == rr.DECISION_EXECUTE

    def test_mandatory_skip_cannot_satisfy_release_never_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        # Every scenario mandatory and skipped -> mandatory_skipped_count > 0
        # -> folded into "blocked" by decide_status, exactly like BLOCKED.
        _write_component(workspace, "tablet", {
            "runId": RUN_ID, "passed_count": 0, "failed_count": 0, "blocked_count": 0, "mandatory_skipped_count": 2,
        })
        decision = self._evaluate(workspace, "tablet")
        assert decision.decision == rr.DECISION_REFUSED

    def test_optional_skip_with_passes_is_still_reusable(self, tmp_path):
        workspace = _workspace(tmp_path)
        # Optional skips don't count toward mandatory_skipped_count -- a
        # scenario suite that otherwise all-passed is still a real PASS.
        _write_component(workspace, "tablet", {
            "runId": RUN_ID, "passed_count": 5, "failed_count": 0, "blocked_count": 0, "mandatory_skipped_count": 0,
        })
        decision = self._evaluate(workspace, "tablet")
        assert decision.decision == rr.DECISION_REUSE

    def test_manual_checks_all_passed_is_reusable(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "manual-checks", {
            "runId": RUN_ID, "checks": [
                {"title": "Calendar shows today", "status": "pass", "mandatory": True},
                {"title": "Optional feature", "status": "pass", "mandatory": False},
            ],
        })
        decision = self._evaluate(workspace, "manual-checks")
        assert decision.decision == rr.DECISION_REUSE

    def test_manual_checks_mandatory_unanswered_is_never_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "manual-checks", {
            "runId": RUN_ID, "checks": [{"title": "Calendar shows today", "status": None, "mandatory": True}],
        })
        decision = self._evaluate(workspace, "manual-checks")
        assert decision.decision == rr.DECISION_REFUSED

    def test_valid_pass_is_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report())
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REUSE
        assert decision.status == cr.STATUS_PASS

    def test_selector_contract_pass_is_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "selector-contract", {"runId": RUN_ID, "status": "ok"})
        decision = self._evaluate(workspace, "selector-contract")
        assert decision.decision == rr.DECISION_REUSE

    def test_mobile_api_pass_is_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "mobile-api", {
            "runId": RUN_ID, "passed_count": 10, "failed_count": 0, "blocked_count": 0, "mandatory_skipped_count": 0,
        })
        decision = self._evaluate(workspace, "mobile-api")
        assert decision.decision == rr.DECISION_REUSE

    def test_recorded_input_digest_mismatch_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        payload = _release_config_report()
        payload["resumeInputDigest"] = "sha256:some-other-digest-entirely"
        _write_component(workspace, "release-config", payload)
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REFUSED
        assert "input digest" in decision.reason

    def test_recorded_input_digest_match_is_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        expected_digest = rr.component_input_digest("release-config", "sha256:baseline")
        payload = _release_config_report()
        payload["resumeInputDigest"] = expected_digest
        _write_component(workspace, "release-config", payload)
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REUSE

    def test_fixture_version_changed_refuses_reuse(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "tablet", {
            "runId": RUN_ID, "passed_count": 5, "failed_count": 0, "blocked_count": 0,
            "mandatory_skipped_count": 0, "fixtureVersion": "v1",
        })
        decision = self._evaluate(workspace, "tablet", current_fixture_version="v2-DIFFERENT")
        assert decision.decision == rr.DECISION_REFUSED
        assert "fixture version" in decision.reason

    def test_fixture_version_unchanged_stays_reusable(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "tablet", {
            "runId": RUN_ID, "passed_count": 5, "failed_count": 0, "blocked_count": 0,
            "mandatory_skipped_count": 0, "fixtureVersion": "v1",
        })
        decision = self._evaluate(workspace, "tablet", current_fixture_version="v1")
        assert decision.decision == rr.DECISION_REUSE

    def test_missing_referenced_evidence_file_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        payload = _release_config_report()
        payload["evidenceFiles"] = [{"path": str(tmp_path / "does-not-exist.json"), "sha256": "x" * 64}]
        _write_component(workspace, "release-config", payload)
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REFUSED
        assert "no longer exists" in decision.reason

    def test_evidence_digest_mismatch_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        evidence_path = tmp_path / "evidence.bin"
        evidence_path.write_bytes(b"original-bytes")
        import hashlib
        original_digest = hashlib.sha256(b"original-bytes").hexdigest()
        payload = _release_config_report()
        payload["evidenceFiles"] = [{"path": str(evidence_path), "sha256": original_digest}]
        _write_component(workspace, "release-config", payload)
        # Tamper the evidence file after recording its digest.
        evidence_path.write_bytes(b"tampered-bytes")
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REFUSED
        assert "digest no longer matches" in decision.reason

    def test_evidence_digest_match_is_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        evidence_path = tmp_path / "evidence.bin"
        evidence_path.write_bytes(b"stable-bytes")
        import hashlib
        digest = hashlib.sha256(b"stable-bytes").hexdigest()
        payload = _release_config_report()
        payload["evidenceFiles"] = [{"path": str(evidence_path), "sha256": digest}]
        _write_component(workspace, "release-config", payload)
        decision = self._evaluate(workspace, "release-config")
        assert decision.decision == rr.DECISION_REUSE


# ---------------------------------------------------------------------------
# evaluate_installation_reuse: the extra bounded, read-only, live check
# ---------------------------------------------------------------------------


class TestEvaluateInstallationReuse:
    def _reuse_decision(self, workspace):
        return rr.evaluate_component_reuse(
            "installation", workspace=workspace, run_id=RUN_ID, release_id="r1",
            baseline_digest="sha256:baseline", run_started_at_epoch=None,
        )

    def test_reuse_without_adb_runner_falls_back_to_execute(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        decision = self._reuse_decision(workspace)
        result = rr.evaluate_installation_reuse(decision, adb_runner=None, tablet_serial="TAB1")
        assert result.decision == rr.DECISION_EXECUTE
        assert result.blocks_resume is False

    def test_reuse_without_recorded_tablet_identity_falls_back_to_execute(self, tmp_path):
        workspace = _workspace(tmp_path)
        payload = _passing_installation_report()
        del payload["tabletStableIdentity"]
        _write_component(workspace, "installation", payload)
        decision = self._reuse_decision(workspace)
        result = rr.evaluate_installation_reuse(decision, adb_runner=_fake_adb(), tablet_serial="TAB1")
        assert result.decision == rr.DECISION_EXECUTE

    def test_device_unreachable_refuses_but_does_not_block_whole_resume(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        decision = self._reuse_decision(workspace)
        result = rr.evaluate_installation_reuse(decision, adb_runner=_fake_adb(device_present=False), tablet_serial="TAB1")
        assert result.decision == rr.DECISION_REFUSED
        assert result.blocks_resume is False

    def test_tablet_stable_identity_mismatch_is_refused_and_blocks_resume(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        decision = self._reuse_decision(workspace)
        different_tablet = _fake_adb(serialno="A-DIFFERENT-TABLET")
        result = rr.evaluate_installation_reuse(decision, adb_runner=different_tablet, tablet_serial="TAB1")
        assert result.decision == rr.DECISION_REFUSED
        assert result.blocks_resume is True

    def test_installed_package_no_longer_present_is_refused_and_blocks_resume(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        decision = self._reuse_decision(workspace)

        def not_installed(argv):
            if "dumpsys" in argv and "com.viso.calee" in argv:
                return ri.AdbResult(0, "")  # empty -> not present
            return _fake_adb()(argv)

        result = rr.evaluate_installation_reuse(decision, adb_runner=not_installed, tablet_serial="TAB1")
        assert result.decision == rr.DECISION_REFUSED
        assert result.blocks_resume is True

    def test_installed_package_version_changed_is_refused_and_blocks_resume(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        decision = self._reuse_decision(workspace)
        changed = _fake_adb(calee_version_name="2.0.0", calee_version_code="200")
        result = rr.evaluate_installation_reuse(decision, adb_runner=changed, tablet_serial="TAB1")
        assert result.decision == rr.DECISION_REFUSED
        assert result.blocks_resume is True

    def test_everything_matches_stays_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        decision = self._reuse_decision(workspace)
        result = rr.evaluate_installation_reuse(decision, adb_runner=_fake_adb(), tablet_serial="TAB1")
        assert result.decision == rr.DECISION_REUSE


# ---------------------------------------------------------------------------
# check_candidate_unchanged: independent byte-level tamper detection
# ---------------------------------------------------------------------------


class TestCheckCandidateUnchanged:
    def _snapshot(self, tmp_path, workspace):
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "calee.apk").write_bytes(b"calee-apk-bytes")
        (bundle / "caleeshell.apk").write_bytes(b"caleeshell-apk-bytes")
        import hashlib

        def _sha(data):
            return hashlib.sha256(data).hexdigest()

        # Schema-v1 shape (no schemaVersion key; flat calee/caleeShell) --
        # the simplest manifest verify_release_bundle accepts (see
        # test_release_candidate_fingerprint.py's own _write_bundle helper).
        manifest = {
            "releaseId": "r1",
            "calee": {
                "included": True, "packageId": "com.viso.calee", "versionName": "1.0.0",
                "versionCode": 100, "gitSha": "c" * 40, "apk": "calee.apk", "sha256": _sha(b"calee-apk-bytes"),
            },
            "caleeShell": {
                "included": True, "packageId": "com.viso.caleeshell", "versionName": "1.0.0",
                "versionCode": 100, "gitSha": "d" * 40, "apk": "caleeshell.apk", "sha256": _sha(b"caleeshell-apk-bytes"),
            },
        }
        (bundle / "release-manifest.json").write_text(json.dumps(manifest))
        (bundle / "checksums.sha256").write_text(
            f"{_sha(b'calee-apk-bytes')}  calee.apk\n{_sha(b'caleeshell-apk-bytes')}  caleeshell.apk\n"
        )
        verification = ri.verify_release_bundle(bundle)
        assert verification.ok, verification.errors
        snapshot_dir = workspace.component_dir("release-candidate")
        fingerprint = release_candidate_mod.snapshot_release_candidate(
            verification, snapshot_dir, release_id="r1", schema_version=1, run_id=workspace.run_id,
            release_config_digest="sha256:cfg1",
        )
        return snapshot_dir, fingerprint

    def test_no_snapshot_is_not_a_problem(self, tmp_path):
        workspace = _workspace(tmp_path)
        assert rr.check_candidate_unchanged(workspace, None) == []

    def test_unchanged_snapshot_has_no_problems(self, tmp_path):
        workspace = _workspace(tmp_path)
        snapshot_dir, fingerprint = self._snapshot(tmp_path, workspace)
        release_config_report = _release_config_report(fingerprint=fingerprint.to_dict(), schema_version=1, release_config_digest="sha256:cfg1")
        assert rr.check_candidate_unchanged(workspace, release_config_report) == []

    def test_tampered_apk_is_detected(self, tmp_path):
        workspace = _workspace(tmp_path)
        snapshot_dir, fingerprint = self._snapshot(tmp_path, workspace)
        # Tamper the snapshot's own copy of the APK after freezing.
        apk_path = snapshot_dir / "calee.apk"
        real_path = apk_path.resolve()
        real_path.write_bytes(b"tampered-apk-bytes")
        release_config_report = _release_config_report(fingerprint=fingerprint.to_dict(), schema_version=1, release_config_digest="sha256:cfg1")
        problems = rr.check_candidate_unchanged(workspace, release_config_report)
        assert problems


# ---------------------------------------------------------------------------
# Attempt ledger: bootstrap, snapshot, history preservation
# ---------------------------------------------------------------------------


class TestAttemptLedger:
    def test_no_attempts_directory_before_first_resume(self, tmp_path):
        workspace = _workspace(tmp_path)
        assert rr.existing_attempt_numbers(workspace) == []

    def test_bootstrap_creates_attempt_one_baseline(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        inputs = rr.ImmutableInputs(release_id="r1")
        record = rr._bootstrap_attempt_one(workspace, inputs)
        assert record.attempt_number == 1
        assert record.mode == "original"
        baseline = rr.load_baseline_immutable_inputs(workspace)
        assert baseline.release_id == "r1"

    def test_a_later_pass_never_erases_an_earlier_attempts_history(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace, fixture_version="v1")
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "environment", {"runId": RUN_ID, "status": "blocked", "detail": ["x"]})

        result1 = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result1.attempt_number == 2
        assert result1.exit_code == EXIT_SUCCESS

        attempt1 = rr.load_attempt(workspace, 1)
        assert attempt1 is not None
        assert attempt1.mode == "original"

        # Resuming again must not delete or rewrite attempt 1 or attempt 2.
        result2 = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result2.attempt_number == 3
        assert rr.load_attempt(workspace, 1).to_dict() == attempt1.to_dict()
        assert rr.load_attempt(workspace, 2) is not None
        assert sorted(rr.existing_attempt_numbers(workspace)) == [1, 2, 3]

    def test_snapshot_components_copies_without_touching_canonical_path(self, tmp_path):
        workspace = _workspace(tmp_path)
        _write_component(workspace, "release-config", _release_config_report())
        rr._snapshot_components(workspace, 1)
        canonical = workspace.component_report_path("release-config")
        snapshot = rr.attempt_dir(workspace, 1) / "components" / "release-config" / "results.json"
        assert canonical.is_file()
        assert snapshot.is_file()
        assert canonical.read_text() == snapshot.read_text()

    def test_snapshot_components_works_with_zero_component_reports(self, tmp_path):
        """Regression test: a run resumed before ANY component has ever
        produced a report (e.g. resuming right after Prepare's very first
        BLOCKED attempt) must not crash creating the attempt directory."""
        workspace = _workspace(tmp_path)
        rr._snapshot_components(workspace, 1)
        assert rr.attempt_dir(workspace, 1).is_dir()

    def test_component_resume_info_reports_reused_and_executed(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "environment", {"runId": RUN_ID, "status": "blocked", "detail": ["x"]})
        rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        info = rr.component_resume_info(workspace)
        assert info["release-config"]["executionMode"] == "reused"
        assert info["release-config"]["reuseValidation"] == "PASS"
        assert info["environment"]["executionMode"] == "executed"

    def test_component_resume_info_empty_for_never_resumed_run(self, tmp_path):
        workspace = _workspace(tmp_path)
        assert rr.component_resume_info(workspace) == {}


# ---------------------------------------------------------------------------
# inspect_resume: read-only, no mutation
# ---------------------------------------------------------------------------


class TestInspectResume:
    def test_never_writes_an_attempt(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        rr.inspect_resume(RUN_ID, repo_root=tmp_path)
        assert rr.existing_attempt_numbers(workspace) == []

    def test_resumable_when_nothing_has_changed(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        outcome = rr.inspect_resume(RUN_ID, repo_root=tmp_path)
        assert outcome.resumable is True
        assert outcome.exit_code == EXIT_SUCCESS

    def test_refused_when_baseline_release_id_no_longer_matches(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report(release_id="r1"))
        rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)  # bootstraps attempt 1

        _write_component(workspace, "release-config", _release_config_report(release_id="r2-DIFFERENT"))
        outcome = rr.inspect_resume(RUN_ID, repo_root=tmp_path)
        assert outcome.resumable is False
        assert outcome.exit_code == EXIT_BLOCKED
        assert any("releaseId" in m for m in outcome.immutable_mismatches)

    def test_installation_reuse_eligibility_reflected_read_only(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "installation", _passing_installation_report())
        outcome = rr.inspect_resume(RUN_ID, repo_root=tmp_path, adb_runner=_fake_adb(), tablet_serial="TAB1")
        install_decision = next(d for d in outcome.decisions if d.component == "installation")
        assert install_decision.decision == rr.DECISION_REUSE


# ---------------------------------------------------------------------------
# perform_resume: end-to-end orchestration
# ---------------------------------------------------------------------------


class TestPerformResumeValidReuse:
    def test_installation_pass_reused_no_reinstall_no_reboot(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "installation", _passing_installation_report())
        result = rr.perform_resume(
            RUN_ID, repo_root=tmp_path, adb_runner=_fake_adb(), tablet_serial="TAB1", prepare_runner=_pass_prepare,
        )
        install_decision = next(d for d in result.decisions if d.component == "installation")
        assert install_decision.decision == rr.DECISION_REUSE
        assert "installation" in result.attempt.components_reused

    def test_release_candidate_reused_when_still_valid(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "release-candidate", {"runId": RUN_ID, "status": "ok"})
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        decision = next(d for d in result.decisions if d.component == "release-candidate")
        assert decision.decision == rr.DECISION_REUSE

    def test_selector_evidence_pass_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "selector-contract", {"runId": RUN_ID, "status": "ok"})
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        decision = next(d for d in result.decisions if d.component == "selector-contract")
        assert decision.decision == rr.DECISION_REUSE

    def test_mobile_api_pass_reused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "mobile-api", {
            "runId": RUN_ID, "passed_count": 8, "failed_count": 0, "blocked_count": 0, "mandatory_skipped_count": 0,
        })
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        decision = next(d for d in result.decisions if d.component == "mobile-api")
        assert decision.decision == rr.DECISION_REUSE

    def test_prior_blocked_prepare_reruns_successfully(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "environment", {"runId": RUN_ID, "status": "blocked", "detail": ["calendar service unavailable"]})
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_SUCCESS
        assert any(e["component"] == "environment" for e in result.attempt.components_executed)
        # The original BLOCKED attempt must remain visible in history.
        attempt1 = rr.load_attempt(workspace, 1)
        assert attempt1.final_result == cr.STATUS_BLOCKED

    def test_downstream_not_run_components_are_marked_for_execution(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "installation", _passing_installation_report())
        result = rr.perform_resume(
            RUN_ID, repo_root=tmp_path, adb_runner=_fake_adb(), tablet_serial="TAB1", prepare_runner=_pass_prepare,
        )
        tablet_decision = next(d for d in result.decisions if d.component == "tablet")
        assert tablet_decision.decision == rr.DECISION_EXECUTE
        assert any(e["component"] == "tablet" for e in result.attempt.components_executed)


class TestPerformResumeRefusedReuse:
    def _bootstrap(self, tmp_path, **release_config_kwargs):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report(**release_config_kwargs))
        rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        return workspace

    def test_release_id_mismatch_refuses_whole_resume(self, tmp_path):
        workspace = self._bootstrap(tmp_path, release_id="r1")
        _write_component(workspace, "release-config", _release_config_report(release_id="r2-DIFFERENT"))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("releaseId" in m for m in result.immutable_mismatches)

    def test_apk_digest_mismatch_refuses_whole_resume(self, tmp_path):
        fp1 = {"envelopeDigest": "digest-A", "apkSha256": {"calee": {"filename": "calee.apk", "sha256": "aa"}}}
        workspace = self._bootstrap(tmp_path, fingerprint=fp1)
        fp2 = {"envelopeDigest": "digest-A", "apkSha256": {"calee": {"filename": "calee.apk", "sha256": "bb-DIFFERENT"}}}
        _write_component(workspace, "release-config", _release_config_report(fingerprint=fp2))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("apkSha256" in m for m in result.immutable_mismatches)

    def test_backend_mismatch_refuses_whole_resume(self, tmp_path):
        workspace = self._bootstrap(tmp_path, backend="https://hub-dev")
        _write_component(workspace, "release-config", _release_config_report(backend="https://hub-prod-DIFFERENT"))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("targetBackend" in m for m in result.immutable_mismatches)

    def test_release_profile_mismatch_refuses_whole_resume(self, tmp_path):
        workspace = self._bootstrap(tmp_path, profile="staging")
        _write_component(workspace, "release-config", _release_config_report(profile="production-DIFFERENT"))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("releaseProfile" in m for m in result.immutable_mismatches)

    def test_platform_scope_mismatch_refuses_whole_resume(self, tmp_path):
        workspace = self._bootstrap(tmp_path, platforms=("tablet",))
        _write_component(workspace, "release-config", _release_config_report(platforms=("tablet", "android")))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("platformScope" in m for m in result.immutable_mismatches)

    def test_feature_scope_mismatch_refuses_whole_resume(self, tmp_path):
        workspace = self._bootstrap(tmp_path, features=())
        _write_component(workspace, "release-config", _release_config_report(features=("meals",)))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("featureScope" in m for m in result.immutable_mismatches)

    def test_caleemobile_sha_mismatch_refuses_whole_resume(self, tmp_path):
        workspace = self._bootstrap(tmp_path, caleemobile={"gitSha": "m" * 40, "buildVersion": "0.0.1+1"})
        _write_component(workspace, "release-config", _release_config_report(
            caleemobile={"gitSha": "z" * 40, "buildVersion": "0.0.1+1"}
        ))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("expectedGitShas" in m or "caleeMobileExpectedSha" in m for m in result.immutable_mismatches)

    def test_regression_sha_mismatch_refuses_whole_resume(self, tmp_path, monkeypatch):
        workspace = self._bootstrap(tmp_path)
        calls = {"n": 0}

        def fake_git_runner(argv):
            calls["n"] += 1
            sha = "a" * 40 if calls["n"] <= 1 else "b" * 40
            return ri.AdbResult(0, sha)

        # First perform_resume call already bootstrapped attempt 1 with the
        # real (production) git-sha collector; force a distinct fake SHA on
        # this next call by monkeypatching the collector directly.
        monkeypatch.setattr(rr, "_git_sha", lambda repo_dir, runner=None: "DIFFERENT-SHA-" + str(repo_dir))
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert any("regressionSha" in m for m in result.immutable_mismatches)

    def test_installed_package_changed_refuses_whole_resume(self, tmp_path):
        workspace = self._bootstrap(tmp_path)
        _write_component(workspace, "installation", _passing_installation_report())
        result = rr.perform_resume(
            RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare,
            adb_runner=_fake_adb(calee_version_name="9.9.9", calee_version_code="999"), tablet_serial="TAB1",
        )
        assert result.exit_code == EXIT_BLOCKED
        install_decision = next(d for d in result.decisions if d.component == "installation")
        assert install_decision.blocks_resume is True

    def test_tablet_stable_identity_mismatch_refuses_whole_resume(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "installation", _passing_installation_report())
        rr.perform_resume(RUN_ID, repo_root=tmp_path, adb_runner=_fake_adb(), tablet_serial="TAB1", prepare_runner=_pass_prepare)

        result = rr.perform_resume(
            RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare,
            adb_runner=_fake_adb(serialno="COMPLETELY-DIFFERENT-TABLET"), tablet_serial="TAB1",
        )
        assert result.exit_code == EXIT_BLOCKED

    def test_report_outside_workspace_is_refused(self, tmp_path):
        workspace = self._bootstrap(tmp_path)
        other_workspace = run_context.RunWorkspace(tmp_path, "a-totally-different-run")
        other_workspace.ensure_created()
        outside_report = other_workspace.component_report_path("release-config")
        outside_report.write_text(json.dumps(_release_config_report(run_id=RUN_ID)))
        # Simulate a report path outside the workspace by validating it
        # directly against the real workspace (the same mechanism
        # evaluate_component_reuse uses internally).
        with pytest.raises(run_context.RunIdError):
            run_context.validate_component_report(
                json.loads(outside_report.read_text()), report_path=outside_report, run_id=RUN_ID,
                workspace=workspace, component="release-config",
            )

    def test_tampered_release_candidate_fingerprint_is_refused(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "calee.apk").write_bytes(b"calee-apk-bytes")
        (bundle / "caleeshell.apk").write_bytes(b"caleeshell-apk-bytes")
        import hashlib

        def _sha(data):
            return hashlib.sha256(data).hexdigest()

        manifest_json = {
            "releaseId": "r1",
            "calee": {
                "included": True, "packageId": "com.viso.calee", "versionName": "1.0.0",
                "versionCode": 100, "gitSha": "c" * 40, "apk": "calee.apk", "sha256": _sha(b"calee-apk-bytes"),
            },
            "caleeShell": {
                "included": True, "packageId": "com.viso.caleeshell", "versionName": "1.0.0",
                "versionCode": 100, "gitSha": "d" * 40, "apk": "caleeshell.apk", "sha256": _sha(b"caleeshell-apk-bytes"),
            },
        }
        (bundle / "release-manifest.json").write_text(json.dumps(manifest_json))
        (bundle / "checksums.sha256").write_text(
            f"{_sha(b'calee-apk-bytes')}  calee.apk\n{_sha(b'caleeshell-apk-bytes')}  caleeshell.apk\n"
        )
        verification = ri.verify_release_bundle(bundle)
        assert verification.ok, verification.errors
        fingerprint = release_candidate_mod.snapshot_release_candidate(
            verification, workspace.component_dir("release-candidate"),
            release_id="r1", schema_version=1, run_id=RUN_ID, release_config_digest="sha256:cfg1",
        )
        _write_component(workspace, "release-config", _release_config_report(fingerprint=fingerprint.to_dict(), schema_version=1, release_config_digest="sha256:cfg1"))
        rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)

        # Tamper the frozen candidate's APK bytes in place.
        apk_path = (workspace.component_dir("release-candidate") / "calee.apk").resolve()
        apk_path.write_bytes(b"TAMPERED")

        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_BLOCKED
        assert result.immutable_mismatches

    def test_stale_or_malformed_report_must_re_execute_not_reuse(self, tmp_path):
        workspace = self._bootstrap(tmp_path)
        path = workspace.component_report_path("tablet")
        path.write_text("{not valid json", encoding="utf-8")
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        tablet_decision = next(d for d in result.decisions if d.component == "tablet")
        assert tablet_decision.decision == rr.DECISION_REFUSED
        assert "tablet" not in result.attempt.components_reused


# ---------------------------------------------------------------------------
# Interruption-point coverage: a run blocked at each named stage is handled
# ---------------------------------------------------------------------------


INTERRUPTION_POINTS = [
    "release-config",
    "release-candidate",
    "installation",
    "environment",
    "selector-contract",
    "tablet",
    "mobile-api",
    "mobile-android",
    "mobile-ios",
    "sync",
    "manual-checks",
    "distributed-build-acceptance",
    "subscribed-fixture",
]


class TestInterruptionPoints:
    @pytest.mark.parametrize("component", INTERRUPTION_POINTS)
    def test_resume_after_interruption_marks_component_for_execution(self, tmp_path, component):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        # The interrupted component itself: BLOCKED (never NOT_RUN, so this
        # also exercises "prior BLOCKED is refused, not reused").
        if component != "release-config":
            _write_component(workspace, component, {"runId": RUN_ID, "status": "blocked", "detail": ["interrupted"]})
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        decision = next(d for d in result.decisions if d.component == component)
        if component == "release-config":
            assert decision.decision == rr.DECISION_REUSE
        elif component == "environment":
            # Prepare is the one component resume-release reruns in-process;
            # a prior BLOCKED Prepare is rerun (via _pass_prepare) and PASSES
            # this attempt, so it is EXECUTED, never silently reused.
            assert decision.decision == rr.DECISION_EXECUTE
            assert any(e["component"] == "environment" for e in result.attempt.components_executed)
        else:
            assert decision.decision == rr.DECISION_REFUSED
            assert decision.component not in result.attempt.components_reused


# ---------------------------------------------------------------------------
# Tester-facing run listing / selection
# ---------------------------------------------------------------------------


class TestRunListingAndSelection:
    def test_list_runs_empty_when_no_runs_directory(self, tmp_path):
        assert rr.list_runs(tmp_path) == []

    def test_list_runs_reports_release_id_and_status(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        runs = rr.list_runs(tmp_path)
        assert len(runs) == 1
        assert runs[0].run_id == RUN_ID
        assert runs[0].release_id == "r1"

    def test_choose_run_requires_explicit_selection_never_auto_picks(self, tmp_path):
        workspace1 = run_context.RunWorkspace(tmp_path, "release-a")
        workspace1.ensure_created()
        _manifest(workspace1)
        workspace2 = run_context.RunWorkspace(tmp_path, "release-b")
        workspace2.ensure_created()
        _manifest(workspace2)
        runs = rr.list_runs(tmp_path)
        assert len(runs) == 2

        prompts = []

        def fake_input(prompt):
            prompts.append(prompt)
            return "2"

        chosen = rr.choose_run(runs, input_fn=fake_input, print_fn=lambda *a: None)
        assert chosen.run_id == runs[1].run_id
        assert prompts  # a prompt was actually shown -- nothing was auto-selected

    def test_choose_run_cancel_returns_none(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        runs = rr.list_runs(tmp_path)
        chosen = rr.choose_run(runs, input_fn=lambda _: "0", print_fn=lambda *a: None)
        assert chosen is None

    def test_choose_run_reprompts_on_invalid_input(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        runs = rr.list_runs(tmp_path)
        responses = iter(["not-a-number", "99", "1"])
        chosen = rr.choose_run(runs, input_fn=lambda _: next(responses), print_fn=lambda *a: None)
        assert chosen.run_id == RUN_ID


# ---------------------------------------------------------------------------
# Exit code contract
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_regression_exit_code_when_a_mandatory_component_already_failed(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "mobile-api", {
            "runId": RUN_ID, "passed_count": 3, "failed_count": 1, "blocked_count": 0, "mandatory_skipped_count": 0,
        })
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_REGRESSION

    def test_blocked_exit_code_when_prepare_rerun_still_blocked(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        _write_component(workspace, "environment", {"runId": RUN_ID, "status": "blocked", "detail": ["x"]})
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_blocked_prepare)
        assert result.exit_code == EXIT_BLOCKED

    def test_success_exit_code_when_nothing_blocks(self, tmp_path):
        workspace = _workspace(tmp_path)
        _manifest(workspace)
        _write_component(workspace, "release-config", _release_config_report())
        result = rr.perform_resume(RUN_ID, repo_root=tmp_path, prepare_runner=_pass_prepare)
        assert result.exit_code == EXIT_SUCCESS
