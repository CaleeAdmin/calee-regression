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
| A scenario reports `blocked` with "Could not start an Appium session" | Appium is down, or the device disconnected between `prepare` and running the suite | Re-run `01 Prepare Test Environment` (this also retries starting Appium automatically), confirm the device is still connected, then retry |
| `calendar_event_fields`/`calendar_recurring_events` are `blocked` or fail to find a fixture event by title | The REG-* regression fixture hasn't been reset (or reset failed) | Run `01 Prepare Test Environment` with fixture credentials configured (see `docs/SETUP_MAC.md`) — check `docs/TEST_DATA_RESET_CONTRACT.md` for what should exist afterward |
| `01 Prepare Test Environment` reports **BLOCKED**: "fixture credentials are not configured" | `CALEE_API_BASE`/`CALEE_TEST_EMAIL`/`CALEE_TEST_PASSWORD` aren't set — this now blocks rather than silently continuing, since release-gating scenarios can't be trusted without the fixture | Set them in your shell profile (see `docs/SETUP_MAC.md`), or pass `--allow-no-fixture --suite <suite-that-does-not-need-it>` from a terminal if you're deliberately running something that doesn't need the fixture |
| A step or scenario reports BLOCKED with "this step is required (default)" or "nothing was actually verified" | A `tap_if_present` target was absent and the step wasn't explicitly marked `optional: true`/`required: false`, or every step in the scenario was skipped/optional | This is by design — see `docs/SCENARIO_REFERENCE.md`'s required/optional step metadata. If the target's absence really is acceptable, mark the step `optional: true`; otherwise this is telling you the real UI id/state changed |
| A suite is BLOCKED even though every scenario that ran passed | A **mandatory** scenario ended up `SKIPPED` (e.g. a `requires_state` mismatch) — this now blocks the suite instead of silently being ignored | Run the suite against a device in the state the skipped scenario actually needs, or mark the scenario `mandatory: false` in its YAML if skipping it really is acceptable for this suite |
| `01 Prepare Test Environment` reports BLOCKED: "could not start Appium automatically" | The `appium` executable isn't installed/on PATH, or it exited immediately, or it didn't become healthy within the timeout — see `reports/appium.log` for details | Install Appium (`npm install -g appium && appium driver install uiautomator2`), or start it manually once to see the real error, then let `prepare` manage it going forward |
| `03`/`04 Test CaleeMobile ...` reports BLOCKED for the UI portion specifically | Flutter isn't installed, `CALEE_TEST_EMAIL`/`CALEE_TEST_PASSWORD` aren't set, or no device/emulator/simulator is connected (or more than one of the same platform is connected, which is ambiguous) | Install Flutter and set credentials (`docs/SETUP_MAC.md`), connect exactly one matching device, or set `CALEE_UI_DEVICE_ID` to pick one explicitly — the backend API checks in the same run are unaffected and still count |
| `03`/`04 Test CaleeMobile ...` reports **FAIL** (not BLOCKED) for the UI portion | A real assertion failed against a running app — `ui/run_ui_suite.py` distinguishes this from a compile/toolchain/device problem (which BLOCKs instead) | Treat this as a real product problem — check `reports/mobile-<platform>-*/flutter.log` and the structured `results.json` for which test and assertion failed |
| `03`/`04 Test CaleeMobile ...` reports BLOCKED immediately | `CaleeMobile-Regression` isn't checked out as a sibling of `calee-regression` | Check it out next to this repo — see `docs/SETUP_MAC.md` §2 |
| A manual check shows as BLOCKED in the consolidated report | It's mandatory for this release and wasn't answered (or was answered Blocked) | Run `05 Record Manual Checks.command` and answer it |
| `doctor` reports `appium_reachable: error`, or every command fails with a connection error | Appium isn't running and wasn't auto-started (only `prepare` auto-starts it; `doctor` only checks) | Run `01 Prepare Test Environment` instead of `doctor` to have Appium started for you, or start it manually: `appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell` |
| Errors mentioning `/session` or `//session`, or 404s from Appium | The Appium server's `--base-path` doesn't match your config's `appium_url` | Make sure `appium_url` ends in `/wd/hub` if the server was started with `--base-path /wd/hub` (the default this framework expects) — or change one to match the other |
| `doctor` reports `android_sdk_env: error` | Neither `ANDROID_HOME` nor `ANDROID_SDK_ROOT` is set | `export ANDROID_HOME=/path/to/Android/sdk` (e.g. `~/Library/Android/sdk` on Mac) |
| `doctor` reports `apk_exists: error` | `apk_path` in your config points to a file that doesn't exist | Fix `apk_path` in `config/tester.local.yaml` |
| `doctor` reports `device_connected: error` or `warning` | Emulator not started, or tablet not connected/authorized | Start the emulator, or connect the tablet via USB with debugging enabled; check `adb devices` |
| Errors mentioning "insecure" or "adb_shell" / "has not been enabled" | Appium wasn't started with the insecure `adb_shell` feature, but a scenario step needs shell access | Restart Appium with `--allow-insecure uiautomator2:adb_shell` |
| Config fails to load with a `PUT_ACTIVITY_HERE` error | A placeholder value was left in the config (usually copy-pasted from an incomplete template) | Replace it with the real value — e.g. `app_activity: ".ui.HomeActivity"`, not `PUT_ACTIVITY_HERE`. See `docs/CALEE_LAUNCH_MODEL.md`. |
| A `logged_in_tablet` scenario is skipped, or fails with *"Calee launched, but the screen is not the logged-in home screen. This scenario requires a prepared tablet or test account."* | The device is on the clean/onboarding screen, not signed in — this is expected behavior, not a bug | Run `smoke-fresh`/`login_qr_states` on clean devices; only run tablet-suites once a technical owner has prepared a logged-in demo account and set `expected_state: logged_in_tablet` (see `docs/TEST_DATA_RESET_CONTRACT.md`) |
| `driver.activate_app("com.viso.calee")`-style errors, or Calee just never opens | Calee has no normal launcher intent-filter — it can't be "activated" like a typical app | Use one of the framework's real `launch_strategy` values (`direct_activity`, `start_action`, `calee_shell`); only use `normal_launcher` for apps that actually have a launcher activity. See `docs/CALEE_LAUNCH_MODEL.md`. |
| `suite --suite release-technical` is refused | Suites containing physical-tablet-only scenarios require explicit confirmation | Pass `--confirm-technical`, or set `allow_release_technical: true` in your config — only do this on a real physical tablet |
