# Scenario reference

## File shape

```yaml
name: calee-example-scenario
tags: [smoke, tablet]
requires_state: logged_in_tablet   # fresh | logged_in_tablet | physical_tablet | any
default_timeout_seconds: 20
mandatory: true                    # optional, defaults to true -- see below
steps:
  - name: Human-readable step name
    action: launch
    # ...action-specific params
```

- `name` — required, shown in reports.
- `tags` — free-form list, informational.
- `requires_state` — controls where this scenario is allowed to run (see below). Defaults to `any`.
- `default_timeout_seconds` — default timeout used by `wait_for_id`/`wait_for_text` steps that don't
  specify their own `timeout_seconds`. Defaults to `20`.
- `mandatory` — whether this scenario is release-critical. Defaults to `true` (the safe default,
  same "default must be required" rule as step-level `optional`/`required` below). If this scenario
  ends up whole-scenario `SKIPPED` (e.g. a `requires_state` mismatch) while `mandatory: true`, the
  suite is BLOCKED, not silently passed — see `SuiteResult.mandatory_skipped_count`. Set
  `mandatory: false` only for a genuinely optional scenario (e.g. one that only applies to some
  account configurations).
- `steps` — required, non-empty list of step dicts, executed in order. If a step's result is
  `failed` **or** `blocked`, later steps in the same scenario are skipped (marked `skipped`, "not
  run: earlier step failed or blocked") — except steps wrapped in `optional`, whose failures never
  stop the scenario. A scenario in which no step actually verified anything (every step
  skipped/optional/no-op) resolves `blocked`, not `passed` — see `tap_if_present` and `optional`
  below for what counts as "actually verified".

## `requires_state` values

| Value | Meaning | Behavior if unmet |
|---|---|---|
| `fresh` | Clean emulator/tablet, no account signed in | (informational only — no automatic skip) |
| `logged_in_tablet` | Prepared tablet/emulator with a signed-in demo account | Skipped if config's `expected_state` is `fresh` |
| `physical_tablet` | Real, physical tablet (not an emulator) | Skipped if the config's `udid` looks like an emulator |
| `any` | No requirement | Never skipped for state reasons |

## Supported actions

### `launch`
No params. Starts Calee using the config's `launch_strategy`.
```yaml
- name: Launch Calee
  action: launch
```

### `start_activity`
Starts a specific activity directly via adb. `package`/`activity` default to the config's
`app_package`/`app_activity`.
```yaml
- name: Start a specific activity
  action: start_activity
  package: com.viso.calee
  activity: .ui.HomeActivity
```

### `start_action`
Starts an app via a custom intent action. `action`/`package` default to the config's
`start_action`/`app_package`.
```yaml
- name: Start via custom action
  action: start_action
  action: com.viso.calee.action.START
  package: com.viso.calee
```

### `shell`
Runs an arbitrary `adb shell` command (string or list of args). Output is captured into the step's
message. Never use destructive commands here (see `docs/TEST_DATA_RESET_CONTRACT.md`).
```yaml
- name: Broadcast a harmless intent
  action: shell
  command: "am broadcast -a android.intent.action.TIME_SET"
```

### `sleep`
Pauses for `seconds` (float allowed).
```yaml
- name: Wait for animation
  action: sleep
  seconds: 2
```

### `screenshot`
Captures a screenshot named `screenshot_name`. If `compare` (default `true`) is `true`, compares
against `baselines/<screenshot_name>.png` using `max_diff_ratio`/`pixel_threshold` (overridable per
step, else config defaults). No baseline present is an informational pass, not a failure.
```yaml
- name: Capture home screen
  action: screenshot
  screenshot_name: 00_home_screen
  compare: false
```

### `assert_text`
Fails if `text` is not found anywhere in the current page source.
```yaml
- name: Assert text present
  action: assert_text
  text: Calendar
```

### `assert_any_text`
Fails if none of `texts` are found.
```yaml
- name: Assert one of several texts is present
  action: assert_any_text
  texts: [Calendar, Today, Month]
```

### `assert_id`
Fails if no element with resource id `id` exists.
```yaml
- name: Assert element by id
  action: assert_id
  id: today_button
```

### `tap`
Taps an element found by exactly one of `id`, `text`, or `xpath`. Fails the scenario if not found.
```yaml
- name: Tap Calendar tab
  action: tap
  text: Calendar
```

### `tap_if_present`
Same params as `tap`, but if the element isn't found the step doesn't fail outright. What happens
next depends on whether the step is marked optional -- **the default is required**:

- **Required (default)**: absence is treated as `blocked`, not `skipped` -- the scenario cannot
  reliably continue, so it BLOCKS (see `docs/RELEASE_POLICY.md`'s required/optional rule). Only use
  the bare, unmarked form when the element's presence is actually guaranteed and you just haven't
  confirmed the exact id yet -- that uncertainty itself should surface as BLOCKED, not a silent
  pass.
- **Explicitly optional** (`optional: true` or `required: false`): absence is marked `skipped`
  ("element not present, skipped (step marked optional)") and the scenario continues normally. Use
  this only when the element's absence is genuinely, deliberately acceptable (e.g. a conditional
  nav tab that some accounts don't have).

```yaml
- name: Open Chores if this account has a Chores tab
  action: tap_if_present
  id: llChores
  optional: true                     # absence is fine -- some accounts have no chore service
  then_wait_for_id: choresRecyclerView   # but if present, the tab MUST actually render
  then_wait_for_id_timeout_seconds: 10
```

`then_wait_for_id` (optional): if the tap succeeds, wait for this id to appear afterward, and
**fail** the step (a real product failure, not BLOCKED/SKIPPED) if it doesn't -- a present-but-
broken tab must not silently pass just because the tap itself landed.

### `tap_if_absent`
Taps `id`/`text`/`xpath` only if a *different* element (`unless_id`) is **not** currently present.
For idempotent toggle controls (e.g. an expand/collapse rail button) where blindly tapping every
run would flip an already-correct, persisted UI state back to the wrong one instead of reliably
reaching a known state.
```yaml
- name: Expand the navigation rail if it is currently collapsed
  action: tap_if_absent
  unless_id: llDay        # if llDay is already visible, the rail is already expanded -- do nothing
  id: ivExpand             # otherwise tap the expand/collapse toggle
```

### `type_text`
Types `text` into the element with resource id `id`.
```yaml
- name: Type search query
  action: type_text
  id: search_box
  text: "Family dinner"
```

### `hide_keyboard`
No params. Hides the on-screen keyboard if present; never fails if there isn't one.
```yaml
- name: Hide keyboard
  action: hide_keyboard
```

### `back`
No params. Presses the Android back button.
```yaml
- name: Go back
  action: back
```

### `wait_for_id`
Polls up to `timeout_seconds` (default: scenario's `default_timeout_seconds`) for an element with
resource id `id` to appear. Fails on timeout.
```yaml
- name: Wait for calendar grid
  action: wait_for_id
  id: calendar_grid
  timeout_seconds: 10
```

### `wait_for_text`
Same as `wait_for_id` but polls for `text` anywhere on screen.
```yaml
- name: Wait for loading to finish
  action: wait_for_text
  text: Today
  timeout_seconds: 10
```

### `optional`
Wraps a nested `step` (a full step dict with its own `name`/`action`/params). If the nested step
would have failed or blocked, this step is marked `warning` instead (carrying the failure message)
and the scenario continues normally. If the nested step passes, this step reflects that real
outcome (not an automatic pass) -- a scenario relying solely on `optional`-wrapped assertions can
never, by itself, prove this scenario actually verified anything (see the all-`skipped`/`warning`
BLOCKED rule above), so don't use `optional` as the *only* assertion in a release-critical scenario.
```yaml
- name: Optionally assert recurring event fields
  action: optional
  step:
    name: Assert recurrence labels
    action: assert_any_text
    texts: [Repeat, Recurring, Weekly]
```

### `fail_if_text`
Fails if any of `text` (single) or `texts` (list) IS present — used to catch crash/error dialogs.
```yaml
- name: Assert no crash dialog
  action: fail_if_text
  texts: ["Unfortunately", "has stopped", "Force Close"]
```

### `assert_current_activity`
Fails unless the current foreground activity contains `activity` (leading-dot-insensitive substring
match in both directions).
```yaml
- name: Assert HomeActivity is showing
  action: assert_current_activity
  activity: HomeActivity
```

## All 14 scenario files

| File | requires_state | Included in suite(s) |
|---|---|---|
| `smoke_fresh.yaml` | `fresh` | `smoke-fresh` |
| `login_qr_states.yaml` | `fresh` | `login_qr_states` |
| `smoke_tablet.yaml` | `logged_in_tablet` | `smoke-tablet`, `full-tester`, `release-technical` |
| `home_navigation.yaml` | `logged_in_tablet` | `smoke-tablet`, `full-tester`, `release-technical` |
| `calendar_smoke.yaml` | `logged_in_tablet` | `calendar`, `full-tester`, `release-technical` |
| `calendar_view_modes.yaml` | `logged_in_tablet` | `calendar`, `full-tester`, `release-technical` |
| `calendar_event_fields.yaml` | `logged_in_tablet` | `calendar`, `full-tester`, `release-technical` |
| `calendar_recurring_events.yaml` | `logged_in_tablet` | `calendar`, `full-tester`, `release-technical` |
| `tasks_smoke.yaml` | `logged_in_tablet` | `tasks_smoke`, `full-tester`, `release-technical` |
| `chores_smoke.yaml` | `logged_in_tablet` | `chores_smoke`, `full-tester`, `release-technical` |
| `settings_smoke.yaml` | `logged_in_tablet` | `settings_smoke`, `full-tester`, `release-technical` |
| `weather_system_messages.yaml` | `logged_in_tablet` | `weather_system_messages`, `full-tester`, `release-technical` |
| `kiosk_admin_physical.yaml` | `physical_tablet` | `kiosk_admin_physical`, `release-technical` |
| `system_receivers.yaml` | `physical_tablet` | `system_receivers`, `release-technical` |
