# calee-regression

A regression testing framework for [Calee](https://github.com/CaleeAdmin/Calee) (Android, package
`com.viso.calee`), the tablet app launched by [CaleeShell](https://github.com/CaleeAdmin/CaleeShell)
(the tablet's real HOME/launcher app). Built around Appium 3 + UiAutomator2, driven by simple YAML
scenario files, with human-readable HTML/JUnit reports.

Calee is **not** a normal launcher-visible app — see
[docs/CALEE_LAUNCH_MODEL.md](docs/CALEE_LAUNCH_MODEL.md) before changing anything about how it's
launched.

## Quickstart

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

**Run this first, always:**

```bash
python -m calee_regression doctor --config config/tester.local.yaml
```

Then:

```bash
python -m calee_regression list-suites
python -m calee_regression suite --config config/tester.local.yaml --suite smoke-fresh
```

`smoke-fresh` is the only suite safe to run against a clean, no-account emulator/tablet. Every other
suite needs a prepared, logged-in demo tablet/emulator — see
[docs/NON_TECH_TESTER_GUIDE.md](docs/NON_TECH_TESTER_GUIDE.md).

## CLI commands

| Command | Purpose |
|---|---|
| `python -m calee_regression doctor --config <config>` | Check Appium/adb/config setup for common mistakes |
| `python -m calee_regression list-suites` | List all suites and the scenario files each resolves to |
| `python -m calee_regression run --config <config> --scenario <path>` | Run a single scenario file |
| `python -m calee_regression suite --config <config> --suite <name>` | Run a named suite |

## Suites

| Suite | Requires | Notes |
|---|---|---|
| `smoke-fresh` | clean emulator/tablet, no account | safe first run |
| `smoke-tablet` | prepared, logged-in tablet/emulator | |
| `calendar` | prepared, logged-in tablet/emulator | smoke, view modes, event fields, recurring events |
| `tasks_smoke` | prepared, logged-in tablet/emulator | |
| `chores_smoke` | prepared, logged-in tablet/emulator | |
| `settings_smoke` | prepared, logged-in tablet/emulator | |
| `weather_system_messages` | prepared, logged-in tablet/emulator | |
| `login_qr_states` | clean emulator/tablet, no account | |
| `full-tester` (alias `full`) | prepared, logged-in tablet/emulator | smoke-tablet + calendar + tasks + chores + settings + weather |
| `release-technical` | **real physical tablet**, `--confirm-technical` | full-tester + kiosk/admin + system receivers |
| `kiosk_admin_physical` | **real physical tablet** | |
| `system_receivers` | **real physical tablet** | |

## Also available via scripts / double-click wrappers

`scripts/*.sh` (technical, terminal) and `tester/*.command` / `tester/technical/*.command` (Mac
double-click, non-technical) wrap the CLI commands above — see
[docs/NON_TECH_TESTER_GUIDE.md](docs/NON_TECH_TESTER_GUIDE.md) and
[docs/SETUP_MAC.md](docs/SETUP_MAC.md).

## Documentation

- [docs/SETUP_MAC.md](docs/SETUP_MAC.md) — first-time Mac setup
- [docs/NON_TECH_TESTER_GUIDE.md](docs/NON_TECH_TESTER_GUIDE.md) — for testers who just double-click `.command` files
- [docs/SCENARIO_REFERENCE.md](docs/SCENARIO_REFERENCE.md) — scenario YAML schema and every supported action
- [docs/CALEE_LAUNCH_MODEL.md](docs/CALEE_LAUNCH_MODEL.md) — why Calee can't be launched like a normal app
- [docs/TEST_DATA_RESET_CONTRACT.md](docs/TEST_DATA_RESET_CONTRACT.md) — what "fresh" vs "logged_in_tablet" state means and who owns resetting it
- [docs/CALENDAR_BIG_CHANGE_COVERAGE.md](docs/CALENDAR_BIG_CHANGE_COVERAGE.md) — how to use the calendar suite around big calendar changes
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — symptom → cause → fix for common setup mistakes

## Framework tests

```bash
python -m pytest
```

CI (`.github/workflows/framework-tests.yml`) runs the same `pytest` suite plus a scenario-file
validation pass, with no Appium/emulator required.
