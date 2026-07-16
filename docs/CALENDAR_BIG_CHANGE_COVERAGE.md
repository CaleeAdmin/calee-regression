# Using the calendar suite around big calendar changes

The `calendar` suite (`calendar_smoke.yaml`, `calendar_view_modes.yaml`,
`calendar_event_fields.yaml`, `calendar_recurring_events.yaml`) exists specifically to catch
regressions when Calee's calendar code changes significantly.

## What each scenario checks

- **`calendar_smoke.yaml`** — Calendar tab opens and its toolbar (`btnAddEvent`) actually renders.
  Cheapest, run this first.
- **`calendar_view_modes.yaml`** — Day, Week, Month, and Agenda toggles (if the navigation rail is
  showing them — see the scenario's own comment on why that tap stays conditional) each render
  without crashing, with a screenshot captured per view, then asserts the Calendar screen is still
  showing. This is the scenario most likely to catch a broken view mode after a calendar refactor.
- **`calendar_event_fields.yaml`** — Requires the deterministic regression fixture (see
  `docs/TEST_DATA_RESET_CONTRACT.md`). Opens `REG-EVENT-TIMED-001` and `REG-EVENT-ALLDAY-001` by
  their exact, guaranteed-to-exist titles, and hard-asserts each event's detail dialog shows the
  right title and time/all-day rendering (`tvEventDetailTitle`, and "All day" text presence/absence).
- **`calendar_recurring_events.yaml`** — Also requires the fixture. Opens `REG-EVENT-RECURRING-001`
  and hard-asserts its recurring indicator (`tvEventDetailRecurring`) is present, then opens
  `REG-EVENT-EXCEPTION-001` (a detached occurrence of the same series with an overridden title) and
  hard-asserts it shows its own overridden title, not the series' original one. Neither assertion is
  wrapped in `optional` — both scenarios fail (or block, if the fixture isn't there) rather than
  silently pass if the fixture is missing or the tablet renders it wrong.

## Prerequisites

Both event/recurrence scenarios require `01 Prepare Test Environment` (or
`python3 manage_fixture.py reset` in the sibling `CaleeMobile-Regression/api`) to have reset the
regression fixture first. If the fixture isn't there, expect these scenarios to fail their
`wait_for_text`/`tap` steps (a real, diagnosable failure) rather than silently pass — see
`docs/TEST_DATA_RESET_CONTRACT.md`.

## Recommended workflow for a big calendar change

1. On a prepared `logged_in_tablet` device with the regression fixture reset, run
   `python -m calee_regression suite --config config/tester.local.yaml --suite calendar` **before**
   making the change. Keep the report folder.
2. Make the calendar change.
3. Run the same suite again **after** the change.
4. Compare the two `reports/calendar-*/` folders — diff `results.json` for pass/fail changes, and
   eyeball the `screenshots/` folders side by side for visual regressions (screenshots in this suite
   are informational, `compare: false`, by design — see `docs/SCENARIO_REFERENCE.md`).
5. If you want strict pixel-level diffing for a specific screen, ask a technical owner to approve a
   baseline image (copy a known-good screenshot into `baselines/<name>.png`) and set that step's
   `compare: true` — see the visual regression section of the main `README.md`.
