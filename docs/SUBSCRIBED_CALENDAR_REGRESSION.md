# Subscribed-calendar regression coverage (Workstream 3)

Proves the subscribed-calendar contract introduced by **calee-hub-core PR #352
and #353**, **Calee PR #973**, and **Calee release #974**
(`931beecfc309ff12220185383fa8daae56af30d8`).

## Controlled fixture (regression-owned, safe, deterministic)

`fixtures/subscribed_calendar/reg_sub_calendar.ics` is a regression-owned
subscription source. It is deterministic (fixed 2026-08 dates, no "today+N"),
uses unique `REG-SUB-*` titles, and contains one of each required shape:

| Title | Shape | Dates |
|---|---|---|
| `REG-SUB-ALLDAY-SINGLE` | single-day all-day | 2026-08-05 (DTEND 08-06, exclusive) |
| `REG-SUB-ALLDAY-MULTI` | multi-day all-day | 2026-08-10..12 (DTEND 08-13, exclusive) |
| `REG-SUB-ALLDAY-DAILY` | daily recurring all-day | 2026-08-20..24, COUNT=5 |
| `REG-SUB-TIMED` | timed | 2026-08-06 09:00–10:00Z |
| `REG-SUB-BARE-ALLDAY` | bare `DTSTART:YYYYMMDD` (no `VALUE=DATE`) | 2026-08-08 |
| (EXDATE) | removes an occurrence | 2026-08-22 excluded from the daily series |
| `REG-SUB-OVERRIDE` | `RECURRENCE-ID` override | replaces the 2026-08-23 occurrence |

**Provisioning contract:** this fixture must be loaded via an **authenticated
regression fixture endpoint / controlled regression ICS endpoint**, associated
with the regression account's calendar id **`regression:regsub`**. Never use a
customer calendar or an unauthenticated production reset endpoint. The private
subscription URL is **not** recorded here or in any report — only the fixture
calendar id is.

**Client workflow (Priorities 5 & 6).** `prepare-subscribed-fixture` (run by
the `06` launcher, and standalone) resolves ONE date + timezone for the run,
generates the today-relative ICS (`subscribed_fixture.generate_today_relative_ics`),
records `reports/runs/<run-id>/subscribed-fixture/results.json` and the generated
event titles as scenario variables (`REG_SUB_TIMED_TITLE` / `REG_SUB_ALLDAY_TITLE`
/ `REG_SUB_DATE`) — which the tablet scenario substitutes into its `${…}`
placeholders so it asserts the exact events THIS run provisioned on both Today
and Calendar.

Publishing itself goes through `subscribed_publisher.build_publisher_from_config`,
which selects one of four adapters from `machine.local.yaml`'s `subscribed_publisher`
section — `webdav`, `presigned-put`, `s3-cli`, or `local` — and PUTs the generated
ICS to the configured publicly-readable URL. `local` (writes to a filesystem path,
no real publish) is accepted only for `fixed-date`/`offline-only` modes; it is
rejected outright when `mode` is `published`, so a published run can never
silently downgrade to a no-op local write.

In `published` mode, `prepare_subscribed_fixture` then runs two independent,
mandatory verification phases before it will report success — neither is ever
faked or skipped:

1. **Public-read verification (Priority 5).** Polls the published URL over
   plain HTTP(S) GET (`ics_contract.parse_ics`) until the downloaded bytes'
   SHA-256 matches exactly what was PUT, both this run's exact titles
   (`REG_SUB_TIMED_TITLE`/`REG_SUB_ALLDAY_TITLE`) are present, and the event
   date matches `REG_SUB_DATE` — a stale or wrong-date ICS left over from a
   prior run never false-passes. Recorded as `publicReadVerificationStatus` /
   `publicReadAttempts` / `publicReadObservedSha256` / `publicReadVerifiedAt`.
2. **Calee-ingestion verification (Priority 6).** Only after phase 1 succeeds,
   polls the *existing, already-authenticated* `GET /client/v1/events` API —
   reusing CaleeMobile-Regression's `sync_smoke_actions.py` receiver via the
   `sync_smoke_bridge.find_event_by_title` subprocess bridge (never a direct
   cross-repo import; credentials travel in the child process environment,
   never on argv) — until the hub reports it actually ingested the
   subscription and shows the exact run-specific event (optionally narrowed
   to the `regression:regsub` calendar id). This is the real proof that Calee
   picked up the feed, not just that the file is reachable. Recorded as
   `ingestionStatus` / `ingestionAttempts` / `ingestionElapsedSeconds` /
   `ingestionTimestamp` / `ingestionApi` / `ingestionObservedEvent`.

`published` mode only reports overall success when **both** phases report
verified. If no publisher is configured, no sibling CaleeMobile-Regression
checkout is available for the ingestion bridge, or either phase times out,
the step records **BLOCKED** — never faked — and the subscribed scenario
stays draft-unverified. `fixed-date` and `offline-only` modes never attempt
either verification phase and are never promoted past draft-unverified either;
they exist only to prove the ICS-generation and date-expansion logic offline.

**Release-identity binding (Priority 7).** `reports/runs/<run-id>/subscribed-fixture/results.json`
records `releaseId` (adopted from this run's own composed `release-config`, or an explicit
`--release-id`/`CALEE_RELEASE_ID`) and `generatedAt` alongside the run ID it already carried. At
consolidation, this evidence is bound to the release the same way selector-contract evidence is: a
missing or mismatched `releaseId` BLOCKS, and a `published`-mode report is re-verified against its own
`publicReadVerificationStatus`/`ingestionStatus` rather than a bare top-level `status: "ok"` being
trusted outright — see `docs/RELEASE_POLICY.md`'s "Subscribed-fixture evidence is bound to the release"
section for the full precedence and rejection rules.

Exercise **both** ingestion paths:

1. **subscription cache expansion** (the normal path — hub expands the feed into
   `subscription_source_cache`);
2. **direct CalDAV / fallback parsing** with the cache-first path disabled or
   bypassed (`subscription_source_fallback`), to prove the same metadata is
   produced without the cache.

## API assertions → contract

| Assertion | Where the contract lives |
|---|---|
| all-day `DTEND` remains exclusive | hub-core all-day rule; proven offline (below) |
| every recurring occurrence gets the correct duration | offline (below) |
| bare `YYYYMMDD` starts treated as all-day | hub-core `core_client_subscription_sources.php` (VALUE=DATE **or** bare 8-digit) |
| `event.calendarId == calendar.id`, IDs never double-prefixed | hub-core `subscription_calendar_events_test.php` tests 13/14; `client_api_public_calendar_id` |
| service IDs prevent cross-service collisions | hub-core test 15; Calee `hubCalendarKey(serviceId, calendarId)` (#973 C) |
| subscribed events carry `readOnly=true` | Calee #973 A; hub-core DTO `readOnly` |
| subscribed events carry `isSubscription=true` | Calee #973 A; hub-core DTO `isSubscription` |
| subscribed events have **no** `writableEventId` | Calee #973 A (no `writableEventId→id` fallback for read-only/subscription) |
| direct-CalDAV subscription events carry the same metadata | hub-core fallback path; same DTO |
| writable calendars remain writable | hub-core tests (create/update succeed on writable) |
| non-subscription read-only stays read-only but **not** a subscription | hub-core tests 5/7/9 (`client_event_read_only_*`) |

### Proven offline now

`framework_tests/test_subscribed_calendar_contract.py` expands the fixture with
a dependency-free expander (`calee_regression/ics_contract.py`) and asserts the
**date semantics** with no backend and no device:

* all-day `DTEND` exclusive, single- and multi-day, **no ±1-day shift**;
* bare `DTSTART:YYYYMMDD` treated as all-day;
* every recurring occurrence carries the master's duration;
* `EXDATE` removes exactly 2026-08-22;
* the `RECURRENCE-ID` override replaces exactly 2026-08-23 (→ `REG-SUB-OVERRIDE`).

### BLOCKED (live API)

The `readOnly`/`isSubscription`/`writableEventId`/`calendarId` and
cache-vs-direct-CalDAV assertions run against a live hub `/events` + `/calendars`
API with the fixture provisioned. **No hub backend is available in this
environment**, so those are reported **BLOCKED**, not PASS. The hub-side
behaviour is unit-tested in-repo at `calee-hub-core`
(`tests/subscription_calendar_events_test.php`,
`tests/subscription_source_cache_events_test.php`).

## Tablet assertions → scenario

`scenarios/subscribed_calendar.yaml` (source-confirmed selectors, gated
`draft-unverified`) covers:

| # | Assertion | Status |
|---|---|---|
| 1 | subscribed timed + all-day events render on Calendar | in scenario |
| 2 | recurring daily events appear on every correct date | in scenario (title render; per-date nav needs device) |
| 3 | all-day event display dates do not shift by one day | offline-proven + in scenario |
| 4 | Today and Calendar show the same visible events | in scenario, verified with the OWNED today-relative event `REG-EVENT-TIMED-001` (provisioned on `ctx.today`), NOT a fixed-date subscribed event — so the Today↔Calendar check is date-correct on any execution date (Priority 7). The fixed-date subscribed events themselves are verified in the date-scoped Agenda list. A today-relative subscribed generator (`calee_regression/subscribed_fixture.py`) exists for when authenticated subscription-source provisioning is wired. |
| 5 | opening a subscribed event shows read-only status | in scenario |
| 6 | Edit is absent | in scenario (`fail_if_id btnEventDetailEdit`) |
| 7 | Delete is absent | in scenario (`fail_if_id btnEventDetailDelete`) |
| 8 | defensive mutation attempts cannot open the edit form | contract #973 B (guards); needs device to drive |
| 9 | both calendars selected shows both calendars' events | in scenario (baseline, before unticking) |
| 10 | unticking one calendar hides only that calendar | in scenario (proves REG-SUB's own events hide; does not independently prove another calendar is unaffected -- see the scenario's header comment) |
| 11 | unticking all calendars shows no events | still needs a row-scoped selector for the account's own primary calendar, whose display name is not a deterministic REG-* fixture value |
| 12 | reticking restores events | in scenario |
| 13 | selection survives tab navigation | in scenario |
| 14 | selection survives refresh and app restart | in scenario for both halves -- see "Product-level risk: selection persistence" below |
| 15 | unknown/malformed calendar ID excluded (not all events appear) | still needs a backend-response-shape test, not a tablet UI action |
| 16 | two services sharing the same raw calendar ID isolated | Calee `hubCalendarKey` (#973 C); needs 2-service fixture |

Test **both** a fresh network response and a **Room-cache** response (the #973
`hub_events`/`hub_calendars` Room schema carries `isSubscription`/`readOnly`
columns, so the cached path must reproduce the same metadata).

### BLOCKED (tablet)

Running the scenario needs a prepared tablet/emulator **and** the REG-SUB
fixture provisioned via the hub backend — neither is available here — so the
tablet assertions are **BLOCKED**. Screenshots for each visibility state and the
fixture calendar ids (never the private subscription URL) go into the structured
report when it runs on a real device.

The calendar-selection matrix's tablet filter/selection selectors are now
source-confirmed: `item_calendar_navigation_list.xml` (`llItem` row
container, `cbCalendar` checkbox, `tvName` title) and `CalendarFragment.kt`
(`ivExpand` toggles the sidebar in `fragment_calendar.xml`; tapping
`cbCalendar` -- or the row itself -- toggles a calendar's visibility via
`HubCalendarSelectionReconciler`). Cases 9, 10, 12, 13, 14 are covered by
`subscribed_calendar.yaml`'s calendar-selection steps; cases 11 and 15
remain out of scope (see the table above and the scenario's own header
comment for exactly why), and 16 still needs a 2-service fixture. The
render + read-only cases 1–8 remain source-confirmed against
`dialog_hub_event_detail.xml` / `activity_home.xml` as before.

### Product-level risk: selection persistence across an app restart

Reading `HubCalendarSelectionReconciler.kt` (the class `CalendarFragment`
uses to track which calendars are ticked) found no persistence mechanism at
all: `selectedIds`/`knownIds` are plain in-memory `LinkedHashSet` fields on
a Fragment-scoped object. A fresh app process constructs a fresh
`CalendarFragment` and therefore a fresh reconciler with empty sets;
`reconcile()`'s `isFirstCalendarLoad` branch then falls back to selecting
**every** visible calendar (`fallbackSelectedAll`) whenever nothing was
ever explicitly selected in that process's lifetime -- which is always true
immediately after a restart.

This means case 14's "survives app restart" half may well not hold against
the real product as it stands today. `subscribed_calendar.yaml`'s
app-restart step (force-stop + relaunch, then re-assert the previously-
unticked calendar's events stay hidden) is written to prove this one way or
the other, not to assume an answer -- per this project's rule against
weakening assertions to make them pass. If a physical run shows this
assertion genuinely fails, that is a real, newly-identified product gap
(calendar visibility selection does not survive an app restart) to report
to the Calee tablet team, not a regression-framework defect.
