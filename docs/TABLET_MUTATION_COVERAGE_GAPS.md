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
| `tvEventDetailTitle` | event-detail dialog title | `dialog_hub_event_detail.xml` |
| `btnEventDetailEdit` | open editing from the detail dialog | `dialog_hub_event_detail.xml` |
| `btnEventDetailDelete` | delete from the detail dialog | `dialog_hub_event_detail.xml` |
| `btnConfirmAction` | primary action of the shared confirm dialog | `dialog_calee_confirm.xml` |

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

* **Calendar delete**: whether confirming returns straight to the Agenda list or
  leaves an empty detail dialog needing a back/close first.
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
