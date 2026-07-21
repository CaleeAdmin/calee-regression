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
import multiprocessing
import os
import threading
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


def test_lock_abandoned_by_dead_process_on_same_host_is_reclaimed(tmp_path):
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    # A pid essentially guaranteed not to be alive in this test process tree.
    dead_pid = 2**30
    paths.lock_path.write_text(json.dumps({"host": ap._current_host(), "pid": dead_pid, "acquiredAt": time.time()}))
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    assert _read(pub_root, "a.txt") == b"v1"


def test_lock_with_live_pid_on_same_host_is_never_reclaimed_regardless_of_age(tmp_path):
    """Priority 4 requirement 4: a lock owned by a DEMONSTRABLY LIVE PID must
    never be reclaimed solely because it is old. This is the corrected
    behaviour -- the previous (pre-Priority-4) implementation reclaimed any
    lock older than _STALE_LOCK_SECONDS even when the owning PID was
    confirmed alive; that was exactly the fail-open bug this closes."""
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    ancient = time.time() - (ap._STALE_LOCK_SECONDS + 3600)
    paths.lock_path.write_text(
        json.dumps({"host": ap._current_host(), "pid": os.getpid(), "acquiredAt": ancient, "token": "live-owner"})
    )
    with pytest.raises(ap.ConcurrentWriterError):
        ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}), lock_timeout=0.2)
    # The live owner's lock file must survive completely untouched.
    assert json.loads(paths.lock_path.read_text())["token"] == "live-owner"
    paths.lock_path.unlink()


def test_lock_from_different_host_old_age_is_reclaimed_even_with_a_locally_live_looking_pid(tmp_path):
    """The documented safe reclaim rule's OTHER branch: a lock recorded for a
    DIFFERENT host cannot be PID-checked from here at all (the shared-
    filesystem scenario the module has always described) -- even though the
    recorded pid happens to number-match a real, live local process, that
    coincidence must not be trusted; only the age-based fallback applies."""
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    ancient = time.time() - (ap._STALE_LOCK_SECONDS + 3600)
    paths.lock_path.write_text(
        json.dumps({"host": "some-other-machine", "pid": os.getpid(), "acquiredAt": ancient})
    )
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    assert _read(pub_root, "a.txt") == b"v1"


def test_lock_from_different_host_young_age_is_protected(tmp_path):
    """The flip side: a different-host lock that is still YOUNG must be
    protected -- the age-based fallback only reclaims once genuinely stale."""
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    paths.lock_path.write_text(
        json.dumps({"host": "some-other-machine", "pid": os.getpid(), "acquiredAt": time.time(), "token": "remote-owner"})
    )
    with pytest.raises(ap.ConcurrentWriterError):
        ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}), lock_timeout=0.2)
    paths.lock_path.unlink()


def test_corrupt_lock_file_is_treated_as_abandoned(tmp_path):
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    paths.lock_path.write_text("not json")
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))
    assert _read(pub_root, "a.txt") == b"v1"


def test_lock_file_records_host_pid_token_and_lease_timestamp(tmp_path):
    """Priority 4 requirement 5: the lock file itself must record a host
    identifier, process identifier, random owner token, and lease timestamp
    -- not just a bare pid+timestamp."""
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    seen = {}

    def _build(tmp_dir):
        # While the lock is held, inspect what was actually written.
        seen.update(json.loads(paths.lock_path.read_text()))
        (tmp_dir / "a.txt").write_bytes(b"v1")

    ap.publish_version(pub_root, _build)
    assert seen["host"] == ap._current_host()
    assert seen["pid"] == os.getpid()
    assert isinstance(seen["token"], str) and len(seen["token"]) >= 16
    assert isinstance(seen["acquiredAt"], (int, float))
    # Released after the publish completes.
    assert not paths.lock_path.exists()


def test_write_lock_file_is_never_observable_partially_written(tmp_path):
    """Direct regression test for a real torn-write race (Priority 4
    bugfix, this session): a concurrent reader must never observe
    ``lock_path`` existing with empty or unparseable content -- only
    "absent" or "one complete, valid JSON object". The original
    implementation created the file with ``O_CREAT | O_EXCL`` and then
    wrote its JSON content as a separate step, leaving exactly that window
    open; ``_read_lock_file`` landing in it got a ``JSONDecodeError``,
    which ``_lock_is_stale`` (correctly, for an actually-corrupt leftover)
    treated as "abandoned -- reclaim it", so a live, mid-write owner could
    have its lock stolen out from under it. This reproduced as a genuine,
    non-rare flake in the two-real-threads concurrency tests below."""
    lock_path = tmp_path / "candidate.lock"
    stop = threading.Event()
    bad_reads = []

    def _hammer_read():
        while not stop.is_set():
            try:
                text = lock_path.read_text()
            except OSError:
                continue
            if text == "":
                bad_reads.append("empty")
                continue
            try:
                json.loads(text)
            except ValueError:
                bad_reads.append(text)

    reader = threading.Thread(target=_hammer_read)
    reader.start()
    try:
        for i in range(300):
            try:
                lock_path.unlink()
            except OSError:
                pass
            ap._write_lock_file(lock_path, {"host": "h", "pid": i, "token": f"t{i}", "acquiredAt": float(i)})
    finally:
        stop.set()
        reader.join(timeout=5)

    assert bad_reads == []


def test_release_does_not_delete_a_lock_reclaimed_by_someone_else(tmp_path):
    """Compare-and-delete on release (Priority 4): if the lock file no longer
    names OUR token when we finish (e.g. a reclaimer decided we were
    abandoned and took over), releasing must NOT delete it -- that would
    destroy the new owner's live lock and let a third acquirer in."""
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)

    with ap._lock(paths, timeout=1.0):
        # Simulate a reclaimer replacing our lock file with its own while we
        # still (incorrectly, from the reclaimer's point of view) believe we
        # hold it.
        paths.lock_path.unlink()
        paths.lock_path.write_text(json.dumps({
            "host": ap._current_host(), "pid": os.getpid() + 1, "token": "someone-elses-token",
            "acquiredAt": time.time(),
        }))

    # Our __exit__ must have left the other owner's lock file untouched.
    assert json.loads(paths.lock_path.read_text())["token"] == "someone-elses-token"


# ── real concurrency: threads/processes actually racing (Priority 4) ───────


def test_two_concurrent_publications_are_serialized_not_corrupted(tmp_path):
    """Two real threads calling publish_version() for the SAME pub_root at
    the same time must serialize through the lock -- neither ever sees a
    partially-written pub_root, and the final state is exactly one of the
    two builds' content in full, never a mix or a crash."""
    pub_root = tmp_path / "candidate"
    start = threading.Barrier(2)
    errors = []

    def _publish(content):
        start.wait(timeout=5)
        try:
            ap.publish_version(pub_root, _build_with({"a.txt": content}))
        except Exception as exc:  # pragma: no cover - surfaced via errors below
            errors.append(exc)

    threads = [threading.Thread(target=_publish, args=(c,)) for c in (b"from-A", b"from-B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    final = _read(pub_root, "a.txt")
    assert final in (b"from-A", b"from-B"), f"unexpected/corrupted final content: {final!r}"
    paths = ap._Paths(pub_root)
    assert not paths.lock_path.exists()
    assert not paths.journal_path.exists()


def test_recovery_and_publication_started_simultaneously_do_not_corrupt(tmp_path):
    """A real recover() call and a real publish_version() call for the same
    root, started at the same time, must serialize through the same lock --
    recover() must never observe (or race) a publish that is mid-flight."""
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"seed"}))

    start = threading.Barrier(2)
    errors = []

    def _do_recover():
        start.wait(timeout=5)
        try:
            ap.recover(pub_root)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def _do_publish():
        start.wait(timeout=5)
        try:
            ap.publish_version(pub_root, _build_with({"a.txt": b"raced-publish"}))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=_do_recover), threading.Thread(target=_do_publish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert _read(pub_root, "a.txt") in (b"seed", b"raced-publish")
    paths = ap._Paths(pub_root)
    assert not paths.lock_path.exists()
    assert not paths.journal_path.exists()


def test_recover_blocks_while_publish_holds_the_lock(tmp_path):
    """Priority 4 requirement 3: the public recover() must acquire the SAME
    lock publish_version uses -- demonstrated by recover() genuinely
    blocking (not racing ahead) while another thread holds it, then
    proceeding once it's released."""
    pub_root = tmp_path / "candidate"
    paths = ap._Paths(pub_root)
    publisher_may_finish = threading.Event()
    publisher_started = threading.Event()

    def _slow_build(tmp_dir):
        (tmp_dir / "a.txt").write_bytes(b"slow")
        publisher_started.set()
        assert publisher_may_finish.wait(timeout=5), "test setup error: never released"

    publish_thread = threading.Thread(target=lambda: ap.publish_version(pub_root, _slow_build))
    publish_thread.start()
    assert publisher_started.wait(timeout=5), "publish never reached its build step"
    assert paths.lock_path.exists(), "the lock must already be held by the time build_fn runs"

    recover_result = {}
    recover_thread = threading.Thread(
        target=lambda: recover_result.update(actions=ap.recover(pub_root, lock_timeout=5.0))
    )
    recover_thread.start()
    # recover() must still be blocked, waiting for the lock the publisher holds.
    recover_thread.join(timeout=0.5)
    assert recover_thread.is_alive(), "recover() proceeded without waiting for the publish lock"

    publisher_may_finish.set()
    publish_thread.join(timeout=5)
    recover_thread.join(timeout=5)
    assert not recover_thread.is_alive()
    assert "actions" in recover_result
    assert _read(pub_root, "a.txt") == b"slow"


def _child_die_after_journal_write(pub_root_str, content):
    """Run in a SEPARATE OS PROCESS (Priority 4 concurrency test): acquire
    the lock and perform every step _publish_locked performs up to and
    including the durable journal write, then hard-exit via os._exit --
    which, unlike a normal return/exception, never runs the lock's
    context-manager cleanup -- leaving the lock file and journal behind
    exactly as a real `kill -9` would, with a PID that is genuinely dead by
    the time the parent process checks it."""
    import os as _os
    from calee_regression import atomic_publish as _ap

    pub_root = _ap.Path(pub_root_str)
    paths = _ap._Paths(pub_root)
    # Keep a reference to the context-manager object alive for the rest of
    # this function: __enter__() alone returns nothing we hold onto, and an
    # unreferenced generator-based CM gets garbage-collected almost
    # immediately, which runs its `finally` (releasing the lock) via
    # GeneratorExit -- exactly the cleanup a real `kill -9` must NOT get.
    lock_cm = _ap._lock(paths, timeout=30.0)
    lock_cm.__enter__()  # never __exit__: simulates a kill
    paths.versions_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = _ap.Path(_ap.tempfile.mkdtemp(dir=paths.versions_dir, prefix=".tmp-"))
    (tmp_dir / "a.txt").write_bytes(content)
    _ap._fsync_tree(tmp_dir)
    version_name = _ap.directory_content_id(tmp_dir)
    _os.rename(str(tmp_dir), str(paths.versions_dir / version_name))
    previous_version = _ap._current_version_name(paths)
    paths.journal_path.write_bytes(_ap.json.dumps(
        {"newVersion": version_name, "previousVersion": previous_version, "phase": "swapping"}
    ).encode("utf-8"))
    assert lock_cm is not None  # keep the reference reachable up to here
    _os._exit(1)  # died here: journal durable, pointer swap never started


def _child_die_after_pointer_swap(pub_root_str, content):
    """Like _child_die_after_journal_write, but dies one step later: the
    pointer swap itself completed (the new content is already live) before
    the hard-exit, so the lock and journal are left behind even though
    activation already succeeded."""
    import os as _os
    from calee_regression import atomic_publish as _ap

    pub_root = _ap.Path(pub_root_str)
    paths = _ap._Paths(pub_root)
    lock_cm = _ap._lock(paths, timeout=30.0)
    lock_cm.__enter__()
    paths.versions_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = _ap.Path(_ap.tempfile.mkdtemp(dir=paths.versions_dir, prefix=".tmp-"))
    (tmp_dir / "a.txt").write_bytes(content)
    _ap._fsync_tree(tmp_dir)
    version_name = _ap.directory_content_id(tmp_dir)
    _os.rename(str(tmp_dir), str(paths.versions_dir / version_name))
    previous_version = _ap._current_version_name(paths)
    paths.journal_path.write_bytes(_ap.json.dumps(
        {"newVersion": version_name, "previousVersion": previous_version, "phase": "swapping"}
    ).encode("utf-8"))
    _ap._swap_pointer(paths, version_name)
    assert lock_cm is not None  # keep the reference reachable up to here
    _os._exit(1)  # died here: already committed, only cleanup never ran


def test_process_dies_after_journal_write_is_reclaimed_and_recovered(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))

    child = multiprocessing.Process(target=_child_die_after_journal_write, args=(str(pub_root), b"v2-crashed"))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 1

    paths = ap._Paths(pub_root)
    assert paths.lock_path.exists(), "test setup: the child must leave its lock file behind"
    held = json.loads(paths.lock_path.read_text())
    assert not ap._pid_alive(held["pid"]), "test setup: the child pid must actually be dead by now"
    assert paths.journal_path.exists(), "test setup: the journal must be left behind"
    # The crash happened BEFORE the pointer swap -- v1 must still be active.
    assert _read(pub_root, "a.txt") == b"v1"

    # The next writer must reclaim the dead lock and discard the interrupted
    # (never-activated) transaction before proceeding with its own publish.
    ap.publish_version(pub_root, _build_with({"a.txt": b"v3"}))
    assert _read(pub_root, "a.txt") == b"v3"
    assert not paths.lock_path.exists()
    assert not paths.journal_path.exists()


def test_process_dies_after_pointer_swap_is_reclaimed_and_cleaned_up(tmp_path):
    pub_root = tmp_path / "candidate"
    ap.publish_version(pub_root, _build_with({"a.txt": b"v1"}))

    child = multiprocessing.Process(target=_child_die_after_pointer_swap, args=(str(pub_root), b"v2-crashed"))
    child.start()
    child.join(timeout=10)
    assert child.exitcode == 1

    paths = ap._Paths(pub_root)
    assert paths.lock_path.exists(), "test setup: the child must leave its lock file behind"
    held = json.loads(paths.lock_path.read_text())
    assert not ap._pid_alive(held["pid"]), "test setup: the child pid must actually be dead by now"
    assert paths.journal_path.exists(), "test setup: the journal must be left behind"
    # The swap already happened before the crash -- new content is already
    # visible even before anything reclaims the lock or runs recovery.
    assert _read(pub_root, "a.txt") == b"v2-crashed"

    actions = ap.recover(pub_root)
    assert any("already committed" in a for a in actions)
    assert _read(pub_root, "a.txt") == b"v2-crashed"
    assert not paths.lock_path.exists()
    assert not paths.journal_path.exists()
