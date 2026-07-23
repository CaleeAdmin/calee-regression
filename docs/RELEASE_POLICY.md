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

### Resuming a blocked run

`python -m calee_regression resume-release --run-id <id>` continues a blocked run's existing
`CALEE_RUN_ID` workspace instead of starting a new one — it never repeats an already-passed
destructive/disruptive step unless something about it can no longer be trusted. See
`calee_regression/resume_release.py` for the implementation; `docs/NON_TECH_TESTER_GUIDE.md`
§12b and the "08 Resume Blocked Release" launcher for the tester-facing workflow.

A run may be resumed only when its immutable inputs still match the ORIGINAL attempt: release ID,
release-manifest schema version, the frozen release-candidate fingerprint, APK SHA-256 digests,
expected package IDs/version names/version codes/signer fingerprints/Git SHAs (Calee, CaleeShell,
CaleeMobile), the effective release-configuration digest, target backend, release profile,
platform/feature scope, this repo's own Git SHA, CaleeMobile-Regression's Git SHA, the tablet's own
stable identity, the manual-check definition version, and the selector-evidence/distributed-build-
evidence requirement flags. **Any mismatch refuses the resume outright (exit `3`) and requires a new
release run — there is no flag to bypass this.** A resume request that fails immutable-input
validation is never silently downgraded to a fresh run and a stale/mismatched checkpoint can never
read as an eligible resume; the mismatch is reported and nothing more happens.

Component reuse is a narrower, per-component decision on top of that gate: a prior **PASS** may be
reused only when its report still validates (same run, same release, its recorded input digest
still matches, every evidence file it references still exists and still hashes the same) — this is
the same rule the "Attempt 1 / Attempt 2" table below shows. **FAIL, BLOCKED, NOT_RUN, and a
mandatory SKIP are never reused** — they always require (re-)execution, same as everywhere else in
this policy: absence or an unproven result must never read as a pass by omission. An optional SKIP
remains explicitly represented rather than silently disappearing. A component whose evidence
exists but fails any integrity check (wrong run, wrong release, a report path outside the workspace,
a tampered/edited report, a missing referenced evidence file, an evidence digest that no longer
matches, or a stale/malformed report) is refused for reuse with a recorded reason — never silently
treated the same as a component that simply never ran.

Installation gets one further, narrower, ADDITIONAL check: even a structurally valid prior PASS is
reused (no reinstall, no reboot) only after a bounded, read-only ADB probe confirms the CURRENTLY
connected tablet is the same physical device (`calee_regression/release_installer.py::
capture_device_identity`/`stable_identity_matches`) and its installed package identity is
unchanged. Either check failing refuses the resume and requires a new release run — resuming never
falls back to "reinstall anyway and hope."

Prepare (environment readiness + the deterministic REG-* fixture) is rerun in-process by
`resume-release` whenever it is not itself a valid reusable PASS — a fixture reset that previously
BLOCKED may simply run again (`fixture_bridge.run_fixture_action`'s reset/verify are idempotent by
construction). A new, different verified fixture version may be established this way; any
component whose OWN report recorded which fixture version it ran against is never reused once that
version has moved on — a resumed run's downstream functional results are always bound to the
CURRENT verified fixture, never a stale one.

Every resume call is recorded as its own immutable attempt under
`reports/runs/<run-id>/attempts/<n>/` — never overwriting a previous attempt. Attempt 1 is the
run's original state (snapshotted the first time anyone asks to resume it) and its
`immutable-inputs.json` is the permanent baseline every later attempt is compared against, not
merely the most recent one. A later PASS never erases an earlier FAIL/BLOCKED attempt's history:

```text
Attempt 1
installation: PASS
prepare: BLOCKED — calendar service unavailable
remaining components: NOT_RUN

Attempt 2
installation: REUSED PASS
prepare: PASS
tablet: PASS
mobile-api: PASS
...
```

`python -m calee_regression inspect-resume --run-id <id>` is the read-only counterpart: it reports
whether a run is resumable, every immutable-input mismatch, which components are reusable, which
require execution, and which can never be reused — without mutating anything (no attempt is
recorded, Prepare is never rerun, and the tablet is only ever touched with the same bounded
read-only probe a real resume would perform).

`resume-release`/`inspect-resume` exit codes: `0` resume inspection is valid, or the resumed
qualification's own directly-executed steps (Prepare, the installation recheck) all succeeded; `1`
a mandatory component already carries a real product FAIL that a resume cannot fix; `2` invalid
`--run-id` or no run workspace; `3` resume refused (a new release run is required) — the same
exit-code contract every other command in this framework already follows.

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

## Exact-identity evidence acquisition

Release evidence (merged-main CI for both regression repositories, selector
certification, distributed-build provenance) is located and authenticated by
`acquire-release-evidence` (`calee_regression/evidence_acquisition.py`),
which is fail-closed by policy:

- every expected identity is derived from the **verified release bundle**,
  the frozen release candidate / recorded immutable baseline, and the
  effective release configuration — never from "the newest GitHub run", an
  operator-typed expected SHA, or artifact content that has not yet been
  origin-authenticated;
- selection requires the exact repository, the approved workflow path, an
  approved event (organic `push` to `main`/`merge_group` for main-CI;
  `workflow_dispatch` for selector certification), a successful conclusion,
  the exact head SHA / release tuple, and exactly one matching artifact —
  zero matches, ambiguity, expiry, or a missing token are BLOCKED;
- every artifact is authenticated against its workflow run (ownership) and
  GitHub's recorded digest before its content is trusted;
- cached evidence under `reports/runs/<run-id>/evidence/acquired/` is
  run-scoped, re-authenticated against freshly fetched GitHub metadata, and
  re-hashed on every reuse — a missing, changed, or mismatched cache file is
  rejected and re-downloaded, never trusted;
- an authenticated exact-identity artifact whose *content* contradicts (a
  failed required gate on a successful run, a failing selector contract for
  the exact release tuple) is a genuine evidence contradiction — exit 1
  under this policy, distinct from "evidence missing" (exit 3);
- distributed-build provider evidence that cannot be automatically collected
  with approved credentials is BLOCKED with a precise remediation; a
  placeholder PASS is never fabricated, and the existing evidence tiers are
  unchanged.

During a resume, `resume-release` acquires still-missing evidence *before*
blocked components are re-decided, binds the acquisition summary to the new
attempt record (`evidenceAcquisition` in `attempts/<n>/attempt.json`), and
never touches a prior attempt's snapshots — evidence that produced an
earlier PASS is never silently replaced (component reuse still re-verifies
every referenced evidence file's digest), and evidence belonging to another
release or immutable input set is rejected by the same exact-tuple matching.

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
- `calee_regression/resume_release.py` — immutable-input collection/diffing, the per-component
  reuse policy, installation's extra live tablet/package-identity recheck, and the
  `reports/runs/<run-id>/attempts/<n>/` attempt ledger `resume-release`/`inspect-resume` (`cli.py`)
  are built on.
- `framework_tests/test_resume_release.py`, `test_cli_resume_release.py` — self-test the resume
  policy against synthetic valid-reuse/refused-reuse/interruption-point scenarios (no real device or
  backend needed).
- `framework_tests/test_consolidated_report.py`, `test_release_platforms.py`,
  `test_mandatory_skip_handling.py`, `test_cli_prepare.py`, `test_cli_consolidate.py`,
  `test_run_context.py` — self-test this policy against synthetic pass/fail/blocked/missing/stale/
  mismatched-run-ID inputs (no real device or backend needed).

## Serial iPhone execution, feature-scope authority, and diagnostic non-certification

**Serial iOS by default.** The physical iPhone stalls when the whole
`integration_test` directory is launched in one Flutter process, so the release
path runs each integration-test **file** in its own process via
`CaleeMobile-Regression/ui/run_ui_manifest.py` (invoked by
`scripts/test_caleemobile.sh` for both platforms). Every file has independent
evidence under `mobile-<platform>/files/<file>/attempt-N/`; the ONE canonical
platform report is `reports/runs/<run-id>/mobile-<platform>/results.json`, and
per-file reports are subordinate evidence. The full-directory physical-iOS
invocation is no longer a release path.

**Retry policy.** At most one bounded retry, and only for a *confirmed*
launch/tooling failure (a recognized launch indicator in the structured result
and/or preserved log). A product FAIL, a fixture-missing skip, an onboarding-
state assertion, a backend mismatch, or a selector mismatch is **never**
retried. A recovered tooling block can make a file's final result PASS but never
erases the initial block from the auditable attempt history; a product failure
is never improved by rerunning.

**Aggregate status precedence.** Any product FAIL → platform FAIL; else any
mandatory BLOCKED → BLOCKED; else a mandatory unexplained skip → BLOCKED; else
PASS.

**Feature-scope authority.** The mobile suite is run with exactly the feature
scope THIS release run composed. `06` `eval`s
`python -m calee_regression release-feature-scope --run-id "$CALEE_RUN_ID"`
before the mobile checks; it prefers this run's schema-v2 release-config feature
scope over the legacy `config/release-platforms.yaml` (falling back to legacy
only when there is genuinely no schema-v2 bundle), and never re-parses the scope
in bash. A missing/malformed scope defaults to mandatory (never silently
optional). Consolidation additionally BLOCKS on any mismatch between the release
configuration's feature scope and the scope the mobile report was actually run
with (`releaseFeatures`).

**Diagnostic tablet runs never certify.** `device_initialization_mode: skip`
(`--device-initialization skip`) is a diagnostic-only mode: its report is marked
`diagnosticMode: true` / `certificationEligible: false`, and the consolidator
never treats it as release-certifying evidence (a would-be PASS is downgraded to
BLOCKED). Standard mode is `certificationEligible: true`. A legacy report with no
certification fields is treated as standard; an ambiguous/partial certification
metadata blocks rather than being inferred eligible.
