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
| Test environment and regression fixture (`prepare`) | **Yes, always — no opt-out.** A non-zero Prepare result blocks the release the same as any other missing mandatory component; there is no "informational note next to an otherwise-green result" — see below. |
| Calee tablet suite (`full-tester`/`release-technical`) | Yes, always |
| CaleeMobile Client API suite | Yes, always |
| CaleeMobile Android UI suite | Driven by `mobile_android` — **defaults to Yes** if no scope source is configured. See "Schema-v2 release bundles are authoritative for scope" below for where this actually comes from. |
| CaleeMobile iPhone UI suite | Driven by `mobile_ios` — **defaults to Yes** if no scope source is configured. Same precedence as above. |
| Cross-device synchronization (`sync-smoke`) | Driven by `release_features.synchronization` — **defaults to Yes** if no scope source is configured (see precedence below). Runs after the mobile UI legs and before manual checks, reusing this run's verified backend/fixture/credentials and the same run ID; its report (`reports/runs/<run-id>/sync/results.json`) is auto-discovered and validated like every other component. A missing/stale/run-ID-mismatched/`BLOCKED`/`FAILED` mandatory sync can never PASS. Currently resolves to `BLOCKED` until the tablet-mutation gap closes (`docs/TABLET_MUTATION_COVERAGE_GAPS.md`) and a real device verifies the flows — the intended safety property, not a silent non-gate. An excluded (`synchronization: false`) sync is still shown as an explicit **optional** component. |
| Manual guided checks | Yes, whenever any are defined for the release profile in question — see `docs/NON_TECH_TESTER_GUIDE.md` and `config/manual-checks.example.json` |

### Schema-v2 release bundles are authoritative for scope (Priority 2)

Platform scope, feature scope, the production/staging profile, and every expected application
identity ultimately come from ONE of two sources, never both at once for the same run:

- **A schema-version-2 release bundle manifest** (`release-manifest.json`'s `schemaVersion: 2` —
  see `docs/RELEASE_INSTALLER.md`), once verified and composed by `release-config` for this run
  (`calee_regression/release_config.py::compose_effective_release_config`). This is
  **self-contained and authoritative**: `platforms`, `features`, `profile`, `backend`, and every
  `tabletSolution`/`caleeMobile` expected identity in the bundle manifest control the whole run —
  `config/release-platforms.yaml` is **not consulted at all**, and a malformed/absent
  `config/release-platforms.yaml` can never block a run driven by a valid schema-v2 bundle.
  Production profile forcing selector-evidence policy (see `docs/RELEASE_INSTALLER.md`'s selector
  section) always follows the bundle's own `profile`, never a co-present legacy file's.
- **`config/release-platforms.yaml`** (schema version 1, or no bundle manifest at all) — the
  original source of truth, still loaded and cross-checked exactly as before schema v2 existed.

Every downstream command that used to read `config/release-platforms.yaml` directly
(`consolidate`, `selector-contract`, `sync-smoke`, `kiosk-admin`) now prefers this run's own
already-composed `release-config` evidence (`reports/runs/<run-id>/release-config/results.json`)
whenever one exists, falling back to `config/release-platforms.yaml` only when it doesn't
(`calee_regression/cli.py::_v2_platforms_features_expected` is the shared conversion). An explicit
CLI flag (e.g. `--android-mandatory`, `--production`) always wins over either source. See
`calee_regression/cli.py::_emit_release_config_vars` for the full set of `RELEASE_*` shell
variables `06 Test Full Calee Solution.command` sources from a composed release-config, including
the per-feature `RELEASE_FEATURE_*` flags and the `RELEASE_EXPECTED_*` identity fields.

### Selector evidence and distributed-build acceptance precedence (Priority 3)

`caleeMobile.selectorEvidenceRequired` and `caleeMobile.distributedBuildAcceptanceRequired` (schema
v2) are enforced, not merely recorded, in `consolidate`'s gating decision (`calee_regression/cli.py`).
For **both** flags the precedence, highest first, is:

1. **Production + a mobile release** (selector evidence only) — unconditionally mandatory; a manifest
   `false` (or a legacy `config/release-platforms.yaml` opt-out) can never suppress it, and
   `--selector-contract-optional` is rejected outright. This is the one rule that overrides a `false`
   flag.
2. **An explicit CLI flag** (`--selector-contract-mandatory/-optional`,
   `--distributed-build-acceptance-mandatory/-optional`) — always wins over the manifest.
3. **The manifest's own flag** (via this run's composed `release-config`) — `true` makes the
   component mandatory (selector evidence: even outside a mobile release); `false` records the
   component as an explicit, visible **not required for this release**, never a silent omission.
4. **A structural default** — selector evidence defaults to mandatory whenever a mobile platform is
   in scope (or a selector-contract report already exists for this run); distributed-build acceptance
   has no structural default and simply does not apply (omitted entirely) when no release-config was
   composed for this run at all (ad-hoc/dev consolidation).

With no physical/distributed evidence, distributed-build acceptance BLOCKS rather than passing — it
is never inferred from a local checkout or an unsigned build (`calee_regression/
distributed_build_acceptance.py`'s `verifiedVia` allow-list enforces this).

### Distributed-build evidence must be authenticated at its origin (Priority 3, this session)

`record-distributed-build-acceptance` (`calee_regression/provider_evidence.py` +
`distributed_build_provenance.py`) can only reach PASS through evidence THIS process itself
independently authenticated, never from an operator's own claim about what a provider said:

- **`--provider {app_store_connect,play_console}`** — a live, JWT-authenticated HTTPS request to the
  real provider API, made by this process right now (tier `provider-api-live`).
- **`--signed-export`** — a detached signature cryptographically verified (RSA or EC, real
  `cryptography`-library verification, not merely "a signature-shaped object is present") against a
  configured trusted public key (tier `verified-signed-export`).
- **`--github-run-id`** — an authenticated GitHub Actions artifact chain (repository/workflow-path/run
  success/artifact ownership/digest — the same style of chain `python -m calee_regression
  verify-main-ci-artifact` enforces for merged-main CI evidence, see `calee_regression/
  main_ci_artifact.py`) whose contained result is itself a live-collected `provider-api` record, never
  a hand-typed claim smuggled through an otherwise-real artifact (tier `github-authenticated-artifact`).
- **`--source`** (an operator-supplied JSON file) and the legacy `--channel`/`--verified-via` flags can
  at best record an explicit `blocked-unverified`/`manual-unverified` claim — **never** a PASS, no
  matter how well-formed or internally self-consistent the content looks. This is enforced by an
  `evidenceTier` stamped by the CLI's own control flow (never read from the operator-supplied content),
  carried inside the same envelope-digest-protected provenance record `distributed_build_provenance.py`
  already used for tamper-evidence, and independently re-checked at consolidation
  (`consolidated_report.component_from_distributed_build_acceptance_report`) — never trusting a
  report's recorded `status` alone, so a hand-edited `results.json` cannot forge a stronger tier either.

### Subscribed-fixture evidence is bound to the release (Priority 7)

The subscribed-calendar-fixture component (`prepare-subscribed-fixture` /
`calee_regression/subscribed_publisher.py`, see `docs/SUBSCRIBED_CALENDAR_REGRESSION.md`) is bound to
the specific release it is evidence for, the same way selector-contract evidence is (Priority 8):

* `prepare-subscribed-fixture` adopts this run's own composed `release-config` `releaseId` (an explicit
  `--release-id`/`CALEE_RELEASE_ID` always wins) and records it in `subscribed-fixture/results.json`.
* At consolidation, `component_from_subscribed_fixture_report` requires the report's `releaseId` to be
  present and match `consolidate`'s own resolved release ID whenever one applies — a missing release id,
  or one for a different release, BLOCKS. Wrong-run and stale-report rejection are not duplicated here:
  every component (subscribed-fixture included) already goes through `_resolve_component` ->
  `run_context.validate_component_report`, which rejects a report with a mismatched run ID or a file
  older than this run's start, exactly like every other component.
* A `mode: "published"` report is re-verified, never merely trusted: it must independently show BOTH
  `publicReadVerificationStatus == "ok"` (Priority 5's exact byte/title/date check) AND
  `ingestionStatus == "ok"` (Priority 6's Calee-ingestion check) before a bare top-level `status: "ok"`
  is accepted as PASS. `fixed-date`/`offline-only` reports never set either status (they never claim
  publication or ingestion at all — Priority 6: "never faked").

### Prepare is mandatory, unconditionally

Earlier versions of `06 Test Full Calee Solution.command` ran Prepare first, captured its exit
code, then only printed an informational `NOTE:` line if Prepare had failed but the rest of the
run (and therefore `consolidate`) still came back PASS — meaning a release could read as PASS
overall even though the environment/fixture was never actually confirmed ready. That gap is closed:
Prepare's outcome (`reports/runs/<run-id>/environment/results.json`) is now itself a mandatory
component `build_release_report` evaluates like any other (`component_from_environment_report`), so
a blocked or never-executed Prepare step blocks the overall result directly — not as a footnote.

## One run ID per release run

`06 Test Full Calee Solution.command` generates one `CALEE_RUN_ID` (e.g.
`release-20260716-153012-a1b2c3`) at startup and passes it to every component (`prepare`, the
tablet `suite`, CaleeMobile's API/UI checks, `record-manual-checks`, `consolidate`). Every component
writes its report to a fixed path inside that run's own workspace,
`reports/runs/<run-id>/<component>/results.json`, instead of a timestamped directory a later step
has to rediscover by listing and sorting, or a shared `*-latest.json` file two runs could race on
overwriting. `consolidate` auto-discovers each component from these fixed paths and validates every
report it uses against `--run-id` before trusting it (see `calee_regression/run_context.py`):

- **missing run ID** in the report — rejected
- **mismatched run ID** — rejected (this report belongs to a different run)
- **report path outside the current run's workspace** — rejected
- **report generated before the current run started** (stale, left over from a workspace directory
  reuse) — rejected

A rejected report is treated exactly like that component never having run at all — `not_run`,
folded into BLOCKED for a mandatory component. `reports/latest-run` is a convenience symlink
`consolidate` (re)creates only *after* a run finishes, for `07 Open Latest Report` to follow; it is
never a consolidation input.

Android/iOS UI mandatory-ness is **not** hard-coded — it comes from an explicit `--android-
mandatory`/`--android-optional`/`--ios-mandatory`/`--ios-optional` on `consolidate` directly, else
(per "Schema-v2 release bundles are authoritative for scope" above) THIS run's own composed
schema-v2 release-config when one exists, else the technical owner's legacy
`config/release-platforms.yaml` (copy `config/release-platforms.example.yaml`). An **omitted** or
absent config/scope source means every platform defaults to mandatory: a platform must be
explicitly opted out (e.g. `mobile_ios: false` for an Android-only hotfix), never silently narrowed
by convenience. `06 Test Full Calee Solution.command` reads the SAME composed scope `consolidate`
uses to decide which platforms to even attempt running when a machine config is present (via the
`release-config` command's emitted `RELEASE_CFG_OUT` variables); only when no machine config
exists at all (schema-v1/bare, the only case a bundle can't be resolved) does it fall back to the
legacy `release-platforms` CLI output directly.

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
- The same rule applies to CaleeMobile's mobile UI reports (Android/iPhone). Each test step there
  carries its own `mandatory` (bool) and `skipCategory` fields (see CaleeMobile-Regression's
  `ui/run_ui_suite.py::classify_skip`, driven by the skip reason a test passes to Dart's
  `markTestSkipped(reason)`): a step skipped with an `OPTIONAL: ...` reason (the signed-in test
  account genuinely doesn't have that feature) stays `SKIP` and does not block; anything else —
  including an unexplained skip — defaults to `mandatory=true` and is folded into BLOCKED by
  `component_from_api_report`, the same way a mandatory tablet-scenario skip is. A skip reasoned
  `FIXTURE_MISSING: ...` (the deterministic regression fixture wasn't reset/verified) is always
  mandatory too, and is deliberately never reported as a product FAIL — see
  `docs/TEST_DATA_RESET_CONTRACT.md`.
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
- `calee_regression/release_platforms.py` — loads `config/release-platforms.yaml` (schema-v1/bare
  runs only) and resolves the legacy Android/iOS mandatory flags; `calee_regression/release_config.py`
  (`compose_effective_release_config`) is the schema-v2 equivalent, authoritative whenever this run
  composed a schema-v2 bundle. `cli.py`'s `consolidate` command (`_v2_platforms_features_expected`)
  picks between the two before passing the resolved flags into `build_release_report`.
- `calee_regression/runner.py::run_scenario`/`_step_tap_if_present` — the required/optional step
  default and the "no real verification occurred" BLOCKED rule.
- `calee_regression/models.py::SuiteResult.mandatory_skipped_count` — a mandatory scenario ending up
  `SKIPPED` feeds into the same blocked bucket as `blocked_count`.
- `calee_regression/cli.py::prepare` — BLOCKS on missing fixture credentials/reset/verify failure
  for a release-gating profile; records status to this run's
  `reports/runs/<run-id>/environment/results.json` (`consolidated_report.component_from_
  environment_report` reads it as a mandatory component; `consolidate` also folds the fixture
  version/target-environment fields into the report's `meta`).
- `calee_regression/run_context.py` — the single-run-ID/workspace primitives:
  `RunWorkspace`/`RunManifest`/`validate_component_report`, shared by `prepare`/`suite`/
  `record-manual-checks`/`consolidate`.
- `framework_tests/test_consolidated_report.py`, `test_release_platforms.py`,
  `test_mandatory_skip_handling.py`, `test_cli_prepare.py`, `test_cli_consolidate.py`,
  `test_run_context.py` — self-test this policy against synthetic pass/fail/blocked/missing/stale/
  mismatched-run-ID inputs (no real device or backend needed).
