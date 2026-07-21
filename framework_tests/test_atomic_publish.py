"""Crash-recoverable directory publication (Priority 4, this session).

Covers atomic_publish.py directly (independent of its two callers,
release_candidate.py and release_bundle_assembly.py):

  * a normal publish activates a version and leaves no transient artifact;
  * a build-time or verify-time failure aborts with the previous version (if
    any) completely untouched, and no orphaned temp directory;
  * a lock file serialises concurrent writers, and a lock abandoned by a
    dead process is reclaimed rather than wedging forever;
  * simulated interruption at each phase of a transaction (an orphaned temp
    dir with no journal; a journal naming a version that was never finished;
    a journal naming a version that WAS finished but the pointer swap itself
    was interrupted; a fully-committed journal not yet deleted) all recover
    to a valid, discoverable pointer on the next call;
  * a killed process leaves the PREVIOUS version installable/usable in every
    case -- there is never a window where pub_root is absent after a prior
    successful publish.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from calee_regression import atomic_publish as ap


def _build_with(content: dict):
    def _build(tmp_dir):
        for name, data in content.items():
            (tmp_dir / name).write_bytes(data)
    return _build


def _read(pub_root, name):
    return (pub_root / name).read_bytes()


# ── basic publish / activate ────────────────────────────────────────────


def test_publish_activates_content_and_pointer_resolves(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    assert pub_root.is_symlink()
    assert _read(pub_root, "a.txt") == b"v1"


def test_publish_no_transient_artifacts_after_success(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    siblings = {p.name for p in pub_root.parent.iterdir() if p != pub_root}
    assert siblings == {".candidate.versions"}
    versions = list((pub_root.parent / ".candidate.versions").iterdir())
    assert len(versions) == 1
    assert not versions[0].name.startswith(".tmp-")


def test_republish_switches_content_and_removes_previous_version(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    ap.publish_version(pub_root, _build_with({"a.txt": b"v2"}))
    assert _read(pub_root, "a.txt") == b"v2"
    versions = list((pub_root.parent / ".candidate.versions").iterdir())
    assert len(versions) == 1, "the old version must be removed only AFTER activation, and then removed"


def test_republish_with_identical_content_dedups_to_one_version(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"same"}))
    ap.publish_version(pub_root, _build_with({"a.txt": b"same"}))
    versions = list((pub_root.parent / ".candidate.versions").iterdir())
    assert len(versions) == 1


def test_directory_content_id_stable_and_sensitive_to_change(tmp_path):
    d1 = tmp_path / "d1"
    d1.mkdir()
    (d1 / "a.txt").write_bytes(b"x")
    (d1 / "b.txt").write_bytes(b"y")
    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / "b.txt").write_bytes(b"y")
    (d2 / "a.txt").write_bytes(b"x")
    assert ap.directory_content_id(d1) == ap.directory_content_id(d2)

    (d2 / "a.txt").write_bytes(b"CHANGED")
    assert ap.directory_content_id(d1) != ap.directory_content_id(d2)


def test_directory_content_id_excludes_named_file(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_bytes(b"x")
    before = ap.directory_content_id(d, exclude={"self.json"})
    (d / "self.json").write_bytes(b"anything, changes constantly")
    after = ap.directory_content_id(d, exclude={"self.json"})
    assert before == after


# ── build/verify failures leave the previous version untouched ─────────


def test_build_failure_raises_publish_error_and_leaves_no_pointer_on_first_publish(tmp_path):
    pub_root = tmp_path / "candidate"

    def _boom(tmp_dir):
        raise OSError("simulated failure")

    with pytest.raises(ap.PublishError):
        ap.publish_version(pub_root, _boom)
    assert not pub_root.exists()
    # No orphaned temp directory left behind.
    versions_dir = pub_root.parent / ".candidate.versions"
    assert not versions_dir.exists() or list(versions_dir.iterdir()) == []


def test_build_failure_leaves_previous_version_active(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))

    def _boom(tmp_dir):
        (tmp_dir / "a.txt").write_bytes(b"partial")
        raise OSError("simulated failure")

    with pytest.raises(ap.PublishError):
        ap.publish_version(pub_root, _boom)
    assert _read(pub_root, "a.txt") == b"v1"
    versions_dir = pub_root.parent / ".candidate.versions"
    assert len(list(versions_dir.iterdir())) == 1


def test_verify_failure_aborts_before_activation(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))

    def _verify_fails(tmp_dir):
        return ["deliberately rejected"]

    with pytest.raises(ap.PublishError, match="deliberately rejected"):
        ap.publish_version(pub_root, _build_with({"a.txt": b"v2"}), verify_fn=_verify_fails)
    assert _read(pub_root, "a.txt") == b"v1"


def test_rename_boundary_failure_is_wrapped_and_recovered_next_call(tmp_path, monkeypatch):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))

    real_rename = os.rename

    def _flaky_rename(src, dst):
        if ".tmp-" in str(src):
            raise OSError("simulated crash renaming tmp dir into versions/")
        return real_rename(src, dst)

    monkeypatch.setattr(ap.os, "rename", _flaky_rename)
    with pytest.raises(ap.PublishError):
        ap.publish_version(pub_root, _build_with({"a.txt": b"v2"}))
    monkeypatch.undo()

    # Previous version still active; no journal/lock left dangling.
    assert _read(pub_root, "a.txt") == b"v1"
    assert not (pub_root.parent / ".candidate.journal.json").exists()
    assert not (pub_root.parent / ".candidate.lock").exists()

    # A subsequent, un-flaky publish succeeds normally.
    ap.publish_version(pub_root, _build_with({"a.txt": b"v3"}))
    assert _read(pub_root, "a.txt") == b"v3"


def test_swap_boundary_failure_recovers_forward_on_next_call(tmp_path, monkeypatch):
    """Simulates a crash exactly at the pointer-swap step: the new version was
    fully written+verified (so it's safe to activate), but the swap raised.
    Priority 4 requires the NEXT invocation to finish (not lose) that
    interrupted transaction."""
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))

    real_replace = os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        if str(dst) == str(pub_root):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated crash during pointer swap")
        return real_replace(src, dst)

    monkeypatch.setattr(ap.os, "replace", _flaky_replace)
    with pytest.raises(ap.PublishError):
        ap.publish_version(pub_root, _build_with({"a.txt": b"v2"}))
    monkeypatch.undo()

    # The failed swap left the new version fully written (just unpointed) and
    # a journal recording the in-flight transaction -- recover() (called at
    # the top of the next publish_version, and directly here) must finish
    # activating it rather than leaving pub_root on the stale v1 forever.
    actions = ap.recover(pub_root)
    assert _read(pub_root, "a.txt") == b"v2"
    assert any("resumed interrupted publish" in a for a in actions)
    assert not (pub_root.parent / ".candidate.journal.json").exists()


# ── simulated interruption scenarios (crafted on-disk state) ────────────


def test_recover_discards_orphaned_tmp_dir_with_no_journal(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    versions_dir = pub_root.parent / ".candidate.versions"
    orphan = versions_dir / ".tmp-abandoned"
    orphan.mkdir()
    (orphan / "partial.txt").write_bytes(b"never finished")

    ap.recover(pub_root)
    assert not orphan.exists()
    assert _read(pub_root, "a.txt") == b"v1"


def test_recover_discards_interrupted_publish_whose_version_was_never_written(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    paths = ap._Paths(pub_root)
    # Craft a journal claiming a swap-in-progress to a version that was
    # never actually written (crash happened before the rename into
    # versions/ completed).
    paths.journal_path.write_text(json.dumps({
        "newVersion": "deadbeef" * 4, "previousVersion": ap._current_version_name(paths), "phase": "swapping",
    }))
    actions = ap.recover(pub_root)
    assert any("discarded an interrupted publish" in a for a in actions)
    assert _read(pub_root, "a.txt") == b"v1"
    assert not paths.journal_path.exists()


def test_recover_finishes_swap_when_new_version_exists_but_pointer_not_yet_updated(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    paths = ap._Paths(pub_root)
    previous = ap._current_version_name(paths)

    # Manually build a second version directory (as publish_version would,
    # up to but not including the pointer swap) and a journal describing it.
    new_dir = paths.versions_dir / "manually-built-v2"
    new_dir.mkdir()
    (new_dir / "a.txt").write_bytes(b"v2")
    paths.journal_path.write_text(json.dumps({
        "newVersion": "manually-built-v2", "previousVersion": previous, "phase": "swapping",
    }))

    actions = ap.recover(pub_root)
    assert _read(pub_root, "a.txt") == b"v2"
    assert any("resumed interrupted publish" in a for a in actions)
    assert not paths.journal_path.exists()
    # The old version is cleaned up now that activation is confirmed.
    assert not (paths.versions_dir / previous).exists()


def test_recover_clears_stale_journal_when_swap_already_committed(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    paths = ap._Paths(pub_root)
    current = ap._current_version_name(paths)
    # The swap actually completed; only journal cleanup was interrupted.
    paths.journal_path.write_text(json.dumps({
        "newVersion": current, "previousVersion": None, "phase": "swapping",
    }))
    actions = ap.recover(pub_root)
    assert any("already committed" in a for a in actions)
    assert _read(pub_root, "a.txt") == b"v1"
    assert not paths.journal_path.exists()


def test_recover_last_resort_repoints_missing_pointer_at_newest_version(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    paths = ap._Paths(pub_root)
    # Simulate total loss of the pointer itself (e.g. an out-of-band delete),
    # with no journal at all to explain it.
    pub_root.unlink()
    assert not pub_root.exists()

    actions = ap.recover(pub_root)
    assert pub_root.exists()
    assert _read(pub_root, "a.txt") == b"v1"
    assert any("pointer was missing" in a for a in actions)


def test_a_previous_valid_version_is_never_absent_across_any_interruption_point(tmp_path):
    """End-to-end sweep: interrupt the publish transaction at every step in
    turn and confirm pub_root is ALWAYS either the old or the new valid
    version -- never absent, never partial -- immediately after the failure
    (no recovery call needed) and after recovery."""
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))

    boundaries = ["build", "verify", "rename", "swap"]
    for boundary in boundaries:
        def _build(tmp_dir, boundary=boundary):
            if boundary == "build":
                raise OSError("boom")
            (tmp_dir / "a.txt").write_bytes(b"vX")

        def _verify(tmp_dir, boundary=boundary):
            if boundary == "verify":
                return ["boom"]
            return []

        import calee_regression.atomic_publish as ap_mod
        patched = {}
        if boundary == "rename":
            real = os.rename
            def _fake_rename(src, dst, real=real):
                if ".tmp-" in str(src):
                    raise OSError("boom")
                return real(src, dst)
            patched["rename"] = (ap_mod.os.rename, _fake_rename)
            ap_mod.os.rename = _fake_rename
        elif boundary == "swap":
            real = os.replace
            def _fake_replace(src, dst, real=real):
                if str(dst) == str(pub_root):
                    raise OSError("boom")
                return real(src, dst)
            patched["replace"] = (ap_mod.os.replace, _fake_replace)
            ap_mod.os.replace = _fake_replace

        try:
            with pytest.raises(ap.PublishError):
                ap.publish_version(pub_root, _build, verify_fn=_verify)
        finally:
            for attr, (orig, _fake) in patched.items():
                setattr(ap_mod.os, attr, orig)

        # Immediately after the interruption, pub_root must resolve to SOME
        # valid, readable version (old or new) -- never be absent/broken.
        assert pub_root.exists(), f"pub_root absent right after a {boundary}-boundary failure"
        content = (pub_root / "a.txt").read_bytes()
        assert content in (b"v1", b"vX"), f"unexpected content after {boundary} failure: {content!r}"

        ap.recover(pub_root)
        assert pub_root.exists(), f"pub_root absent after recover() following a {boundary}-boundary failure"
        content = (pub_root / "a.txt").read_bytes()
        assert content in (b"v1", b"vX"), f"unexpected content after recovering {boundary} failure: {content!r}"


# ── locking ──────────────────────────────────────────────────────────────


def test_concurrent_writer_is_rejected(tmp_path):
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    paths.lock_path.write_text(json.dumps({"pid": os.getpid(), "acquiredAt": time.time()}))
    with pytest.raises(ap.ConcurrentWriterError):
        ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}), lock_timeout=0.2)
    paths.lock_path.unlink()


def test_lock_abandoned_by_dead_process_is_reclaimed(tmp_path):
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    # A pid essentially guaranteed not to be alive in this test process tree.
    dead_pid = 2**30
    paths.lock_path.write_text(json.dumps({"pid": dead_pid, "acquiredAt": time.time()}))
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    assert _read(pub_root, "a.txt") == b"v1"


def test_lock_older_than_stale_threshold_is_reclaimed_even_if_pid_alive(tmp_path):
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    ancient = time.time() - (ap._STALE_LOCK_SECONDS + 3600)
    paths.lock_path.write_text(json.dumps({"pid": os.getpid(), "acquiredAt": ancient}))
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    assert _read(pub_root, "a.txt") == b"v1"


def test_corrupt_lock_file_is_treated_as_abandoned(tmp_path):
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    paths.lock_path.write_text("not json")
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    assert _read(pub_root, "a.txt") == b"v1"
