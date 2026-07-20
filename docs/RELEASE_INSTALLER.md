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

Two orthogonal facts are recorded per app and must never be conflated:

* **`installArtifact`** — does this release ship an APK to install for this app?
* **`expectedInstalled`** — the identity the tablet must carry *after* the
  release, **whether or not** this release installed the app. An unchanged app
  is not "ignored": it still declares an expected identity that the post-reboot
  complete-solution check verifies.

Complete-solution shape (recommended):

```json
{
  "releaseId": "2026.07.20-rc2",
  "calee": {
    "installArtifact": true,
    "apk": "calee.apk",
    "sha256": "<64-char hex>",
    "expectedInstalled": {
      "packageId": "com.viso.calee",
      "versionName": "founder-v0.3.26",
      "versionCode": 326,
      "gitSha": "<full 40-char SHA>",
      "signerSha256": "<64-char hex>"
    }
  },
  "caleeShell": {
    "installArtifact": false,
    "expectedInstalled": { "...": "same shape; NO apk/sha256 for an unchanged app" }
  }
}
```

Legacy flat shape stays supported: `{ "included": true, "packageId": ...,
"versionName": ..., "versionCode": ..., "gitSha": ..., "apk": ..., "sha256": ...
}`, with `{ "included": false }` (or omitted) meaning that app is absent from
the release. `installArtifact` supersedes `included` when both appear.

At least one app must ship an artifact (`installArtifact`/`included: true`). The
**expected installed identity** of every declared app is validated regardless of
`installArtifact`: canonical `packageId` (`com.viso.calee` /
`com.viso.caleeshell`), a well-formed `versionName`, a positive integer
`versionCode`, a **full** 40-character `gitSha` (an abbreviated SHA is ambiguous
and rejected), and — if present — a 64-hex `signerSha256`. Only an app that is
actually installed (`installArtifact: true`) additionally requires an `apk`
(plain in-bundle filename; path separators / `..` / absolute paths rejected) and
a 64-hex `sha256`; an unchanged app must **not** carry an install artifact.

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

## Complete-solution verification (after every release)

After a successful install + reboot, `install-tablet-release` runs
`verify_tablet_solution`, which checks the **whole** installed solution — both
Calee **and** CaleeShell — regardless of which app(s) this release replaced. For
each app it verifies the package is present, the installed `versionName`/
`versionCode` match the **expected** identity, and (when a `signerSha256` is
declared) the installed signer matches the expected trusted signer; plus Calee's
custom `START` action resolves to Calee and CaleeShell is the `HOME` launcher. A
missing/mismatched/unreadable expectation on **either** app — including an
*unchanged* app — `BLOCKS`. An unreadable installed signer is treated as
`BLOCKED` (unknown, never assumed trusted), consistent with the pre-install
signer gate.

## No device this session

No `adb`/device was available, so no install was executed. `--plan-only`
constructs and records the full ordered plan for review; a real run of
`install-tablet-release`/`inspect-tablet` with nothing attached exits
`BLOCKED` with an honest "no device" result. The command construction,
ordering, downgrade/signature/version/HOME classification, and post-install
`dumpsys` parsing are all proven offline with an injected fake adb runner.
