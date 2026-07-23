"""Host-local fixture ownership lock (concurrent-run protection).

The regression fixture is a SHARED production account with a deterministic
REG-* dataset: two overlapping runs against the same backend + account +
fixture version are dangerous (one run's fixture reset destroys the other
run's in-flight state). No backend-side lease exists and production cannot
be modified, so this module provides the strongest guarantee that is
actually available: a robust HOST-LOCAL lock.

Explicit limitation (recorded in every piece of evidence this module
produces): the lock guarantees exclusivity ONLY among runs on the current
host (``"exclusivityScope": "host-local"``). Cross-host exclusivity for a
shared production account is NOT established -- a run on another machine is
invisible to this lock, and a lock whose recorded hostname differs from ours
is treated as active/ambiguous, never stale.

Mechanism:

  * The lock scope key is backend URL + account fingerprint (sha256 of the
    regression email, first 12 hex chars -- never the secret or the full
    email) + fixture version, hashed into a filesystem-safe name.
  * Acquisition is an atomic ``os.mkdir`` of
    ``<lock_root>/fixture-<scope-hash>.lock/`` followed by writing
    ``owner.json`` inside (run id, host, pid, timestamps -- no secrets ever).
  * A lock is NEVER auto-broken. Stale recovery (same host, dead pid) is an
    explicit, separate operation (``recover_stale``) that writes an audit
    record before removing anything. ``interrupted_owner`` (owner.json
    missing/corrupt) and ``foreign_host_lock`` refuse recovery entirely.

Every external effect is injectable (clock, pid-liveness probe, hostname,
pid), so all states are unit-testable with no real processes.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

EXCLUSIVITY_SCOPE = "host-local"
EXCLUSIVITY_LIMITATION = (
    "This lock guarantees exclusivity only among runs on this host. No "
    "backend-side lease exists for the shared production regression account, "
    "so cross-host exclusivity is NOT established -- a concurrent run on "
    "another machine cannot be detected or excluded."
)

# Acquisition / inspection states.
STATE_ACQUIRED = "acquired"
STATE_ACTIVE_OWNER = "active_owner"          # same host, owner pid is alive
STATE_STALE_LOCK = "stale_lock"              # same host, owner pid is dead
STATE_FOREIGN_HOST_LOCK = "foreign_host_lock"  # other host -- liveness unprovable
STATE_INTERRUPTED_OWNER = "interrupted_owner"  # lock dir exists, owner.json unusable
STATE_UNAVAILABLE = "unavailable"            # OS error touching the lock root
# Release / recovery states.
STATE_RELEASED = "released"
STATE_NOT_OWNER = "not_owner"                # owner.json records a different run id
STATE_NOT_HELD = "not_held"                  # no lock directory exists
STATE_RECOVERED = "recovered"
STATE_RECOVERY_REFUSED = "recovery_refused"

OWNER_FILE_NAME = "owner.json"


def account_fingerprint(email: str) -> str:
    """A non-reversible, non-secret identifier for the regression account:
    the first 12 hex chars of sha256(email). Safe to record in lock files,
    evidence, and reports -- never the secret or the full email."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


def scope_hash(*, backend: str, account_fingerprint: str, fixture_version: str) -> str:
    """A filesystem-safe hash of the lock scope (backend URL + account
    fingerprint + fixture version): two runs collide on the lock exactly when
    they would collide on the fixture."""
    key = f"{backend}\n{account_fingerprint}\n{fixture_version}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def default_pid_alive(pid: int) -> bool:
    """True when a process with this pid exists on THIS host (signal 0).
    EPERM means the process exists but is not ours -- still alive."""
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _process_start_hint(pid: int) -> "str | None":
    """A best-effort process-start marker from /proc (Linux: field 22 of
    /proc/<pid>/stat, the process start time in clock ticks). None where
    /proc is unavailable (e.g. macOS) -- the hint is diagnostic only, never
    load-bearing for the staleness decision."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # Field 2 (comm) may contain spaces; everything after the closing
        # paren is space-separated, with starttime at index 19 there.
        after = stat_text.rsplit(")", 1)[1].split()
        return after[19]
    except (OSError, IndexError):
        return None


@dataclass(frozen=True)
class LockScope:
    """The identity of the fixture a lock protects. ``account_fingerprint``
    is already the non-secret sha256 prefix (see account_fingerprint)."""

    backend: str
    account_fingerprint: str
    fixture_version: str

    @property
    def hash(self) -> str:
        return scope_hash(
            backend=self.backend,
            account_fingerprint=self.account_fingerprint,
            fixture_version=self.fixture_version,
        )

    def lock_dir(self, lock_root: Path) -> Path:
        return Path(lock_root) / f"fixture-{self.hash}.lock"


@dataclass
class LockResult:
    """The typed outcome of acquire/release/recover_stale, carrying the
    observed owner metadata for evidence. Never contains a secret."""

    state: str
    lock_path: "str | None" = None
    owner: "dict | None" = None
    detail: str = ""
    audit_path: "str | None" = None
    exclusivity_scope: str = EXCLUSIVITY_SCOPE

    @property
    def acquired(self) -> bool:
        return self.state == STATE_ACQUIRED

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "lockPath": self.lock_path,
            "owner": dict(self.owner) if self.owner else None,
            "detail": self.detail,
            "auditPath": self.audit_path,
            "exclusivityScope": self.exclusivity_scope,
            "exclusivityLimitation": EXCLUSIVITY_LIMITATION,
        }

    # Alias so callers embedding this in reports read naturally.
    evidence = to_dict


def _owner_payload(
    scope: LockScope, *, run_id: str, hostname: str, pid: int, acquired_at: str
) -> dict:
    return {
        "runId": run_id,
        "backend": scope.backend,
        "accountFingerprint": scope.account_fingerprint,
        "fixtureVersion": scope.fixture_version,
        "hostname": hostname,
        "pid": pid,
        "processStartHint": _process_start_hint(pid),
        "acquiredAt": acquired_at,
        "exclusivityScope": EXCLUSIVITY_SCOPE,
    }


def _read_owner(lock_dir: Path) -> "dict | None":
    """The owner.json payload, or None when it is missing/unreadable/corrupt
    (an interrupted owner -- the acquiring process died between mkdir and
    write, or the file was damaged)."""
    try:
        data = json.loads((lock_dir / OWNER_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _classify_existing(
    lock_dir: Path,
    *,
    hostname: str,
    pid_alive: "Callable[[int], bool]",
) -> LockResult:
    """Classify an already-existing lock directory: interrupted / foreign /
    active / stale. Shared by acquire (contention) and recover_stale
    (re-verification)."""
    owner = _read_owner(lock_dir)
    if owner is None:
        return LockResult(
            state=STATE_INTERRUPTED_OWNER, lock_path=str(lock_dir),
            detail=(
                "lock directory exists but owner.json is missing/unreadable/corrupt -- "
                "the owning process may have been interrupted mid-acquisition. Ownership "
                "cannot be established, so this lock is never auto-recovered."
            ),
        )
    owner_host = owner.get("hostname")
    if owner_host != hostname:
        return LockResult(
            state=STATE_FOREIGN_HOST_LOCK, lock_path=str(lock_dir), owner=owner,
            detail=(
                f"lock is owned by a run on host {owner_host!r} (this host: {hostname!r}). "
                f"Liveness of a foreign-host process cannot be proven from here, so the "
                f"lock is treated as active/ambiguous -- never stale."
            ),
        )
    owner_pid = owner.get("pid")
    alive = isinstance(owner_pid, int) and pid_alive(owner_pid)
    if alive:
        return LockResult(
            state=STATE_ACTIVE_OWNER, lock_path=str(lock_dir), owner=owner,
            detail=f"lock is held by run {owner.get('runId')!r} (pid {owner_pid}, alive) on this host.",
        )
    return LockResult(
        state=STATE_STALE_LOCK, lock_path=str(lock_dir), owner=owner,
        detail=(
            f"lock owner run {owner.get('runId')!r} (pid {owner_pid}) on this host is no "
            f"longer alive -- the lock is stale. It is NEVER auto-broken; use the explicit "
            f"recover-stale operation."
        ),
    )


def status(
    lock_root: "Path | str",
    scope: LockScope,
    *,
    hostname: "str | None" = None,
    pid_alive: "Callable[[int], bool] | None" = None,
) -> LockResult:
    """Read-only inspection of the lock's current state: not_held when no
    lock directory exists, else the same classification acquire would see
    (active_owner / stale_lock / foreign_host_lock / interrupted_owner)."""
    lock_dir = scope.lock_dir(Path(lock_root))
    if not lock_dir.is_dir():
        return LockResult(
            state=STATE_NOT_HELD, lock_path=str(lock_dir),
            detail="no lock is currently held for this scope.",
        )
    return _classify_existing(
        lock_dir,
        hostname=hostname or socket.gethostname(),
        pid_alive=pid_alive or default_pid_alive,
    )


def acquire(
    lock_root: "Path | str",
    scope: LockScope,
    *,
    run_id: str,
    hostname: "str | None" = None,
    pid: "int | None" = None,
    now: "Callable[[], str] | None" = None,
    pid_alive: "Callable[[int], bool] | None" = None,
) -> LockResult:
    """Attempt to acquire the fixture lock for ``run_id``. Atomic (os.mkdir),
    never raises for contention, never breaks an existing lock. Returns a
    LockResult whose state is acquired / active_owner / stale_lock /
    foreign_host_lock / interrupted_owner / unavailable."""
    lock_root = Path(lock_root)
    hostname = hostname or socket.gethostname()
    pid = pid if pid is not None else os.getpid()
    pid_alive = pid_alive or default_pid_alive
    lock_dir = scope.lock_dir(lock_root)
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return LockResult(
            state=STATE_UNAVAILABLE, lock_path=str(lock_dir),
            detail=f"could not create the lock root {lock_root}: {exc}",
        )
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        return _classify_existing(lock_dir, hostname=hostname, pid_alive=pid_alive)
    except OSError as exc:
        return LockResult(
            state=STATE_UNAVAILABLE, lock_path=str(lock_dir),
            detail=f"could not create the lock directory: {exc}",
        )
    acquired_at = now() if now else _default_now()
    owner = _owner_payload(scope, run_id=run_id, hostname=hostname, pid=pid, acquired_at=acquired_at)
    try:
        (lock_dir / OWNER_FILE_NAME).write_text(
            json.dumps(owner, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        # Roll back the half-acquired lock rather than leaving an
        # interrupted_owner behind for every later run.
        shutil.rmtree(lock_dir, ignore_errors=True)
        return LockResult(
            state=STATE_UNAVAILABLE, lock_path=str(lock_dir),
            detail=f"could not write owner.json: {exc}",
        )
    return LockResult(
        state=STATE_ACQUIRED, lock_path=str(lock_dir), owner=owner,
        detail=f"lock acquired for run {run_id!r} on host {hostname!r} (pid {pid}).",
    )


def release(lock_root: "Path | str", scope: LockScope, *, run_id: str) -> LockResult:
    """Release the lock IF AND ONLY IF owner.json records this run id.
    Releasing a lock owned by someone else (or an interrupted lock) is an
    error RESULT (not_owner), never an exception and never a removal."""
    lock_dir = scope.lock_dir(Path(lock_root))
    if not lock_dir.is_dir():
        return LockResult(
            state=STATE_NOT_HELD, lock_path=str(lock_dir),
            detail="no lock directory exists -- nothing to release.",
        )
    owner = _read_owner(lock_dir)
    if owner is None or owner.get("runId") != run_id:
        return LockResult(
            state=STATE_NOT_OWNER, lock_path=str(lock_dir), owner=owner,
            detail=(
                f"lock is not owned by run {run_id!r} "
                f"(owner: {owner.get('runId') if owner else 'unreadable'}) -- refusing to release it."
            ),
        )
    try:
        shutil.rmtree(lock_dir)
    except OSError as exc:
        return LockResult(
            state=STATE_UNAVAILABLE, lock_path=str(lock_dir), owner=owner,
            detail=f"could not remove the lock directory: {exc}",
        )
    return LockResult(
        state=STATE_RELEASED, lock_path=str(lock_dir), owner=owner,
        detail=f"lock released by its owner run {run_id!r}.",
    )


def recover_stale(
    lock_root: "Path | str",
    scope: LockScope,
    *,
    recovering_run_id: str,
    reason: str,
    hostname: "str | None" = None,
    now: "Callable[[], str] | None" = None,
    pid_alive: "Callable[[int], bool] | None" = None,
) -> LockResult:
    """EXPLICIT stale-lock recovery: re-verify the lock is provably stale
    (same host, owner pid dead) and, BEFORE removing it, write an audit
    record ``recovery-<scope-hash>-<recovering-run-id>.json`` next to the
    lock documenting the removed owner. interrupted_owner and
    foreign_host_lock states REFUSE recovery -- staleness cannot be proven
    for either, and a foreign host's run may still be live."""
    lock_root = Path(lock_root)
    hostname = hostname or socket.gethostname()
    pid_alive = pid_alive or default_pid_alive
    lock_dir = scope.lock_dir(lock_root)
    if not lock_dir.is_dir():
        return LockResult(
            state=STATE_NOT_HELD, lock_path=str(lock_dir),
            detail="no lock directory exists -- nothing to recover.",
        )
    classified = _classify_existing(lock_dir, hostname=hostname, pid_alive=pid_alive)
    if classified.state != STATE_STALE_LOCK:
        return LockResult(
            state=STATE_RECOVERY_REFUSED, lock_path=str(lock_dir), owner=classified.owner,
            detail=(
                f"refusing recovery: lock state is {classified.state!r}, not provably stale. "
                + classified.detail
            ),
        )
    recovered_at = now() if now else _default_now()
    audit_path = lock_root / f"recovery-{scope.hash}-{recovering_run_id}.json"
    audit = {
        "recoveredOwner": classified.owner,
        "recoveringRunId": recovering_run_id,
        "recoveredAt": recovered_at,
        "reason": reason,
        "lockPath": str(lock_dir),
        "exclusivityScope": EXCLUSIVITY_SCOPE,
    }
    try:
        audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return LockResult(
            state=STATE_UNAVAILABLE, lock_path=str(lock_dir), owner=classified.owner,
            detail=f"could not write the recovery audit record -- lock NOT removed: {exc}",
        )
    try:
        shutil.rmtree(lock_dir)
    except OSError as exc:
        return LockResult(
            state=STATE_UNAVAILABLE, lock_path=str(lock_dir), owner=classified.owner,
            audit_path=str(audit_path),
            detail=f"audit written but the lock directory could not be removed: {exc}",
        )
    return LockResult(
        state=STATE_RECOVERED, lock_path=str(lock_dir), owner=classified.owner,
        audit_path=str(audit_path),
        detail=f"stale lock recovered by run {recovering_run_id!r}; audit: {audit_path}",
    )


def _default_now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()
