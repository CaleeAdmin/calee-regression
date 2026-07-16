# Mac setup

One-time setup for running the Calee regression framework on a Mac.

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

## 2. Get the repo

If you were sent a zip or a link, unzip/clone it to a convenient location, e.g. `~/calee-regression`.

## 3. Create the virtualenv and install

```bash
cd ~/calee-regression
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

Double-click `tester/Check Setup.command`, or from a terminal:

```bash
bash scripts/doctor.sh
```

Fix anything reported as `[ERROR]` before running any suite.
