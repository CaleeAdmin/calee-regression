# Focused Post-Fix Verification (`focused-verify`)

The permanent command a tester runs after a fix, to re-check exactly the
areas the last blocked release run flagged — without a full release run and
**without ever claiming a release certification**. The old downloaded shell
wrappers are retired and are not a supported workflow.

```
python -m calee_regression focused-verify --plan            # show the plan, run nothing
python -m calee_regression focused-verify --preflight-only  # validate readiness, mutate nothing
python -m calee_regression focused-verify                   # the real focused run
```

A tester can use `tester/09 Focused Post-Fix Verification.command`, which
bootstraps the environment (`scripts/ensure_environment.sh`), runs the
permanent Python command, prints the run ID and summary path, and preserves
the exit code. It never downloads a script, never prompts for a password in
the shell, and never updates a Git repository.

## Fixture preparation vs tablet preparation

Preparation is split so an unavailable tablet toolchain can never prevent
the API/iPhone checks, and product checks can never run against an
unverified fixture:

- `prepare-fixture` — validates backend + credentials, resets and verifies
  the deterministic REG-* fixture, and writes the typed
  `fixture-preparation` report (`reports/runs/<run-id>/environment/results.json`).
  **Requires no Appium/ADB/tablet/APK.**
- `prepare-tablet-environment` — validates Appium, ADB, the device and the
  APK, and writes a typed `tablet-environment-preparation` report. Never
  touches the fixture.
- `prepare` — the strict full-release command, unchanged behaviour: Appium +
  device preflight first, then the *same* fixture flow.

## Dependency graph

```
credential preflight
    └─ fixture preparation (prepare-fixture, Appium-independent)
         ├─ focused API attempt 1        (fixture only)
         ├─ focused API attempt 2        (fixture only; attempt 1 never gates it)
         ├─ focused iPhone environment check (fixture only)
         └─ + Appium/tablet readiness
              ├─ standard recurring-calendar attempts
              └─ diagnostic recurring-calendar attempts
```

A failed prerequisite marks each dependent step `blocked_not_run`, naming
the exact prerequisite and its report path. Independent branches are never
suppressed by each other's failures; there is no silent skip status.

## Credentials

The parent resolves the regression username/password **once** through the
authoritative provider chain (explicit environment → macOS Keychain →
otherwise BLOCKED, before any product mutation). They are injected into each
child's **environment only** (`CALEE_TEST_EMAIL` / `CALEE_TEST_PASSWORD`) —
never argv, never any report, log, exception, timeout message or summary.
The summary records only the credential **source category**
(`environment`/`keychain`), and the parent scrubs its temporary secret dicts
as soon as orchestration finishes. Standalone CLI behaviour in both
repositories is unchanged.

## Same-run verified context

Child commands are built **only after** fixture verification passes, from an
immutable `FocusedVerifiedContext` derived from the same-run
`fixture-preparation` report — never a default, never a stray pre-existing
environment variable. Every child is bound (argv for non-secrets) to the run
ID, diagnostic release ID, verified backend, fixture version, execution
purpose, repository SHAs and the applicable device ID.

The focused API children run with `--require-explicit-context`, which makes
the mobile API CLI refuse (exit 2) its production base-url default and any
missing run/fixture/purpose context. The focused iPhone child receives the
complete context (`--device-id` where known, `--fixture-status ok`,
`--expected-backend`, `--mobile-backend`, `--release-run-id`, `--release-id`,
`--fixture-version`) plus `--execution-purpose focused-environment-check`,
which records the handoff gates as **not applicable with a reason** (an
app-boot check exercises no onboarding/Google OAuth journey) — reporter-native
backend evidence and build identity stay mandatory, and the result is
non-certifying.

## Reporter-native infrastructure evidence

The `CALEE_ENV_CONTRACT` synthetic test is infrastructure evidence, not a
product test: `run_ui_suite.py` parses it separately, keeps it out of the
product PASS/FAIL counts in a dedicated `environmentContract`/
`environmentContractSteps` section, requires exactly one per child, blocks
on duplicates, and blocks a child that ran *only* the contract without ever
starting its intended product test.

## Child report validation

`focused-verify` never trusts exit codes alone. For every child it validates
report existence, `reportType`, `reportSchemaVersion`, run/release identity,
backend, fixture version, execution purpose, device identity where
applicable, status/exit-code consistency, and that the report never claims
`certificationEligible: true`. Any disagreement is BLOCKED; a product FAIL
stands only when a valid report proves it. Each validated report's SHA-256
digest is recorded in the summary.

## Supervision, timeouts and cleanup

Every child runs under `focused_supervision.run_supervised`: a per-step-class
deadline (`fixture` 900 s, `tablet` 3600 s, `api` 600 s, `ios` 1800 s;
override with `--step-timeout class=seconds`), its own process group, and on
timeout SIGTERM → bounded grace → SIGKILL, always reaped. Elapsed time and
every termination action are recorded; a bounded, redacted output tail is
kept. `KeyboardInterrupt` still terminates and reaps the child and runs the
framework's Appium cleanup — no orphaned or suspended job survives.

## Exit codes

The framework contract is preserved end to end:

| code | meaning |
|------|---------|
| 0 | PASS |
| 1 | verified product regression |
| 2 | invalid invocation/configuration (a child's exit 2 is **not** rewritten to 3) |
| 3 | environment/tooling blocker |

Aggregate precedence: FAIL > INVALID_CONFIG > BLOCKED > PASS. Invalid static
configuration halts before any product mutation with exit 2; unexpected
child exit codes are BLOCKED with the exact code recorded; the summary
status and the process exit code can never disagree.

## Immutable summaries and the run manifest

Each invocation writes `reports/runs/<run-id>/focused-verify/<invocation-id>/summary.json`
(schema `focused-verify-summary` v2) — typed, versioned, digest-bound,
read-only on disk, `certificationEligible: false`. The run manifest records
every focused invocation in its worst-wins attempt history, so a later run
can never overwrite or improve an earlier result, and the summary can never
satisfy a release-certification component.

## Cross-repository contract

CaleeMobile-Regression exports the machine-readable focused-execution
contract (`python3 -m caleemobile_regression --describe-contract`);
calee-regression vendors it at `schemas/focused-execution-contract.json`.
Offline tests on both sides verify option/suite/purpose/schema agreement,
and the orchestrator BLOCKS on an unsupported contract version. Refresh the
vendored copy after any mobile-side contract change (see
`calee_regression/focused_contract.py`).

## Post-merge physical qualification (run on the Mac, not in CI)

```
cd /Users/yiwen/CaleeRelease2Check/calee-regression
git checkout main && git pull --ff-only
python -m calee_regression focused-verify --plan
python -m calee_regression focused-verify --preflight-only
python -m calee_regression focused-verify
```

CI never performs physical-device certification; it validates the framework
logic offline only.
