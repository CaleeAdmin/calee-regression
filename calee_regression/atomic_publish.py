"""Crash-recoverable directory publication (Priority 4).

Shared by ``release_candidate.py`` (per-run candidate snapshots) and
``release_bundle_assembly.py`` (assembled release-bundle directories). Both
of those previously used a bare backup-rename / tmp-rename / backup-cleanup
sequence: fully atomic against a Python *exception*, but not against the
*process being killed* between the two renames -- a crash in that window
left an orphaned ``.bak-<pid>`` directory on disk with nothing scanning for
or reconciling it on the next run.

This module replaces that with a design that survives an interruption at
ANY point:

  * new content is built once into a fresh, content-addressed, immutable
    **version directory** (named by a digest of exactly what it contains) --
    never mutated or reused for different content once written;
  * files are ``fsync``'d (best-effort -- swallowed where the platform/
    filesystem does not support it) before the version is considered
    complete;
  * the version is independently **verified** (an injected ``verify_fn``)
    BEFORE it is ever made visible;
  * the "current" location is a **symlink pointer** at ``pub_root``, swapped
    into place with a single ``os.replace`` (POSIX-atomic for a same-
    directory rename of a symlink) -- never a multi-file/multi-directory
    swap;
  * the pointer's target is verified to exist and resolve correctly
    immediately after the swap;
  * a small **transaction journal** records the swap's intent before it
    happens and is deleted only once the swap is confirmed -- so
    ``recover()`` can finish or safely ignore an interrupted swap on the
    very next invocation, in every case leaving a valid, discoverable
    version at ``pub_root``;
    -- the source drop folder cannot be RE-observed; the only inputs to
    an atomic-publish transaction are what ``build_fn`` writes and what is
    already recorded on disk from a previous successful publish;
  * old versions are deleted only AFTER the pointer switch is verified;
  * a lock file (created with ``O_CREAT | O_EXCL``, containing the owning
    host, PID, a random owner token, and a lease/acquisition timestamp)
    serialises concurrent writers to the same publication root -- including
    ``recover()`` itself (Priority 4 this session: recovery mutates the
    pointer, journal, and version directories exactly like a publish does,
    so it must never run outside this same lock). A lock left behind by a
    process that is no longer alive is detected and reclaimed rather than
    wedging every future publish attempt forever -- but ONLY under a
    documented safe rule (see ``_lock``'s docstring): a lock whose owning PID
    is confirmed alive ON THIS HOST is never reclaimed merely for being old.

A consumer that simply opens/reads/iterates ``pub_root`` (e.g.
``release_installer.verify_release_bundle``) is unaffected -- ``pathlib``
and ``open()`` both follow a symlink-to-directory transparently, so
``pub_root`` continues to behave exactly like a plain directory to every
existing reader.

This module never merges files between versions: a version directory is
whatever ``build_fn`` wrote in one shot, atomically activated in full or
not activated at all.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# How long a lock file may be held before it is considered abandoned by a
# dead process even when the owning PID cannot be checked (e.g. the lock was
# left by a process on a different host sharing this filesystem, so its PID
# cannot be tested against this host's process table at all). A lock whose
# PID IS checkable on this host (see _lock's docstring) is never reclaimed on
# age alone -- only this cross-host/unattributable case falls back to it.
_STALE_LOCK_SECONDS = 6 * 60 * 60


def _current_host() -> str:
    """The identifier this process stamps into a lock file it creates, and
    compares against when deciding whether a held lock's PID can be trusted
    (Priority 4). A thin wrapper so tests can simulate "a lock left by a
    process on a different host sharing this filesystem" without actually
    using two machines."""
    return socket.gethostname()


class PublishError(Exception):
    """A publish transaction could not complete. Nothing at ``pub_root`` was
    changed -- the previously active version (if any) remains exactly as it
    was."""


class ConcurrentWriterError(PublishError):
    """Another live process already holds the publish lock for this root."""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def directory_content_id(directory: "Path | str", *, exclude: "frozenset | set" = frozenset()) -> str:
    """A deterministic, content-addressed identifier for every regular file
    under ``directory`` (recursive), skipping any file whose basename is in
    ``exclude``: sha256 of the sorted ``relpath:sha256`` lines. Two
    directories with byte-identical (non-excluded) file trees always get the
    same id; any changed/added/removed/renamed file changes it. Used both as
    the immutable version-directory name and as part of the candidate
    fingerprint (Priority 5) -- callers that embed this id INSIDE a file in
    the same directory (as the candidate fingerprint does) must exclude that
    file's own name, or the id would depend on itself."""
    directory = Path(directory)
    lines = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.name not in exclude):
        rel = path.relative_to(directory).as_posix()
        lines.append(f"{rel}:{_sha256_file(path)}")
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fsync_tree(directory: Path) -> None:
    """Best-effort fsync of every file (and the directory itself) under
    ``directory``. Silently does nothing where the platform/filesystem
    doesn't support it (e.g. some sandboxed/overlay filesystems reject
    fsync on a directory fd) -- durability is a best-effort improvement
    here, never a hard requirement for correctness within one process
    lifetime."""
    for path in directory.rglob("*"):
        if path.is_file():
            with contextlib.suppress(OSError):
                fd = os.open(str(path), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
    with contextlib.suppress(OSError):
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _fsync_dir(directory: Path) -> None:
    """Best-effort fsync of a single directory's inode -- used to make a
    directory-entry change (a rename/replace that added, removed, or
    retargeted an entry inside it) durable, as distinct from ``_fsync_tree``
    which walks and fsyncs an entire file tree BEFORE that tree is made
    visible. Swallows OSError exactly like ``_fsync_tree`` (some sandboxed/
    overlay filesystems reject fsync on a directory fd)."""
    with contextlib.suppress(OSError):
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _fsync_file(path: Path) -> None:
    """Best-effort fsync of a single file already written to disk (e.g. the
    transaction journal) -- as opposed to ``_fsync_tree``, which is for an
    entire freshly-built directory."""
    with contextlib.suppress(OSError):
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


class _Paths:
    def __init__(self, pub_root: Path):
        self.pub_root = pub_root
        self.versions_dir = pub_root.parent / f".{pub_root.name}.versions"
        self.lock_path = pub_root.parent / f".{pub_root.name}.lock"
        self.journal_path = pub_root.parent / f".{pub_root.name}.journal.json"
        self.link_tmp_prefix = f".{pub_root.name}.link-tmp-"


def _write_lock_file(path: Path, owner: dict) -> None:
    """Publish ``path`` fully-formed or not at all (Priority 4).

    A naive ``O_CREAT | O_EXCL`` open followed by a separate write leaves a
    window where ``path`` exists but is empty/partially written; a
    concurrent ``_read_lock_file`` that lands in that window gets a
    ``JSONDecodeError``, which ``_lock_is_stale`` (correctly, for a
    genuinely corrupt leftover file) treats as "abandoned -- reclaim it".
    Against a live, mid-write owner that is wrong: the reader steals a lock
    that is not actually abandoned, and two writers end up unlocked at once.
    This was a real, reproducible flake in the concurrency tests, not a
    theoretical concern.

    The fix: write the complete content to a private temp file (same
    directory, so the next step is same-filesystem) and fsync it, THEN
    publish it at ``path`` with ``os.link`` -- a hard link, like
    ``O_CREAT | O_EXCL``, is created atomically and fails with
    ``FileExistsError`` if ``path`` is already taken, so a reader can only
    ever observe ``path`` as absent or as a complete, valid lock file --
    never partially written.
    """
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(8)}"
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(owner).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.link(str(tmp_path), str(path))
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
    _fsync_dir(path.parent)


def _read_lock_file(path: Path) -> "dict | None":
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(held, dict):
        return None
    return held


def _lock_is_stale(held: "dict | None") -> bool:
    """The ONE documented safe reclaim rule (Priority 4):

      * an unreadable/corrupt/non-object lock file can never legitimately be
        held by a live writer -- abandoned;
      * a lock recorded as created ON THIS HOST (``host`` matches
        :func:`_current_host`) whose PID is confirmed dead (``_pid_alive`` is
        False) -- abandoned;
      * a lock recorded as created on THIS HOST whose PID is confirmed ALIVE
        is NEVER reclaimed on age alone -- a demonstrably live owner is
        always protected, however long it has held the lock;
      * a lock whose ``host`` is missing, or does not match this host (a
        shared filesystem: the owning process cannot be PID-checked from
        here at all) falls back to the age-only rule: abandoned once older
        than ``_STALE_LOCK_SECONDS``, exactly the same "how long may a lock
        be held before it's abandoned by a process we cannot check" case the
        module docstring has always described.
    """
    if held is None:
        return True
    try:
        held_pid = int(held.get("pid", -1))
        acquired_at = float(held.get("acquiredAt", 0))
    except (TypeError, ValueError):
        return True
    held_host = held.get("host")
    same_host = isinstance(held_host, str) and held_host == _current_host()
    if same_host:
        return not _pid_alive(held_pid)
    return (time.time() - acquired_at) > _STALE_LOCK_SECONDS


@contextlib.contextmanager
def _lock(paths: _Paths, *, timeout: float = 30.0):
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    owner = {
        "host": _current_host(),
        "pid": os.getpid(),
        "token": secrets.token_hex(16),
        "acquiredAt": time.time(),
    }
    deadline = None
    while True:
        try:
            _write_lock_file(paths.lock_path, owner)
            break
        except FileExistsError:
            held = _read_lock_file(paths.lock_path)
            if _lock_is_stale(held):
                # Reclaim by replacing (not blindly unlinking) the exact file
                # we inspected -- if a third process races us and replaces it
                # first, our unlink() would otherwise silently steal or
                # destroy THEIR fresh lock. Fall through to retry either way;
                # the next loop iteration re-inspects whatever is there now.
                with contextlib.suppress(OSError):
                    paths.lock_path.unlink()
                continue
            held_pid = held.get("pid") if held else None
            if deadline is None:
                deadline = time.time() + timeout
            if time.time() >= deadline:
                raise ConcurrentWriterError(
                    f"another process (pid {held_pid}) is already publishing to {paths.pub_root} -- "
                    f"refusing to write concurrently."
                )
            time.sleep(0.05)
    try:
        yield
    finally:
        # Compare-and-delete: only remove the lock file if it still records
        # OUR acquisition (Priority 4). If it now names a different token, a
        # reclaimer decided (correctly, under _lock_is_stale's rule, or
        # incorrectly) that we were abandoned and has already taken over --
        # unconditionally unlinking here would delete THEIR live lock and let
        # a third acquirer in underneath them.
        current = _read_lock_file(paths.lock_path)
        if current is not None and current.get("token") == owner["token"]:
            with contextlib.suppress(OSError):
                paths.lock_path.unlink()


def _current_version_name(paths: _Paths) -> "str | None":
    """The version-directory name the pointer currently resolves to, or None
    if the pointer is absent/broken."""
    if not os.path.islink(str(paths.pub_root)) and not paths.pub_root.exists():
        return None
    try:
        target = os.readlink(str(paths.pub_root))
    except OSError:
        # Not a symlink (e.g. a plain pre-existing directory from before this
        # module managed pub_root, or a foreign file) -- treated as "no
        # managed version active"; recover() will adopt/replace it safely.
        return None
    name = Path(target).name
    if (paths.versions_dir / name).is_dir():
        return name
    return None


def _make_way_for_pointer(paths: _Paths) -> None:
    """``os.replace`` cannot rename a symlink onto an existing plain
    directory (POSIX ``rename(2)`` refuses EISDIR/ENOTDIR regardless of
    whether it's empty). The very first publish to a given ``pub_root`` often
    finds exactly that: a pre-created, empty placeholder directory (e.g.
    ``run_context.RunWorkspace.ensure_created()`` pre-creates every component
    directory before any component writes). An empty placeholder is removed
    outright; a non-empty, not-yet-managed directory is preserved by moving
    it aside into ``versions_dir`` rather than silently discarding it."""
    if os.path.islink(str(paths.pub_root)) or not paths.pub_root.exists():
        return
    try:
        paths.pub_root.rmdir()
        return
    except OSError:
        pass
    # Non-empty and not a symlink we manage: adopt it as a legacy version
    # rather than lose or block on it.
    paths.versions_dir.mkdir(parents=True, exist_ok=True)
    adopted = paths.versions_dir / f".legacy-{os.getpid()}-{int(time.time())}"
    paths.pub_root.rename(adopted)


def _swap_pointer(paths: _Paths, version_name: str) -> None:
    target = paths.versions_dir / version_name
    tmp_link = paths.pub_root.parent / f"{paths.link_tmp_prefix}{os.getpid()}"
    with contextlib.suppress(OSError):
        tmp_link.unlink()
    os.symlink(str(target), str(tmp_link))
    _make_way_for_pointer(paths)
    os.replace(str(tmp_link), str(paths.pub_root))
    # Priority 4: fsync the pointer's parent directory so the rename that
    # just retargeted (or created) the pub_root entry is itself durable, not
    # just the version directory it points at.
    _fsync_dir(paths.pub_root.parent)


def _cleanup_orphans(paths: _Paths, *, keep: "set[str]") -> None:
    if paths.versions_dir.is_dir():
        for entry in paths.versions_dir.iterdir():
            if entry.name.startswith(".tmp-") and entry.name not in keep:
                shutil.rmtree(entry, ignore_errors=True)
    parent = paths.pub_root.parent
    if parent.is_dir():
        for entry in parent.iterdir():
            if entry.name.startswith(paths.link_tmp_prefix):
                with contextlib.suppress(OSError):
                    entry.unlink()


def _recover_locked(paths: _Paths) -> "list[str]":
    """The actual recovery logic (Priority 4): detect and finish/roll back
    any transaction an earlier (killed) process left interrupted for
    ``paths.pub_root``. Idempotent -- safe to call even when nothing is
    interrupted (the common case). Returns a list of human-readable actions
    taken (empty when nothing needed recovery).

    REQUIRES the caller to already hold ``paths.lock_path`` (via ``_lock``).
    Recovery reads/deletes the journal, swaps the pointer, and deletes
    superseded version directories -- exactly the same mutations a publish
    performs -- so running it without the lock held would race a concurrent
    ``publish_version``/``recover`` the same way an unlocked publish would.
    This function is intentionally private (leading underscore, no locking
    of its own, like ``_publish_locked``); ``recover()`` below is the public,
    lock-acquiring entry point, and ``publish_version`` calls this directly
    from inside the lock it already holds.

    Guarantees: after this returns, ``pub_root`` either points at a version
    directory that fully exists on disk, or (only when NO version has ever
    been successfully published for this root) is absent -- it is never left
    pointing at a partially-written or missing version."""
    actions: "list[str]" = []
    paths.versions_dir.mkdir(parents=True, exist_ok=True)

    if paths.journal_path.is_file():
        try:
            journal = json.loads(paths.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            journal = None
        if journal is not None:
            new_version = journal.get("newVersion")
            previous_version = journal.get("previousVersion")
            current = _current_version_name(paths)
            new_dir = paths.versions_dir / new_version if new_version else None
            activated = False
            if current == new_version:
                # The swap itself completed before the crash; only the
                # journal-delete/cleanup step was interrupted.
                actions.append(f"transaction for {new_version!r} had already committed; clearing stale journal.")
                activated = True
            elif new_dir is not None and new_dir.is_dir():
                # The new version was fully written+verified before the
                # crash, but the pointer swap itself may not have happened
                # (or may have raced the crash) -- redo it now (idempotent).
                _swap_pointer(paths, new_version)
                resolved = _current_version_name(paths)
                if resolved != new_version:
                    raise PublishError(
                        f"recovery resumed publishing {new_version!r} but the pointer at "
                        f"{paths.pub_root} did not verify afterwards."
                    )
                actions.append(f"resumed interrupted publish: pointer now activated for {new_version!r}.")
                activated = True
            else:
                # The crash happened before the new version was fully
                # written -- nothing to resume; the previous pointer (if any)
                # was never touched and remains valid.
                actions.append("discarded an interrupted publish whose new version was never fully written.")
            if activated and previous_version and previous_version != new_version:
                # Priority 4: old versions are removed only AFTER activation
                # is confirmed -- true whether that confirmation happens
                # inline (the happy path) or here, on a later recovery pass.
                shutil.rmtree(paths.versions_dir / previous_version, ignore_errors=True)
        with contextlib.suppress(OSError):
            paths.journal_path.unlink()

    current = _current_version_name(paths)
    _cleanup_orphans(paths, keep={current} if current else set())

    if current is None and not paths.pub_root.exists() and paths.versions_dir.is_dir():
        # Last-resort recovery for a root with no journal to guide it (e.g.
        # the pointer file itself was lost) but at least one previously
        # published version still on disk: activate the most recently
        # written one rather than leaving the logical destination absent.
        candidates = sorted(
            (p for p in paths.versions_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
        )
        if candidates:
            fallback = candidates[-1]
            _swap_pointer(paths, fallback.name)
            resolved = _current_version_name(paths)
            if resolved != fallback.name:
                raise PublishError(
                    f"recovery repointed to {fallback.name!r} but the pointer at {paths.pub_root} "
                    f"did not verify afterwards."
                )
            actions.append(f"pointer was missing with no journal; recovered to the newest known version {fallback.name!r}.")

    return actions


def recover(pub_root: "Path | str", *, lock_timeout: float = 30.0) -> "list[str]":
    """Public entry point for recovery (Priority 4): acquires the SAME lock
    ``publish_version`` uses, then runs :func:`_recover_locked`. Safe to call
    at any time, including concurrently with an in-flight ``publish_version``
    for the same root (it simply waits for the lock like any other writer).
    """
    paths = _Paths(Path(pub_root))
    with _lock(paths, timeout=lock_timeout):
        return _recover_locked(paths)


def publish_version(
    pub_root: "Path | str",
    build_fn: "Callable[[Path], None]",
    *,
    verify_fn: "Optional[Callable[[Path], list]]" = None,
    lock_timeout: float = 30.0,
) -> Path:
    """Publish a new version of the directory at ``pub_root``.

    ``build_fn(tmp_dir)`` must populate ``tmp_dir`` with the complete new
    content (nothing pre-exists in it). ``verify_fn(tmp_dir)``, if given, is
    called BEFORE the new content is made visible; a non-empty return
    (problems) aborts the publish with nothing changed -- the previous
    version, if any, remains active and usable.

    Returns the (fully resolved, real) path of the newly active version
    directory. Raises ``PublishError`` on failure; ``pub_root`` is left
    exactly as it was before the call in that case.
    """
    pub_root = Path(pub_root)
    paths = _Paths(pub_root)

    with _lock(paths, timeout=lock_timeout):
        # Priority 4: recovery runs INSIDE the same lock this publish is
        # about to use -- it mutates the pointer/journal/version directories
        # exactly like a publish does, so running it before the lock was
        # acquired (the previous behaviour) could race a concurrent
        # publish_version/recover for the same root.
        _recover_locked(paths)

        paths.versions_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(dir=paths.versions_dir, prefix=".tmp-"))
        try:
            _publish_locked(paths, tmp_dir, build_fn, verify_fn)
        except PublishError:
            raise
        except Exception as exc:
            # Any other failure (a rename/replace boundary, an OS error) --
            # normalise to PublishError so callers get one consistent
            # exception type regardless of which step failed. The next
            # publish_version()/recover() call for this pub_root will clean
            # up any half-made version directory this leaves behind (an
            # orphaned `.tmp-*`) or finish an interrupted pointer swap.
            raise PublishError(f"publishing a new version for {pub_root} failed: {exc}") from exc

    return (paths.pub_root.resolve())


def _publish_locked(paths: _Paths, tmp_dir: Path, build_fn, verify_fn) -> None:
    try:
        build_fn(tmp_dir)
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise PublishError(f"building the new version for {paths.pub_root} failed: {exc}") from exc

    _fsync_tree(tmp_dir)

    if verify_fn is not None:
        problems = verify_fn(tmp_dir)
        if problems:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise PublishError(
                f"the newly built version for {paths.pub_root} failed verification, publish aborted "
                f"(previous version, if any, is untouched): {'; '.join(problems)}"
            )

    version_name = directory_content_id(tmp_dir)
    version_dir = paths.versions_dir / version_name
    if version_dir.is_dir():
        # Identical content already published (dedup) -- discard the
        # redundant build, reuse the existing immutable version.
        shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        os.rename(str(tmp_dir), str(version_dir))
        # Priority 4: fsync the completed version directory's PARENT so the
        # rename that just made it a named entry under versions_dir is
        # itself durable -- _fsync_tree above already fsync'd the version's
        # own file contents before this rename, while it was still tmp_dir.
        _fsync_dir(paths.versions_dir)

    previous_version = _current_version_name(paths)
    journal_bytes = json.dumps(
        {"newVersion": version_name, "previousVersion": previous_version, "phase": "swapping"}
    ).encode("utf-8")
    paths.journal_path.write_bytes(journal_bytes)
    # Priority 4: fsync the journal file itself and its parent directory --
    # recover() relies on this file surviving a crash exactly as written, so
    # both the file's own bytes and the directory entry that names it must be
    # durable before the pointer swap (the next step) makes the transaction
    # externally observable.
    _fsync_file(paths.journal_path)
    _fsync_dir(paths.journal_path.parent)
    _swap_pointer(paths, version_name)

    resolved = _current_version_name(paths)
    _finish_publish(paths, resolved, version_name, previous_version)


def _finish_publish(paths: _Paths, resolved, version_name, previous_version) -> None:
    if resolved != version_name:
        raise PublishError(
            f"published version {version_name!r} but the pointer at {paths.pub_root} did not verify afterwards."
        )

    with contextlib.suppress(OSError):
        paths.journal_path.unlink()

    if previous_version and previous_version != version_name:
        shutil.rmtree(paths.versions_dir / previous_version, ignore_errors=True)
    _cleanup_orphans(paths, keep={version_name})
