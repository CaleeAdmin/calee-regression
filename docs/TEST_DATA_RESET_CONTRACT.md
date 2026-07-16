# Test data / device state contract

This framework does not itself wipe, reset, or provision any device or account — no scenario file
issues a destructive shell command. Preparing and resetting test devices is a manual, out-of-band
responsibility of a **technical owner**, following the contract below.

## Deterministic regression fixture (REG-* records)

The `logged_in_tablet` demo account must contain a set of deterministic, predictably-named records
so scenarios can target an exact known title instead of guessing at whatever data happens to
already exist. These are created and verified through the Calee Client API — calee-hub-core has no
seed/test-data endpoint, so fixture records go through the same ordinary CRUD endpoints CaleeMobile
itself uses — by `caleemobile_regression.fixture` in the sibling `CaleeMobile-Regression` repo:

```
REG-EVENT-TIMED-001       a timed (non-all-day) event
REG-EVENT-ALLDAY-001      an all-day event
REG-EVENT-RECURRING-001   a recurring event (FREQ=WEEKLY)
REG-EVENT-EXCEPTION-001   a detached occurrence of REG-EVENT-RECURRING-001 with an overridden title
REG-TASK-OPEN-001         an incomplete task
REG-TASK-COMPLETE-001     a completed task
REG-CHORE-REPEATING-001   a repeating chore
REG-CHORE-SKIPPED-001     a chore that has been skipped past today
```

Before running `calendar` or `full-tester`/`release-technical` against a real environment, a
technical owner (or the `01 Prepare Test Environment` launcher) runs, from a checkout with
`CaleeMobile-Regression` cloned as a sibling of this repo:

```bash
cd ../CaleeMobile-Regression/api
python3 manage_fixture.py reset --base-url <env> --email <test-account> --password <...>
```

This is idempotent — re-running it deletes only the fixture's own `REG-FIXTURE-*` collections
(never unrelated user data) and recreates them, so a stale run never accumulates. It exits `0` on
success and a non-zero **blocked** exit code if it can't prepare the fixture (bad credentials,
unreachable environment, an unexpected API response) — never a false product-failure signal. See
`CaleeMobile-Regression/api/caleemobile_regression/fixture.py` for the exact fields/assertions and
`api/tests/test_fixture.py` for its own self-tests against a fake server.

`calendar_event_fields.yaml` and `calendar_recurring_events.yaml` now hard-require these records —
they tap the fixture's exact titles and assert real Calee resource ids (`tvEventDetailTitle`,
`tvEventDetailRecurring`, ...), so they can no longer pass without the fixture actually being in
place and the tablet actually rendering it correctly.

## Fixture/backend alignment before mobile UI checks

The fixture above is reset against a specific backend (`--base-url <env>`). Before running any
CaleeMobile UI assertion, `run_ui_suite.py` (in `CaleeMobile-Regression/ui/`) verifies:

- **This run's fixture is actually ready** — `prepare`'s `fixtureVerificationStatus` (from this
  run's `reports/runs/<run-id>/environment/results.json`) must be exactly `"ok"`. Anything else
  (absent, `blocked`, `blocked_missing_credentials`, `not_run`, ...) BLOCKS before a single UI
  assertion runs — never assert against fixture data that was never confirmed present.
- **The fixture's backend matches what CaleeMobile's build actually talks to.** This currently
  has a hard limitation worth being explicit about: CaleeMobile's `CaleeHubClient()` has **no
  build-time backend override** today (no dart-define/flavor mechanism) — every build always
  talks to `https://hub.calee.com.au` (see `run_ui_suite.py::KNOWN_CALEE_MOBILE_BACKEND`, sourced
  directly from CaleeMobile's `lib/app/calee_app.dart` and `lib/data/api/calee_hub_client.dart`).
  If the fixture was reset against a different backend (e.g. `hub-dev.calee.com.au`), that
  mismatch is detected and BLOCKS before any UI assertion — but the fixture must actually be
  reset against **production** for CaleeMobile's mobile UI checks to run meaningfully at all,
  until CaleeMobile gains real backend configurability. Treat that as a standing constraint on
  what a mobile UI PASS can mean today, not a bug in this check.

Both checks are wired through `scripts/test_caleemobile.sh`, which reads this run's own
`environment/results.json` and exports `CALEE_FIXTURE_STATUS`/`CALEE_EXPECTED_BACKEND` before
invoking `run_ui_suite.py` — see `check_fixture_and_backend_alignment` there and
`docs/RELEASE_POLICY.md`.

## "fresh" state

- No Hub session on the device (no stored access/refresh tokens).
- `HomeActivity` redirects to `SignUpActivity`, showing the QR/manual login onboarding screen.
- Used by: `smoke-fresh`, `login_qr_states`.
- Typically a brand-new emulator, or a tablet that has had its Calee app data cleared and no demo
  account signed in.

## "logged_in_tablet" state

- A signed-in Hub demo/test account (via QR or manual login — see `docs/CALEE_LAUNCH_MODEL.md` and
  Calee's `docs/auth-model.md` for the auth model).
- At minimum, a family/calendar with a few known fixture events, **including at least one recurring
  event**, so `calendar_recurring_events.yaml` and `calendar_event_fields.yaml` exercise real content
  instead of skipping their `optional`-wrapped assertions every run.
- Used by: `smoke-tablet`, `home_navigation`, `calendar`, `tasks_smoke`, `chores_smoke`,
  `settings_smoke`, `weather_system_messages`, and (as part of `full-tester`) `release-technical`.

## "physical_tablet" state

- A real physical tablet, not an emulator, with kiosk/admin access as applicable.
- Used only by `kiosk_admin_physical` and `system_receivers`.

## Who resets what, and when

- A **technical owner** should prepare and periodically reset the shared logged-in tablet/emulator to
  a known fixture state — ideally before each `full-tester` run, so results are comparable run to
  run.
- Testers running `.command` files should never attempt to sign out, clear app data, or otherwise
  change device state themselves — ask a technical owner if a device seems to be in the wrong state.
- The framework's own skip logic (`requires_state` vs. the config's `expected_state`) is a safety net,
  not a substitute for keeping the device in the right state: it prevents a false failure report, but
  it can't make a `logged_in_tablet` scenario meaningful on a `fresh` device.
