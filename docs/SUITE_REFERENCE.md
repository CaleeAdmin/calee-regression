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
| `sync-smoke` | Both (orchestrated) | Not yet automated — see "Known gap" below | Standard | Prepared tablet + a CaleeMobile session, both on the same household | Yes — REG-* fixture | Yes | Yes, once implemented |
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

## Known gap: `sync-smoke`

There is no automated cross-device synchronization suite yet. Section 10 of the project brief
("Cross-device synchronization suite") describes the intended shape: prepare the fixture, change an
event/task/chore through the API or CaleeMobile, poll (bounded, not `sleep`-based) until it appears
on the tablet, modify it from the tablet, poll until it's visible back through the API/CaleeMobile,
then delete and verify propagation. This requires a real tablet and a real CaleeMobile
session simultaneously and wasn't executable in the environment this round of work was done in (no
physical devices available) — see the deliverables summary for what's still needed to build it.

## Notes

- `list-suites` (`python -m calee_regression list-suites`) always reflects the authoritative,
  current suite membership for the `calee-regression` side of this table — treat this document as
  a map of where things live, not a substitute for it.
- "Release-gating" here means "a real release should not ship without this profile having run and
  passed" — it does not mean every commit must run it (see `docs/RELEASE_POLICY.md` for exactly
  which components are mandatory for an overall PASS).
