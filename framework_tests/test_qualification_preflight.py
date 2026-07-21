"""Priority 9 (this session) -- technical-owner qualification preflight.
Every check is read-only/injectable; these tests run fully offline, with no
real device/Appium/Flutter/Keychain/network required.
"""

from __future__ import annotations

import json

import pytest
import yaml

from calee_regression import qualification_preflight as qp

_VALID_MACHINE = {
    "tablet_serial": "TAB123", "expected_tablet_state": "logged_in_tablet",
    "calee_package_id": "com.viso.calee", "caleeshell_package_id": "com.viso.caleeshell",
    "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
    "calee_launch_action": "com.viso.calee.action.START",
    "release_bundle_dir": "~/Calee-Releases/current", "backend_url": "https://hub-dev.calee.com.au",
    "release_profile": "staging", "report_dir": "reports", "mobile_platforms": ["android"],
}


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _write_machine_config(tmp_path, **overrides):
    data = dict(_VALID_MACHINE, **overrides)
    path = tmp_path / "machine.local.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# ── individual checks ────────────────────────────────────────────────────


def test_check_machine_config_missing_is_blocked(tmp_path):
    result = qp.check_machine_config(tmp_path / "nope.yaml")
    assert result.status == qp.STATUS_BLOCKED


def test_check_machine_config_valid_is_ready(tmp_path):
    path = _write_machine_config(tmp_path)
    result = qp.check_machine_config(path)
    assert result.status == qp.STATUS_READY
    assert "hunter2" not in result.detail  # sanity: no secret ever appears


def test_check_machine_config_with_secret_is_blocked(tmp_path):
    path = _write_machine_config(tmp_path, regression_password="hunter2")
    result = qp.check_machine_config(path)
    assert result.status == qp.STATUS_BLOCKED
    assert "hunter2" not in result.detail


def test_check_report_root_missing_is_warning(tmp_path):
    result = qp.check_report_root(str(tmp_path / "nonexistent-reports"), env={})
    assert result.status == qp.STATUS_WARNING


def test_check_report_root_existing_writable_is_ready(tmp_path):
    (tmp_path / "reports").mkdir()
    result = qp.check_report_root(str(tmp_path / "reports"), env={})
    assert result.status == qp.STATUS_READY


def test_check_report_root_filesystem_root_is_blocked():
    result = qp.check_report_root("/", env={})
    assert result.status == qp.STATUS_BLOCKED


def test_check_android_sdk_found_via_which():
    result = qp.check_android_sdk(which=lambda name: "/usr/bin/adb" if name == "adb" else None, env={})
    assert result.status == qp.STATUS_READY


def test_check_android_sdk_not_found_is_blocked():
    # env={} makes this hermetic -- without it, a CI runner with a real
    # ANDROID_HOME/platform-tools/adb on disk would pass via the env-var
    # branch regardless of the faked `which`, exactly as happened in CI.
    result = qp.check_android_sdk(which=lambda name: None, env={})
    assert result.status == qp.STATUS_BLOCKED


def test_check_android_sdk_found_via_android_home_env(tmp_path):
    (tmp_path / "platform-tools").mkdir()
    (tmp_path / "platform-tools" / "adb").write_text("")
    result = qp.check_android_sdk(which=lambda name: None, env={"ANDROID_HOME": str(tmp_path)})
    assert result.status == qp.STATUS_READY


def test_check_adb_devices_none_connected_is_blocked():
    availability, serial_check, connected = qp.check_adb_devices(
        "TAB123", adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\n"),
    )
    assert availability.status == qp.STATUS_BLOCKED
    assert connected == []


def test_check_adb_devices_expected_serial_connected_is_ready():
    availability, serial_check, connected = qp.check_adb_devices(
        "TAB123", adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
    )
    assert availability.status == qp.STATUS_READY
    assert serial_check.status == qp.STATUS_READY
    assert connected == ["TAB123"]


def test_check_adb_devices_wrong_serial_connected_is_blocked():
    availability, serial_check, connected = qp.check_adb_devices(
        "TAB123", adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nOTHER\tdevice\n"),
    )
    assert availability.status == qp.STATUS_READY
    assert serial_check.status == qp.STATUS_BLOCKED


def test_check_adb_devices_no_expected_serial_is_warning():
    _, serial_check, _ = qp.check_adb_devices(
        None, adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nX\tdevice\n"),
    )
    assert serial_check.status == qp.STATUS_WARNING


def test_check_adb_devices_adb_not_runnable_is_blocked():
    def _boom(argv):
        raise OSError("adb not found")

    availability, serial_check, connected = qp.check_adb_devices("TAB123", adb_runner=_boom)
    assert availability.status == qp.STATUS_BLOCKED
    assert connected == []


def test_check_appium_no_url_is_warning():
    assert qp.check_appium(None).status == qp.STATUS_NOT_APPLICABLE


def test_check_appium_reachable_is_ready():
    result = qp.check_appium("http://127.0.0.1:4723/wd/hub", opener=lambda url: object())
    assert result.status == qp.STATUS_READY


def test_check_appium_unreachable_is_blocked():
    def _boom(url):
        raise OSError("connection refused")

    result = qp.check_appium("http://127.0.0.1:4723/wd/hub", opener=_boom)
    assert result.status == qp.STATUS_BLOCKED


def _flutter_version_runner(version=qp.EXPECTED_FLUTTER_VERSION):
    import json as _json
    return lambda argv: _FakeCompletedProcess(stdout=_json.dumps({"frameworkVersion": version, "dartSdkVersion": "3.5.0"}))


def test_check_flutter_found_with_pinned_version_is_ready():
    result = qp.check_flutter(
        which=lambda name: "/usr/bin/flutter" if name == "flutter" else None, runner=_flutter_version_runner(),
    )
    assert result.status == qp.STATUS_READY


def test_check_flutter_missing_is_blocked():
    assert qp.check_flutter(which=lambda name: None).status == qp.STATUS_BLOCKED


def test_check_flutter_wrong_version_is_blocked():
    # Priority 7 requirement 8: EXACT pinned version, not just presence.
    result = qp.check_flutter(
        which=lambda name: "/usr/bin/flutter" if name == "flutter" else None,
        runner=_flutter_version_runner(version="3.10.0"),
    )
    assert result.status == qp.STATUS_BLOCKED
    assert "3.10.0" in result.detail


def test_check_flutter_unparseable_version_output_is_blocked():
    result = qp.check_flutter(
        which=lambda name: "/usr/bin/flutter" if name == "flutter" else None,
        runner=lambda argv: _FakeCompletedProcess(stdout="not json"),
    )
    assert result.status == qp.STATUS_BLOCKED


def test_check_flutter_missing_is_only_a_warning_when_not_required():
    """Priority 9 (this session): a release scope with no mobile platform
    enabled at all is never blocked merely because Flutter isn't installed
    on this machine."""
    result = qp.check_flutter(which=lambda name: None, required=False)
    assert result.status == qp.STATUS_NOT_APPLICABLE


def test_check_appium_no_url_blocks_when_required():
    """Priority 9 (this session): when the release scope needs Appium-
    driven mobile UI testing, an unconfigured Appium is a real gap, not
    merely a warning."""
    result = qp.check_appium(None, required=True)
    assert result.status == qp.STATUS_BLOCKED


def test_check_appium_unreachable_is_blocked_even_when_not_required():
    """A configured-but-unreachable Appium is always worth surfacing hard,
    regardless of scope -- unlike simply not being configured at all."""
    def _boom(url):
        raise OSError("connection refused")

    result = qp.check_appium("http://127.0.0.1:4723/wd/hub", opener=_boom, required=False)
    assert result.status == qp.STATUS_BLOCKED


def test_check_ingestion_bridge_unavailable_is_only_a_warning_by_default(tmp_path):
    result = qp.check_ingestion_bridge(tmp_path)
    assert result.status == qp.STATUS_NOT_APPLICABLE


def test_check_ingestion_bridge_unavailable_blocks_when_calendar_required(tmp_path):
    """Priority 9 (this session): when the release scope enables the
    google_calendar (subscribed-calendar) feature, a missing ingestion
    bridge BLOCKS instead of merely warning."""
    result = qp.check_ingestion_bridge(tmp_path, required=True)
    assert result.status == qp.STATUS_BLOCKED


def test_run_qualification_preflight_no_mobile_platform_never_blocks_on_flutter_or_appium(tmp_path):
    """Priority 9, end-to-end: with no bundle given and a machine config
    declaring no mobile platforms/tablet at all, the derived scope is empty
    -- Flutter/Appium never BLOCK on being absent, they downgrade to
    WARNING, since nothing in this (admittedly minimal, no-bundle)
    invocation needs them."""
    config_path = _write_machine_config(tmp_path, mobile_platforms=[], tablet_serial=None)
    report = qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\n"),
        which=lambda name: None,
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        env={},
    )
    names_to_status = {c.name: c.status for c in report.checks}
    assert names_to_status["flutter"] == qp.STATUS_NOT_APPLICABLE
    assert names_to_status["appium"] == qp.STATUS_NOT_APPLICABLE


def test_check_mobile_devices_for_scope_android_required_and_present():
    checks = qp.check_mobile_devices_for_scope(["android"], android_connected=["TAB123"])
    assert checks[0].status == qp.STATUS_READY


def test_check_mobile_devices_for_scope_android_required_and_absent():
    checks = qp.check_mobile_devices_for_scope(["android"], android_connected=[])
    assert checks[0].status == qp.STATUS_BLOCKED


def test_check_mobile_devices_for_scope_ios_required_and_unknown():
    checks = qp.check_mobile_devices_for_scope(["ios"], android_connected=[], iphone_available=None)
    assert checks[0].status == qp.STATUS_WARNING


def test_check_mobile_devices_for_scope_ios_required_and_absent():
    checks = qp.check_mobile_devices_for_scope(["ios"], android_connected=[], iphone_available=False)
    assert checks[0].status == qp.STATUS_BLOCKED


def test_check_mobile_devices_for_scope_not_in_scope_produces_no_check():
    checks = qp.check_mobile_devices_for_scope(["tablet"], android_connected=[])
    assert checks == []


def test_check_sibling_checkout_missing_is_blocked(tmp_path):
    result = qp.check_sibling_checkout("caleemobile_checkout", tmp_path / "CaleeMobile")
    assert result.status == qp.STATUS_BLOCKED


def test_check_sibling_checkout_present_is_ready(tmp_path):
    d = tmp_path / "CaleeMobile"
    d.mkdir()
    (d / ".git").mkdir()
    result = qp.check_sibling_checkout("caleemobile_checkout", d)
    assert result.status == qp.STATUS_READY


def test_check_sibling_checkout_not_a_git_repo_is_warning(tmp_path):
    d = tmp_path / "CaleeMobile"
    d.mkdir()
    result = qp.check_sibling_checkout("caleemobile_checkout", d)
    assert result.status == qp.STATUS_WARNING


def test_check_keychain_credentials_present_via_injected_resolver():
    from calee_regression import credentials as credentials_mod

    resolver = credentials_mod.default_resolver(injected={
        "regression_username": "tester@example.com", "regression_password": "hunter2",
    })
    result = qp.check_keychain_credentials(resolver=resolver)
    assert result.status == qp.STATUS_READY
    assert "hunter2" not in result.detail
    assert "tester@example.com" not in result.detail


def test_check_keychain_credentials_missing_is_blocked():
    from calee_regression import credentials as credentials_mod

    resolver = credentials_mod.CredentialResolver([credentials_mod.EnvironmentProvider({})])
    result = qp.check_keychain_credentials(resolver=resolver)
    assert result.status == qp.STATUS_BLOCKED


def test_check_ics_publisher_config_no_section_is_warning():
    assert qp.check_ics_publisher_config(None).status == qp.STATUS_NOT_APPLICABLE


def test_check_ics_publisher_config_offline_only_is_warning():
    assert qp.check_ics_publisher_config({"mode": "offline-only"}).status == qp.STATUS_NOT_APPLICABLE


def test_check_ics_publisher_config_published_valid_is_ready():
    result = qp.check_ics_publisher_config({
        "mode": "published", "publisher": "webdav", "public_url": "https://example.com/reg_sub.ics",
    })
    assert result.status == qp.STATUS_READY


def test_check_ics_publisher_config_published_invalid_url_is_blocked():
    result = qp.check_ics_publisher_config({
        "mode": "published", "publisher": "webdav", "public_url": "http://example.com/reg_sub.ics",
    })
    assert result.status == qp.STATUS_BLOCKED


def test_check_public_ics_url_reachable_is_ready():
    result = qp.check_public_ics_url("https://example.com/reg_sub.ics", opener=lambda url: object())
    assert result.status == qp.STATUS_READY


def test_check_public_ics_url_unreachable_is_blocked():
    def _boom(url):
        raise OSError("404")

    result = qp.check_public_ics_url("https://example.com/reg_sub.ics", opener=_boom)
    assert result.status == qp.STATUS_BLOCKED


# ── selector CI evidence availability (Priority 6) ──────────────────────


def test_check_selector_ci_evidence_availability_no_token_is_warning_by_default():
    result = qp.check_selector_ci_evidence_availability(env={})
    assert result.status == qp.STATUS_NOT_APPLICABLE


def test_check_selector_ci_evidence_availability_no_token_blocks_when_required():
    result = qp.check_selector_ci_evidence_availability(env={}, required=True)
    assert result.status == qp.STATUS_BLOCKED


def test_check_selector_ci_evidence_availability_token_alone_is_not_evidence():
    """Priority 6's core requirement: a resolvable credential with no
    workflow-run/artifact id given must NOT be treated as READY -- the
    previous defect this closes was exactly 'credential presence -> READY'
    with no artifact ever actually authenticated."""
    result = qp.check_selector_ci_evidence_availability(env={"GITHUB_TOKEN": "tok"})
    assert result.status == qp.STATUS_NOT_APPLICABLE


def test_check_selector_ci_evidence_availability_token_alone_blocks_when_required():
    result = qp.check_selector_ci_evidence_availability(env={"GITHUB_TOKEN": "tok"}, required=True)
    assert result.status == qp.STATUS_BLOCKED


def test_check_selector_ci_evidence_availability_authenticates_a_real_artifact(monkeypatch):
    from calee_regression import github_artifact as ga_mod

    captured = {}

    def _fake_acquire(**kwargs):
        captured.update(kwargs)
        return ga_mod.GithubArtifactChain(ok=True, problems=[])

    monkeypatch.setattr(ga_mod, "acquire_github_artifact", _fake_acquire)
    result = qp.check_selector_ci_evidence_availability(
        env={"GITHUB_TOKEN": "tok"}, workflow_run_id="555", artifact_id="666",
        expected_regression_sha="a" * 40, expected_tested_sha="b" * 40, expected_version="0.0.24+24",
    )
    assert result.status == qp.STATUS_READY
    assert captured["run_id"] == "555"
    assert captured["artifact_id"] == "666"
    assert captured["expected_regression_sha"] == "a" * 40
    assert captured["expected_tested_sha"] == "b" * 40
    assert captured["expected_version"] == "0.0.24+24"


def test_check_selector_ci_evidence_availability_rejected_artifact_blocks(monkeypatch):
    from calee_regression import github_artifact as ga_mod

    def _fake_acquire(**kwargs):
        return ga_mod.GithubArtifactChain(ok=False, problems=["wrong regressionSha"])

    monkeypatch.setattr(ga_mod, "acquire_github_artifact", _fake_acquire)
    result = qp.check_selector_ci_evidence_availability(
        env={"GITHUB_TOKEN": "tok"}, workflow_run_id="555", artifact_id="666",
    )
    assert result.status == qp.STATUS_BLOCKED


def test_check_distributed_build_evidence_availability_none_given_is_warning():
    assert qp.check_distributed_build_evidence_availability(None).status == qp.STATUS_NOT_APPLICABLE


def _write_run_scoped_distributed_build_report(tmp_path, *, run_id="run-1", evidence_tier=None, **overrides):
    """Priority 7 (this session): the REAL shape 'record-distributed-build-
    acceptance' writes -- an authenticated, envelope-/raw-byte-digest-
    protected ``provenance`` record, not a bare evidence dict. Mirrors
    test_distributed_build_mandatory_consolidation.py's
    ``_write_provenance_acceptance`` fixture (same authenticated-tier
    re-verification is exercised here, one layer up, via the preflight
    check that now delegates to it)."""
    import datetime

    from calee_regression import distributed_build_provenance as dbp
    from calee_regression import provider_evidence as pe

    if evidence_tier is None:
        evidence_tier = pe.TIER_PROVIDER_API_LIVE
    fresh_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence = dict(
        schemaVersion=2, component="caleemobile-distributed-build-acceptance",
        provider="app_store_connect", channel="testflight", distributedBuildId="TF-1",
        releaseId="r1", testedGitSha="a" * 40, testedVersion="0.0.24+24",
        providerAccountOrProject="acct", providerRecordId="rec-1",
        providerObservedAt=fresh_ts, generatedBy="provider-api",
        sourceDigest="sha256:" + "1" * 64, timestamp=fresh_ts,
    )
    evidence.update(overrides)
    raw_bytes = json.dumps(evidence).encode("utf-8")
    record = dbp.build_provenance_record(
        evidence, release_run_id=run_id, adopted_at=fresh_ts, adopted_by="technical-owner",
        source_path="/tmp/fake-evidence.json", raw_source_bytes=raw_bytes, evidence_tier=evidence_tier,
    )
    component_dir = tmp_path / "distributed-build-acceptance"
    dbp.write_evidence_bundle(component_dir, record, source_bytes=raw_bytes)
    path = component_dir / "results.json"
    path.write_text(json.dumps({"runId": run_id, "provenance": record, "status": "passed"}))
    return path


def test_check_distributed_build_evidence_availability_valid(tmp_path):
    path = _write_run_scoped_distributed_build_report(tmp_path)
    result = qp.check_distributed_build_evidence_availability(
        path, expected_release_id="r1", expected_git_sha="a" * 40, expected_version="0.0.24+24",
    )
    assert result.status == qp.STATUS_READY, result.detail


def test_check_distributed_build_evidence_availability_malformed(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({"generatedBy": "manual_claim"}))
    result = qp.check_distributed_build_evidence_availability(path)
    assert result.status == qp.STATUS_BLOCKED


def test_check_distributed_build_evidence_availability_hand_typed_json_still_blocks(tmp_path):
    """Priority 7's core requirement: a hand-typed JSON with every
    plausible-looking field (correct-shaped evidence, matching identity) but
    NO authenticated ``provenance`` record at all must still BLOCK -- the
    exact 'arbitrary --source JSON passes every offline format check' hole
    this session closes. The old, format-only check would have accepted
    this file as READY."""
    import datetime

    fresh_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence = {
        "schemaVersion": 2, "component": "caleemobile-distributed-build-acceptance",
        "provider": "app_store_connect", "channel": "testflight", "distributedBuildId": "TF-1",
        "releaseId": "r1", "testedGitSha": "a" * 40, "testedVersion": "0.0.24+24",
        "providerAccountOrProject": "acct", "providerRecordId": "rec-1",
        "providerObservedAt": fresh_ts, "generatedBy": "provider-api",
        "sourceDigest": "sha256:" + "1" * 64, "timestamp": fresh_ts,
    }
    path = tmp_path / "hand-typed-report.json"
    path.write_text(json.dumps({"runId": "run-1", "status": "passed", "evidence": evidence}))
    result = qp.check_distributed_build_evidence_availability(
        path, expected_release_id="r1", expected_git_sha="a" * 40, expected_version="0.0.24+24",
    )
    assert result.status == qp.STATUS_BLOCKED


def test_check_distributed_build_evidence_availability_wrong_tier_blocks(tmp_path):
    """A genuinely provenance-wrapped record, correctly digested, but
    stamped with a NON-authenticated evidenceTier (exactly what
    record-distributed-build-acceptance stamps on its deprecated --source
    path) still BLOCKS -- the digest checks alone only prove the record
    wasn't tampered with after being recorded, never that it was ever
    authenticated to begin with."""
    from calee_regression import provider_evidence as pe

    path = _write_run_scoped_distributed_build_report(tmp_path, evidence_tier=pe.TIER_MANUAL_UNVERIFIED)
    result = qp.check_distributed_build_evidence_availability(
        path, expected_release_id="r1", expected_git_sha="a" * 40, expected_version="0.0.24+24",
    )
    assert result.status == qp.STATUS_BLOCKED


def test_check_distributed_build_evidence_availability_wrong_identity_blocks(tmp_path):
    """An authenticated record whose testedGitSha doesn't match the
    release's expected SHA still BLOCKS -- authentication proves the record
    wasn't fabricated, not that it's for the right build."""
    path = _write_run_scoped_distributed_build_report(tmp_path)
    result = qp.check_distributed_build_evidence_availability(
        path, expected_release_id="r1", expected_git_sha="b" * 40, expected_version="0.0.24+24",
    )
    assert result.status == qp.STATUS_BLOCKED


def test_check_frozen_candidate_ability_none_given_is_warning():
    assert qp.check_frozen_candidate_ability(None).status == qp.STATUS_NOT_APPLICABLE


def test_check_frozen_candidate_ability_valid_bundle(tmp_path):
    import hashlib

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    calee_bytes = b"calee-apk-bytes"
    (bundle / "calee.apk").write_bytes(calee_bytes)
    sha = hashlib.sha256(calee_bytes).hexdigest()
    (bundle / "release-manifest.json").write_text(json.dumps({
        "releaseId": "r1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": "a" * 40, "apk": "calee.apk", "sha256": sha},
    }))
    (bundle / "checksums.sha256").write_text(f"{sha}  calee.apk\n")
    result = qp.check_frozen_candidate_ability(bundle)
    assert result.status == qp.STATUS_READY


def test_check_frozen_candidate_ability_invalid_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = qp.check_frozen_candidate_ability(bundle)
    assert result.status == qp.STATUS_BLOCKED


def test_check_manual_check_definitions_missing_is_blocked(tmp_path):
    result = qp.check_manual_check_definitions(tmp_path / "nope.json")
    assert result.status == qp.STATUS_BLOCKED


def test_check_manual_check_definitions_valid(tmp_path):
    path = tmp_path / "manual-checks.json"
    path.write_text(json.dumps([{"title": "t", "instruction": "i"}]))
    result = qp.check_manual_check_definitions(path)
    assert result.status == qp.STATUS_READY


# ── main-CI authenticated artifact check (Priority 8) ───────────────────


def test_check_main_ci_artifact_authenticated_no_run_or_artifact_id_is_warning_by_default():
    result = qp.check_main_ci_artifact_authenticated(
        repository="CaleeAdmin/calee-regression", workflow_run_id=None, artifact_id=None,
        expected_merge_sha="a" * 40,
    )
    assert result.status == qp.STATUS_NOT_APPLICABLE


def test_check_main_ci_artifact_authenticated_no_run_or_artifact_id_blocks_when_required():
    result = qp.check_main_ci_artifact_authenticated(
        repository="CaleeAdmin/calee-regression", workflow_run_id=None, artifact_id=None,
        expected_merge_sha="a" * 40, required=True,
    )
    assert result.status == qp.STATUS_BLOCKED


def test_check_main_ci_artifact_authenticated_uses_the_given_check_name():
    result = qp.check_main_ci_artifact_authenticated(
        check_name="caleemobile_regression_main_ci_authenticated",
        repository="CaleeAdmin/CaleeMobile-Regression", workflow_run_id=None, artifact_id=None,
        expected_merge_sha=None,
    )
    assert result.name == "caleemobile_regression_main_ci_authenticated"


def test_check_main_ci_artifact_authenticated_unrecognised_repository_blocks(monkeypatch):
    calls = []

    def _fake_acquire(**kwargs):
        calls.append(kwargs)
        raise AssertionError("must not be called for an unrecognised repository")

    monkeypatch.setattr("calee_regression.main_ci_artifact.acquire_main_ci_artifact", _fake_acquire)
    result = qp.check_main_ci_artifact_authenticated(
        repository="CaleeAdmin/SomeOtherRepo", workflow_run_id="1", artifact_id="2",
        expected_merge_sha="a" * 40,
    )
    assert result.status == qp.STATUS_BLOCKED
    assert not calls


def test_check_main_ci_artifact_authenticated_passes_through_its_own_distinct_sha(monkeypatch):
    """Priority 8's core requirement, at the single-check level: each call
    authenticates against exactly the expected_merge_sha IT was given --
    never a different repository's SHA."""
    captured = {}

    def _fake_acquire(**kwargs):
        captured.update(kwargs)
        from calee_regression import main_ci_artifact as mca_mod
        return mca_mod.MainCiArtifactChain(ok=True, problems=[])

    monkeypatch.setattr("calee_regression.main_ci_artifact.acquire_main_ci_artifact", _fake_acquire)
    result = qp.check_main_ci_artifact_authenticated(
        check_name="calee_regression_main_ci_authenticated",
        repository="CaleeAdmin/calee-regression", workflow_run_id="111", artifact_id="222",
        expected_merge_sha="c" * 40, env={},
    )
    assert result.status == qp.STATUS_READY
    assert captured["expected_merge_sha"] == "c" * 40
    assert captured["repository"] == "CaleeAdmin/calee-regression"
    assert captured["run_id"] == "111"
    assert captured["artifact_id"] == "222"


def test_run_qualification_preflight_regression_repo_main_ci_shas_never_cross_contaminate(tmp_path, monkeypatch):
    """Priority 8, end-to-end: calee-regression's and CaleeMobile-
    Regression's own main-CI checks each authenticate against THEIR OWN
    --calee-regression-main-sha / --caleemobile-regression-main-sha --
    never each other's, and never the CaleeMobile PRODUCT SHA
    (expected_caleemobile_sha)."""
    from calee_regression import main_ci_artifact as mca_mod

    config_path = _write_machine_config(tmp_path)
    (tmp_path / "reports").mkdir()
    seen_shas_by_repo = {}

    def _fake_acquire(**kwargs):
        seen_shas_by_repo[kwargs["repository"]] = kwargs["expected_merge_sha"]
        return mca_mod.MainCiArtifactChain(ok=True, problems=[])

    monkeypatch.setattr(mca_mod, "acquire_main_ci_artifact", _fake_acquire)

    calee_regression_sha = "1" * 40
    caleemobile_regression_sha = "2" * 40
    caleemobile_product_sha = "3" * 40

    report = qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        subprocess_runner=_multiplex_subprocess_runner,
        env={},
        expected_caleemobile_sha=caleemobile_product_sha,
        calee_regression_main_sha=calee_regression_sha,
        calee_regression_main_workflow_run_id="111", calee_regression_main_artifact_id="222",
        caleemobile_regression_main_sha=caleemobile_regression_sha,
        caleemobile_regression_main_workflow_run_id="333", caleemobile_regression_main_artifact_id="444",
    )
    assert seen_shas_by_repo == {
        "CaleeAdmin/calee-regression": calee_regression_sha,
        "CaleeAdmin/CaleeMobile-Regression": caleemobile_regression_sha,
    }
    names_to_status = {c.name: c.status for c in report.checks}
    assert names_to_status["calee_regression_main_ci_authenticated"] == qp.STATUS_READY
    assert names_to_status["caleemobile_regression_main_ci_authenticated"] == qp.STATUS_READY


def test_run_qualification_preflight_missing_either_regression_main_ci_artifact_blocks(tmp_path, monkeypatch):
    """Offline test 22: supplying a repository's expected SHA but leaving
    out its workflow-run-id/artifact-id is an incomplete configuration that
    BLOCKS that repository's own check -- independently of whether the
    OTHER regression repository's main-CI check is fully configured and
    passing."""
    from calee_regression import main_ci_artifact as mca_mod

    config_path = _write_machine_config(tmp_path)
    (tmp_path / "reports").mkdir()

    def _fake_acquire(**kwargs):
        return mca_mod.MainCiArtifactChain(ok=True, problems=[])

    monkeypatch.setattr(mca_mod, "acquire_main_ci_artifact", _fake_acquire)

    report = qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        subprocess_runner=_multiplex_subprocess_runner,
        env={},
        # calee-regression: fully configured -- should PASS.
        calee_regression_main_sha="1" * 40,
        calee_regression_main_workflow_run_id="111", calee_regression_main_artifact_id="222",
        # CaleeMobile-Regression: SHA given, but run-id/artifact-id missing
        # -- an incomplete configuration that must BLOCK, not merely warn.
        caleemobile_regression_main_sha="2" * 40,
    )
    names_to_status = {c.name: c.status for c in report.checks}
    assert names_to_status["calee_regression_main_ci_authenticated"] == qp.STATUS_READY
    assert names_to_status["caleemobile_regression_main_ci_authenticated"] == qp.STATUS_BLOCKED
    assert report.overall == qp.STATUS_BLOCKED


# ── report aggregation ───────────────────────────────────────────────────


def test_preflight_report_overall_warning_when_a_check_warns_and_none_blocked():
    # Priority 7 requirement 17: any warning prevents an unqualified READY --
    # this is the corrected behaviour. The PREVIOUS behaviour (any number of
    # WARNINGs still reported READY) was exactly the fail-open defect this
    # closes; do not reintroduce it.
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_WARNING, "meh"),
    ])
    assert report.overall == qp.STATUS_WARNING
    assert report.to_dict()["overall"] == "WARNING"
    assert report.to_dict()["warnedCapabilities"] == ["b"]


def test_preflight_report_overall_ready_only_when_every_check_is_ready():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_READY, "ok"),
    ])
    assert report.overall == qp.STATUS_READY
    assert report.to_dict()["overall"] == "READY"
    assert report.to_dict()["blockedCapabilities"] == []
    assert report.to_dict()["warnedCapabilities"] == []


def test_preflight_report_overall_blocked_beats_warning():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_WARNING, "meh"),
        qp.PreflightCheck("b", qp.STATUS_BLOCKED, "nope"),
    ])
    assert report.overall == qp.STATUS_BLOCKED
    assert report.to_dict()["blockedCapabilities"] == ["b"]


def test_preflight_report_overall_blocked_when_any_blocked():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("a", qp.STATUS_READY, "ok"),
        qp.PreflightCheck("b", qp.STATUS_BLOCKED, "nope"),
    ])
    assert report.overall == qp.STATUS_BLOCKED
    assert report.to_dict()["overall"] == "BLOCKED"


# ── report sections (Priority 11) ───────────────────────────────────────


def test_sections_cover_every_required_named_bucket():
    """Priority 11's core requirement: qualification output must separate
    release candidate identity, product build identity, regression
    framework identity, selector evidence, distributed build provider
    evidence, distributed build provenance, physical devices, toolchains,
    and subscribed-calendar infrastructure."""
    report = qp.PreflightReport(checks=[])
    section_keys = {s["section"] for s in report.sections()}
    required = {
        qp.SECTION_RELEASE_CANDIDATE_IDENTITY, qp.SECTION_PRODUCT_BUILD_IDENTITY,
        qp.SECTION_REGRESSION_FRAMEWORK_IDENTITY, qp.SECTION_SELECTOR_EVIDENCE,
        qp.SECTION_DISTRIBUTED_BUILD_PROVIDER_EVIDENCE, qp.SECTION_DISTRIBUTED_BUILD_PROVENANCE,
        qp.SECTION_PHYSICAL_DEVICES, qp.SECTION_TOOLCHAINS, qp.SECTION_SUBSCRIBED_CALENDAR_INFRASTRUCTURE,
    }
    assert required.issubset(section_keys)


def test_sections_empty_section_is_not_applicable():
    report = qp.PreflightReport(checks=[qp.PreflightCheck("machine_config", qp.STATUS_READY, "ok")])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_SELECTOR_EVIDENCE]["status"] == "NOT_APPLICABLE"
    assert by_key[qp.SECTION_RELEASE_CANDIDATE_IDENTITY]["status"] == "READY"


def test_sections_all_not_applicable_checks_roll_up_to_not_applicable():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("appium", qp.STATUS_NOT_APPLICABLE, "not configured", not_applicable=True),
        qp.PreflightCheck("flutter", qp.STATUS_NOT_APPLICABLE, "not on PATH", not_applicable=True),
    ])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_TOOLCHAINS]["status"] == "NOT_APPLICABLE"


def test_sections_mixed_ready_and_not_applicable_is_ready():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("appium", qp.STATUS_NOT_APPLICABLE, "not configured", not_applicable=True),
        qp.PreflightCheck("flutter", qp.STATUS_READY, "found"),
    ])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_TOOLCHAINS]["status"] == "READY"


def test_sections_genuine_warning_is_not_not_applicable():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("appium", qp.STATUS_WARNING, "ambiguous", not_applicable=False),
    ])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_TOOLCHAINS]["status"] == "WARNING"


def test_sections_blocked_beats_everything():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("appium", qp.STATUS_NOT_APPLICABLE, "not configured", not_applicable=True),
        qp.PreflightCheck("flutter", qp.STATUS_BLOCKED, "wrong version"),
    ])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_TOOLCHAINS]["status"] == "BLOCKED"


def test_sections_distributed_build_check_covers_both_distributed_sections():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("distributed_build_evidence_availability", qp.STATUS_READY, "ok"),
    ])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_DISTRIBUTED_BUILD_PROVIDER_EVIDENCE]["status"] == "READY"
    assert by_key[qp.SECTION_DISTRIBUTED_BUILD_PROVENANCE]["status"] == "READY"
    assert "distributed_build_evidence_availability" in by_key[qp.SECTION_DISTRIBUTED_BUILD_PROVIDER_EVIDENCE]["checks"]
    assert "distributed_build_evidence_availability" in by_key[qp.SECTION_DISTRIBUTED_BUILD_PROVENANCE]["checks"]


def test_sections_release_scope_conflict_maps_to_release_candidate_identity():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("release_scope_conflict:kiosk_admin", qp.STATUS_BLOCKED, "not authorised"),
    ])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_RELEASE_CANDIDATE_IDENTITY]["status"] == "BLOCKED"


def test_sections_remediation_collects_hints_from_non_ready_checks_only():
    report = qp.PreflightReport(checks=[
        qp.PreflightCheck("flutter", qp.STATUS_BLOCKED, "missing", hint="install flutter"),
        qp.PreflightCheck("appium", qp.STATUS_READY, "ok", hint="unused hint"),
    ])
    by_key = {s["section"]: s for s in report.sections()}
    assert by_key[qp.SECTION_TOOLCHAINS]["remediation"] == ["install flutter"]


def test_run_qualification_preflight_end_to_end_sections_present(tmp_path):
    """Priority 11, end-to-end: the real orchestrator's report payload
    includes the sections structure alongside the flat check list."""
    config_path = _write_machine_config(tmp_path)
    (tmp_path / "reports").mkdir()

    report = qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        subprocess_runner=_multiplex_subprocess_runner,
        env={},
    )
    data = report.to_dict()
    assert "sections" in data
    section_keys = {s["section"] for s in data["sections"]}
    required = {
        qp.SECTION_RELEASE_CANDIDATE_IDENTITY, qp.SECTION_PRODUCT_BUILD_IDENTITY,
        qp.SECTION_REGRESSION_FRAMEWORK_IDENTITY, qp.SECTION_SELECTOR_EVIDENCE,
        qp.SECTION_DISTRIBUTED_BUILD_PROVIDER_EVIDENCE, qp.SECTION_DISTRIBUTED_BUILD_PROVENANCE,
        qp.SECTION_PHYSICAL_DEVICES, qp.SECTION_TOOLCHAINS, qp.SECTION_SUBSCRIBED_CALENDAR_INFRASTRUCTURE,
    }
    assert required.issubset(section_keys)
    for section in data["sections"]:
        assert section["status"] in ("READY", "WARNING", "BLOCKED", "NOT_APPLICABLE")
    # every check name appears in at least one section (Priority 11: no
    # check falls through the cracks, ungrouped).
    grouped_names = {name for s in data["sections"] for name in s["checks"]}
    for check in report.checks:
        assert check.name in grouped_names, f"{check.name} is not mapped to any section"
    dumped = json.dumps(data)
    assert "hunter2" not in dumped


# ── full orchestration (injected seams, fully offline) ──────────────────


def _multiplex_subprocess_runner(argv):
    import json as _json

    if argv[:1] == ["/usr/bin/flutter"] or (argv and argv[0].endswith("flutter")):
        return _FakeCompletedProcess(stdout=_json.dumps({"frameworkVersion": qp.EXPECTED_FLUTTER_VERSION, "dartSdkVersion": "3.5.0"}))
    if argv[:1] == ["git"]:
        return _FakeCompletedProcess(stdout="a" * 40)
    return _FakeCompletedProcess(stdout="")


def test_run_qualification_preflight_end_to_end_offline(tmp_path):
    config_path = _write_machine_config(tmp_path)
    (tmp_path / "reports").mkdir()

    report = qp.run_qualification_preflight(
        config_path=config_path,
        repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout="List of devices attached\nTAB123\tdevice\n"),
        which=lambda name: {"adb": "/usr/bin/adb", "flutter": "/usr/bin/flutter"}.get(name),
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network in test")),
        subprocess_runner=_multiplex_subprocess_runner,
        env={},
    )
    data = report.to_dict()
    assert data["overall"] in ("READY", "WARNING", "BLOCKED")
    assert "blockedCapabilities" in data
    assert "warnedCapabilities" in data
    names = {c["name"] for c in data["checks"]}
    assert "machine_config" in names
    assert "report_root" in names
    assert "android_sdk_tools" in names
    assert "android_build_tools" in names
    assert "adb_device_availability" in names
    assert "expected_tablet_serial" in names
    assert "appium" in names
    assert "appium_drivers" in names
    assert "flutter" in names
    assert "caleemobile_checkout" in names
    assert "caleemobile_regression_checkout" in names
    assert "keychain_credentials" in names
    assert "external_ics_publisher" in names
    assert "public_ics_url" in names
    assert "ingestion_api_bridge" in names
    assert "selector_ci_evidence_availability" in names
    assert "distributed_build_evidence_availability" in names
    assert "frozen_candidate_ability" in names
    assert "manual_check_definitions" in names
    assert "main_ci_evidence" in names
    assert "calee_regression_main_ci_authenticated" in names
    assert "caleemobile_regression_main_ci_authenticated" in names
    # Secret-free output, no matter what.
    dumped = json.dumps(data)
    assert "hunter2" not in dumped


def test_run_qualification_preflight_never_mutates_anything(tmp_path):
    """No APK install, no fixture write, no product API call -- proven here
    by the fact that nothing this test doesn't explicitly create appears on
    disk afterwards."""
    config_path = _write_machine_config(tmp_path)
    before = set(p.name for p in tmp_path.iterdir())
    qp.run_qualification_preflight(
        config_path=config_path, repo_root=tmp_path,
        adb_runner=lambda argv: _FakeCompletedProcess(stdout=""),
        which=lambda name: None,
        http_opener=lambda url: (_ for _ in ()).throw(OSError("no network")),
        env={},
    )
    after = set(p.name for p in tmp_path.iterdir())
    # reports/ may legitimately not exist yet (report_root check is read-only
    # and does not create it) -- the set of top-level entries must be
    # unchanged either way.
    assert after == before
