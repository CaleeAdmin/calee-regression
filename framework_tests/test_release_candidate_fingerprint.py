"""Release-candidate fingerprint + immutable snapshot (Priority 4).

Closes the TOCTOU gap between release-config approving a release bundle and
install-tablet-release's first mutating ADB command:

  * unit-level tests on release_candidate.py itself -- snapshotting a
    verified bundle, round-tripping the fingerprint, and detecting tampering
    with each individual file (manifest, checksums, each APK) after the
    snapshot was taken;
  * CLI-level tests proving install-tablet-release installs ONLY from the
    frozen snapshot (never the original, still-mutable --bundle path), and
    that mutating any file -- in the snapshot, or in the original drop
    folder after approval -- either has no effect (the original) or BLOCKS
    with ZERO ADB mutation commands ever dispatched (the snapshot).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from calee_regression import release_candidate as rcand
from calee_regression import release_installer as ri
from calee_regression import cli
from calee_regression.models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_SUCCESS

CALEE_SHA = "a" * 40
SHELL_SHA = "b" * 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bundle(tmp_path, *, calee_bytes=b"calee-apk-bytes", shell_bytes=b"caleeshell-apk-bytes"):
    bundle = tmp_path / "Calee-Tablet-Release"
    bundle.mkdir(parents=True)
    (bundle / "calee.apk").write_bytes(calee_bytes)
    (bundle / "caleeshell.apk").write_bytes(shell_bytes)
    manifest = {
        "releaseId": "2026.07.20-rc1",
        "calee": {"included": True, "packageId": "com.viso.calee", "versionName": "founder-v0.3.25",
                  "versionCode": 325, "gitSha": CALEE_SHA, "apk": "calee.apk", "sha256": _sha256(calee_bytes)},
        "caleeShell": {"included": True, "packageId": "com.viso.caleeshell", "versionName": "founder-v0.2.12",
                       "versionCode": 212, "gitSha": SHELL_SHA, "apk": "caleeshell.apk", "sha256": _sha256(shell_bytes)},
    }
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(calee_bytes)}  calee.apk\n{_sha256(shell_bytes)}  caleeshell.apk\n"
    )
    return bundle


# ── unit-level: snapshot_release_candidate / verify_candidate_fingerprint ──


def test_snapshot_copies_manifest_checksums_and_every_apk(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    assert verification.ok, verification.errors

    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="2026.07.20-rc1", schema_version=1)

    assert (snapshot_dir / "release-manifest.json").is_file()
    assert (snapshot_dir / "checksums.sha256").is_file()
    assert (snapshot_dir / "calee.apk").is_file()
    assert (snapshot_dir / "caleeshell.apk").is_file()
    assert (snapshot_dir / rcand.FINGERPRINT_FILENAME).is_file()
    assert fp.manifest_sha256 == _sha256((bundle / "release-manifest.json").read_bytes())
    assert fp.apk_sha256["calee"]["sha256"] == _sha256(b"calee-apk-bytes")
    assert fp.apk_sha256["caleeShell"]["sha256"] == _sha256(b"caleeshell-apk-bytes")


def test_fresh_snapshot_verifies_clean(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert problems == []


def test_fingerprint_round_trips_through_load(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=2)
    loaded = rcand.load_candidate_fingerprint(snapshot_dir / rcand.FINGERPRINT_FILENAME)
    assert loaded.manifest_sha256 == fp.manifest_sha256
    assert loaded.envelope_digest == fp.envelope_digest
    assert rcand.verify_candidate_fingerprint(snapshot_dir, loaded) == []


def test_tampered_manifest_after_snapshot_is_detected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    (snapshot_dir / "release-manifest.json").write_text('{"tampered": true}')
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("release-manifest.json changed" in p for p in problems)


def test_tampered_checksums_after_snapshot_is_detected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    (snapshot_dir / "checksums.sha256").write_text("tampered\n")
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("checksums.sha256 changed" in p for p in problems)


def test_tampered_calee_apk_after_snapshot_is_detected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    (snapshot_dir / "calee.apk").write_bytes(b"a-different-re-signed-apk")
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("calee APK" in p and "changed" in p for p in problems)


def test_tampered_caleeshell_apk_after_snapshot_is_detected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    (snapshot_dir / "caleeshell.apk").write_bytes(b"a-different-re-signed-apk")
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("caleeShell APK" in p and "changed" in p for p in problems)


def test_removed_apk_after_snapshot_is_detected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    (snapshot_dir / "calee.apk").unlink()
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("is missing from the release-candidate snapshot" in p for p in problems)


def test_re_pointed_symlink_apk_target_after_snapshot_is_detected(tmp_path):
    # A symlinked APK in the ORIGINAL bundle whose target changes AFTER the
    # snapshot was taken: since the snapshot copied the dereferenced bytes,
    # this scenario is equivalent to "the snapshot's own APK bytes changed"
    # -- exercised directly here by re-writing the snapshot's copy, which is
    # exactly what a re-pointed symlink into the snapshot (if one existed)
    # would resolve to.
    bundle = _write_bundle(tmp_path)
    real_target = bundle / "real-calee.apk"
    real_target.write_bytes(b"calee-apk-bytes")
    symlink_apk = bundle / "calee-link.apk"
    symlink_apk.symlink_to(real_target)
    manifest = json.loads((bundle / "release-manifest.json").read_text())
    manifest["calee"]["apk"] = "calee-link.apk"
    (bundle / "release-manifest.json").write_text(json.dumps(manifest))
    (bundle / "checksums.sha256").write_text(
        f"{_sha256(b'calee-apk-bytes')}  calee-link.apk\n{_sha256(b'caleeshell-apk-bytes')}  caleeshell.apk\n"
    )
    verification = ri.verify_release_bundle(bundle)
    assert verification.ok, verification.errors

    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    assert rcand.verify_candidate_fingerprint(snapshot_dir, fp) == []

    # Now re-point the ORIGINAL symlink's target after the snapshot exists --
    # the snapshot itself is untouched (a plain copy, not a symlink), so it
    # must still verify clean; this proves the snapshot is truly independent
    # of the original bundle once taken.
    real_target.write_bytes(b"a-completely-different-apk")
    assert rcand.verify_candidate_fingerprint(snapshot_dir, fp) == []
    # But the snapshot's OWN bytes changing (simulating a direct edit of the
    # frozen candidate) is what must be caught:
    snapshotted_name = fp.apk_sha256["calee"]["filename"]
    (snapshot_dir / snapshotted_name).write_bytes(b"a-completely-different-apk")
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("calee APK" in p and "changed" in p for p in problems)


def test_tampered_fingerprint_file_itself_is_detected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    fp.manifest_sha256 = "0" * 64  # edit the record without recomputing the envelope
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("envelope digest mismatch" in p for p in problems)


# ── Priority 5: complete fingerprint binding ────────────────────────────


def test_fingerprint_binds_run_id_release_config_digest_and_created_at(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(
        verification, snapshot_dir, release_id="r1", schema_version=2,
        run_id="release-20260720-000000-abc123", release_config_digest="sha256:" + "1" * 64,
    )
    assert fp.run_id == "release-20260720-000000-abc123"
    assert fp.release_config_digest == "sha256:" + "1" * 64
    assert fp.candidate_id
    assert fp.created_at
    assert rcand.verify_candidate_fingerprint(snapshot_dir, fp) == []


@pytest.mark.parametrize("field_name", ["run_id", "candidate_id", "release_config_digest", "created_at"])
def test_tampering_any_priority5_field_without_recompute_is_detected(tmp_path, field_name):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(
        verification, snapshot_dir, release_id="r1", schema_version=2,
        run_id="run-a", release_config_digest="sha256:" + "2" * 64,
    )
    setattr(fp, field_name, "tampered-value")
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("envelope digest mismatch" in p for p in problems), (field_name, problems)


def test_candidate_copied_from_another_run_is_rejected_by_expected_run_id(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(
        verification, snapshot_dir, release_id="r1", schema_version=2, run_id="run-a",
    )
    # The fingerprint is internally self-consistent (envelope digest matches,
    # every byte on disk matches) -- it is simply NOT for the run trying to
    # install it (as if the whole directory had been copied wholesale from
    # run-a's workspace into run-b's).
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp, expected_run_id="run-b")
    assert any("belongs to a DIFFERENT run" in p for p in problems)
    assert rcand.verify_candidate_fingerprint(snapshot_dir, fp, expected_run_id="run-a") == []


def test_fingerprint_edited_to_name_another_release_or_schema_is_rejected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=2, run_id="run-a")
    assert any(
        "releaseId" in p for p in rcand.verify_candidate_fingerprint(snapshot_dir, fp, expected_release_id="r2")
    )
    assert any(
        "schemaVersion" in p
        for p in rcand.verify_candidate_fingerprint(snapshot_dir, fp, expected_schema_version=1)
    )


def test_release_config_report_fingerprint_digest_mismatch_is_rejected(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(
        verification, snapshot_dir, release_id="r1", schema_version=2, run_id="run-a",
        release_config_digest="sha256:" + "3" * 64,
    )
    # The release-config report that "approved" this candidate recorded a
    # DIFFERENT digest than what the candidate's own fingerprint carries --
    # e.g. release-config was re-run (or tampered) after the candidate was
    # frozen against a different composition.
    problems = rcand.verify_candidate_fingerprint(
        snapshot_dir, fp, expected_release_config_digest="sha256:" + "4" * 64,
    )
    assert any("releaseConfigDigest" in p for p in problems)
    assert rcand.verify_candidate_fingerprint(
        snapshot_dir, fp, expected_release_config_digest="sha256:" + "3" * 64,
    ) == []


def test_candidate_id_mismatch_detects_extra_file_dropped_into_snapshot(tmp_path):
    """An extra file smuggled into the snapshot after freezing isn't
    referenced by any of the per-file digest checks (which only look at the
    manifest/checksums/APK filenames the fingerprint already knows about) --
    the whole-directory candidateId is what catches it."""
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=2, run_id="run-a")
    assert rcand.verify_candidate_fingerprint(snapshot_dir, fp) == []

    (snapshot_dir / "smuggled-extra-file.apk").write_bytes(b"not part of the approved candidate")
    problems = rcand.verify_candidate_fingerprint(snapshot_dir, fp)
    assert any("candidateId mismatch" in p for p in problems)


def test_candidate_root_is_excluded_from_envelope_digest_but_recorded(tmp_path):
    """bundleRoot is a machine-local, mutable diagnostic path -- editing it
    alone must NOT trip the envelope digest (it is deliberately excluded),
    unlike every other Priority 5 field."""
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    fp = rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=2, run_id="run-a")
    assert fp.bundle_root
    fp.bundle_root = "/some/other/machine/path"
    assert rcand.verify_candidate_fingerprint(snapshot_dir, fp) == []


def test_snapshot_reused_dir_leaves_no_stale_files(tmp_path):
    bundle = _write_bundle(tmp_path)
    verification = ri.verify_release_bundle(bundle)
    snapshot_dir = tmp_path / "snapshot"
    rcand.snapshot_release_candidate(verification, snapshot_dir, release_id="r1", schema_version=1)
    (snapshot_dir / "unrelated-stale-file.txt").write_text("leftover from a previous release")

    bundle2 = _write_bundle(tmp_path / "second", calee_bytes=b"calee-v2-apk-bytes", shell_bytes=b"caleeshell-v2-apk-bytes")
    verification2 = ri.verify_release_bundle(bundle2)
    rcand.snapshot_release_candidate(verification2, snapshot_dir, release_id="r2", schema_version=1)

    assert not (snapshot_dir / "unrelated-stale-file.txt").exists()
    assert (snapshot_dir / "calee.apk").read_bytes() == b"calee-v2-apk-bytes"


# ── CLI-level: install-tablet-release installs ONLY from the frozen snapshot ──


def _write_machine_yaml(tmp_path, bundle_dir):
    import yaml
    data = dict(
        tablet_serial="TAB123", expected_tablet_state="logged_in_tablet",
        calee_package_id="com.viso.calee", caleeshell_package_id="com.viso.caleeshell",
        home_activity="com.viso.caleeshell/.ui.LauncherActivity",
        calee_launch_action="com.viso.calee.action.START",
        release_bundle_dir=str(bundle_dir),
        backend_url="https://hub-dev.calee.com.au", release_profile="staging",
        report_dir="reports", mobile_platforms=["android", "ios"],
        iphone_device="00008110-DEADBEEF", android_device="R5CANDROID",
        allow_caleeshell_technical=True,
    )
    p = tmp_path / "machine.local.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_install_uses_snapshot_path_not_original_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    bundle = _write_bundle(tmp_path)
    machine = _write_machine_yaml(tmp_path, bundle)
    run_id = "release-20260720-101010-freeze1"

    rc_result = CliRunner().invoke(
        cli.main, ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )
    assert rc_result.exit_code == EXIT_SUCCESS, rc_result.output

    # --plan-only exits right after building the install plan, before any
    # APK-content-inspection tooling is needed -- exactly what this test
    # needs to check (which bundle root the plan's APK paths resolve into)
    # without depending on apkanalyzer/aapt2 being installed in this
    # environment.
    report_path = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--run-id", run_id,
         "--plan-only", "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(report_path.read_text())
    snapshot_dir = tmp_path / "reports" / "runs" / run_id / "release-candidate"
    assert payload["bundleVerification"]["bundleRoot"] == str(snapshot_dir.resolve())
    argvs = [a for step in payload["plan"]["steps"] for a in step["argv"]]
    apk_args = [a for a in argvs if a.endswith(".apk")]
    assert apk_args
    # snapshot_dir is a symlink pointer (Priority 4 crash-recoverable
    # publication -- see atomic_publish.py) into a content-addressed version
    # directory; every consumer that just opens/reads it (verify_release_bundle
    # included) is unaffected, but the RESOLVED APK path -- which is what
    # actually reaches an install command -- lives under the pointer's real
    # target rather than repeating the pointer path textually.
    for a in apk_args:
        assert Path(a).is_relative_to(snapshot_dir.resolve())
        assert str(bundle) not in a


def test_tampering_original_bundle_after_approval_has_no_effect_on_install(tmp_path, monkeypatch):
    # Requirement: refuse to install from the original mutable drop folder --
    # proven here by corrupting it AFTER approval and confirming the install
    # plan still builds cleanly (from the untouched snapshot), because it
    # never reads the original bundle again.
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    bundle = _write_bundle(tmp_path)
    machine = _write_machine_yaml(tmp_path, bundle)
    run_id = "release-20260720-101010-freeze2"

    rc_result = CliRunner().invoke(
        cli.main, ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )
    assert rc_result.exit_code == EXIT_SUCCESS, rc_result.output

    # Corrupt the ORIGINAL bundle's manifest after approval.
    (bundle / "release-manifest.json").write_text('{"tampered": "yes"}')

    report_path = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--run-id", run_id,
         "--plan-only", "--report", str(report_path)],
    )
    # Not "invalid" (which is what a corrupted bundle would normally produce)
    # -- the snapshot is what's actually used, and it's untouched.
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(report_path.read_text())
    assert payload["status"] != "invalid", payload


def test_tampering_snapshot_after_approval_blocks_with_zero_adb_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    bundle = _write_bundle(tmp_path)
    machine = _write_machine_yaml(tmp_path, bundle)
    run_id = "release-20260720-101010-freeze3"

    rc_result = CliRunner().invoke(
        cli.main, ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )
    assert rc_result.exit_code == EXIT_SUCCESS, rc_result.output

    snapshot_dir = tmp_path / "reports" / "runs" / run_id / "release-candidate"
    (snapshot_dir / "calee.apk").write_bytes(b"a-tampered-re-signed-apk")

    from calee_regression import release_installer
    executed = {"called": False}

    def _spy_execute(*args, **kwargs):
        executed["called"] = True
        raise AssertionError("execute_install_plan must NOT run when the candidate was tampered with")

    monkeypatch.setattr(release_installer, "execute_install_plan", _spy_execute)

    report_path = tmp_path / "install.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--run-id", run_id,
         "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert executed["called"] is False
    payload = json.loads(report_path.read_text())
    assert payload["status"] == "blocked"
    assert any("changed since release-config" in d or "calee APK" in d for d in payload["detail"])


def test_missing_snapshot_dir_after_approval_blocks_with_zero_adb_mutation(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    bundle = _write_bundle(tmp_path)
    machine = _write_machine_yaml(tmp_path, bundle)
    run_id = "release-20260720-101010-freeze4"

    rc_result = CliRunner().invoke(
        cli.main, ["release-config", "--config", str(machine), "--bundle", str(bundle), "--run-id", run_id],
    )
    assert rc_result.exit_code == EXIT_SUCCESS, rc_result.output

    snapshot_dir = tmp_path / "reports" / "runs" / run_id / "release-candidate"
    (snapshot_dir / "checksums.sha256").unlink()

    from calee_regression import release_installer
    executed = {"called": False}

    def _spy_execute(*args, **kwargs):
        executed["called"] = True
        raise AssertionError("execute_install_plan must NOT run when the candidate snapshot is incomplete")

    monkeypatch.setattr(release_installer, "execute_install_plan", _spy_execute)

    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--run-id", run_id],
    )
    assert result.exit_code == EXIT_BLOCKED, result.output
    assert executed["called"] is False


def test_no_run_id_is_unaffected_installs_from_bundle_as_before(tmp_path):
    # A bare/diagnostic invocation with no --run-id never had a snapshot to
    # begin with -- behaviour is unchanged (backward compatible).
    bundle = _write_bundle(tmp_path)
    report_path = tmp_path / "plan.json"
    result = CliRunner().invoke(
        cli.main,
        ["install-tablet-release", "--bundle", str(bundle), "--serial", "TAB1", "--plan-only",
         "--report", str(report_path)],
    )
    assert result.exit_code == EXIT_SUCCESS, result.output
    payload = json.loads(report_path.read_text())
    argvs = [a for step in payload["plan"]["steps"] for a in step["argv"]]
    apk_args = [a for a in argvs if a.endswith(".apk")]
    for a in apk_args:
        assert str(bundle) in a
