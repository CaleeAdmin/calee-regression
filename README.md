# calee-regression

A regression testing framework for [Calee](https://github.com/CaleeAdmin/Calee) (Android, package
`com.viso.calee`), the tablet app launched by [CaleeShell](https://github.com/CaleeAdmin/CaleeShell)
(the tablet's real HOME/launcher app). Built around Appium 3 + UiAutomator2, driven by simple YAML
scenario files, with human-readable HTML/JUnit reports, and a PASS/FAIL/BLOCKED result model — a
disconnected device or unreachable Appium server is never reported as a product failure.

Calee is **not** a normal launcher-visible app — see
[docs/CALEE_LAUNCH_MODEL.md](docs/CALEE_LAUNCH_MODEL.md) before changing anything about how it's
launched.

This repo works alongside its sibling,
[CaleeMobile-Regression](https://github.com/CaleeAdmin/CaleeMobile-Regression) (CaleeMobile +
backend Client API checks) — see [docs/SETUP_MAC.md](docs/SETUP_MAC.md) for the expected
side-by-side checkout layout.

## Non-technical testers

If you're running checks by double-clicking files, start with
[docs/NON_TECH_TESTER_GUIDE.md](docs/NON_TECH_TESTER_GUIDE.md) — you shouldn't need anything below
this point. The six launchers you need are in `tester/`:

`01 Prepare Test Environment` → `02 Test Calee Tablet` / `03 Test CaleeMobile Android` /
`04 Test CaleeMobile iPhone` / `05 Test Full Calee Solution` → `06 Open Latest Report`.

## Technical quickstart

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m pytest
```

Start Appium 3 (required flags — see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)):

```bash
appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell
```

Copy the example config and fill in real values for your machine:

```bash
cp config/tester.local.example.yaml config/tester.local.yaml
# edit config/tester.local.yaml: apk_path, udid, etc.
```

**Run this first, always** — checks the local environment *and* resets the deterministic
regression fixture (see [docs/TEST_DATA_RESET_CONTRACT.md](docs/TEST_DATA_RESET_CONTRACT.md)) if
`CALEE_API_BASE`/`CALEE_TEST_EMAIL`/`CALEE_TEST_PASSWORD` are set:

```bash
python -m calee_regression prepare --config config/tester.local.yaml
```

Then:

```bash
python -m calee_regression list-suites
python -m calee_regression suite --config config/tester.local.yaml --suite smoke-fresh
```

`smoke-fresh` is the only suite safe to run against a clean, no-account emulator/tablet. Every other
suite needs a prepared, logged-in demo tablet/emulator.

## Result model and exit codes

Every scenario resolves to exactly one of `passed` / `failed` / `blocked` / `skipped` — see
[docs/RELEASE_POLICY.md](docs/RELEASE_POLICY.md). CLI exit codes are consistent across every
command: `0` success, `1` product regression, `2` invalid usage/configuration, `3` blocked
(environment/device/fixture/tooling problem, never a product failure).

## CLI commands

| Command | Purpose |
|---|---|
| `python -m calee_regression prepare --config <config>` | Check the environment *and* reset the REG-* regression fixture |
| `python -m calee_regression doctor --config <config>` | Check Appium/adb/config setup only (no fixture reset) |
| `python -m calee_regression list-suites` | List all suites and the scenario files each resolves to |
| `python -m calee_regression run --config <config> --scenario <path>` | Run a single scenario file |
| `python -m calee_regression suite --config <config> --suite <name>` | Run a named suite |
| `python -m calee_regression consolidate --tablet-report <json> --mobile-api-report <json> ...` | Combine per-framework JSON reports into one release report (HTML/JSON/JUnit + zip bundle) |

## Suites

See [docs/SUITE_REFERENCE.md](docs/SUITE_REFERENCE.md) for the full cross-repo profile table
(`framework-self-test`, `mobile-api`, `sync-smoke`, etc.). Suites defined in this repo:

| Suite | Requires | Notes |
|---|---|---|
| `smoke-fresh` | clean emulator/tablet, no account | safe first run |
| `smoke-tablet` (alias `tablet-smoke`) | prepared, logged-in tablet/emulator | |
| `calendar` | prepared, logged-in tablet/emulator, **fixture reset** | smoke, view modes, event fields, recurring events — the last two hard-require the REG-* fixture |
| `tasks_smoke` | prepared, logged-in tablet/emulator | |
| `chores_smoke` | prepared, logged-in tablet/emulator | Chores tab is conditional — see the scenario for how "no chore service" is handled |
| `settings_smoke` | prepared, logged-in tablet/emulator | |
| `weather_system_messages` | prepared, logged-in tablet/emulator | |
| `login_qr_states` | clean emulator/tablet, no account | |
| `full-tester` (aliases `full`, `tablet-full`, `full-release`) | prepared, logged-in tablet/emulator, fixture reset | smoke-tablet + calendar + tasks + chores + settings + weather |
| `release-technical` | **real physical tablet**, `--confirm-technical` | full-tester + kiosk/admin + system receivers |
| `kiosk_admin_physical` | **real physical tablet** | |
| `system_receivers` | **real physical tablet** | |

## Also available via scripts / double-click wrappers

`scripts/*.sh` (technical, terminal) and `tester/*.command` (Mac double-click, non-technical) wrap
the CLI commands above. `tester/advanced/*.command` covers individual suites the numbered launchers
don't call directly; `tester/technical/*.command` requires a real physical tablet. See
[docs/NON_TECH_TESTER_GUIDE.md](docs/NON_TECH_TESTER_GUIDE.md) and
[docs/SETUP_MAC.md](docs/SETUP_MAC.md).

## Documentation

- [docs/SETUP_MAC.md](docs/SETUP_MAC.md) — first-time Mac setup (technical owner)
- [docs/NON_TECH_TESTER_GUIDE.md](docs/NON_TECH_TESTER_GUIDE.md) — for testers who just double-click `.command` files
- [docs/RELEASE_POLICY.md](docs/RELEASE_POLICY.md) — the PASS/FAIL/BLOCKED release-approval rule
- [docs/SUITE_REFERENCE.md](docs/SUITE_REFERENCE.md) — all ten canonical suite profiles across both repos
- [docs/SCENARIO_REFERENCE.md](docs/SCENARIO_REFERENCE.md) — scenario YAML schema and every supported action
- [docs/CALEE_LAUNCH_MODEL.md](docs/CALEE_LAUNCH_MODEL.md) — why Calee can't be launched like a normal app
- [docs/TEST_DATA_RESET_CONTRACT.md](docs/TEST_DATA_RESET_CONTRACT.md) — device state contract + the deterministic REG-* fixture
- [docs/CALENDAR_BIG_CHANGE_COVERAGE.md](docs/CALENDAR_BIG_CHANGE_COVERAGE.md) — how to use the calendar suite around big calendar changes
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — symptom → cause → fix, including exit codes and BLOCKED scenarios
- [docs/sample-report/](docs/sample-report/) — a synthetic example consolidated report (HTML/JSON/JUnit + release bundle)

## Framework tests

```bash
python -m pytest
```

CI (`.github/workflows/framework-tests.yml`) runs the same `pytest` suite, scenario-file and
config-template validation, and shellchecks every tester launcher/script — all with no
Appium/emulator required.
