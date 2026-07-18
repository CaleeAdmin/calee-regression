# Release version identity (Priority 9)

## The problem

CaleeMobile `dev` reported pubspec version `0.0.23+23` — byte-for-byte the same
identity as the shipped release `main` (`836b2db3bc8b1314cf9088978386c6ef7423458e`,
"0.0.23", PR #463) — despite `dev` having accumulated substantial post-release
product code, most significantly the `CalendarReminderCoordinator` (PR #467).
Two materially different builds sharing one store identity means a release, its
selector evidence, and its build-identity gate cannot tell them apart: shipping
the reminder-enabled build under `0.0.23` would misrepresent which build users
receive.

This was directly visible in the two selector artifacts retained for the
release-main and dev SHAs (see
CaleeMobile-Regression/docs/CROSS_REPO_DISPATCH_ARTIFACTS.md): both report
`pubspecVersion: 0.0.23+23`.

## The decision

Bump patch **and** build number together, following the established
`0.0.22+22 -> 0.0.23+23` cadence:

| | before | after |
|---|---|---|
| CaleeMobile pubspec `version:` | `0.0.23+23` | `0.0.24+24` |

Rationale: the project has released via patch increments (0.0.22, 0.0.23); a
conventional next-patch `0.0.24+24` gives the reminder-enabled build its own
identity with the least surprise. If the team classes the reminder feature as a
minor-level change, `0.1.0+24` is the alternative — a one-line pubspec change.
The invariant that matters for the release gate is simply that the
reminder-enabled build is **not** `0.0.23`.

## The merged dev head (authoritative release identity)

The `0.0.24+24` change reached `dev` through two PRs — **#469** (reminder
fingerprinting + cleanup) and **#470** (dispatch response parsing + the
`0.0.24+24` version bump). The **merged `dev` head** is:

```
CaleeMobile dev = 41c97a97eddaf8676d43bb5efd5b2018d51b7faa   (version 0.0.24+24)
```

`3e431382b6f658da794866bdcfcb87e14d8a3321` was the **PR head** of #470 *before*
it was squash-merged into `dev`; it is **not** the commit that now lives on
`dev`. A release ships the merged commit, so the authoritative release identity
is the merged dev head, not the PR head:

- **version** `0.0.24+24`
- **CaleeMobile SHA** `41c97a97eddaf8676d43bb5efd5b2018d51b7faa`

A production consolidation must pin `caleemobile_build_version: 0.0.24+24` and
`caleemobile_git_sha: 41c97a97eddaf8676d43bb5efd5b2018d51b7faa` in
`config/release-platforms.yaml` (see `config/release-platforms.example.yaml`) —
NOT the 0.0.23 identity (already-shipped release main) and NOT the `3e43138`
PR-head SHA (never merged as-is).

> Tree equivalence is not a substitute for exact-SHA evidence: even if the
> merged tree matched the PR-head tree, the release gate proves selectors
> against the exact **commit** being released, so evidence must name `41c97a9`.

## Exact selector evidence for the merged dev head (release-candidate evidence)

Real CI selector-contract evidence for the merged dev head was produced by
dispatching the receiver `ci.yml` on receiver `main`, pinned to
`caleemobile_ref=41c97a97eddaf8676d43bb5efd5b2018d51b7faa` with
`expected_sha=41c97a9…` and `expected_version=0.0.24+24` (the run's
"Enforce expected identity when provided" step passed, so the checked-out SHA
and version were verified to match the release target):

| Build | CaleeMobile SHA | Receiver run ID | selector-contract job | Artifact ID | GitHub artifact digest | version | Flutter | Selectors |
|---|---|---|---|---|---|---|---|---|
| **merged dev (release candidate)** | `41c97a97eddaf8676d43bb5efd5b2018d51b7faa` | `29647533154` | success | `8430482479` | `sha256:ec78e501b640efd2bdbee372d92a33c4d149e8ada741cc85592bd7f4116099ac` | `0.0.24+24` | `3.44.1` | 62/62 PASS |

Verified contents (extracted from the artifact ZIP; the ZIP's raw-byte SHA-256
equals the GitHub digest above — see the retained bundle at
`baselines/selector-contract/merged-dev-41c97a9/`):

```json
{
  "schemaVersion": 1, "component": "caleemobile-selector-contract",
  "caleemobileRef": "41c97a97eddaf8676d43bb5efd5b2018d51b7faa",
  "testedSha": "41c97a97eddaf8676d43bb5efd5b2018d51b7faa",
  "pubspecVersion": "0.0.24+24", "flutterVersion": "3.44.1",
  "contract": "PASS", "selectorsChecked": 62, "selectorsPresent": 62, "missing": [],
  "timestamp": "2026-07-18T14:11:02Z",
  "regressionSha": "08d5eec88dc020256ecd5cf1715504e35a785f47",
  "workflowRunId": "29647533154", "generatedBy": "ci"
}
```

The retained bundle preserves the exact downloaded ZIP bytes
(`source-artifact.zip`), the exact extracted JSON bytes (`source-result.json`),
raw-byte SHA-256 sidecars for both, and an envelope-protected `provenance.json`
(Priority 3). Downloaded-ZIP raw-byte SHA-256 =
`ec78e501b640efd2bdbee372d92a33c4d149e8ada741cc85592bd7f4116099ac` = GitHub's
recorded artifact digest.

### Merged-commit CI verification (CaleeMobile Flutter CI)

The complete merged commit was independently verified by CaleeMobile's own
product CI (`.github/workflows/flutter-ci.yml` — `dart format --set-exit-if-changed`,
`flutter analyze --fatal-infos`, `flutter test` with `TZ=Australia/Perth`):

| Commit | Branch | Workflow | Run ID | Event | Conclusion |
|---|---|---|---|---|---|
| `41c97a97eddaf8676d43bb5efd5b2018d51b7faa` | `dev` | flutter-ci | `29646891292` | push | success |

So the merged dev head has both (a) exact-SHA selector-contract evidence
(receiver run `29647533154`) and (b) a green CaleeMobile product-CI run on the
same commit.

## PR-head evidence (NOT release-candidate evidence)

An earlier dispatch tested the `3e43138` **PR head** (before merge). It is kept
only as PR-head evidence — it must not be used as the release-candidate proof,
because `3e43138` is not the commit on `dev`:

| Build | CaleeMobile SHA | Receiver run ID | Artifact ID | GitHub artifact digest | version | Flutter | Selectors | Classification |
|---|---|---|---|---|---|---|---|---|
| PR-head #470 | `3e431382b6f658da794866bdcfcb87e14d8a3321` | `29641311999` | `8428705832` | `sha256:8914113e655bbe6f31710b812f5fa2aa2cd983970179187bb9cb062c47b4c39a` | `0.0.24+24` | `3.44.1` | 62/62 PASS | **PR-head only** |

Both proofs are distinct from the shipped-`main` `0.0.23` evidence: 62/62
selectors present for `0.0.24+24`, produced by CI. Only the merged-dev
`41c97a9` row above is release-candidate evidence.
