# Tablet functional mutation coverage — current gap

## What this is

Every existing tablet scenario before this round (`calendar_smoke`, `calendar_view_modes`,
`calendar_event_fields`, `calendar_recurring_events`, `tasks_smoke`, `chores_smoke`, ...) either
renders a screen or reads already-existing content. None of them create, edit, delete, complete,
reopen, or skip anything — real functional mutation coverage for the tablet did not exist.

Three draft scenarios now exist that exercise exactly that:

| File | Suite (draft-only) | Covers |
|---|---|---|
| `scenarios/calendar_event_mutation.yaml` | `calendar_event_mutation` | create → verify → edit → verify → delete → verify disappearance |
| `scenarios/tasks_mutation.yaml` | `tasks_mutation` | locate `REG-TASK-OPEN-001` → complete → verify → reopen → verify (self-cleaning) |
| `scenarios/chores_mutation.yaml` | `chores_mutation` | locate `REG-CHORE-REPEATING-001` → skip → verify (relies on fixture reset to restore) |

**None of these are runnable yet.** They are structurally complete — correct flow, correct use of
the deterministic REG-* fixture, self-cleaning where a UI-level undo is plausible, framework actions
that already exist and are unit-tested — but every interactive element beyond simple navigation uses
a resource id that has never been confirmed against the real Calee tablet app.

## Why the ids are placeholders instead of a best guess

This framework's accessible source is three repositories: `CaleeMobile` (the Flutter phone app),
`CaleeMobile-Regression`, and `calee-regression` (this repo). **The Calee Android tablet app's own
source is not one of them.** The ids already used by the existing scenarios (`llCalendar`,
`btnAddEvent`, `tvEventDetailTitle`, `panelHubDay`, ...) were confirmed by whoever originally wrote
this framework, evidently with access to the app's source and/or a live device (see the comments in
`scenarios/calendar_view_modes.yaml` referencing `HubCalendarFragment.kt` directly). This session has
neither: no access to that source, and — see `docs/RELEASE_POLICY.md` / the session's final report —
no Android SDK, no emulator, and no Appium binary in this execution environment, so there was no way
to start a live Appium Inspector session either.

Guessing plausible-looking ids (`btnEventSave`, `cbTaskComplete`, ...) was deliberately avoided. A
wrong guess doesn't fail cleanly — it produces a **misleading FAIL** that looks like a product
regression when it's actually a wrong selector, which is exactly the failure mode that got the
original broad CaleeMobile selector PR reverted (see `CaleeMobile-Regression/docs/CALEEMOBILE_SELECTOR_GAPS.md`
for that history). Every placeholder below is instead named `UNCONFIRMED_...` so it fails immediately
and obviously, and is easy to `grep -rn UNCONFIRMED_ scenarios/` in one shot.

## How these are kept from affecting a real release run

Three independent layers, so no single mistake lets an unconfirmed scenario silently gate a release:

1. Each scenario file sets `mandatory: false` explicitly.
2. Each scenario's suite (`calendar_event_mutation`, `tasks_mutation`, `chores_mutation`) exists only
   as a standalone entry in `calee_regression/suites.py`'s `SCENARIO_GROUPS` — none of them appear in
   `COMPOSITE_SUITES["full-tester"]` or `["release-technical"]`, so `06 Test Full Calee Solution` and
   `02 Test Calee Tablet` never run them.
3. `framework_tests/test_tablet_mutation_drafts.py` enforces both of the above as regression tests,
   plus that every scenario still parses as valid YAML.

## Exact confirmation checklist

A technical owner with either (a) a real, prepared `logged_in_tablet` device/emulator and an Appium
Inspector session, or (b) access to the Calee Android app's source, needs to resolve each of these.
Replace the placeholder token with the real resource id (or correct the assumption) directly in the
scenario file, run the scenario for real, and only then flip `mandatory: false` → not set (defaults to
`true`) and add the suite name into `COMPOSITE_SUITES["full-tester"]` in `suites.py`.

### `scenarios/calendar_event_mutation.yaml`

| Placeholder | What it needs to be |
|---|---|
| `UNCONFIRMED_event_title_input` | The add-event form's title `EditText` resource id |
| `UNCONFIRMED_event_save_button` | The add-event form's save/confirm button resource id |
| `UNCONFIRMED_event_edit_button` | The button/icon in the event-detail dialog that opens editing |
| `UNCONFIRMED_event_edit_title_input` | The edit form's title field id (may turn out identical to the create form's — if so, simplify to reuse one token) |
| `UNCONFIRMED_event_edit_save_button` | The edit form's save button id (ditto) |
| `UNCONFIRMED_event_delete_button` | The event-detail dialog's delete affordance |
| `UNCONFIRMED_event_delete_confirm_button` | The delete-confirmation dialog's confirm button |

Also verify: whether confirming deletion dismisses straight back to the Agenda list, or leaves an
empty detail dialog needing an explicit `back`/close step first (see the comment at that step in the
file).

### `scenarios/tasks_mutation.yaml`

| Placeholder | What it needs to be |
|---|---|
| `UNCONFIRMED_task_complete_toggle_for_REG_TASK_OPEN_001` | Whatever control marks a task row complete — confirm first whether this is a per-row checkbox, a swipe gesture (which this framework's `tap`/`tap_if_present` actions cannot express yet — a `swipe` action would need to be added if so), or a detail-screen button |
| `UNCONFIRMED_task_completed_indicator_for_REG_TASK_OPEN_001` | Whatever element/id proves "this task is now shown as completed" — could be a checked checkbox id, a distinct completed-list container, or something else entirely |
| `UNCONFIRMED_task_reopen_toggle_for_REG_TASK_OPEN_001` | The control that reverses completion (may be the same element as the complete toggle, if it's a single stateful toggle) |

This file has the least grounding of the three — confirm the actual interaction model first before
just filling in ids, since the current draft assumes distinct "complete" and "reopen" controls and
that may not match reality.

### `scenarios/chores_mutation.yaml`

| Placeholder | What it needs to be |
|---|---|
| `UNCONFIRMED_chore_skip_button_for_REG_CHORE_REPEATING_001` | The control that skips today's occurrence of a repeating chore |
| `UNCONFIRMED_chore_skipped_indicator_for_REG_CHORE_REPEATING_001` | Whatever element/id proves "this chore is now shown as skipped" |

Also worth doing once the above is confirmed: replace the plain `tap: id: llChores` with the same
`tap_if_present` + `optional: true` + `then_wait_for_id` pattern `scenarios/chores_smoke.yaml` already
uses, so an account with no chores service gets a clean skip instead of a hard failure.

## Framework additions made alongside these drafts

Two small, generic actions were added to support the above — neither is a guess about Calee's UI,
both are standard Appium/UiAutomator element operations:

- `clear_text` (`calee_regression/runner.py`, `appium_driver.py`) — clears a field's existing value.
  `type_text` alone only appends (`send_keys` with no prior `.clear()`), which would silently
  concatenate onto an edit form's pre-filled title instead of replacing it.
- `fail_if_id` — the id-based counterpart to the existing `fail_if_text`, and the negation of
  `assert_id`. Needed to verify something was actually removed/reverted by id rather than by visible
  text (e.g. a completed-indicator id disappearing after reopening a task).

Both are documented in `docs/SCENARIO_REFERENCE.md` and unit-tested in
`framework_tests/test_mandatory_skip_handling.py`.
