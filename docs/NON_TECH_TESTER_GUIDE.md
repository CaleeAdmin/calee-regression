# Guide for testers (double-click, no terminal needed)

This guide is for running regression checks by double-clicking files in the `tester/` folder — no
command line required, once a technical owner has done the one-time setup in `docs/SETUP_MAC.md`.

## Always run this first

**`tester/Check Setup.command`** — confirms Appium is running, the device is connected, and the
config is valid. If it reports `FAILED`, stop and ask a technical owner before continuing.

## Which test to run depends on the device state

| Device state | What to run |
|---|---|
| Clean emulator or freshly-wiped tablet, **no account signed in** | `tester/Run Smoke Fresh.command` |
| Tablet/emulator **prepared by a technical owner with a demo account already signed in** | `tester/Run Smoke Tablet.command`, `tester/Run Calendar Regression.command`, `tester/Run Full Tester Regression.command` |

**Never** run the tablet-only tests against a clean device — they will fail because the app is
still on the sign-in screen, not because anything is actually broken. The report will say so
clearly (see below).

**Never** double-click anything inside `tester/technical/` unless a technical owner specifically
asks you to — those tests require a real physical tablet and specific admin access.

## Reading the report

Each run opens (or you can open via `tester/Open Latest Report.command`) a `summary.html` file with:

- **Green** = passed, **red** = failed, **gray** = skipped, **amber** = warning
- A screenshot for each step that took one
- A yellow **hint** box under anything that failed, explaining what likely went wrong and what to
  check

If a tablet-only scenario fails with the message *"Calee launched, but the screen is not the
logged-in home screen. This scenario requires a prepared tablet or test account."* — that means the
device isn't in the state the test expected (still on sign-in), not that Calee is broken. Check with
whoever prepared the device.

## Sending a report back to Yiwen

Each run creates a folder under `reports/`, named like `smoke-tablet-20260715-143012/`. To send it:

1. In Finder, navigate to the `calee-regression/reports/` folder.
2. Right-click the folder for the run in question → **Compress**.
3. Attach/upload the resulting `.zip` to email, Slack, or Drive, and send it to Yiwen.

Include a note about what device/emulator you ran it on and what you expected to happen.

## Baselines (screenshot comparison)

Most scenarios take screenshots for humans to look at (`compare: false`) rather than doing strict
pixel comparison — you do not need to do anything special with these. Strict baseline image
comparison is opt-in and **only a technical owner should approve new baseline images** (copying a
screenshot into `baselines/`) — testers should never do this themselves, since an approved-by-mistake
baseline would hide real regressions from then on.
