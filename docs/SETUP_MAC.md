# Mac setup

One-time setup for running the Calee regression framework on a Mac. Once this is done, the tester
never touches Terminal again — see `docs/NON_TECH_TESTER_GUIDE.md`.

## 1. Install prerequisites

- **Xcode Command Line Tools**: `xcode-select --install`
- **Homebrew** (if not already installed): https://brew.sh
- **Android platform-tools (adb)**:
  ```bash
  brew install --cask android-platform-tools
  ```
  Or install the full Android SDK via Android Studio, and set `ANDROID_HOME` (or
  `ANDROID_SDK_ROOT`) to point at it, e.g. `~/Library/Android/sdk`.
- **Node.js** (Appium requires it): `brew install node`
- **Appium 3**:
  ```bash
  npm install -g appium
  appium driver install uiautomator2
  ```
- **Python 3.11**: `brew install python@3.11`
- **Flutter** (only needed for `03/04 Test CaleeMobile Android/iPhone`'s UI checks — the API checks
  work without it): https://docs.flutter.dev/get-started/install/macos, then `flutter doctor` to
  confirm it's healthy for the platform(s) you'll test (Xcode for iOS, Android SDK for Android —
  already covered above).

## 2. Get the repos

This framework works alongside its sibling, `CaleeMobile-Regression` — both `01 Prepare Test
Environment` (fixture reset) and `03`/`04` (CaleeMobile checks) expect it to be checked out right
next to this repo:

```
some-parent-dir/
  calee-regression/
  CaleeMobile-Regression/
    ui/            (also needs CaleeMobile checked out as ITS sibling, for the UI suite)
```

If you were sent a zip or a link for each, unzip/clone them so they sit side by side, e.g. under
`~/calee/`.

## 3. Create the virtualenv and install

You normally don't need to do this by hand — `01 Prepare Test Environment.command` (and every other
launcher) creates the virtualenv and installs dependencies automatically on first run. Only do this
manually if you're working from a terminal for development:

```bash
cd ~/calee/calee-regression
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## 4. Configure

```bash
cp config/tester.local.example.yaml config/tester.local.yaml
```

Edit `config/tester.local.yaml`:

- `apk_path` — full path to the Calee `.apk` you were given
- `udid` — your emulator id (e.g. `emulator-5554`) or physical tablet's adb serial (`adb devices`)
- `expected_state` — `fresh` for a clean emulator/tablet with no account, `logged_in_tablet` once a
  technical owner has signed in a demo account (see `docs/TEST_DATA_RESET_CONTRACT.md`)

`config/tester.local.yaml` is your personal, per-machine config — it is gitignored and never
committed. `config/tester.local.example.yaml` is the template everyone starts from.

### Fixture credentials (for automatic REG-* fixture reset)

`01 Prepare Test Environment.command` resets the deterministic regression fixture (see
`docs/TEST_DATA_RESET_CONTRACT.md`) automatically if it can find a target environment and test
account. Store these as environment variables in your shell profile (`~/.zshrc` or similar) — never
commit them:

```bash
export CALEE_API_BASE="https://hub-dev.calee.com.au"
export CALEE_TEST_EMAIL="demo@example.com"
export CALEE_TEST_PASSWORD="..."
```

If these aren't set, `01 Prepare Test Environment.command` now **BLOCKS** rather than silently
reporting READY — a release-gating run (the default) must not proceed without a real, verified
fixture. If you're deliberately preparing for a suite that genuinely doesn't need the fixture (e.g.
`smoke-fresh`), pass `--allow-no-fixture --suite smoke-fresh` from a terminal; the numbered
launchers never do this automatically.

### Release platform profile (which platforms this release includes)

If you're testing against a schema-v2 release bundle (`release-manifest.json` with
`schemaVersion: 2`, resolved via `config/machine.local.yaml`'s `release_bundle_dir`), the bundle
itself is authoritative for which platforms/features this release includes — this file is not
consulted at all, and you can skip this step. Otherwise (a schema-v1 bundle, or no bundle):

```bash
cp config/release-platforms.example.yaml config/release-platforms.yaml
```

Edit it to say which platforms this release build actually includes (`tablet`, `mobile_android`,
`mobile_ios`) — this decides whether the CaleeMobile Android/iOS UI results are mandatory for an
overall PASS. If you skip this step, every platform defaults to mandatory, which is the safe
default but may block on a platform you never intended to test this round.

### Manual checks (optional but recommended)

```bash
cp config/manual-checks.example.json config/manual-checks.json
```

Edit the checklist to match your release's real manual checks. If you skip this, `05 Record Manual
Checks.command` falls back to the example checklist and says so.

## 5. Appium

`01 Prepare Test Environment.command` starts Appium automatically (with the required flags) if it
isn't already reachable at your config's `appium_url` — you do not need to open a separate terminal
for this anymore. Logs go to `reports/appium.log`; if it fails to start you'll see a plain-language
BLOCKED message there and in the launcher's own output. `06 Test Full Calee Solution.command` stops
Appium again at the end of the run, but only if it was the one that started it — an Appium you
started yourself (see below) is never touched.

To start it manually anyway (e.g. for interactive debugging outside this framework):

```bash
appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell
```

Both flags matter — see `docs/TROUBLESHOOTING.md` if you forget one.

### Optional: build/version metadata for the consolidated report

`06 Test Full Calee Solution.command` picks these up automatically if set (never fabricated if
absent):

```bash
export CALEE_BUILD_VERSION="founder-v0.3.24"     # Calee tablet versionName (example — use your build's)
export CALEEMOBILE_BUILD_VERSION="0.0.23+23"     # CaleeMobile pubspec version+build (example)
export CALEE_EXPECTED_BUILD_VERSION="founder-v0.3.24"  # optional: block if the detected build differs
export CALEEMOBILE_EXPECTED_BUILD_VERSION="0.0.23+23"
export CALEESHELL_VERSION="founder-v0.2.11"      # optional (example)
export CALEE_GIT_SHA="$(git -C /path/to/Calee rev-parse HEAD)"           # optional
export CALEEMOBILE_GIT_SHA="$(git -C /path/to/CaleeMobile rev-parse HEAD)" # optional
```

## 6. Start your emulator (or connect the tablet)

Start an Android emulator from Android Studio, or connect a physical tablet via USB with USB
debugging enabled, then confirm it's visible:

```bash
adb devices
```

## 7. Check your setup

Double-click `tester/01 Prepare Test Environment.command`, or from a terminal:

```bash
python -m calee_regression prepare --config config/tester.local.yaml
```

Fix anything reported as `[ERROR]` before running any suite.
