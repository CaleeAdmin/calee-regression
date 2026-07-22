# Configuration, credentials, one-button run, and physical qualification

## One machine config (`config/machine.local.yaml`)

A technical owner fills in **one** file per MacBook (copy
`config/machine.local.example.yaml`). It centralises: tablet ADB serial,
expected tablet state, app package ids, HOME activity, Calee launch action,
release-bundle folder, backend URL, active release profile, enabled mobile
platforms, optional iPhone device override, report location, and the
CaleeShell technical-test permission. Loaded/validated by
`calee_regression/machine_config.py`; surfaced to launchers as `MACHINE_*`
shell vars via `python -m calee_regression machine-config`.

**No secrets go in this file.** The loader rejects it if it contains any
`password`/`token`/`secret`/`key`-shaped key.

## Credentials (`calee_regression/credentials.py`)

Secrets (regression username/password, optional API token, optional
AI-analysis key) are resolved at run time from, in order: injected values
(CI/tests) → environment variables → the macOS Keychain
(`security find-generic-password -w`). Rules:

* a **required** secret that can't be resolved raises `CredentialError` →
  **BLOCKED** (never a product FAIL, never a silent empty string);
* an **optional** secret simply resolves to `None`;
* secrets are **never** placed on a command line (`build_env` puts them in the
  child environment); `redact` scrubs every resolved value out of logs/reports
  before they are written; the resolver's `repr` never contains a secret.

## One-button run (`tester/00 Run Calee Release Regression.command`)

The nontechnical tester double-clicks one launcher. It reads the machine
config, verifies the release bundle, installs it (data-preserving), delegates
the full regression to `06 Test Full Calee Solution`, and opens the report.
Every state is plain language — **Ready / Installing / Testing / Passed /
Failed / Blocked / Needs technical owner** — and every blocker says what could
not run, whether it is a product failure, one safe action, and which report to
send. The tester never edits YAML/JSON/env.

## Coverage manifest & promotion state machine

* `coverage/coverage-manifest.yaml` — the single machine-readable statement of
  what each component has automated / tested offline / verified on a device /
  gates a release. Render it with `coverage-report`; validate it with
  `coverage-report --check` (CI).
* `scenarios/promotion/*.yaml` — the per-scenario promotion record.
  `coverage-report --check` also validates these against the scenario YAML +
  `suites.py`, so a draft scenario can never be slipped into a release suite
  while its record still calls it a draft (and vice versa). **Nothing is
  physically verified in this repo state.**

## Physical qualification checklist (next MacBook session)

Run once, with a prepared physical Calee tablet + a connected iPhone:

1. `pip install -e '.[dev]'`; `python -m pytest` (offline sanity).
2. `python -m calee_regression coverage-report --check`.
3. Fill in `config/machine.local.yaml`; store the regression password in the
   Keychain (`security add-generic-password -s calee-regression -a
   regression-password -w`) or export `CALEE_TEST_PASSWORD`.
4. Drop the signed release bundle in the release folder;
   `verify-release-bundle --bundle <dir>` must pass.
5. `inspect-tablet` — confirm the device, adb, and current versions.
6. Double-click **00 Run Calee Release Regression** and follow the guided
   states.
7. For each draft tablet scenario that PASSES on the tablet, record the
   evidence (`runId`, `tabletModel`, `androidVersion`, `caleeVersion`,
   `caleeGitSha`, `screenshotPaths`, `resultsJson`) into its
   `scenarios/promotion/<name>.yaml`, flip `physicalConfirmation.status:
   passed` and `releaseSuiteEligible: true`, drop `draft-unverified` /
   `mandatory: false` in the scenario, and add its suite to `full-tester` —
   `coverage-report --check` (and `test_promotion.py` / the promotion
   invariant) will refuse an inconsistent promotion.
8. Re-run to confirm the promoted scenarios now gate the release.

## Automatic exact-identity evidence acquisition

The normal qualification workflow no longer requires a technical owner to
find or copy GitHub workflow run IDs, artifact IDs, or local ZIP paths:

```bash
python -m calee_regression acquire-release-evidence \
  --bundle /path/to/release-bundle \
  --run-id <release-run-id>
```

derives every expected identity from the **verified release bundle** and the
effective release configuration (framework SHAs come from the run's recorded
immutable baseline when one exists), locates the exact matching GitHub
Actions evidence for:

1. calee-regression merged-main CI (`framework-tests.yml`, organic `push` to
   `main` or `merge_group`, head SHA exactly the expected framework SHA);
2. CaleeMobile-Regression merged-main CI (`ci.yml`, its canonical required
   gates);
3. selector certification (exact CaleeMobile SHA + version + release ID +
   supported schema version, `workflow_dispatch` only);
4. distributed-build evidence per required mobile platform (already-recorded
   authenticated provenance in the run workspace is re-verified and reused;
   otherwise BLOCKED with a precise remediation — a placeholder PASS is
   never fabricated);

authenticates each artifact against its workflow run and GitHub-recorded
digest, caches the bytes under `reports/runs/<run-id>/evidence/acquired/`
(atomic, private, immutable names embedding type/repo/run/artifact IDs) and
writes a secret-free `evidence/acquisition-manifest.json`. Zero matches,
multiple (ambiguous) matches, expired artifacts, or a missing GitHub token
are all **BLOCKED** — "the latest successful run" is never used, and no
unauthenticated fallback exists. The read-only twin is
`inspect-release-evidence` (same arguments; downloads and writes nothing).

`qualification-preflight` runs this acquisition automatically when given
`--bundle` and `--run-id`, so the normal preflight is just:

```bash
python -m calee_regression qualification-preflight \
  --bundle /path/to/release-bundle --run-id <release-run-id>
```

The preflight output labels each evidence item `automatic`,
`explicit-override`, `cache`, or `recorded-evidence`, and reports blocked/
rejected items with remediation.

**Migration note:** the manual `--selector-workflow-run-id` /
`--selector-artifact-id` / `--selector-artifact-zip` /
`--*-main-workflow-run-id` / `--*-main-artifact-id` flags remain fully
supported as *diagnostic overrides* — they are authenticated exactly as
strictly as discovered evidence and a mismatch BLOCKS — but they are no
longer the standard operator workflow.

## Selector evidence preflight

Before a production qualification, a technical owner can authenticate selector
evidence without downloading its redirected GitHub artifact ZIP again:

```bash
python -m calee_regression qualification-preflight \
  --bundle /path/to/release-bundle \
  --selector-workflow-run-id <run-id> \
  --selector-artifact-id <artifact-id> \
  --selector-artifact-zip /path/to/selector-contract-result.zip
```

`--selector-artifact-zip` (or the launcher-compatible
`CALEEMOBILE_SELECTOR_GITHUB_ARTIFACT_ZIP` environment variable) supplies only
the ZIP bytes. The run ID and artifact ID remain mandatory, and a GitHub API
token (`REGRESSION_API_TOKEN`, `GITHUB_TOKEN`, or `GH_TOKEN`) is still required
to authenticate the repository, workflow, successful run, artifact ownership,
and GitHub-recorded SHA-256 digest. A missing token, mismatched ZIP, malformed
archive, or identity mismatch is **BLOCKED**; a local file never bypasses
GitHub-origin verification. If no local ZIP is supplied, preflight retains the
normal authenticated GitHub download behavior.

### CaleeShell kiosk/admin — physical qualification (BLOCKED here)

On a **disposable** device-owner tablet only (never a customer device),
confirm: HOME persistence, reboot recovery, Home restriction, Recents
restriction, notification-shade behaviour, admin-entry gesture, admin PIN,
Wi-Fi access policy, update recovery, and Calee relaunch after update. This
stays `BLOCKED` until run on such a device.
