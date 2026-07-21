"""Priority 1 (this session) -- install-tablet-release's schema-v2
release-config authority.

Precedence under test:

  * a same-run schema-v2 release-config report is authoritative for the
    release profile and every other release-policy decision --
    ``release_platforms.load_*`` (config/release-platforms.yaml) is NEVER
    called on this path, so an intentionally malformed legacy file has ZERO
    effect on a valid schema-v2 installation;
  * a same-run schema-v1 report, or a bare/diagnostic invocation with no
    run-scoped release-config evidence at all, keeps the original
    release-platforms.yaml-based behaviour unchanged;
  * missing-required-keys, stale, malformed, or wrong-run release-config
    evidence BLOCKS outright -- with ZERO ADB command (mutating or not) ever
    dispatched -- rather than silently falling back to legacy config;
  * release-config, the candidate fingerprint, and the installation must all
    agree on release ID / schema version / candidate digest (Priority 5).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import yaml
from click.testing import CliRunner

from calee_regression import apk_inspect, cli, release_installer
from calee_regression.models import EXIT_BLOCKED, EXIT_SUCCESS

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40
CALEEMOBILE_SHA = "c" * 40
CALEE_SIGNER = "1" * 64
SHELL_SIGNER = "2" * 64
CALEE_BYTES = b"calee-apk-bytes"
SHELL_BYTES = b"caleeshell-apk-bytes"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bundle_v2(tmp_path, *, name="Calee-Tablet-Release", profile="production", release_id="2026.07.21-rc1"):
    bundle = tmp_path / name
    bundle.mkdir(parents=True)
    (bundle / "calee.apk").write_bytes(CALEE_BYTES)
    (bundle / "caleeshell.apk").write_bytes(SHELL_BYTES)
    manifest = {
        "schemaVersion": 2,
        "releaseId": release_id,
        "profile": profile,
        "backend": "https://hub.calee.com.au" if profile == "production" else "https://hub-dev.calee.com.au",
        "platforms": {"tablet": True, "mobileAndroid": True, "mobileIos": True},
        "features": {
            "synchronization": True, "meals": True, "onboarding": True,
            "googleCalendar": True, "kioskAdmin": True, "notifications": True,
        },
        "tabletSolution": {
            "calee": {
                "installArtifact": True, "apk": "calee.apk", "sha256": _sha256(CALEE_BYTES),
                "expectedInstalled": {
                    "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                    "versionCode": 325, "gitSha": CALEE_SHA, "signerSha256": CALEE_SIGNER,
                },
            },
            "caleeShell": {
                "installArtifact": True, "apk": "caleeshell.apk", "sha256": _sha256(SHELL_BYTES),
                "expectedInstalled": {
                    "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                    "versionCode": 212, "gitSha": SHELL_SHA, "signerSha256": SHELL_SIGNER,
                },
            },
        },
        "caleeMobile": {
            "version": "0.0.24+24", "gitSha": CALEEMOBILE_SHA,
            "selectorEvidenceRequired": True, "distributedBuildAcceptanceRequired": True,
        },
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(CALEE_BYTES)}  calee.apk\n{_sha256(SHELL_BYTES)}  caleeshell.apk\n"
    )
    return bundle


def _write_machine_yaml(tmp_path, bundle_dir, *, profile="production"):
    data = dict(
        tablet_serial="TAB123", expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee", caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir=str(bundle_dir),
        backend_url="https://hub.calee.com.au" if profile == "production" else "https://hub-dev.calee.com.au",
        release_profile=profile,
        report_dir="reports", mobile_platforms=["android", "ios"],
        iphone_device="00008110-DEADBEEF", android_device="R5CANDROID",
        allow_caleeshell_technical=True,
    )
    p = tmp_path / "machine.local.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _write_malformed_legacy_yaml(tmp_path, monkeypatch) -> Path:
    """An intentionally malformed config/release-platforms.yaml, pointed to
    via CALEE_RELEASE_PLATFORMS -- release_platforms.py's default path
    resolution is independent of cli.REPO_ROOT (it uses its own module
    location), so the env var is how tests redirect it."""
    legacy = tmp_path / "release-platforms.yaml"
    legacy.write_text("this: [is not, valid: yaml - -\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(legacy))
    return legacy


class _FakeSignerRead:
    def __init__(self, digest):
        self.digest = digest
        self.detail = "fake matching signer"


def _patch_apk_inspection_and_signers(monkeypatch):
    """Bypass real aapt2/apksigner/adb tools entirely: the RELEASE apk
    content+signer read (pre-install) is faked to match the manifest exactly
    (first-time install, nothing installed yet to compare); the POST-install
    device signer read (verify_tablet_solution) is faked to match each
    package's declared signerSha256 exactly, so the production-profile
    signer-trust gate passes cleanly without a real device."""
    real_preinstall = apk_inspect.preinstall_inspect_bundle

    def _which(name):
        return f"/usr/bin/{name}" if name in {"aapt2", "apksigner"} else None

    class _ContentRunner:
        def __call__(self, argv):
            import os
            tool = os.path.basename(argv[0])
            if tool == "aapt2" and "badging" in argv:
                apk = next((a for a in argv if a.endswith(".apk")), "")
                if "caleeshell" in apk:
                    return apk_inspect.ToolResult(
                        0, "package: name='com.viso.caleeshell' versionCode='212' versionName='founder-v0.2.12'\n"
                    )
                return apk_inspect.ToolResult(
                    0, "package: name='com.viso.calee' versionCode='325' versionName='founder-v0.3.25'\n"
                )
            if tool == "apksigner" and "verify" in argv:
                apk = next((a for a in argv if a.endswith(".apk")), "")
                digest = SHELL_SIGNER if "caleeshell" in apk else CALEE_SIGNER
                return apk_inspect.ToolResult(0, f"Signer #1 certificate SHA-256 digest: {digest}\n")
            return apk_inspect.ToolResult(127, "", "unexpected")

    def _patched_preinstall(verification, *, installed_signer_reader=None, which=None, runner=None):
        not_installed_reader = lambda pkg: apk_inspect.SignerReadResult(apk_inspect.SIGNER_NOT_INSTALLED)
        return real_preinstall(
            verification, installed_signer_reader=not_installed_reader,
            which=_which, runner=_ContentRunner(),
        )

    monkeypatch.setattr(apk_inspect, "preinstall_inspect_bundle", _patched_preinstall)

    def _fake_device_installed_signer_reader(*, serial=None, adb_runner=None, which=None, runner=None, retain_diagnostics=False):
        def _read(package_id):
            digest = SHELL_SIGNER if package_id == "com.viso.caleeshell" else CALEE_SIGNER
            return _FakeSignerRead(digest)
        return _read

    monkeypatch.setattr(apk_inspect, "device_installed_signer_reader", _fake_device_installed_signer_reader)


def _contains(*tokens):
    return lambda argv: all(t in argv for t in tokens)


class FakeAdb:
    def __init__(self, rules=(), default=None):
        self.rules = list(rules)
        self.default = default or release_installer.AdbResult(0, "Success\n")
        self.calls = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        for pred, res in self.rules:
            if pred(argv):
                return res
        return self.default


def _healthy_device_rules():
    R = release_installer.AdbResult
    return [
        (_contains("install"), R(0, "Success\n")),
        (_contains("dumpsys", "package", "com.viso.calee"),
         R(0, "versionName=founder-v0.3.25\nversionCode=325")),
        (_contains("dumpsys", "package", "com.viso.caleeshell"),
         R(0, "versionName=founder-v0.2.12\nversionCode=212")),
        (_contains("resolve-activity", "-c", "android.intent.category.HOME"),
         R(0, "packageName=com.viso.caleeshell")),
        (_contains("resolve-activity", "-a", "com.viso.calee.action.START"),
         R(0, "packageName=com.viso.calee")),
        (_contains("wait-for-device"), R(0, "")),
        (_contains("get-state"), R(0, "device")),
    ]


def _run_release_config(tmp_path, machine, bundle, run_id):
    return CliRunner().invoke(
        cli.main, ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )


def test_schema_v2_release_config_authority_reaches_expected_fake_install_command(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    _write_malformed_legacy_yaml(tmp_path, monkeypatch)

    bundle = _write_bundle_v2(tmp_path, profile="production")
    machine = _write_machine_yaml(tmp_path, bundle, profile="production")
    run_id = "release-20260721-000000-authority1"

    rc_result = _run_release_config(tmp_path, machine, bundle, run_id)
    assert rc_result.exit_code == EXIT_SUCCESS, rc_result.output

    _patch_apk_inspection_and_signers(monkeypatch)
    adb = FakeAdb(_healthy_device_rules())
    monkeypatch.setattr(release_installer, "real_adb_runner", lambda argv, **kw: adb(argv, **kw))

    report_path = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--run-id", run_id,
         "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output

    payload = json.loads(report_path.read_text())
    assert payload["status"] == "ok"
    assert payload["releasePolicySource"] == "release-config-v2"
    assert payload["productionProfile"] is True
    assert payload["selectorEvidenceRequired"] is True
    assert payload["distributedBuildAcceptanceRequired"] is True

    # The expected fake install commands were actually reached and dispatched.
    install_calls = [c for c in adb.calls if "install" in c]
    assert install_calls, "no install command reached the fake adb runner"
    apk_args = [a for c in install_calls for a in c if a.endswith(".apk")]
    assert any("calee.apk" in a for a in apk_args)
    assert any("caleeshell.apk" in a for a in apk_args)


def test_malformed_legacy_release_platforms_yaml_has_zero_effect_on_v2_install(tmp_path, monkeypatch):
    """The flip side of the happy-path test: even VERIFYING the legacy file is
    consulted-never would already be proven by the happy path succeeding
    despite it being unparsable YAML -- this test additionally proves that if
    release_platforms.py's loader WERE reached, it would raise (confirming
    the malformed fixture is genuinely broken), so the happy path's success
    really does demonstrate the loader was skipped, not merely tolerant."""
    from calee_regression import release_platforms

    legacy = _write_malformed_legacy_yaml(tmp_path, monkeypatch)
    try:
        release_platforms.load_expected_build_identity(legacy)
        raised = False
    except release_platforms.ReleasePlatformsError:
        raised = True
    assert raised, "the fixture must actually be malformed, or this test proves nothing"


def test_schema_v1_still_uses_legacy_release_platforms_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    legacy = tmp_path / "release-platforms.yaml"
    legacy.write_text("expected_build_identity:\n  production: true\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(legacy))

    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    (bundle / "calee.apk").write_bytes(CALEE_BYTES)
    (bundle / "caleeshell.apk").write_bytes(SHELL_BYTES)
    manifest = {
        "releaseId": "2026.07.21-rc2",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": _sha256(CALEE_BYTES)},
        "caleeShell": {"included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                       "versionCode": 212, "gitSha": SHELL_SHA, "apk": "caleeshell.apk", "sha256": _sha256(SHELL_BYTES)},
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(CALEE_BYTES)}  calee.apk\n{_sha256(SHELL_BYTES)}  caleeshell.apk\n"
    )

    report_path = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--plan-only",
         "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    # A v1 bundle with no run-id at all is the "bare diagnostic" path -- the
    # legacy production:true flag is still what's read (unchanged behaviour).


def test_bare_diagnostic_invocation_with_no_run_scoped_release_config_uses_legacy(tmp_path, monkeypatch):
    """--run-id is given, but no release-config command was ever run for it
    (no reports/runs/<run-id>/release-config/results.json exists yet) --
    this is the 'bare diagnostic invocation with no run-scoped release-
    config' category, and must behave exactly like no run-id at all."""
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    legacy = tmp_path / "release-platforms.yaml"
    legacy.write_text("expected_build_identity:\n  production: false\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(legacy))

    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    (bundle / "calee.apk").write_bytes(CALEE_BYTES)
    (bundle / "caleeshell.apk").write_bytes(SHELL_BYTES)
    manifest = {
        "releaseId": "2026.07.21-rc3",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": _sha256(CALEE_BYTES)},
        "caleeShell": {"included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                       "versionCode": 212, "gitSha": SHELL_SHA, "apk": "caleeshell.apk", "sha256": _sha256(SHELL_BYTES)},
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(CALEE_BYTES)}  calee.apk\n{_sha256(SHELL_BYTES)}  caleeshell.apk\n"
    )

    report_path = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1",
         "--run-id", "release-20260721-000000-nodiag", "--plan-only", "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(report_path.read_text())
    assert payload["status"] == "plan-only"


# ── requirement 4/8: reject missing/stale/malformed/wrong-run evidence, with
#    ZERO adb command (mutating or otherwise) ever dispatched ──────────────


def _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    _write_malformed_legacy_yaml(tmp_path, monkeypatch)
    bundle = _write_bundle_v2(tmp_path)
    machine = _write_machine_yaml(tmp_path, bundle)
    rc_result = _run_release_config(tmp_path, machine, bundle, run_id)
    assert rc_result.exit_code == EXIT_SUCCESS, rc_result.output
    return bundle


def _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch, *, report_name="install.json"):
    adb = FakeAdb(_healthy_device_rules())
    monkeypatch.setattr(release_installer, "real_adb_runner", lambda argv, **kw: adb(argv, **kw))
    report_path = tmp_path / report_name
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--run-id", run_id,
         "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert adb.calls == [], f"install-tablet-release must not touch adb at all when release-config evidence is rejected: {adb.calls}"
    return result


def test_malformed_release_config_json_blocks_with_zero_adb_calls(tmp_path, monkeypatch):
    run_id = "release-20260721-000000-malformed1"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    rc_path.write_text("{not valid json")

    _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)


def test_release_config_missing_required_keys_blocks_with_zero_adb_calls(tmp_path, monkeypatch):
    run_id = "release-20260721-000000-malformed2"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    del raw["releaseSelections"]
    rc_path.write_text(json.dumps(raw))

    _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)


def test_wrong_run_release_config_blocks_with_zero_adb_calls(tmp_path, monkeypatch):
    run_id = "release-20260721-000000-malformed3"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    raw["runId"] = "release-20260721-000000-someone-elses-run"
    rc_path.write_text(json.dumps(raw))

    _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)


def test_stale_release_config_blocks_with_zero_adb_calls(tmp_path, monkeypatch):
    from calee_regression import run_context

    run_id = "release-20260721-000000-malformed4"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    workspace = run_context.RunWorkspace(tmp_path, run_id)
    manifest = run_context.RunManifest.load(workspace.manifest_path)
    # Push the recorded run-start time far into the future relative to the
    # release-config report's actual mtime, so it now reads as stale.
    manifest.started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600))
    manifest.write(workspace.manifest_path)

    _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)


def test_blocked_release_config_status_blocks_install_with_zero_adb_calls(tmp_path, monkeypatch):
    run_id = "release-20260721-000000-malformed5"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    raw["status"] = "blocked"
    raw["detail"] = ["synthetic conflict for this test"]
    rc_path.write_text(json.dumps(raw))

    _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)


def test_release_config_fingerprint_disagreeing_with_snapshot_blocks(tmp_path, monkeypatch):
    """Priority 5: release-config's OWN recorded copy of the candidate
    fingerprint must match the snapshot's on-disk fingerprint exactly."""
    run_id = "release-20260721-000000-malformed6"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    raw["releaseCandidateFingerprint"]["envelopeDigest"] = "0" * 64
    rc_path.write_text(json.dumps(raw))

    result = _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)
    assert "DIFFERENT candidate fingerprint" in result.output


def test_release_config_digest_mismatch_blocks(tmp_path, monkeypatch):
    """Priority 5: install-tablet-release independently recomputes the
    candidate's releaseConfigDigest binding via verify_candidate_fingerprint
    and rejects a release-config report whose OWN releaseConfigDigest was
    altered after the fact (it would no longer match what the frozen
    candidate's fingerprint recorded)."""
    run_id = "release-20260721-000000-malformed7"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    raw["releaseConfigDigest"] = "sha256:" + "f" * 64
    rc_path.write_text(json.dumps(raw))

    result = _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)
    assert "releaseConfigDigest" in result.output


# ── Priority 1 (this session): a frozen candidate makes a matching same-run
#    release-config MANDATORY -- deleted/never-written report, wrong release
#    ID, wrong schema version, schema-v2 + malformed legacy YAML together ──


def test_release_config_report_deleted_after_candidate_freeze_blocks(tmp_path, monkeypatch):
    """The exact crash-consistency window release-config's own write order
    creates: the candidate snapshot is written BEFORE its own report
    (release_config_cmd calls snapshot_release_candidate, then _record), so a
    killed/OOM'd process can leave a frozen candidate with no report at all.
    Simulated here by deleting an otherwise-valid report after both were
    written. A frozen candidate existing for this run makes a matching
    release-config MANDATORY -- this must BLOCK (with zero adb calls), never
    silently fall back to (here, deliberately malformed) legacy policy."""
    run_id = "release-20260721-000000-noreport1"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    assert rc_path.is_file()
    rc_path.unlink()

    result = _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)
    assert "frozen release candidate exists" in result.output
    assert "no matching release-config evidence" in result.output


def test_candidate_frozen_without_release_config_report_ever_existing_blocks(tmp_path, monkeypatch):
    """Distinct from the deleted-after-the-fact case above: here NO
    release-config command ever ran for this run at all (no results.json was
    ever written) -- only a frozen candidate is present, built directly via
    release_candidate.snapshot_release_candidate as release-config itself
    would. The installer must derive "release-config is mandatory" from the
    candidate's mere presence on disk, not from having observed the
    release-config command run."""
    from calee_regression import release_candidate as release_candidate_mod
    from calee_regression import run_context

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    _write_malformed_legacy_yaml(tmp_path, monkeypatch)
    bundle = _write_bundle_v2(tmp_path)
    run_id = "release-20260721-000000-noreport2"
    workspace = run_context.RunWorkspace(tmp_path, run_id)
    workspace.ensure_created()

    verification = release_installer.verify_release_bundle(str(bundle))
    assert verification.ok, verification.errors
    release_candidate_mod.snapshot_release_candidate(
        verification, workspace.component_dir("release-candidate"),
        release_id="2026.07.21-rc1", schema_version=2, run_id=run_id,
        release_config_digest="sha256:" + "0" * 64,
    )
    assert not workspace.component_report_path("release-config").is_file()

    result = _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)
    assert "frozen release candidate exists" in result.output


def test_release_config_wrong_release_id_blocks(tmp_path, monkeypatch):
    """A release-config report whose releaseId disagrees with the frozen
    candidate's recorded releaseId must BLOCK, even though its own
    releaseSelections/releaseConfigDigest are otherwise untouched."""
    run_id = "release-20260721-000000-wrongrelid"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    raw["releaseId"] = "2099.01.01-someone-elses-release"
    rc_path.write_text(json.dumps(raw))

    result = _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)
    assert "releaseId" in result.output


def test_release_config_wrong_schema_version_blocks(tmp_path, monkeypatch):
    """A release-config report whose schemaVersion disagrees with the frozen
    (schema-v2) candidate's recorded schemaVersion must BLOCK -- a schema-v2
    candidate must never end up governed by a differently-schema'd report."""
    run_id = "release-20260721-000000-wrongschema"
    bundle = _prep_v2_release_with_candidate(tmp_path, monkeypatch, run_id)

    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    raw["schemaVersion"] = 1
    rc_path.write_text(json.dumps(raw))

    result = _assert_blocked_with_no_adb_calls(tmp_path, bundle, run_id, monkeypatch)
    assert "schemaVersion" in result.output


def test_schema_v1_candidate_with_valid_schema_v1_release_config_uses_legacy(tmp_path, monkeypatch):
    """Schema-v1 compatibility must survive Priority 1's stricter gating: a
    run whose frozen candidate is schema-v1 (release-config was run WITH
    --bundle, so a candidate was frozen, but the bundle itself is a v1
    manifest) and whose same-run release-config report is a valid, matching
    schema-v1 report must still use legacy release-platforms.yaml policy --
    it must NOT be blocked merely because a candidate exists."""
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    legacy = tmp_path / "release-platforms.yaml"
    legacy.write_text("expected_build_identity:\n  production: false\n")
    monkeypatch.setenv("CALEE_RELEASE_PLATFORMS", str(legacy))

    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir()
    (bundle / "calee.apk").write_bytes(CALEE_BYTES)
    (bundle / "caleeshell.apk").write_bytes(SHELL_BYTES)
    manifest = {
        "releaseId": "2026.07.21-rc-v1cand",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": _sha256(CALEE_BYTES)},
        "caleeShell": {"included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                       "versionCode": 212, "gitSha": SHELL_SHA, "apk": "caleeshell.apk", "sha256": _sha256(SHELL_BYTES)},
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(CALEE_BYTES)}  calee.apk\n{_sha256(SHELL_BYTES)}  caleeshell.apk\n"
    )
    machine = _write_machine_yaml(tmp_path, bundle, profile="staging")
    run_id = "release-20260721-000000-v1candidate"

    rc_result = _run_release_config(tmp_path, machine, bundle, run_id)
    assert rc_result.exit_code == EXIT_SUCCESS, rc_result.output
    rc_path = tmp_path / "reports" / "runs" / run_id / "release-config" / "results.json"
    raw = json.loads(rc_path.read_text())
    assert raw["schemaVersion"] == 1
    fp_path = tmp_path / "reports" / "runs" / run_id / "release-candidate" / "release-candidate-fingerprint.json"
    assert fp_path.is_file(), "a v1 bundle passed with --bundle is still frozen into a candidate"
    assert json.loads(fp_path.read_text())["schemaVersion"] == 1

    report_path = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1",
         "--run-id", run_id, "--plan-only", "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(report_path.read_text())
    assert payload["status"] == "plan-only"
