# Cloud / offline framework work vs. physical qualification

This framework is developed and validated in two very different places:

- an **offline / cloud environment** (a Linux container with no devices), where
  the *framework itself* is engineered and validated; and
- **Yiwen's qualification Mac**, where *physical qualification* and *release
  certification* actually happen against real devices and a real backend.

A cloud environment **cannot** perform device certification. Nothing here lets
it: the physical-qualification dimensions stay `blocked` without validated
device evidence, and `host-capabilities` reports `OFFLINE_FRAMEWORK_ONLY`.

## The four levels of "tested" (do not conflate them)

| Level | Where | What it proves | Command(s) |
|---|---|---|---|
| **Offline framework validation** | cloud or Mac | the framework's own code/config is correct | `pytest`, `coverage-report --check`, `framework-completeness --check` |
| **Diagnostic focused verification** | Mac | a fix works end-to-end; **no** certification claim | `focused-verify` |
| **Physical qualification** | Mac + devices | a capability produced validated, certification-eligible device/backend evidence | full-solution release run |
| **Release certification** | Mac + devices | the mandatory release scope is qualified and ready to ship | consolidated release report + `releaseReadiness == pass` |

`framework-completeness` reports these as three independent measures —
implementation, qualification, release readiness — see
[COMPLETENESS_MODEL.md](COMPLETENESS_MODEL.md).

## Hermetic Python / bootstrap contract

Every tester launcher runs the framework through an **absolute,
repository-owned interpreter**, never a bare `python`/`python3` from `PATH`:

- `scripts/ensure_environment.sh` resolves and exports `CALEE_PYTHON`
  (`<repo>/.venv/bin/python`), `CALEE_PIP` and `CALEE_BOOTSTRAP_VERSION`. It
  installs deps only via `"$CALEE_PYTHON" -m pip` and creates the venv only via
  `"$PYTHON_BIN" -m venv` — never a bare `pip`.
- `scripts/lib/hermetic_python.sh` is the shared, Bash-3.2-compatible resolver
  (honours a validated pre-set `CALEE_PYTHON`, else the repo `.venv`, else a
  system python). Standalone scripts source it so they are hermetic even when
  run directly.
- A present-but-broken `.venv` is diagnosed explicitly and never silently
  recreated or replaced by a system python — that silent fallback is exactly the
  portability footgun this contract closes.
- `bootstrap_provenance.py` records secret-free interpreter/venv/bootstrap
  identity (`pythonExecutable`, `pythonVersion`, `virtualEnvironment`,
  `bootstrapVersion`) for reports.

The `hermetic` CI job installs into an isolated `.venv` and runs the launcher
tests under a `PATH` stripped of that `.venv`, proving **no globally-installed
project dependency is required.**

## host-capabilities — what can THIS host do?

```bash
python -m calee_regression host-capabilities            # json + text
```

Read-only. Reports OS/arch/host, interpreter/venv, ADB / Appium / Flutter /
Xcode availability, visible devices, macOS Keychain, a configured backend and
credential **sources** (presence only — never a value), and a single
`executionCapability` classification. A cloud container reports
`OFFLINE_FRAMEWORK_ONLY`; an equipped Mac reports
`PHYSICAL_QUALIFICATION_CAPABLE` with per-platform detail. Deterministic reason
codes distinguish `unavailable` / `not-configured` / `unsupported-on-host`.

## qualification-plan — the Mac handoff

```bash
python -m calee_regression qualification-plan --config config/tester.local.yaml
```

Generates a concrete, secret-free, ordered plan to move the qualification
measure on the Mac: host prerequisites, required repo/product SHAs, required
devices, required credentials by source category, and ordered steps each
labelled with phase (read-only / focused-diagnostic / release-certification),
fixture mutation, which dimensions it advances, and whether it needs manual
guided evidence / kiosk authorisation / an Android device. It never uses a
literal `<RUN_ID>` in a command (`$CALEE_RUN_ID` is generated and reused) and
never silently narrows the release scope. The double-click launcher is
`tester/10 Qualification Plan.command`.

## evidence-bundle — moving evidence between environments

A physical run happens on the Mac; analysis may happen in the cloud. Move a
run's evidence as a self-describing, integrity-checked, **secret-free** zip:

```bash
python -m calee_regression evidence-bundle export --run-id "$CALEE_RUN_ID" \
    --profile audit --output ~/calee-audit-$CALEE_RUN_ID.zip
python -m calee_regression evidence-bundle verify  ~/calee-audit-$CALEE_RUN_ID.zip
python -m calee_regression evidence-bundle inspect ~/calee-audit-$CALEE_RUN_ID.zip
```

Security model (fail-closed): only an allowlist of report/diagnostic file types
is included; symlinks and path escapes are rejected; every textual file is
scanned for credential shapes **before** export and a match aborts it; every
file carries a SHA-256 digest that verification recomputes; smuggled entries and
path-traversal entries are rejected. Verify/inspect work **offline** and never
populate a live run directory or overwrite local evidence.

Two profiles:

- **`audit`** — device identifiers pseudonymized; marked **non-certifying after
  import**, so it can never promote a scenario or satisfy a release gate. Safe to
  share with a cloud analysis session.
- **`local-certification-transfer`** — preserves the exact source reports and
  identities and their recorded eligibility.

**Transport-integrity limitation:** SHA-256 digests prove the bundle's
*integrity* (bytes unchanged). They do **not** prove the exporter's *identity /
authenticity* unless an authenticated signing mechanism is added. Do not treat a
matching hash as proof of who produced the evidence.
