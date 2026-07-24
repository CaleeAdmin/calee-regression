# Distributed fixture-exclusivity decision

**Status: host-local lock retained. Cross-host certification with the same
account is `BLOCKED`. No product/backend change made this session.**

## The problem

The regression fixture is a **shared production account** with a deterministic
`REG-*` dataset. Two runs against the same *backend + account + fixture
version* are dangerous: one run's `prepare` fixture reset destroys the other
run's in-flight state. Exclusivity must therefore be guaranteed for the whole
duration of a run.

## Current mechanism (retained)

`calee_regression/fixture_ownership.py` provides a robust **host-local** lock:

- scope key = backend URL + account fingerprint (`sha256(email)[:12]`, never
  the secret or full email) + fixture version;
- acquisition is an **atomic `os.mkdir`** of `fixture-<scope-hash>.lock/`
  followed by an `owner.json` (run id, host, pid, timestamps — no secrets);
- a lock is **never auto-broken**; stale recovery (same host, dead pid) is an
  explicit, audited operation; an `interrupted_owner` or `foreign_host_lock`
  refuses recovery entirely.

Its own module docstring and every `LockResult` it emits already record the
limitation verbatim (`"exclusivityScope": "host-local"`,
`EXCLUSIVITY_LIMITATION`): exclusivity holds **only among runs on the current
host**. A run on another machine is invisible to this lock.

## What a valid distributed lease would require

A lease that could safely replace the host-local scope for a shared production
account must provide **all** of:

1. **atomic acquire** — the acquire either wins or loses with no interleaving;
2. **owner / run identity** — the winner is recorded so a release can be
   verified;
3. **expiry** — a crashed owner's lease must self-release after a bounded TTL;
4. **compare-and-release** — release succeeds only for the recorded owner;
5. **mutual exclusion** — two simultaneous acquires can never both succeed;
6. **no credential storage** — never persist the account secret anywhere;
7. **safe stale-owner handling** — a stale/expired owner is reclaimed
   deterministically, never ambiguously.

## Investigation of the read-only backend contracts (evidence)

The read-only backend (`CaleeAdmin/calee-hub-core`) was inspected for an
existing atomic primitive that could back such a lease **without a product
change**:

- **No lease / lock / mutex / semaphore / advisory-lock resource exists.** A
  repository-wide search for `lease`, `lock`, `mutex`, `semaphore`,
  `advisory lock` returned no client-facing primitive.
- **No client-facing conditional update exists.** The `Client v1` API
  (`/client/v1/{events,calendars,tasks}` + `PATCH
  /client/v1/calendars/{id}/appearance`) is plain CRUD. There is **no**
  `If-Match` / `If-None-Match` / ETag precondition on any *client-writable*
  resource. The only `etag` columns in the schema
  (`external_etag`, subscription `etag`) belong to **upstream** external-feed
  caching (Google/CalDAV provider sync), not to a client-side compare-and-swap.
- Therefore there is **no existing atomic ownership primitive** exposed to the
  regression client.

### Why "create + readback" is explicitly rejected

A lease built from a normal calendar/task/chore **create followed by a
non-atomic readback** does **not** provide mutual exclusion: two hosts can both
`POST` a "lease marker" event and both read back a success. Last-writer-wins on
a plain create is not compare-and-swap. Per the task's own constraint, this is
**not** an acceptable lease and was not implemented.

## Decision

- **Retain the host-local lock.** It is the strongest guarantee actually
  available without a product change, and it is honest about its scope.
- **Keep the limitation explicit** — it already is, in code and in every piece
  of evidence the module emits, and now here.
- **Full certification with the same account on multiple hosts is `BLOCKED`.**
  The `fixtureExclusivity` dimension of the framework-completeness report reads
  `partial` (host-local implemented + offline-tested; distributed blocked) and
  names this file as the blocker. Do not run two certification hosts against
  the same backend + account + fixture version concurrently.
- **Do not pretend the limitation is resolved.**

## Focused product/backend design proposal (not implemented here)

To close the gap, `calee-hub-core` would need to add a small, regression-only
atomic-lease endpoint — the minimum that satisfies the seven requirements
above. A sketch:

```
POST   /client/v1/regression-leases      # atomic acquire (compare-and-set)
  body: { scopeKey, ownerRunId, host, ttlSeconds }
  201 -> lease granted   (returns leaseId + expiresAt)
  409 -> already held    (returns current owner + expiresAt)   # mutual exclusion

DELETE /client/v1/regression-leases/{leaseId}
  header/body: ownerRunId                # compare-and-release
  204 -> released        (only when ownerRunId matches)
  409 -> not owner       (refused)

# server-side: a single unique row keyed by scopeKey, acquired via an atomic
# INSERT ... ON CONFLICT (scopeKey) DO NOTHING (or equivalent CAS), with an
# expiresAt the server enforces so a crashed owner self-releases; never stores
# the account secret (scopeKey is the same non-secret fingerprint the
# host-local lock already uses).
```

Until such a primitive exists, `fixture_ownership.py` remains the authoritative
mechanism and multi-host certification stays `BLOCKED`.
