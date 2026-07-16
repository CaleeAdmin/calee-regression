# Troubleshooting

Start with `python -m calee_regression prepare --config <your config>` for any of the below (or
double-click `01 Prepare Test Environment.command`) — it checks most of these automatically and
prints a plain-language hint. `prepare` also resets the regression fixture; use
`python -m calee_regression doctor --config <your config>` if you only want the local-environment
checks without touching the fixture.

## Exit codes

| Exit code | Meaning |
|---|---|
| `0` | Success — everything that ran passed. |
| `1` | Product regression — a real assertion failed. |
| `2` | Invalid usage/configuration — bad `--config`, unknown suite name, missing `--confirm-technical`, missing credentials. |
| `3` | Blocked — the test environment (device, Appium, fixture, credentials, tooling) wasn't ready. Not a product failure. |

## BLOCKED scenarios specifically

A scenario reporting `blocked` (not `failed`) means the framework couldn't determine PASS or FAIL
at all — e.g. Appium was unreachable, or the scenario file itself was invalid. Check the scenario's
`blocked_reason` in the report; it's written in plain language and points at what to fix. Never
treat a BLOCKED result as evidence of a product bug.

| Symptom | Cause | Fix |
|---|---|---|
| A scenario reports `blocked` with "Could not start an Appium session" | Appium is down, or the device disconnected between `prepare` and running the suite | Re-run `01 Prepare Test Environment`, confirm the device is still connected, then retry |
| `calendar_event_fields`/`calendar_recurring_events` are `blocked` or fail to find a fixture event by title | The REG-* regression fixture hasn't been reset (or reset failed) | Run `01 Prepare Test Environment` with fixture credentials configured (see `docs/SETUP_MAC.md`) — check `docs/TEST_DATA_RESET_CONTRACT.md` for what should exist afterward |
| `01 Prepare Test Environment` says "Fixture reset skipped: no target environment/test-account configured" | `CALEE_API_BASE`/`CALEE_TEST_EMAIL`/`CALEE_TEST_PASSWORD` aren't set | Set them in your shell profile — see `docs/SETUP_MAC.md` |
| `03`/`04 Test CaleeMobile ...` reports BLOCKED for the UI portion specifically | Flutter isn't installed, or no device/emulator/simulator is connected | Install Flutter (`docs/SETUP_MAC.md`) and connect a device — the backend API checks in the same run are unaffected and still count |
| `03`/`04 Test CaleeMobile ...` reports BLOCKED immediately | `CaleeMobile-Regression` isn't checked out as a sibling of `calee-regression` | Check it out next to this repo — see `docs/SETUP_MAC.md` §2 |
| `doctor` reports `appium_reachable: error`, or every command fails with a connection error | Appium isn't running | Start it: `appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell` |
| Errors mentioning `/session` or `//session`, or 404s from Appium | The Appium server's `--base-path` doesn't match your config's `appium_url` | Make sure `appium_url` ends in `/wd/hub` if the server was started with `--base-path /wd/hub` (the default this framework expects) — or change one to match the other |
| `doctor` reports `android_sdk_env: error` | Neither `ANDROID_HOME` nor `ANDROID_SDK_ROOT` is set | `export ANDROID_HOME=/path/to/Android/sdk` (e.g. `~/Library/Android/sdk` on Mac) |
| `doctor` reports `apk_exists: error` | `apk_path` in your config points to a file that doesn't exist | Fix `apk_path` in `config/tester.local.yaml` |
| `doctor` reports `device_connected: error` or `warning` | Emulator not started, or tablet not connected/authorized | Start the emulator, or connect the tablet via USB with debugging enabled; check `adb devices` |
| Errors mentioning "insecure" or "adb_shell" / "has not been enabled" | Appium wasn't started with the insecure `adb_shell` feature, but a scenario step needs shell access | Restart Appium with `--allow-insecure uiautomator2:adb_shell` |
| Config fails to load with a `PUT_ACTIVITY_HERE` error | A placeholder value was left in the config (usually copy-pasted from an incomplete template) | Replace it with the real value — e.g. `app_activity: ".ui.HomeActivity"`, not `PUT_ACTIVITY_HERE`. See `docs/CALEE_LAUNCH_MODEL.md`. |
| A `logged_in_tablet` scenario is skipped, or fails with *"Calee launched, but the screen is not the logged-in home screen. This scenario requires a prepared tablet or test account."* | The device is on the clean/onboarding screen, not signed in — this is expected behavior, not a bug | Run `smoke-fresh`/`login_qr_states` on clean devices; only run tablet-suites once a technical owner has prepared a logged-in demo account and set `expected_state: logged_in_tablet` (see `docs/TEST_DATA_RESET_CONTRACT.md`) |
| `driver.activate_app("com.viso.calee")`-style errors, or Calee just never opens | Calee has no normal launcher intent-filter — it can't be "activated" like a typical app | Use one of the framework's real `launch_strategy` values (`direct_activity`, `start_action`, `calee_shell`); only use `normal_launcher` for apps that actually have a launcher activity. See `docs/CALEE_LAUNCH_MODEL.md`. |
| `suite --suite release-technical` is refused | Suites containing physical-tablet-only scenarios require explicit confirmation | Pass `--confirm-technical`, or set `allow_release_technical: true` in your config — only do this on a real physical tablet |
