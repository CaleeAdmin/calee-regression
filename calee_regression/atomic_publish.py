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
    PID) serialises concurrent writers to the same publication root; a lock
    left behind by a process that is no longer alive is detected and
    reclaimed rather than wedging every future publish attempt forever.

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
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

# How long a lock file may be held before it is considered abandoned by a
# dead process even when the owning PID cannot be checked (e.g. the lock was
# left by a process on a different host sharing this filesystem).
_STALE_LOCK_SECONDS = 6 * 60 * 60


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


@contextlib.contextmanager
def _lock(paths: _Paths, *, timeout: float = 30.0):
    paths.pub_root.parent.mkdir(parents=True, exist_ok=True)
    deadline = None
    while True:
        try:
            fd = os.open(str(paths.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps({"pid": os.getpid(), "acquiredAt": time.time()}))
            break
        except FileExistsError:
            stale = False
            try:
                held = json.loads(paths.lock_path.read_text(encoding="utf-8"))
                held_pid = int(held.get("pid", -1))
                acquired_at = float(held.get("acquiredAt", 0))
            except (OSError, ValueError, json.JSONDecodeError):
                # An unreadable/corrupt lock file can never legitimately be
                # held by a live writer -- treat it as abandoned.
                stale = True
                held_pid = -1
                acquired_at = 0
            if not stale and not _pid_alive(held_pid):
                stale = True
            if not stale and (time.time() - acquired_at) > _STALE_LOCK_SECONDS:
                stale = True
            if stale:
                with contextlib.suppress(OSError):
                    paths.lock_path.unlink()
                continue
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


def recover(pub_root: "Path | str") -> "list[str]":
    """Detect and finish/roll back any transaction an earlier (killed)
    process left interrupted for ``pub_root``. Idempotent -- safe to call
    even when nothing is interrupted (the common case). Returns a list of
    human-readable actions taken (empty when nothing needed recovery).

    Guarantees: after this returns, ``pub_root`` either points at a version
    directory that fully exists on disk, or (only when NO version has ever
    been successfully published for this root) is absent -- it is never left
    pointing at a partially-written or missing version."""
    paths = _Paths(Path(pub_root))
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
            actions.append(f"pointer was missing with no journal; recovered to the newest known version {fallback.name!r}.")

    return actions


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
    recover(pub_root)

    with _lock(paths, timeout=lock_timeout):
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

    previous_version = _current_version_name(paths)
    paths.journal_path.write_text(
        json.dumps({"newVersion": version_name, "previousVersion": previous_version, "phase": "swapping"}),
        encoding="utf-8",
    )
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
