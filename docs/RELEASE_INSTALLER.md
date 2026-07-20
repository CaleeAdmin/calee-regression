# Tablet release installer

`calee_regression/release_installer.py` (+ the `verify-release-bundle`,
`inspect-tablet`, `install-tablet-release` CLI commands) turns a signed
release bundle into an **audited, ordered, data-preserving ADB install plan**
and classifies the outcome. Everything except actually running `adb` is a pure
function testable with no device — see `framework_tests/test_release_installer.py`
and `test_cli_installer.py`.

## Bundle layout

```
Calee-Tablet-Release/
├── calee.apk
├── caleeshell.apk          # optional — omit when CaleeShell did not change
├── release-manifest.json
└── checksums.sha256
```

A bundle must be **flat** (no subdirectories) and contain only `.apk`,
`.json`, `.sha256` files — a stray script/archive is rejected (a bundle is
data to install, never code to run).

## Manifest contract (`release-manifest.json`)

```json
{
  "releaseId": "2026.07.20-rc1",
  "calee": {
    "included": true,
    "packageId": "com.viso.calee",
    "versionName": "founder-v0.3.25",
    "versionCode": 325,
    "gitSha": "<full 40-char SHA>",
    "apk": "calee.apk",
    "sha256": "<64-char hex>"
  },
  "caleeShell": { "...": "same shape; or { \"included\": false }, or omitted" }
}
```

At least one app must be `included: true`. Each included app is validated:
canonical `packageId` (`com.viso.calee` / `com.viso.caleeshell`), a
well-formed `versionName`, a positive integer `versionCode`, a **full**
40-character `gitSha` (an abbreviated SHA is ambiguous and rejected), an `apk`
that is a plain in-bundle filename (path separators / `..` / absolute paths
rejected), and a 64-hex `sha256`.

## What `verify-release-bundle` checks

Manifest schema · full Git SHAs · package ids · version formats · positive
version codes · APK files exist · SHA-256 matches the manifest **and**
`checksums.sha256` · no unexpected files · no duplicate APK names · no path
traversal · Calee and CaleeShell identities recorded **separately**. Any
problem makes the whole bundle invalid (every problem is listed), and the
installer **refuses to build a plan from a bundle that did not pass**.

## Install policy (the generated plan)

1. Install Calee first, CaleeShell second — both **data-preserving**
   (`adb install -r`, never `-d` unless a downgrade is explicitly authorised,
   never an `uninstall`/`clear`).
2. Reassert CaleeShell as HOME.
3. Reboot; wait for the device.
4. Verify installed versions + package identities.
5. Verify HOME resolves to CaleeShell.
6. Verify the Calee launch action resolves to Calee.

A **downgrade** (target `versionCode` < installed) is `BLOCKED` unless
`--allow-downgrade` is passed. A **signature mismatch**, a **version
mismatch** after install, a **HOME mismatch**, an **unavailable adb**, or an
**unavailable device** are each `BLOCKED` — and the installer **never**
responds to any of them with a destructive uninstall/clear. Execution halts on
the first blocking outcome; nothing downstream is faked.

## No device this session

No `adb`/device was available, so no install was executed. `--plan-only`
constructs and records the full ordered plan for review; a real run of
`install-tablet-release`/`inspect-tablet` with nothing attached exits
`BLOCKED` with an honest "no device" result. The command construction,
ordering, downgrade/signature/version/HOME classification, and post-install
`dumpsys` parsing are all proven offline with an injected fake adb runner.
