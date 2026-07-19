# Suite reference

The project defines ten canonical suite profiles, spanning both `calee-regression` (this repo, the
Calee tablet) and `CaleeMobile-Regression` (the sibling repo, CaleeMobile + the backend Client
API). No profile makes a numeric time promise — durations vary too much by machine/network to be
meaningful; use the duration *category* instead.

| Profile | Lives in | How to run | Duration | Device state required | Fixture required | Physical hardware mandatory? | Release-gating? |
|---|---|---|---|---|---|---|---|
| `framework-self-test` | Both | `calee-regression`: `python -m pytest`. `CaleeMobile-Regression`: `python3 -m unittest discover -s . -p "test_*.py" -t .` in `api/` | Quick | None — no device involved | None — runs against an in-memory fake server / synthetic data | No | Yes — CI must pass this on every change |
| `smoke-fresh` | `calee-regression` | `python -m calee_regression suite --suite smoke-fresh`, or `tester/advanced/Run Smoke Fresh.command` | Quick | Clean emulator/tablet, no account signed in | None | No (works on emulator) | No — a first-look smoke check, not itself a release gate |
| `tablet-smoke` (alias of `smoke-tablet`) | `calee-regression` | `python -m calee_regression suite --suite tablet-smoke` | Quick | Prepared, logged-in demo account | None | No | No |
| `tablet-full` (alias of `full-tester`) | `calee-regression` | `02 Test Calee Tablet.command` | Standard | Prepared, logged-in demo account | Yes — REG-* fixture (calendar scenarios require it) | No | Yes |
| `mobile-api` | `CaleeMobile-Regression` | `python3 run_regression.py` in `api/` | Standard | None — hits the backend directly | Auto-managed per run (run-tagged `RT ...` records + best-effort cleanup); can also target the shared REG-* fixture | No | Yes |
| `mobile-android` | `CaleeMobile-Regression` | `ui/run_ui_suite.py --platform android` (auto-resolves the device, passes credentials via `--dart-define`), or `03 Test CaleeMobile Android.command` | Standard | Signed-in CaleeMobile session on an Android device/emulator; `CALEE_TEST_EMAIL`/`CALEE_TEST_PASSWORD` configured | Recommended (REG-* fixture) — the calendar/tasks flow tests assert against it | Yes (or an Android emulator) | Driven by `config/release-platforms.yaml`'s `mobile_android` — defaults to Yes. Format/analyze/unit-tests pass; device execution depends on an Android emulator/device being available where this runs — see the session's final report for what was and wasn't actually executed |
| `mobile-ios` | `CaleeMobile-Regression` | `ui/run_ui_suite.py --platform ios`, or `04 Test CaleeMobile iPhone.command` | Standard | Signed-in CaleeMobile session on an iPhone/simulator; same credentials as above | Same as `mobile-android` | Yes (a Mac with Xcode; simulator counts) | Driven by `config/release-platforms.yaml`'s `mobile_ios` — defaults to Yes. iOS device/simulator execution requires a real Mac; never executable on Linux |
| `sync-smoke` | Both (orchestrated) | `python -m calee_regression sync-smoke --run-id <id> --base-url ... --email ... --password ...` | Standard | Prepared tablet + a CaleeMobile session, both on the same household | Yes — REG-* fixture | Yes | Not yet — see "Partially implemented" below |
| `full-release` (alias of `full-tester`) / full solution | Both (orchestrated) | `06 Test Full Calee Solution.command` (prepare incl. Appium auto-start, tablet, CaleeMobile API+UI per `config/release-platforms.yaml`, guided manual checks, consolidate) | Extended | Prepared tablet; CaleeMobile if attached | Yes | Tablet+API always mandatory; mobile UI mandatory-ness follows `config/release-platforms.yaml` (default Yes per platform, not hard-coded optional) | Yes — see `docs/RELEASE_POLICY.md` |
| `release-technical` | `calee-regression` | `tester/technical/Run Release Technical.command` | Extended | Real physical tablet, admin/kiosk access | No | Yes — refuses to run on an emulator | Yes, for kiosk/admin/system-receiver coverage specifically |

## Draft, non-canonical suites: `calendar_event_mutation` / `tasks_mutation` / `chores_mutation`

Three additional suites exist (`python -m calee_regression list-suites` shows them) that are **not**
among the ten canonical profiles above and are **not** release-gating — they are unfinished drafts for
functional mutation coverage (create/edit/delete a calendar event; complete/reopen a task; skip a
chore), blocked on tablet UI resource ids that have never been confirmed against the real Calee app.
Each is `mandatory: false` in its own scenario file and deliberately absent from every
`COMPOSITE_SUITES` entry, so no existing launcher or CI job ever runs them. See
`docs/TABLET_MUTATION_COVERAGE_GAPS.md` for the exact gap and confirmation checklist.

## Draft, non-canonical suite: `calendar_appearance`

Like `subscribed_calendar` below, `calendar_appearance` (three scenario files —
`calendar_appearance_subscription.yaml` / `calendar_appearance_owned.yaml` /
`calendar_appearance_shared_readonly.yaml`) is **not** among the ten canonical profiles and is **not**
release-gating: source-confirmed selectors (Calee PR CaleeAdmin/Calee#977) but physically unverified,
and two of the three files additionally need a fixture calendar that does not exist in this repo yet.
Each file is `mandatory: false`, tagged `draft-unverified`, and absent from every `COMPOSITE_SUITES`
entry. See `docs/CALENDAR_APPEARANCE_REGRESSION.md` and `test_calendar_appearance_scenarios.py`.
The genuinely cross-device half of this same contract (rename on one surface, verify on another;
colour change; refresh-preserves-override) is **not** a YAML scenario at all — a single scenario file
cannot express a cross-device assertion (`ScenarioRunner` drives one `CaleeDriver`/one device per run)
— it lives in `run_calendar_appearance_sync_flow`, documented in "Partially implemented: `sync-smoke`"
below.

## Partially implemented: `sync-smoke`

`calee_regression/sync_smoke.py` orchestrates four flows across the API, CaleeMobile, and the
tablet, each with bounded polling (never `sleep`-and-hope) and structured evidence per step (source
operation, expected/observed state, timeout, polling attempts, device/build info, screenshots, API
response excerpts) — see `SyncStepEvidence`/`SyncFlowResult` there and
`framework_tests/test_sync_smoke.py` for the fully-tested orchestration logic:

- **Event flow**: create via the Calee Client API → poll the tablet (bounded) → *modify on the
  tablet* → delete via the API → poll both for deletion.
- **Task flow**: poll the tablet baseline → complete `REG-TASK-OPEN-001` via CaleeMobile
  (`sync_task_complete_test.dart`) → poll the tablet → *reopen on the tablet*, falling back to an
  API-based reopen purely as cleanup → verify final state.
- **Chore flow**: poll the tablet baseline → complete then un-complete `REG-CHORE-REPEATING-001` via
  CaleeMobile's row toggle (`sync_chore_complete_test.dart`, fully self-contained and self-cleaning)
  → poll the tablet again. No tablet-side mutation needed at all for this one.
- **Calendar-appearance flow** (`run_calendar_appearance_sync_flow`, calee-hub-core's `PATCH
  /client/v1/calendars/{id}/appearance`, Calee PR CaleeAdmin/Calee#977): capture a baseline → rename
  via the API → poll the tablet for the new name → change colour via the API → verify it persisted →
  *verify the colour change on the tablet* → trigger a provider/subscription refresh → verify the
  local name+colour override survived it (API and tablet) and the provider's own `sourceName` was
  never touched → confirm the calendar's events still report non-editable (API, and a tablet weak
  signal). See `docs/CALENDAR_APPEARANCE_REGRESSION.md`.

The *italicized* steps above are always recorded `BLOCKED`, never attempted and never faked as
passing. For the event/task flows this is the tablet-mutation resource-id gap
`calendar_event_mutation`/`tasks_mutation` (above) are also blocked on — see
`docs/TABLET_MUTATION_COVERAGE_GAPS.md`. The calendar-appearance flow's *verify the colour change on
the tablet* step is a DIFFERENT, permanent gap: no colour-reading primitive exists in `CaleeDriver` at
all (not a resource-id problem — there is nothing to read even once ids are confirmed) — see
`docs/CALENDAR_APPEARANCE_REGRESSION.md`. That flow's API-leg callables
(`api_set_calendar_appearance`/`api_get_calendar`/`api_trigger_calendar_refresh`) are also not yet
wired into `build_real_environment()` (needs new CaleeMobile-Regression API actions, out of scope for
that change) — every step needing one of those records `BLOCKED` for that reason instead, distinct
from the colour gap.

**`sync-smoke` is now a release-gating component (Workstream 1).** `06 Test Full Calee Solution`
invokes it after the mobile UI legs and before manual checks, reusing this run's verified backend +
regression fixture + credentials and the same `CALEE_RUN_ID`; its report
(`reports/runs/<run-id>/sync/results.json`) is auto-discovered and validated by `consolidate` like
every other component, and for a full Calee solution release synchronization defaults to **mandatory**
(`config/release-platforms.yaml` `release_features.synchronization`). A missing, stale,
run-ID-mismatched, `BLOCKED` or `FAILED` mandatory sync can never read as a release PASS.

This is *not* a silent non-gate. Because the italicized tablet-mutation steps are still `BLOCKED`
(unconfirmed resource ids) **and no real device has yet verified the flows in this environment**, a
mandatory sync currently resolves to `BLOCKED`, so a full-solution release BLOCKS on it — which is
the intended safety property ("a PASS must not be possible while synchronization is unverified"), not
an oversight. Once the tablet-mutation gap closes (`docs/TABLET_MUTATION_COVERAGE_GAPS.md`) and a real
device run reaches a clean `ok`, sync will PASS on its own with no further wiring. A release that
genuinely does not include cross-device sync can set `release_features.synchronization: false`, which
keeps sync in the report as an explicit **optional** component rather than silently omitting it.

Every *other* step in the event/task/chore flows is exercised for real against whatever
backend/device the caller points it at — `sync_smoke_bridge.py` shells out to CaleeMobile-Regression's
`api/sync_smoke_actions.py` (API leg, `framework_tests/test_sync_smoke_bridge.py`) and `ui/run_ui_suite.py`
(CaleeMobile leg), and a live `CaleeDriver`/Appium session drives the tablet-read legs. Section 10 of
the project brief ("Cross-device synchronization suite") is the origin of this suite's intended
shape; this closes it as far as the current tablet-mutation gap allows.

The calendar-appearance flow's API leg is not yet wired into `build_real_environment()` the same way
(see `docs/CALENDAR_APPEARANCE_REGRESSION.md`'s "Two DISTINCT gaps, not one") — every step needing it
records `BLOCKED` for that reason on top of the always-`BLOCKED` colour-verification step, so this
flow currently contributes even less live-exercised coverage than the other three until that wiring
lands. Its orchestration logic is nonetheless fully exercised with fakes, same as the other three —
see `framework_tests/test_sync_smoke.py`.

## Notes

- `list-suites` (`python -m calee_regression list-suites`) always reflects the authoritative,
  current suite membership for the `calee-regression` side of this table — treat this document as
  a map of where things live, not a substitute for it.
- "Release-gating" here means "a real release should not ship without this profile having run and
  passed" — it does not mean every commit must run it (see `docs/RELEASE_POLICY.md` for exactly
  which components are mandatory for an overall PASS).
