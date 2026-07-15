# Troubleshooting

Start with `python -m calee_regression doctor --config <your config>` for any of the below — it
checks most of these automatically and prints a hint.

| Symptom | Cause | Fix |
|---|---|---|
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
