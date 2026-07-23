"""Tests for the host-local fixture ownership lock (fixture_ownership.py):
acquisition, contention, stale/foreign/interrupted classification, owner-only
release, explicit audit-recorded stale recovery, and the no-secrets and
host-local-scope guarantees. Everything injectable -- no real processes are
probed and nothing outside tmp_path is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from calee_regression import fixture_ownership as fo

BACKEND = "https://staging.calee.invalid"
EMAIL = "reg-user@example.com"
PASSWORD = "s3cret-value"
NOW = "2026-07-23T01:02:03+00:00"


def _scope(fixture_version: str = "REG-9") -> fo.LockScope:
    return fo.LockScope(
        backend=BACKEND,
        account_fingerprint=fo.account_fingerprint(EMAIL),
        fixture_version=fixture_version,
    )


def _acquire(tmp_path, *, run_id="run-1", hostname="this-host", pid=100,
             pid_alive=lambda pid: True):
    return fo.acquire(
        tmp_path / "locks", _scope(), run_id=run_id, hostname=hostname, pid=pid,
        now=lambda: NOW, pid_alive=pid_alive,
    )


# ── acquisition ────────────────────────────────────────────────────────────
def test_acquisition_writes_owner_metadata(tmp_path):
    result = _acquire(tmp_path)
    assert result.state == fo.STATE_ACQUIRED and result.acquired
    owner = json.loads((Path(result.lock_path) / "owner.json").read_text())
    assert owner["runId"] == "run-1"
    assert owner["backend"] == BACKEND
    assert owner["accountFingerprint"] == fo.account_fingerprint(EMAIL)
    assert owner["fixtureVersion"] == "REG-9"
    assert owner["hostname"] == "this-host"
    assert owner["pid"] == 100
    assert owner["acquiredAt"] == NOW
    assert owner["exclusivityScope"] == "host-local"
    assert "processStartHint" in owner  # may be null off-Linux; always present


def test_account_fingerprint_is_short_hash_not_the_email():
    fp = fo.account_fingerprint(EMAIL)
    assert len(fp) == 12 and EMAIL not in fp
    assert fp == fo.account_fingerprint(EMAIL)  # deterministic


def test_evidence_records_host_local_scope_and_limitation(tmp_path):
    evidence = _acquire(tmp_path).to_dict()
    assert evidence["exclusivityScope"] == "host-local"
    assert "cross-host" in evidence["exclusivityLimitation"].lower()


def test_no_secret_or_full_email_in_any_lock_file(tmp_path):
    result = _acquire(tmp_path)
    fo.recover_stale(  # also exercise the audit record's content
        tmp_path / "locks", _scope(), recovering_run_id="run-2", reason="test",
        hostname="this-host", now=lambda: NOW, pid_alive=lambda pid: False,
    )
    for path in (tmp_path / "locks").rglob("*.json"):
        text = path.read_text()
        assert EMAIL not in text, path
        assert PASSWORD not in text, path
    assert EMAIL not in json.dumps(result.to_dict())


def test_unavailable_when_lock_root_cannot_be_created(tmp_path):
    blocker = tmp_path / "locks"
    blocker.write_text("a file where the lock root should be\n")
    result = _acquire(tmp_path)
    assert result.state == fo.STATE_UNAVAILABLE


# ── contention / classification ────────────────────────────────────────────
def test_second_acquire_sees_active_owner(tmp_path):
    first = _acquire(tmp_path, run_id="run-1", pid=100)
    second = _acquire(tmp_path, run_id="run-2", pid=200, pid_alive=lambda pid: True)
    assert first.state == fo.STATE_ACQUIRED
    assert second.state == fo.STATE_ACTIVE_OWNER
    assert second.owner["runId"] == "run-1"  # owner metadata surfaced for evidence
    # the original lock is untouched
    assert Path(first.lock_path).is_dir()


def test_dead_owner_pid_is_stale_but_never_auto_broken(tmp_path):
    _acquire(tmp_path, run_id="run-1", pid=100)
    second = _acquire(tmp_path, run_id="run-2", pid=200, pid_alive=lambda pid: False)
    assert second.state == fo.STATE_STALE_LOCK
    # acquire never breaks the lock, even when stale
    assert (Path(second.lock_path) / "owner.json").is_file()


def test_foreign_host_lock_is_never_stale(tmp_path):
    _acquire(tmp_path, run_id="run-1", hostname="other-host", pid=100)
    second = fo.acquire(
        tmp_path / "locks", _scope(), run_id="run-2", hostname="this-host", pid=200,
        now=lambda: NOW, pid_alive=lambda pid: False,  # even with every pid "dead"
    )
    assert second.state == fo.STATE_FOREIGN_HOST_LOCK
    assert "never stale" in second.detail


def test_missing_or_corrupt_owner_json_is_interrupted_owner(tmp_path):
    result = _acquire(tmp_path)
    owner_file = Path(result.lock_path) / "owner.json"
    owner_file.write_text("{not json")
    second = _acquire(tmp_path, run_id="run-2")
    assert second.state == fo.STATE_INTERRUPTED_OWNER
    owner_file.unlink()
    third = _acquire(tmp_path, run_id="run-3")
    assert third.state == fo.STATE_INTERRUPTED_OWNER


def test_different_fixture_version_is_a_different_lock(tmp_path):
    a = fo.acquire(tmp_path / "locks", _scope("REG-9"), run_id="run-1",
                   hostname="h", pid=1, now=lambda: NOW)
    b = fo.acquire(tmp_path / "locks", _scope("REG-10"), run_id="run-2",
                   hostname="h", pid=2, now=lambda: NOW)
    assert a.state == b.state == fo.STATE_ACQUIRED
    assert a.lock_path != b.lock_path


# ── release ────────────────────────────────────────────────────────────────
def test_owner_release_removes_the_lock(tmp_path):
    result = _acquire(tmp_path)
    released = fo.release(tmp_path / "locks", _scope(), run_id="run-1")
    assert released.state == fo.STATE_RELEASED
    assert not Path(result.lock_path).exists()


def test_release_by_non_owner_is_an_error_result_not_a_removal(tmp_path):
    result = _acquire(tmp_path, run_id="run-1")
    refused = fo.release(tmp_path / "locks", _scope(), run_id="someone-else")
    assert refused.state == fo.STATE_NOT_OWNER
    assert Path(result.lock_path).is_dir()  # still held


def test_release_without_a_lock_is_not_held(tmp_path):
    assert fo.release(tmp_path / "locks", _scope(), run_id="run-1").state == fo.STATE_NOT_HELD


def test_release_in_finally_pattern_always_releases(tmp_path):
    # The focused-verify integration releases in a finally even when the
    # orchestration raises; the module-level contract is simply that release
    # after acquire always succeeds for the owner.
    _acquire(tmp_path, run_id="run-1")
    try:
        raise RuntimeError("orchestration exploded")
    except RuntimeError:
        pass
    finally:
        released = fo.release(tmp_path / "locks", _scope(), run_id="run-1")
    assert released.state == fo.STATE_RELEASED
    assert fo.status(tmp_path / "locks", _scope()).state == fo.STATE_NOT_HELD


# ── explicit stale recovery ────────────────────────────────────────────────
def test_recover_stale_writes_audit_before_removing(tmp_path):
    _acquire(tmp_path, run_id="run-1", pid=100)
    recovered = fo.recover_stale(
        tmp_path / "locks", _scope(), recovering_run_id="run-2",
        reason="owner crashed", hostname="this-host", now=lambda: NOW,
        pid_alive=lambda pid: False,
    )
    assert recovered.state == fo.STATE_RECOVERED
    assert not Path(recovered.lock_path).exists()
    audit = json.loads(Path(recovered.audit_path).read_text())
    assert audit["recoveredOwner"]["runId"] == "run-1"
    assert audit["recoveringRunId"] == "run-2"
    assert audit["recoveredAt"] == NOW
    assert audit["reason"] == "owner crashed"
    assert audit["exclusivityScope"] == "host-local"


def test_recover_stale_refuses_a_live_owner(tmp_path):
    _acquire(tmp_path, run_id="run-1")
    refused = fo.recover_stale(
        tmp_path / "locks", _scope(), recovering_run_id="run-2", reason="r",
        hostname="this-host", pid_alive=lambda pid: True,
    )
    assert refused.state == fo.STATE_RECOVERY_REFUSED
    assert (Path(refused.lock_path) / "owner.json").is_file()


def test_recover_stale_refuses_foreign_host_and_interrupted(tmp_path):
    _acquire(tmp_path, run_id="run-1", hostname="other-host")
    foreign = fo.recover_stale(
        tmp_path / "locks", _scope(), recovering_run_id="run-2", reason="r",
        hostname="this-host", pid_alive=lambda pid: False,
    )
    assert foreign.state == fo.STATE_RECOVERY_REFUSED
    assert "foreign_host_lock" in foreign.detail
    (Path(foreign.lock_path) / "owner.json").unlink()
    interrupted = fo.recover_stale(
        tmp_path / "locks", _scope(), recovering_run_id="run-2", reason="r",
        hostname="this-host", pid_alive=lambda pid: False,
    )
    assert interrupted.state == fo.STATE_RECOVERY_REFUSED
    assert "interrupted_owner" in interrupted.detail
    assert Path(interrupted.lock_path).is_dir()  # never removed


# ── status ─────────────────────────────────────────────────────────────────
def test_status_reports_not_held_then_classification(tmp_path):
    assert fo.status(tmp_path / "locks", _scope()).state == fo.STATE_NOT_HELD
    _acquire(tmp_path, run_id="run-1")
    held = fo.status(tmp_path / "locks", _scope(), hostname="this-host",
                     pid_alive=lambda pid: True)
    assert held.state == fo.STATE_ACTIVE_OWNER
    assert held.owner["runId"] == "run-1"


def test_default_pid_alive_probes_real_processes():
    import os

    assert fo.default_pid_alive(os.getpid()) is True
