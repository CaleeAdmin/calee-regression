# Release approval policy

This is the decision rule the consolidated report (`python -m calee_regression consolidate`,
run by `06 Test Full Calee Solution.command`) applies. It is implemented in
`calee_regression/consolidated_report.py::decide_status`/`build_release_report` — this document
describes the rule; the code is the source of truth if they ever disagree.

## The three outcomes

- **PASS** — every mandatory component (the Calee tablet suite, the CaleeMobile Client API suite,
  and all mandatory manual guided checks) passed. Nothing was blocked, nothing mandatory was
  skipped or left unexecuted, and the tested application versions match the release candidate.
- **FAIL** — at least one product assertion failed somewhere. FAIL always wins over BLOCKED: a real
  regression must never be hidden behind an unrelated blocked component.
- **BLOCKED** — no product failure was proven, but at least one mandatory component could not run
  (an environment/device/connectivity/credential/fixture/tooling problem) or was never run at all.
  **BLOCKED is never converted to PASS.** A missing/not-executed mandatory component is treated
  exactly like a blocked one — an absent result must never read as a pass by omission.

## Mandatory vs. optional components

| Component | Mandatory for overall PASS? |
|---|---|
| Calee tablet suite (`full-tester`/`release-technical`) | Yes, always |
| CaleeMobile Client API suite | Yes, always |
| CaleeMobile Android UI suite | Driven by `config/release-platforms.yaml`'s `mobile_android` — **defaults to Yes** if that file is absent |
| CaleeMobile iPhone UI suite | Driven by `config/release-platforms.yaml`'s `mobile_ios` — **defaults to Yes** if that file is absent |
| Manual guided checks | Yes, whenever any are defined for the release profile in question — see `docs/NON_TECH_TESTER_GUIDE.md` and `config/manual-checks.example.json` |

Android/iOS UI mandatory-ness is **not** hard-coded — it comes from the technical owner's
`config/release-platforms.yaml` (copy `config/release-platforms.example.yaml`), or `--android-
mandatory`/`--android-optional`/`--ios-mandatory`/`--ios-optional` on `consolidate` directly. An
**omitted** or absent config file means every platform defaults to mandatory: a platform must be
explicitly opted out (e.g. `mobile_ios: false` for an Android-only hotfix), never silently narrowed
by convenience. `06 Test Full Calee Solution.command` reads this same profile to decide which
platforms to even attempt running (see `release-platforms` CLI output).

Any component simply omitted from `consolidate`'s inputs is recorded as `not_run`, which is treated
exactly like BLOCKED for a mandatory component — its absence can never become an easy PASS.

## Additional PASS preconditions

Beyond the pass/fail/blocked roll-up above, an overall PASS additionally requires:

- No mandatory scenario/step was skipped. A mandatory (release-critical) scenario that ends up
  `SKIPPED` — e.g. a `requires_state` mismatch — is folded into the same blocking bucket as an
  outright-blocked one (`SuiteResult.mandatory_skipped_count`); only a scenario explicitly marked
  `mandatory: false` may be skipped without blocking. The same applies at the step level: a
  `tap_if_present` step whose target is absent BLOCKS the scenario unless the step is explicitly
  marked `optional: true`/`required: false` — the default is always required.
- No scenario passes on the strength of skipped/optional steps alone — a scenario where nothing
  actually asserted anything (every step skipped, absent-and-optional, or wrapped in `optional`)
  cannot resolve to PASS; it BLOCKS instead, since nothing was actually verified.
- The tested application version(s) match the intended release candidate. Pass `--calee-build-
  version`/`--caleemobile-build-version` (detected) alongside `--expected-calee-build-version`/
  `--expected-caleemobile-build-version` (technical-owner-configured) to `consolidate` for an
  automated block-on-mismatch check; if no expected version is configured, this isn't checked and
  falls back to manual confirmation, same as before.

## Where this is enforced in code

- `calee_regression/consolidated_report.py::decide_status` — the core PASS/FAIL/BLOCKED decision
  from raw counts, shared by both the CLI's own suite/scenario exit codes (`cli.py::_exit_code_for`)
  and the consolidated report, so they can never disagree.
- `calee_regression/consolidated_report.py::build_release_report` — combines the tablet suite, the
  CaleeMobile API/UI reports, and manual checks into one `ReleaseReport`, applying the
  mandatory/optional distinction above and `component_from_build_version_match`.
- `calee_regression/release_platforms.py` — loads `config/release-platforms.yaml` and resolves the
  Android/iOS mandatory flags passed into `build_release_report`.
- `calee_regression/runner.py::run_scenario`/`_step_tap_if_present` — the required/optional step
  default and the "no real verification occurred" BLOCKED rule.
- `calee_regression/models.py::SuiteResult.mandatory_skipped_count` — a mandatory scenario ending up
  `SKIPPED` feeds into the same blocked bucket as `blocked_count`.
- `calee_regression/cli.py::prepare` — BLOCKS on missing fixture credentials/reset/verify failure
  for a release-gating profile; records fixture version/status to
  `reports/environment-status-latest.json`, which `consolidate` folds into the report's `meta`.
- `framework_tests/test_consolidated_report.py`, `test_release_platforms.py`,
  `test_mandatory_skip_handling.py`, `test_cli_prepare.py` — self-test this policy against synthetic
  pass/fail/blocked/missing inputs (no real device or backend needed).
