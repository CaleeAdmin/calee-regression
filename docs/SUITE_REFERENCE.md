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

## Partially implemented: `sync-smoke`

`calee_regression/sync_smoke.py` orchestrates three flows across the API, CaleeMobile, and the
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

The *italicized* steps above are always recorded `BLOCKED`, never attempted and never faked as
passing — they need tablet-side mutation resource ids that have never been confirmed against the
real Calee app, the same gap `calendar_event_mutation`/`tasks_mutation` (above) are blocked on. See
`docs/TABLET_MUTATION_COVERAGE_GAPS.md`. Because of this, **`sync-smoke` is not yet release-gating**:
the event and task flows can never reach a clean `ok` status until that gap closes, so making it
mandatory today would mean no release could ever pass. Its report
(`reports/runs/<run-id>/sync/results.json`) is written for every run but is not yet auto-discovered
by `consolidate` — informational only until the tablet-mutation gap closes, at which point it should
be wired in as a mandatory component the same way `environment`/`tablet`/`mobile-*` already are (see
`docs/RELEASE_POLICY.md`).

Every *other* step in all three flows is exercised for real against whatever backend/device the
caller points it at — `sync_smoke_bridge.py` shells out to CaleeMobile-Regression's
`api/sync_smoke_actions.py` (API leg, `framework_tests/test_sync_smoke_bridge.py`) and `ui/run_ui_suite.py`
(CaleeMobile leg), and a live `CaleeDriver`/Appium session drives the tablet-read legs. Section 10 of
the project brief ("Cross-device synchronization suite") is the origin of this suite's intended
shape; this closes it as far as the current tablet-mutation gap allows.

## Notes

- `list-suites` (`python -m calee_regression list-suites`) always reflects the authoritative,
  current suite membership for the `calee-regression` side of this table — treat this document as
  a map of where things live, not a substitute for it.
- "Release-gating" here means "a real release should not ship without this profile having run and
  passed" — it does not mean every commit must run it (see `docs/RELEASE_POLICY.md` for exactly
  which components are mandatory for an overall PASS).
