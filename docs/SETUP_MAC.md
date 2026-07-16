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

If these aren't set, Prepare still runs the local environment checks — it just skips the fixture
reset step and says so, rather than failing.

## 5. Start Appium

In its own terminal window/tab, leave this running while you test:

```bash
appium --base-path /wd/hub --allow-insecure uiautomator2:adb_shell
```

Both flags matter — see `docs/TROUBLESHOOTING.md` if you forget one.

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
