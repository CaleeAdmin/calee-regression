# Test data / device state contract

This framework does not itself wipe, reset, or provision any device or account — no scenario file
issues a destructive shell command. Preparing and resetting test devices is a manual, out-of-band
responsibility of a **technical owner**, following the contract below.

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
