# Using the calendar suite around big calendar changes

The `calendar` suite (`calendar_smoke.yaml`, `calendar_view_modes.yaml`,
`calendar_event_fields.yaml`, `calendar_recurring_events.yaml`) exists specifically to catch
regressions when Calee's calendar code changes significantly.

## What each scenario checks

- **`calendar_smoke.yaml`** — Calendar tab opens and shows recognizably calendar-ish content
  (`Calendar`/`Today`/`Month`/`Week`/`Day`). Cheapest, run this first.
- **`calendar_view_modes.yaml`** — Day, Week, and Month view toggles (if present) each render without
  crashing, with a screenshot captured per view. This is the scenario most likely to catch a broken
  view mode after a calendar refactor.
- **`calendar_event_fields.yaml`** — Opens an existing event (if one is visible) and checks for
  expected field labels (Title/Time/Location/Notes/Start/End). Best-effort: wrapped in `optional`
  since a calendar with no fixture events has nothing to open.
- **`calendar_recurring_events.yaml`** — Same idea, specifically for recurrence indicators
  (Repeat/Recurring/Weekly/etc.). Also `optional`-wrapped for the same fixture-data reason.

## Current limitations

- Every navigation tap uses `tap_if_present`, and both event-detail assertions use `optional` — this
  makes the suite resilient to missing fixture data or label drift, but it also means a passing run
  doesn't prove event/recurrence UI is fully correct if no fixture events exist yet. See
  `docs/TEST_DATA_RESET_CONTRACT.md` for the recommended fixture setup (at least one recurring
  event).
- The exact text used in `assert_any_text` calls is a best guess based on typical calendar UI
  copy, not confirmed against the actual Calee UI strings. A technical owner should tune these once
  real screens are inspected, especially after a big visual change.

## Recommended workflow for a big calendar change

1. On a prepared `logged_in_tablet` device with known fixture events, run
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
