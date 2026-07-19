# Tablet functional mutation coverage — status

## What this is

Three scenarios exercise real functional mutation on the Calee tablet (create/
edit/delete, complete/reopen, skip) — the coverage that did not exist before:

| File | Suite | Covers |
|---|---|---|
| `scenarios/calendar_event_mutation.yaml` | `calendar_event_mutation` | create → verify → edit → verify → delete → verify disappearance |
| `scenarios/tasks_mutation.yaml` | `tasks_mutation` | locate `REG-TASK-OPEN-001` → complete → verify → reopen (single stateful toggle) |
| `scenarios/chores_mutation.yaml` | `chores_mutation` | locate `REG-CHORE-REPEATING-001` → open row action menu → Skip This Time → confirm |

## Source of the resource ids (corrected)

**The Calee Android tablet app source _is_ accessible: `CaleeAdmin/Calee` is the
canonical tablet source, and this framework is developed against it.** An
earlier revision of this document claimed the tablet source was "not one of"
the accessible repositories and that the ids therefore had to stay
`UNCONFIRMED_*` placeholders — that claim was wrong and has been removed.

Every interactive resource id in the three scenarios above is now read directly
from that source (verified against Calee release **#974**, commit
`931beecfc309ff12220185383fa8daae56af30d8` on `main`; the mutation layout files
are byte-identical on the dev head `b32807d`). They are **source-confirmed**,
not guesses:

### Calendar event (`dialog_hub_event_form.xml`, `dialog_hub_event_detail.xml`, `dialog_calee_confirm.xml`)

| Resource id | Role | Layout file |
|---|---|---|
| `etEventTitle` | event title field (create + edit reuse the same form) | `dialog_hub_event_form.xml` |
| `btnEventSave` | save the event form | `dialog_hub_event_form.xml` |
| `rowEventMoreDetails` | expands the collapsed "more details" section (location/description/repeat) | `dialog_hub_event_form.xml` |
| `etEventLocation` | event location field, inside the "more details" section -- the second, "additional meaningful field" `calendar_event_mutation.yaml` now edits alongside the title | `dialog_hub_event_form.xml` |
| `tvEventDetailTitle` | event-detail dialog title | `dialog_hub_event_detail.xml` |
| `btnEventDetailEdit` | open editing from the detail dialog | `dialog_hub_event_detail.xml` |
| `btnEventDetailDelete` | delete from the detail dialog | `dialog_hub_event_detail.xml` |
| `btnConfirmAction` | primary action of the shared confirm dialog | `dialog_calee_confirm.xml` |

Re-confirmed against the current checkout (branch `claude/tablet-sync-blockers-8oizn3`,
Calee 0.3.25 "Add calendar appearance editing (name + colour) support" (#979), commit
`d5b99712158c27f435681946326f0c7b8df54a3e`) -- all ids above are unchanged since release #974.

### Task row (`item_task_list.xml`) — repeated ids, needs row-scoping

| Resource id | Role |
|---|---|
| `taskItemCard` | the row card (enclosing container) |
| `tvName` | the task title cell |
| `flCheckboxTarget` | the completion toggle target (**single stateful toggle**: same target completes and reopens) |
| `ivIcon` | the state icon (drawable swap, not a distinct id) |

### Chore row (`item_chores_list_item.xml`) — repeated ids, needs row-scoping

| Resource id | Role |
|---|---|
| `choresItemCard` | the row card |
| `tvTitle` | the chore title cell |
| `tvActionMenu` | the per-row action menu ("…") — **visible only when "Show chore edit actions" is enabled** (`AccountMgr.getShowChoreEditOptions()`) |
| `btnConfirmAction` | confirm dialog primary action (skip confirmation) |

The chore skip action itself is the action-sheet entry **"Skip This Time"**
(`R.string.chore_action_skip_this_time`), matched by text, not an id.

## Row-scoped actions (why "first global flCheckboxTarget" is wrong)

Task and chore rows reuse the same descendant ids on every row, so tapping the
first global `flCheckboxTarget`/`tvActionMenu` would act on whatever row bound
first, not the fixture row under test. The framework therefore has generic
row-scoped actions (`tap_in_row`, `assert_in_row`, `fail_if_in_row` — see
`calee_regression/runner.py` and `appium_driver.py`) that:

1. find the row card (`card_id`) whose subtree contains the unique fixture
   `title` as visible text/content-desc;
2. **fail loudly on zero or multiple matching rows** (never act on a guess);
3. resolve a descendant control (`target_id`) *within that one row*;
4. retry safely after a RecyclerView rebind (a stale-element/empty transient is
   retried; a genuine zero/multiple-row ambiguity is not).

They are unit-tested in `framework_tests/test_row_scoped_actions.py`.

## What is still gated — physical confirmation (retained requirement)

Source confirmation closes the *selector* gap. It does **not** close the
*physical* gap: none of the three scenarios has yet been run end to end against
a prepared, logged-in tablet/emulator. Until that happens they stay non-release-
gating, and **physical confirmation remains required before any selector becomes
release-gating**. Concretely, each scenario keeps:

* `mandatory: false`;
* the `draft-unverified` tag;
* absence from every release suite (`full-tester` / `release-technical` and the
  `calendar` composite) — `calee_regression/suites.py`'s `SCENARIO_GROUPS` lists
  each only as a standalone entry.

`framework_tests/test_tablet_mutation_drafts.py` enforces this two-state machine
and the promotion rules below.

### Promotion (per scenario, independently)

Promote a scenario **only after it passes on a prepared physical tablet**:

1. remove the `draft-unverified` tag;
2. remove `mandatory: false`;
3. confirm no `UNCONFIRMED_` token remains (already true — source-confirmed);
4. add its suite to `COMPOSITE_SUITES["full-tester"]` in `suites.py`;
5. keep the `source_verification.calee_source_sha` (the full Calee commit the
   selectors were verified against) recorded in the scenario.

The promotion invariant (`test_promotion_invariant_for_full_tester_members`)
makes this self-enforcing: a scenario cannot be in `full-tester` while still
tagged `draft-unverified`, `mandatory: false`, carrying an `UNCONFIRMED_`
token, or missing a full recorded Calee source SHA.

### What a physical pass must still establish

These are behaviours the source tells us the shape of but only a device run can
confirm:

* **Calendar delete**: source-confirmed (not yet physically confirmed) that
  confirming returns straight to the Agenda list, not an empty detail dialog
  needing a back/close first -- `HubEventDetailDialog.kt`'s delete-result
  listener calls `dismissAllowingStateLoss()` as soon as
  `RESULT_KEY_DELETE_SUCCESS` is `true`. `calendar_event_mutation.yaml`'s
  post-delete "Switch to Agenda view" step is accordingly redundant-but-
  harmless, not a workaround for an open dialog -- a physical run should
  confirm this holds and, if so, that step could later be simplified away.
* **Task completed state**: completion swaps the `ivIcon` drawable and moves the
  row toward a completed section — neither is a distinct resource id, so the
  authoritative completed/open assertion is the **API/sync leg (Workstream 5)**,
  not a tablet-side id. A device run should establish whether a content-desc or
  completed-section id exists to strengthen the UI assertion.
* **Chore skip**: requires the regression account to have "Show chore edit
  actions" enabled (or `tvActionMenu` is `GONE`); the authoritative skipped-
  occurrence state is the API/sync leg.

## Best-effort cleanup for interrupted runs

* `calendar_event_mutation.yaml` is self-cleaning (creates and deletes its own
  `REG-SCRATCH-EVENT-MUTATION-ALPHA`/`-BRAVO`). If a run crashes before the
  delete step, remove a leftover scratch event by hand before re-running.
* `tasks_mutation.yaml` reopens the fixture task it completed, leaving
  `REG-TASK-OPEN-001` as found.
* `chores_mutation.yaml` is one-directional (no confirmed un-skip UI); the
  fixture reset (`01 Prepare Test Environment` / `manage_fixture.py reset`, see
  `docs/TEST_DATA_RESET_CONTRACT.md`) restores `REG-CHORE-REPEATING-001`. Do not
  run it back-to-back without an intervening reset.

## Framework additions supporting these scenarios

- `clear_text` / `fail_if_id` (pre-existing) — clear a pre-filled field; assert
  an element is gone by id.
- `tap_in_row` / `assert_in_row` / `fail_if_in_row` (Workstream 4) — the
  row-scoped actions above.
- `tap_unique_text` (`calee_regression/appium_driver.py::tap_unique_text`,
  `runner.py`'s `tap_unique_text` action) — the flat-list equivalent of
  `tap_in_row`'s "never select an arbitrary row" guarantee, for screens with
  no row-container resource id to scope by. Matches by EXACT `@text`/
  `@content-desc` equality (never substring) and fails loudly on zero or
  multiple matches instead of silently acting on whichever element
  `find_element` returns first. `calendar_event_mutation.yaml` uses this to
  open the newly-created/edited scratch event in the Agenda list. Unit-tested
  in `framework_tests/test_tap_unique_text.py` (unique match, substring-
  collision safety, zero-match, multiple-match, and the runner action
  wiring).
- `blocks_on_absence: true` (a per-step opt-in flag, mirroring the existing
  `optional`/`required` idiom) — declares that THIS step's own failure means a
  precondition (the deterministic REG-* fixture, or an account-config-gated
  control like chores' "Show chore edit actions") was absent, not that the
  product regressed, so `runner.py::_execute_step` records `BLOCKED` instead
  of the framework's default `FAILED`. Applied to `tasks_mutation.yaml`'s and
  `chores_mutation.yaml`'s initial fixture-presence `wait_for_text` steps, and
  to `chores_mutation.yaml`'s `tap_in_row` step that opens `tvActionMenu`
  (Phase 3/4: "treat fixture absence as BLOCKED, not FAIL"; "a missing
  required action menu must block the scenario"). Deliberately opt-in, not the
  new default for every step: an unmarked row/element resolution failure could
  just as easily be a real product regression (e.g. a RecyclerView that
  stopped rendering), and silently downgrading every such failure to
  `BLOCKED` would risk masking one. Unit-tested in
  `framework_tests/test_result_model.py`.
- Generic on-failure diagnostics capture (`runner.py::_execute_step`,
  `CaleeDriver.capture_diagnostics`) — Phase 2's "capture screenshots and
  page-source diagnostics on failure" previously only applied to row-scoped
  action failures (`RowResolutionError`/`RowAmbiguityError`, which carry their
  own capture). Any OTHER failed/blocked step (a plain `tap`, `assert_text`,
  `wait_for_id`, `tap_unique_text`, ...) now also gets a best-effort
  screenshot + page-source capture attached to its `StepResult`, using the
  same run-workspace `diagnostics_dir` the row-scoped path already writes
  into. Best-effort by design: a driver/test-double without this capability,
  or a capture that itself fails, never masks the underlying assertion
  error. Unit-tested in `framework_tests/test_result_model.py`.
