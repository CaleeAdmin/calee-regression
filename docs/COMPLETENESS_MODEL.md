# Framework completeness model — three independent measures

`python -m calee_regression framework-completeness` reports **three orthogonal
measures**. They exist because a single blended number (the legacy
`weightedCompletionPercent`) conflates two very different questions and made an
offline framework look "half done" when its architecture is in fact
substantially complete and only *physical qualification* is outstanding.

Every value below is **derived** from repository metadata and validated
physical reports (`coverage/coverage-manifest.yaml`, `suites.py`,
`scenarios/promotion/*.yaml`, `config/release-platforms.yaml`, and
`reports/runs/<run-id>/...`). Nothing is hand-edited, and a documentation-only
change can never move a number.

## A. Implementation completeness — *is it built?*

> Has the capability been implemented, offline-tested, and wired into
> orchestration?

States: `complete` · `partial` · `not-implemented`.

- A capability can be **implementation-complete without any physical run.**
- A `draft` scenario is *implemented, offline-tested automation pending physical
  promotion* — that is a qualification concern, so it counts as **complete**
  implementation, never as missing code.
- A physical blocker (no device, no backend) is **never** counted as missing
  implementation work.

`implementationCompletionPercent` weights each dimension by the same weights as
the legacy measure, scoring `complete=1.0, partial=0.5, not-implemented=0.0`.

## B. Qualification completeness — *is it proven on real devices/backend?*

> Has the capability produced validated physical/backend evidence that was
> certification-eligible and is current for the tested build/platform?

States: `qualified` · `implemented-unqualified` · `blocked` · `not-applicable`.

- **Offline tests are never counted here.** Only a validated,
  certification-eligible report under `reports/runs/<run-id>/...` qualifies a
  dimension.
- **Stale ≠ missing.** Evidence whose recorded build/platform no longer matches
  the build under test is `implemented-unqualified` (stale), reported
  separately from a dimension that has *no* evidence at all (`blocked`).
- Offline-internal dimensions (framework architecture, release-evidence
  integrity, fixture exclusivity) are `not-applicable` and are **excluded from
  the qualification-percentage denominator** — they never need a device run.
- Android / iOS / tablet / kiosk qualification are distinct dimensions.

`qualificationCompletionPercent` scores `qualified=1.0,
implemented-unqualified=0.5, blocked=0.0` over the applicable dimensions only.

## C. Release readiness — *may we ship?*

> Derived from the **mandatory release scope** (the release-gating dimensions)
> plus validated release evidence.

States: `pass` · `fail` · `blocked` · `not-applicable`. **This is not a
percentage, and a percentage is never turned into a PASS.**

- `pass` — every release-gating dimension is `qualified` (or is a built,
  offline-validated internal dimension that needs no device run).
- `blocked` — at least one gating dimension lacks current qualification
  evidence. (An offline checkout is always `blocked`.)
- `fail` — a gating dimension has a validated, certification-eligible report
  whose status is a hard **fail** (a product regression), not merely missing.
- `not-applicable` — the current scope has no release-gating dimensions.

## Migration notes for report-schema consumers (v1 → v2)

`framework-completeness.json` is now `schemaVersion: 2`.

- **Nothing v1 was removed.** `status`, `releaseGating`, `statusVocabulary`,
  `statusCounts`, `summary.weightedCompletionPercent`, and the per-dimension
  `statusScore` are all still present and unchanged. A v1 consumer keeps
  working.
- **Added at the top level:** `implementationCompleteness`,
  `qualificationCompleteness`, `releaseReadiness`,
  `implementationStatusVocabulary`, `qualificationStatusVocabulary`,
  `releaseReadinessVocabulary`.
- **Added per dimension:** `implementationStatus`, `qualificationStatus`,
  `qualificationEvidenceStale`, `implementationScore`, `qualificationScore`
  (the latter is `null` for `not-applicable` dimensions). Physical-evidence
  entries gained `qualificationBuild`, `qualificationPlatform`, and `stale`.
- **Recommended migration:** stop treating `weightedCompletionPercent` as a
  readiness signal. Use `implementationCompletionPercent` to track build
  progress, `qualificationCompletionPercent` to track device/backend proof, and
  `releaseReadiness.status` (never a percentage) to make a ship/no-ship
  decision.

## What each measure must never do

- Never count a physical blocker as missing implementation work.
- Never count offline tests as physical qualification.
- Never turn a percentage into a release PASS.
- Never raise any measure because documentation alone changed.
