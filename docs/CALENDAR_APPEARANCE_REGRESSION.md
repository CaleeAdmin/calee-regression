# Calendar name/colour appearance-editing regression coverage

Proves the calendar appearance-editing contract introduced by
**calee-hub-core's `PATCH /client/v1/calendars/{id}/appearance`** and
**Calee PR CaleeAdmin/Calee#977** (branch
`claude/calendar-name-colour-editing-balgeo`, commit
`f1b92ddae9275cb0abea0f6df34126930e3aa71d`, based on latest `dev`).

## The contract

`GET /client/v1/calendars` calendar objects now carry `sourceName`,
`sourceColor`, `appearanceMode` (one of `source_metadata` /
`subscription_mapping` / `external_calendar` / `unsupported`), and
`capabilities` (`canEditAppearance` / `canEditEvents` /
`canEditSourceMetadata` / `canRemoveFromCalee` / `canDeleteSource`). The new
`PATCH /client/v1/calendars/{id}/appearance` endpoint edits name/colour,
routed by `appearanceMode`:

| `appearanceMode` | What a PATCH does | `canEditAppearance` | Reaches the tablet edit dialog? |
|---|---|---|---|
| `subscription_mapping` | Local Calee-side display only -- the source's own name/colour is untouched | `true` | Yes -- "local display only" note |
| `external_calendar` | Local Calee-side display only | `true` | Yes -- "local display only" note |
| `source_metadata` | PROPPATCHes the real owned CalDAV calendar | `true` | Yes -- "updates the real calendar" note |
| `unsupported` (shared read-only CalDAV) | Rejected with a stable `409 CALENDAR_APPEARANCE_NOT_SUPPORTED`; read-only/owner-managed messaging instead | `false` | No -- `ivEdit` is invisible, dialog never opens |

This is a backend contract (`calee-hub-core`) this session does not
implement or unit-test directly -- it is transcribed, source-confirmed
context for the tablet (`CaleeAdmin/Calee`) and sync-smoke coverage below.

## What the tablet shipped (Calee PR #977)

**Calendar sidebar row** (`CalendarFragment.kt`,
`item_calendar_navigation_list.xml`): the pre-existing pencil/edit icon
(`ivEdit`) is `VISIBLE`+clickable only when `capabilities.canEditAppearance`
is `true`, else `INVISIBLE`. A new `tvOwnerManagedNote` `TextView` is
`VISIBLE` with the exact text **"This shared calendar is managed by its
owner."** (`R.string.hub_calendar_owner_managed_message`) when
`appearanceMode == "unsupported"`, else `GONE`.

**Calendar edit dialog** (`HubCalendarEditDialog.kt`,
`dialog_hub_collection_manage.xml`, opened by tapping `ivEdit`):
`tvCollectionTitle` now reads **"Edit Name & Colour"**
(`hub_calendar_edit_title`); `tvCollectionNameLabel` now reads **"Name in
Calee"** (`hub_calendar_name_hint`); `etCollectionName`/`btnCollectionSave`/
`btnCollectionCancel`/`btnCollectionDelete` are unchanged ids. Two ids are
new: `tvCollectionColorLabel` (an existing label, now reads **"Colour in
Calee"**, `hub_calendar_color_label`) and `tvCollectionAppearanceNote`
(`VISIBLE` with **"These changes only affect how this calendar appears in
Calee."** for `subscription_mapping`/`external_calendar`,
`hub_calendar_appearance_note_local_only`; `VISIBLE` with **"This updates
the calendar name and colour."** for `source_metadata`,
`hub_calendar_appearance_note_source_metadata`; `GONE` otherwise --
`unsupported`-mode calendars never reach this dialog, since `ivEdit` is
invisible for them). Saving calls the new `PATCH .../appearance` endpoint,
not the old plain calendar-update endpoint (backend-internal, not
tablet-UI-observable).

These selectors/strings are **source-confirmed** (read directly from the
real shipped diff, commit `f1b92ddae9275cb0abea0f6df34126930e3aa71d`), not
guessed -- but per this repo's established convention, still
`physical_confirmation: pending` until run on a real device (see "What is
still gated" below).

## Proven offline now

No backend and no device is needed for either of these:

* **`framework_tests/test_sync_smoke.py`** (the calendar-appearance section)
  proves `run_calendar_appearance_sync_flow`'s orchestration logic against
  fakes -- including two genuine-regression cases that must FAIL, not
  silently pass: `test_calendar_appearance_flow_source_name_check_fails_if_provider_metadata_actually_changes`
  (a refresh that clobbers the provider's own `sourceName` is caught) and
  `test_calendar_appearance_flow_events_non_editable_check_fails_if_capability_flips_true`
  (a calendar whose events become editable mid-flow is caught). This is the
  same kind of "prove the logic independent of live execution" evidence
  `docs/SUBSCRIBED_CALENDAR_REGRESSION.md` gets from `ics_contract.py`'s
  dependency-free ICS expansion -- just for orchestration/sequencing
  correctness rather than date math.
* **`framework_tests/test_calendar_appearance_scenarios.py`** proves the
  three tablet scenario files below are internally consistent with their own
  recorded contract: the right note text per `appearanceMode`, `ivEdit`
  asserted present for subscription/owned but absent for shared-readonly,
  the shared-readonly scenario never referencing the edit dialog's ids at
  all, no scenario ever taps `btnCollectionSave` (none of them mutate the
  shared fixture), every recorded selector actually used, the exact Calee
  commit SHA recorded, and the repo-wide crash-dialog guard as the last step
  of every scenario.

## Tablet assertions -> scenario (single-device-observable)

Three scenario files, gated `draft-unverified` / `mandatory: false`,
standalone under the `calendar_appearance` suite
(`python -m calee_regression list-suites`), never in `full-tester`/
`release-technical`:

| # | Assertion | File | Status |
|---|---|---|---|
| 1 | `ivEdit` visible+tappable for a subscription calendar (`canEditAppearance`) | `calendar_appearance_subscription.yaml` | in scenario, real fixture |
| 2 | Edit dialog title/labels ("Edit Name & Colour" / "Name in Calee" / "Colour in Calee") | `calendar_appearance_subscription.yaml`, `calendar_appearance_owned.yaml` | in scenario |
| 3 | Appearance note = local-display-only text for a subscription calendar | `calendar_appearance_subscription.yaml` | in scenario, real fixture |
| 4 | `ivEdit` visible+tappable for an owned CalDAV calendar | `calendar_appearance_owned.yaml` | in scenario, **needs new fixture** |
| 5 | Appearance note = "updates the real calendar" text for an owned CalDAV calendar | `calendar_appearance_owned.yaml` | in scenario, **needs new fixture** |
| 6 | `ivEdit` absent for a shared read-only CalDAV calendar | `calendar_appearance_shared_readonly.yaml` | in scenario, **needs new fixture** (see the INVISIBLE-vs-GONE caveat in the file's header) |
| 7 | `tvOwnerManagedNote` present with the exact owner-managed text | `calendar_appearance_shared_readonly.yaml` | in scenario, **needs new fixture** |
| 8 | Colour control/label is present and interactable | all three | in scenario (colour-independent proxy -- see the gap below) |
| 9 | A real name/colour change actually persists and is visible on another surface | -- | **not YAML-appropriate**, see `run_calendar_appearance_sync_flow` below |

Not covered by any scenario in this repo: the `external_calendar`
`appearanceMode` (no fixture of that type exists, and it shares its note
text with `subscription_mapping`, so `calendar_appearance_subscription.yaml`
already exercises that specific note-text branch of the tablet code).

### BLOCKED (tablet)

Running any of the three scenarios needs a prepared tablet/emulator, which
is not available in this environment -- BLOCKED, same as every other draft
scenario in this repo. Additionally:

* `calendar_appearance_subscription.yaml` needs the REG-SUB subscription
  fixture provisioned exactly as
  `docs/SUBSCRIBED_CALENDAR_REGRESSION.md` describes.
* `calendar_appearance_owned.yaml` and `calendar_appearance_shared_readonly.yaml`
  need fixture calendars that **do not exist anywhere in this repo or its
  siblings yet** -- see "Fixture / provisioning-contract status" below.

## Cross-device assertions -> `run_calendar_appearance_sync_flow` (genuinely cross-device)

A single YAML scenario cannot express "rename here, verify there" --
`ScenarioRunner` drives exactly one `CaleeDriver`/one device per CLI
invocation (`calee_regression/runner.py:536`). The genuinely cross-device
half of this contract lives in `calee_regression/sync_smoke.py`'s
`run_calendar_appearance_sync_flow`, following the exact shape
(`SyncStepEvidence`/`SyncFlowResult`, bounded `poll_until`, a `surface` per
step, honest `BLOCKED` for anything not actually attempted) the existing
event/task/chore flows already established:

1. `capture_baseline_via_api` -- a baseline read before any mutation (needed
   to later prove the provider's own metadata was never touched).
2. `rename_via_api` -- sets the calendar's local display name (models "via
   API or CaleeMobile"; see "Design choices" below).
3. `poll_tablet_for_renamed_calendar` -- bounded poll for the new name on
   the tablet.
4. `change_color_via_api` / `verify_color_persisted_via_api` -- sets a new
   colour, then a **fresh** GET confirms the persisted hex value (the same
   colour-independent-of-tablet proxy described below).
5. `verify_color_change_on_tablet` -- **always `BLOCKED`**: no colour-reading
   primitive exists in `CaleeDriver` at all (see the gap below). Unlike
   every other step in this flow, this one is not contingent on env wiring
   -- it can never become attemptable just because a real API/tablet
   session is available.
6. `trigger_provider_refresh_via_api` -- asks the hub to refresh this
   calendar from its provider/subscription source now.
7. `verify_override_survives_refresh_via_api` /
   `verify_override_survives_refresh_on_tablet` -- the local name+colour
   override must survive a refresh (proves `subscription_mapping`/
   `external_calendar`'s "local display only" promise isn't silently
   clobbered by the next sync).
8. `verify_source_name_preserved_via_api` -- the provider's own `sourceName`
   (captured in step 1) must be unchanged -- proves the rename never touched
   the source, only the local display (the "external-provider name-change-
   preserves-provider-name" requirement).
9. `verify_events_non_editable_via_api` /
   `verify_events_non_editable_on_tablet_weak_signal` -- the calendar's
   events must still report `capabilities.canEditEvents == false`
   throughout (API leg, authoritative); the tablet leg is a weak/partial
   signal only (title-presence), the same idiom `run_task_sync_flow`/
   `run_chore_sync_flow` already use for a tablet-side check with no
   dedicated assertion primitive.

**Design choices:** the rename/recolour "via API" leg stands in for "via
API or CaleeMobile" -- both ultimately call the same `PATCH .../appearance`
endpoint, and CaleeMobile-Regression has no dedicated appearance-editing UI
flow to shell out to yet (the same substitution `run_event_sync_flow`
already makes: an event "created via API" stands for "created from
off-tablet"). Colour is changed via the API leg, never the tablet leg, for
the same reason the tablet-side colour-verification step is blocked: there
is no honest way to drive or verify a tablet-INITIATED colour change with
what `CaleeDriver` can do today.

### Two DISTINCT gaps, not one

`run_calendar_appearance_sync_flow` mixes two different reasons a step can
be `BLOCKED`, and it matters which is which:

* **The colour-assertion gap** (step 5 above): permanent, and does not
  depend on `SyncSmokeEnvironment` wiring. It stays `BLOCKED` even in a
  fully-wired, fully-passing run, exactly like `run_event_sync_flow`'s
  `modify_on_tablet`/`run_task_sync_flow`'s `reopen_on_tablet` stay `BLOCKED`
  for the (different) tablet-mutation gap.
* **The "not wired into `build_real_environment()` yet" gap**: unlike the
  event/task/chore flows, `api_set_calendar_appearance`/`api_get_calendar`/
  `api_trigger_calendar_refresh` are `Optional` fields on
  `SyncSmokeEnvironment` (default `None`) that `build_real_environment()`
  does not currently populate. Wiring them for real needs new
  CaleeMobile-Regression API actions (`set-calendar-appearance` /
  `get-calendar` / `trigger-calendar-refresh`) that do not exist in
  `sync_smoke_actions.py`/`sync_smoke_cli.py` today (confirmed by reading
  that sibling script's `argparse` action list: only `create-scratch-event`
  / `get-event` / `delete-event` / `reopen-task` exist) -- and
  CaleeMobile-Regression is explicitly out of scope for this change. Every
  step that needs one of these callables records `BLOCKED` honestly with
  `APPEARANCE_API_NOT_WIRED_DETAIL` instead of raising; the orchestration
  logic around them is still fully exercised with fakes (see "Proven
  offline now"). Closing this gap is future work: add the three actions to
  `sync_smoke_actions.py`/`sync_smoke_cli.py`, then wire
  `build_real_environment()` to call them, mirroring
  `create_scratch_event`/`get_event`/`delete_event`'s existing pattern in
  `calee_regression/sync_smoke_bridge.py`.

### Release-gating status

`run_calendar_appearance_sync_flow` is one of the flows
`run_all_sync_flows()` returns, alongside the existing event/task/chore
flows -- and `component_from_sync_report` (in `consolidated_report.py`)
builds the `sync` release component from *whatever* flows a report
contains, generically, by status. That means this flow becomes
release-gating through the **exact same, already-existing mechanism** the
other three flows use -- `release_features.synchronization` in
`config/release-platforms.yaml` (defaults to mandatory when the file is
absent) -- with **no new feature flag, CLI option, or consolidation code**
needed. See `docs/SUITE_REFERENCE.md`'s "Partially implemented: `sync-smoke`"
section, now listing all four flows.

Like the other three flows, this one currently, always resolves to
`BLOCKED` for its live-device legs (the colour-assertion gap is permanent;
the API-wiring gap needs sibling-repo work; and even once both close, there
is no physical tablet/backend in this sandbox to run against) -- by design,
matching this repo's existing safety property ("a PASS must not be possible
while a live check is unverified"), not a shortcoming introduced here.

## The colour-assertion gap

No colour-reading primitive exists anywhere in `CaleeDriver`
(`calee_regression/appium_driver.py`) -- it offers only id/text presence,
tap, type, and clear (`text_present`/`find_by_id`/`tap_by_id`/`type_text`/
`clear_text`; no `get_attribute`, no pixel/colour inspection of any kind).
The YAML action vocabulary (`runner.ACTIONS`) has nothing like
`assert_color` either.

This is the same shape of gap `docs/CALENDAR_BIG_CHANGE_COVERAGE.md`
already documents for `calendar_view_modes.yaml`: a purely visual/style
property ("the nav row's own 'selected' look is pure background/text-color
styling with no UiAutomator-visible state") that cannot be asserted
directly, so the existing scenario asserts a **different, real, observable
proxy** instead (the view-mode content panel becoming visible) rather than
inventing an action the driver doesn't have.

This repo follows the same approach here, in two places:

* **YAML scenarios** (`calendar_appearance_subscription.yaml`/`_owned.yaml`)
  assert that the colour **label/control** (`tvCollectionColorLabel`) is
  present and its text is correct -- proving the UI offers colour editing at
  all, not that any particular colour renders correctly.
* **`run_calendar_appearance_sync_flow`** verifies a persisted colour value
  the only way it honestly can: a **fresh `GET` via the API** confirming the
  stored hex string matches what was set (`verify_color_persisted_via_api`)
  -- never a tablet pixel/attribute read. The tablet-side colour-
  verification step (`verify_color_change_on_tablet`) stays permanently
  `BLOCKED` and says so.

Closing this gap for real would mean adding a genuine colour-reading
capability to `CaleeDriver` (e.g. reading a swatch view's background-colour
attribute via `get_attribute`, if UiAutomator2 exposes one) and a
corresponding `assert_color`-style YAML action -- out of scope for this
change; not attempted here, and not faked.

## Fixture / provisioning-contract status

Stated as plainly as `docs/SUBSCRIBED_CALENDAR_REGRESSION.md` already does
for the subscription type:

| Calendar type | `appearanceMode` | Fixture status |
|---|---|---|
| Subscription | `subscription_mapping` | **Provisioned** -- reuses the existing REG-SUB fixture (`fixtures/subscribed_calendar/reg_sub_calendar.ics`, calendar id `regression:regsub`, display name `REG-SUB Regression Subscription` from its `X-WR-CALNAME`). Still needs the hub backend + a prepared tablet to actually load it -- BLOCKED here, not because the fixture is undefined. |
| Owned CalDAV | `source_metadata` | **Not provisioned.** No deterministic, uniquely-named REG-* calendar fixture exists in this repo today (only REG-EVENT-*/REG-TASK-*/REG-CHORE-* *records* are documented in `docs/TEST_DATA_RESET_CONTRACT.md`, which imply an owned calendar exists behind them but never name it). This doc **proposes** a new entry, `REG-OWNED-CALENDAR-APPEARANCE-001`, provisioned the same way the rest of the REG-* fixture is -- ordinary `POST /client/v1/calendars` via the Calee Client API (calee-hub-core has no seed/test-data endpoint). **Not implemented anywhere in this repo or `CaleeMobile-Regression` as of this change.** |
| External calendar | `external_calendar` | **Not provisioned, and no scenario targets it.** Shares its note text with `subscription_mapping` (already exercised by `calendar_appearance_subscription.yaml`), so this was judged lower priority than the other two gaps for this change. Provisioning it for real needs whatever external-provider integration (e.g. OAuth-based sync) `appearanceMode: external_calendar` actually maps to -- out of scope to design here. |
| Shared read-only CalDAV | `unsupported` | **Not provisioned -- the hardest of the three.** Proposes `REG-SHARED-READONLY-CALENDAR-001`: a calendar owned by a *second* regression-owned account, shared read-only to the primary regression account under test. Unlike the owned-calendar proposal above, this cannot be provisioned via ordinary single-account CRUD -- it needs either a second regression account to share from, or a backend admin/fixture endpoint that does not exist today. **Not implemented anywhere in this repo or its siblings as of this change.** |

None of these proposed fixture entries are added to
`docs/TEST_DATA_RESET_CONTRACT.md` itself in this change -- that document
describes what `manage_fixture.py reset` (in the sibling
`CaleeMobile-Regression` repo, out of scope here) actually does today, and
adding unimplemented entries there would make it inaccurate. They are
recorded here instead, exactly as proposals, until `manage_fixture.py`
actually grows them.

## Known gaps beyond source-confirmation

Kept in one place for visibility, since several show up more than once
above:

1. **Physical confirmation** (all three scenarios, both flow's live legs):
   no Appium/adb/emulator/Flutter toolchain exists in this sandbox (a prior
   investigation of this exact repo confirmed this) -- nothing here has run
   against a real or emulated device.
2. **Row-scoping**: `item_calendar_navigation_list.xml`'s row-CONTAINER id
   (the `card_id` `tap_in_row`/`assert_in_row`/`fail_if_in_row` would need,
   mirroring how task/chore rows needed exactly this -- see
   `docs/TABLET_MUTATION_COVERAGE_GAPS.md`) was not part of the transcribed
   diff and is not source-confirmed. The three scenarios target `ivEdit`/
   `tvOwnerManagedNote` by bare id, not row-scoped; see each scenario's
   header comment for the exact, per-direction consequence (tapping/
   asserting the wrong row vs. a false `fail_if_id` failure from an
   unrelated row's visible `ivEdit`).
3. **`ivEdit`'s `INVISIBLE` (not `GONE`) hidden state**: `fail_if_id` asserts
   by presence, not by a displayed/enabled check `CaleeDriver` doesn't have.
   Whether UiAutomator2 still surfaces an `INVISIBLE` `ivEdit` on the actual
   Android/Appium combination in use is unconfirmed -- see
   `calendar_appearance_shared_readonly.yaml`'s header.
4. **The colour-assertion gap** -- see the dedicated section above.
5. **`run_calendar_appearance_sync_flow`'s API-leg wiring gap** -- see
   "Two DISTINCT gaps, not one" above.
6. **Fixture provisioning for owned/external/shared-readonly calendars** --
   see the table above.

None of these are hidden behind an apparent PASS: every scenario stays
`mandatory: false` / `draft-unverified` / outside every release suite, and
every sync-smoke step affected by one of these gaps records `BLOCKED` with
a detail naming the exact gap, never a fabricated pass.
