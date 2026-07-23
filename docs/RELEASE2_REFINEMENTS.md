# Release 2 framework refinements (calee-regression)

This document describes the tablet-framework refinements made after the
physical-device / production-API run `focused-next-20260723-163940-6d25db`, and
how a tester resumes device testing after they merge.

> These are permanent repository commands. There are **no downloaded or
> temporary scripts** — anything that used to tell you to download a shell
> script to "repair Appium" is obsolete; the framework now owns that itself.

## 1. Every tablet command owns Appium availability (Workstream 1)

`run`, `suite`, `run-repeat` (and `prepare`, and the new `focused-verify`) each
**ensure the configured Appium endpoint is up before creating a session**. You
no longer need to run `prepare` first in the same shell.

* If Appium isn't running, the command starts it (reusing the single
  `appium_lifecycle` implementation).
* If a framework-started server had been stopped by some earlier step, the next
  command **restarts** it (recorded as `state: restarted`).
* A command **never stops** Appium — only the explicit `stop-appium` command and
  the `focused-verify` orchestration's own `finally` cleanup do. So one command
  can never accidentally stop a server the next one needs.
* If Appium cannot be started/reached, the command exits **BLOCKED** and writes a
  traceable report; no product scenario is ever started against a dead endpoint.

The lifecycle disposition (`already_running` / `started` / `restarted` /
`unavailable`) is echoed and, on failure, recorded on the BLOCKED step.

## 2. Explicit Appium Settings session bootstrap (Workstream 2)

The old import-time monkey-patch of `CaleeDriver.start_session` is gone. Session
creation now goes through the explicit, testable
`session_bootstrap.bootstrap_session`:

1. Attempt standard session creation once.
2. **Only** on the exact known `Appium Settings app is not running` failure:
   preserve the first exception; inspect whether `io.appium.settings` is
   installed, its version, resolved launchable activity, declared services &
   receivers, process state, device-policy/package restrictions; capture a
   narrowly-bounded, **redacted** logcat window and the UiAutomator2 + Appium
   versions; uninstall **only** the stale helper; let UiAutomator2 reinstall it;
   retry **exactly once**.
3. It never loops, and **never** switches from standard to diagnostic mode.
4. On a second failure it produces a structured **BLOCKED** bootstrap report
   (first failure, recovery actions, command return codes, second failure,
   diagnostic paths), classified as one of:
   `appium_server_unavailable`, `uiautomator2_server_unavailable`,
   `appium_settings_install_failed`, `appium_settings_start_failed`,
   `appium_settings_device_policy_blocked`, `session_creation_failed_other`.

An inability to keep the Settings helper alive is **never** a Calee product
failure — it is always BLOCKED. The helper is never launched as an activity, and
no arbitrary APK is ever installed.

## 3. Non-mutating scrolling text assertions (Workstream 3)

Two new scenario actions resolve **exactly one** element by exact text /
content-description **without tapping**:

```yaml
- name: Wait for the recurring fixture event (scrolling, no tap)
  action: wait_for_unique_text     # waits up to timeout_seconds for the one match
  text: REG-EVENT-RECURRING-001
  timeout_seconds: 25
  scroll: true                     # bounded, bidirectional; no unbounded gestures
  max_swipes: 12
```

`assert_unique_text` is the snapshot form (default timeout 0). Both reuse the
driver's exact-text resolver (re-querying, bounded scrolling with no-progress
detection, stale-element recovery, ambiguity detection). Semantics:

* zero matches → wait to the bounded deadline, then **FAIL** with a screenshot +
  page source and scroll metrics;
* more than one exact match → **FAIL** as ambiguity (never taps an arbitrary one);
* exactly one → **PASS**, recording attempts / elapsed / scroll count &
  directions / whether scrolling was exhausted / final match count.

`scenarios/calendar_recurring_events.yaml` now uses `wait_for_unique_text` for
`REG-EVENT-RECURRING-001` and `REG-EVENT-EXCEPTION-001`, then taps the uniquely
resolved event through a separate explicit `tap_unique_text` step. The
non-scrolling `wait_for_text` remains available for fixed-screen labels.

## 4. Standard vs diagnostic targeted reports stay separate (Workstream 4)

`run-repeat` now writes into a **mode-scoped** subtree so a diagnostic run can
never overwrite a standard one:

```
reports/runs/<run-id>/tablet-targeted/standard/<invocation-id>/…
reports/runs/<run-id>/tablet-targeted/diagnostic/<invocation-id>/…
reports/runs/<run-id>/tablet-targeted/index.json      ← top-level index
```

Every invocation stays immutable. The top-level `index.json` references **both**
modes and every invocation, with per-invocation certification eligibility. The
**canonical certifying result is always the standard mode's latest result** — a
diagnostic run is recorded for investigation but can never become the certifying
result nor improve standard's certification status. The run manifest records both
modes (`tablet-targeted-standard` / `tablet-targeted-diagnostic`, worst-wins).

## 5. Report classification (Workstream 10)

| Situation | Classification |
|---|---|
| Appium unavailable | **BLOCKED** |
| Appium Settings helper cannot start | **BLOCKED** (never a product FAIL) |
| Diagnostic tablet run (`--device-initialization skip`) | non-certifying |
| Recurring event absent after valid scrolling/exhaustion | **FAIL** (unless a step is `blocks_on_absence`) |
| A real product assertion failing | **FAIL** (FAIL beats BLOCKED) |

## 6. Permanent focused post-fix verification command (Workstream 9)

One permanent command replaces the downloaded scripts:

```bash
python -m calee_regression focused-verify \
  --config config/tester.local.yaml \
  --tablet-scenario scenarios/calendar_recurring_events.yaml \
  --tablet-repeat 2 \
  --api-suite chores-stop-repeating \
  --ios-target integration_test/app_boot_test.dart
```

Under a **single fresh run id** it runs: environment + fixture prep; the standard
recurring-calendar scenario twice; the diagnostic recurring-calendar scenario
twice; the focused stop-repeating API scenario twice; a focused iPhone
environment/app-boot check; and one aggregate summary. The framework **owns the
Appium lifecycle** — ensured once, stopped once at the very end, **never between**
the standard and diagnostic attempts. Standard and diagnostic tablet reports are
separated; API and iPhone invocations are immutable. It makes **no full-release
claim** (the summary says so), uses `FAIL > BLOCKED > PASS` exit-code precedence,
cleans up in `finally`, never prompts on a TTY, and never puts a credential on any
child's argv (each child resolves credentials from the environment / macOS
Keychain).

## 7. How Yiwen resumes device testing after merge

Once both framework PRs are merged, from the checkout parent directory:

```bash
# 1. Update both framework repos to the merged main.
cd /Users/yiwen/CaleeRelease2Check/calee-regression && git checkout main && git pull --ff-only
cd /Users/yiwen/CaleeRelease2Check/CaleeMobile-Regression && git checkout main && git pull --ff-only

# 2. Provide credentials via the environment / macOS Keychain (never on argv).
export CALEE_API_BASE="https://hub.calee.com.au"
export CALEE_TEST_EMAIL="…"        # or store in the login Keychain
export CALEE_TEST_PASSWORD="…"     # or store in the login Keychain

# 3. Connect the physical tablet and the iPhone, then run the ONE permanent
#    focused verification command (it owns Appium; no downloaded scripts):
cd /Users/yiwen/CaleeRelease2Check/calee-regression
python -m calee_regression focused-verify \
  --config config/tester.local.yaml \
  --tablet-scenario scenarios/calendar_recurring_events.yaml \
  --tablet-repeat 2 \
  --api-suite chores-stop-repeating \
  --ios-target integration_test/app_boot_test.dart
```

The command prints a per-step summary and an overall `FAIL / BLOCKED / PASS`, and
writes `reports/runs/<run-id>/focused-verify/summary.json`. This is a focused
post-fix check, **not** a full release certification — run
`00 Run Calee Release Regression` for that.
