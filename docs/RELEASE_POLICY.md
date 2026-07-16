# Release approval policy

This is the decision rule the consolidated report (`python -m calee_regression consolidate`,
run by `05 Test Full Calee Solution.command`) applies. It is implemented in
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
| Calee tablet suite (`full-tester`/`release-technical`) | Yes |
| CaleeMobile Client API suite | Yes |
| CaleeMobile Android UI suite | No — flags BLOCKED if run and blocked, but its absence alone doesn't block overall PASS (Flutter/device availability varies by machine) |
| CaleeMobile iPhone UI suite | No — same as Android; requires a Mac with Xcode |
| Manual guided checks | Yes, whenever any are defined for the release profile in question — see `docs/NON_TECH_TESTER_GUIDE.md` and `config/manual-checks.example.json` |

Only the tablet suite and the API suite are unconditionally mandatory, because they're the only two
components guaranteed to run in every environment without extra device/toolchain requirements.
Whoever runs `05 Test Full Calee Solution.command` for an actual release decision should also
attach the mobile UI suites and manual checks so the release report reflects real coverage — the
policy doesn't silently let their absence become an easy PASS: any component simply omitted from
`consolidate`'s inputs recorded as `not_run`, which is treated the same as BLOCKED.

## Additional PASS preconditions

Beyond the pass/fail/blocked roll-up above, an overall PASS additionally requires:

- No mandatory test was skipped for a non-optional reason (a `SKIPPED` scenario/step that isn't
  explicitly, deliberately optional for this test configuration still shows up as a mandatory
  component not fully passing — see the scenario's own `skip_reason`).
- The tested application version(s) — Calee build, CaleeMobile build — match the intended release
  candidate. This isn't automatically checked by `consolidate` today; record it in the report's
  `meta` (`--build-version`) and confirm manually before treating a report as authoritative for a
  specific release.

## Where this is enforced in code

- `calee_regression/consolidated_report.py::decide_status` — the core PASS/FAIL/BLOCKED decision
  from raw counts, shared by both the CLI's own suite/scenario exit codes (`cli.py::_exit_code_for`)
  and the consolidated report, so they can never disagree.
- `calee_regression/consolidated_report.py::build_release_report` — combines the tablet suite, the
  CaleeMobile API/UI reports, and manual checks into one `ReleaseReport`, applying the
  mandatory/optional distinction above.
- `framework_tests/test_consolidated_report.py` — self-tests this policy against synthetic
  pass/fail/blocked/missing inputs (no real device or backend needed).
