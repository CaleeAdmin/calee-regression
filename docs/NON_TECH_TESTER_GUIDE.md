# Guide for testers (double-click, no terminal needed)

This guide is for running regression checks by double-clicking files in the `tester/` folder — no
command line, no typed commands, no environment variables. All of that is handled for you. A
technical owner must have completed the one-time setup in `docs/SETUP_MAC.md` first.

## 1. What equipment you need

- The Mac that has already been set up by a technical owner (see `docs/SETUP_MAC.md`).
- The Calee tablet (or emulator) you were told to test on, with its USB cable.
- If you were also asked to test CaleeMobile: an Android phone/emulator and/or an iPhone, as
  applicable, each with the CaleeMobile app installed and signed in.
- Whatever device state you were told to start from (a **clean** device with no account, or a
  **prepared** device with a demo account already signed in) — see the table below.

## 2. Connect the devices

- Plug the tablet into the Mac with its USB cable. Unlock it. If a prompt appears asking to
  **"Allow USB debugging"**, tap **Allow**.
- Leave the tablet unlocked and awake for the duration of the test run.
- For CaleeMobile Android/iPhone checks, make sure the phone is unlocked and connected (USB for
  Android, or already paired for an iOS simulator/device) before you start.

## 3. Which launcher to double-click

Always start with **`01 Prepare Test Environment.command`** — this also starts Appium
automatically in the background if it isn't already running, so you never need to open a separate
Terminal window yourself. Then pick the one launcher that matches what you were asked to test:

| You were asked to test | Double-click |
|---|---|
| Everything is ready to go | `01 Prepare Test Environment.command`, then `06 Test Full Calee Solution.command` (this already includes manual checks and Appium) |
| Just the Calee tablet | `01 Prepare Test Environment.command`, then `02 Test Calee Tablet.command` |
| Just CaleeMobile on Android | `03 Test CaleeMobile Android.command` |
| Just CaleeMobile on iPhone | `04 Test CaleeMobile iPhone.command` |
| Just the manual guided checks | `05 Record Manual Checks.command` |
| To see the last report again | `07 Open Latest Report.command` |

You should not normally need anything outside these seven files. Files under `tester/advanced/` and
`tester/technical/` are for a technical owner — **never** double-click anything in
`tester/technical/` unless specifically asked to; it requires a real physical tablet and admin
access.

`06 Test Full Calee Solution.command` runs everything end to end: prepares the environment
(including Appium), the tablet suite, CaleeMobile (whichever platforms your technical owner has
configured for this release), the guided manual checks, and combines all of it into one
consolidated report and release bundle.

## 4. What PASS means

**PASS** (green) means the feature was actually exercised and worked correctly. Nothing further to
do — the report is ready to file or send on as-is.

## 5. What FAIL means

**FAIL** (red) means a feature was exercised and produced the **wrong** result — this is a real
product problem. The report's yellow hint box explains what was expected vs. what happened. Send
the report (see below) — don't try to diagnose it yourself.

## 6. What BLOCKED means

**BLOCKED** (purple) means the test **could not run at all** — a disconnected device, an app that
wasn't in the state the test expected, a network problem, or similar. This is explicitly **not** a
claim that Calee or CaleeMobile is broken. Common reasons you'll see spelled out in the report:

- The device wasn't connected properly, or wasn't unlocked.
- The device was in the wrong state for the test you ran (e.g. you ran a "prepared tablet" test on
  a clean device, or vice versa — see the table above).
- The regression fixture (the known test data the calendar checks look for) hasn't been reset.
  `01 Prepare Test Environment.command` resets and verifies it automatically — if it reports
  BLOCKED because the fixture credentials aren't configured, that is not something you can fix
  yourself; ask your technical owner (see `docs/SETUP_MAC.md`).
- Appium (a background tool the tablet checks depend on) couldn't be started automatically. This is
  now handled for you by `01 Prepare Test Environment.command` — if it still fails, ask your
  technical owner rather than trying to start it yourself.
- A required check was skipped (e.g. an on-screen control the test needed wasn't where it expected)
  — this reports BLOCKED rather than a false PASS, since nothing was actually verified.

If you see BLOCKED, the right move is almost always: check the device is connected and in the
right state, then retry (see §12). If it's still BLOCKED after that, send the report to your
technical owner — don't report it as a bug yourself.

You may also see gray **SKIP** entries — these are deliberately optional checks (e.g. a feature
that doesn't apply to this account) and are not evidence of anything being broken.

## 6b. Recording manual checks

Some things can't be checked automatically (e.g. "does the kiosk lock actually prevent escaping to
the home screen?"). `05 Record Manual Checks.command` walks you through each one with a simple
numbered menu — you only ever type a single digit and press Enter:

```
Manual check 1 of 4: Kiosk escape check

Instruction:
Swipe down from the top and press Home/Recents.

Expected:
The notification shade, quick settings and recents must not open.

Choose:
1. Pass
2. Fail
3. Blocked
4. Add note
5. Add screenshot path
6. Go back
```

You never need to open or edit a JSON/YAML file yourself — this launcher writes the results for
you. `06 Test Full Calee Solution.command` runs this step automatically as part of the full run; a
manual check that's mandatory for this release and left unanswered will correctly show as BLOCKED,
not PASS.

## 7. How to add a note

If something looks wrong on screen but the automated check didn't catch it (e.g. a slightly odd
color, a button that looked briefly frozen), don't just close the window. Open a text file (Notes,
TextEdit, anything) and write down:

- Which launcher you ran and roughly what time.
- What you saw and what screen you were on.
- What you expected instead.

Include this note when you send the report — see §9.

## 8. How to capture evidence

The framework already takes screenshots automatically at key steps — you don't need to do this
yourself for anything the report already covers. If you want to capture something extra (something
that looked wrong but wasn't part of an automated screenshot moment):

- On the tablet: press the physical power + volume-down buttons together (standard Android
  screenshot), or use whatever screenshot method the device supports.
- Save it somewhere you'll remember (e.g. your Desktop) and mention it in your note (§7) so it can
  be attached alongside the report.

## 9. Where the report is saved

Each run creates a folder under `reports/`, and `06 Test Full Calee Solution.command` additionally
produces one combined report under `reports/consolidated-<date-time>/`, including a
`Calee-Regression-<date>-<build>-<PASS|FAIL|BLOCKED>.zip` bundle. `07 Open Latest Report.command`
always opens the most recent one.

## 10. What to send to the technical owner

1. In Finder, go to the `reports/` folder.
2. Find the run (or the `Calee-Regression-...zip` bundle, if you ran the full solution) — it's
   already a `.zip` if it's a bundle; otherwise right-click the folder → **Compress**.
3. Attach/upload it and send it, along with:
   - Your note from §7, if you made one.
   - What device/emulator you ran it on.
   - What you expected to happen.

## 11. What never to do during testing

- Don't sign out, clear app data, or change any device settings yourself — ask a technical owner if
  a device seems to be in the wrong state.
- Don't run anything in `tester/technical/` unless specifically asked.
- Don't approve/replace baseline screenshots in `baselines/` — that's a technical-owner-only action
  (see §13 below); doing it by mistake can hide a real regression from every future run.
- Don't edit any `config/*.yaml` file — ask a technical owner if configuration seems wrong.
- Don't edit any manual-check JSON file by hand — use `05 Record Manual Checks.command` instead.
- Don't try to start Appium yourself in a Terminal — `01 Prepare Test Environment.command` does
  this automatically.
- Don't report a BLOCKED result as if it were a product bug — see §6.

## 12. How to retry a blocked test safely

1. Check the device is still connected, unlocked, and awake.
2. Confirm it's in the state the test expects (clean vs. prepared — see the table in §3).
3. Re-run `01 Prepare Test Environment.command`, then the same test again.
4. If it's still BLOCKED after that, stop and send the report to your technical owner rather than
   retrying repeatedly — repeated retries won't fix an environment problem, and the report already
   captures what's needed for them to diagnose it.

## 13. Baselines (screenshot comparison)

Most scenarios take screenshots for humans to look at (`compare: false`) rather than doing strict
pixel comparison — you do not need to do anything special with these. Strict baseline image
comparison is opt-in and **only a technical owner should approve new baseline images** (copying a
screenshot into `baselines/`) — see §11.
