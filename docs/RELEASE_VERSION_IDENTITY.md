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

## Regenerated expected build identity

Produced by `python -m calee_regression build-identity --caleemobile-source
../CaleeMobile` against the bumped checkout:

```
AUTO_CALEEMOBILE_IDENTITY_AVAILABLE=true
AUTO_CALEEMOBILE_DIRTY=false
AUTO_CALEEMOBILE_BUILD_VERSION=0.0.24+24
AUTO_CALEEMOBILE_GIT_SHA=3e431382b6f658da794866bdcfcb87e14d8a3321
```

So the expected release identity for the reminder-enabled build is:

- **version** `0.0.24+24`
- **CaleeMobile SHA** `3e431382b6f658da794866bdcfcb87e14d8a3321`

A production consolidation should pin `caleemobile_build_version: 0.0.24+24` and
`caleemobile_git_sha: 3e431382b6f658da794866bdcfcb87e14d8a3321` in
`config/release-platforms.yaml` (see `config/release-platforms.example.yaml`) —
NOT the 0.0.23 identity, which belongs to the already-shipped release main.

## Exact selector evidence for the new identity

Real CI selector-contract evidence for the new SHA/version was produced by
dispatching the receiver `ci.yml` pinned to `3e43138`
(`expected_version=0.0.24+24`):

| Build | CaleeMobile SHA | Receiver run ID | Artifact ID | GitHub artifact digest | version | Flutter | Selectors |
|---|---|---|---|---|---|---|---|
| reminder-enabled dev | `3e431382b6f658da794866bdcfcb87e14d8a3321` | `29641311999` | `8428705832` | `sha256:8914113e655bbe6f31710b812f5fa2aa2cd983970179187bb9cb062c47b4c39a` | `0.0.24+24` | `3.44.1` | 62/62 PASS |

Verified contents (byte-for-byte from the artifact):

```json
{
  "schemaVersion": 1, "component": "caleemobile-selector-contract",
  "caleemobileRef": "3e431382b6f658da794866bdcfcb87e14d8a3321",
  "testedSha": "3e431382b6f658da794866bdcfcb87e14d8a3321",
  "pubspecVersion": "0.0.24+24", "flutterVersion": "3.44.1",
  "contract": "PASS", "selectorsChecked": 62, "selectorsPresent": 62, "missing": [],
  "timestamp": "2026-07-18T10:43:47Z",
  "regressionSha": "25f47d3671cfd4b1311132a5ab9cb9344880d6cd",
  "workflowRunId": "29641311999", "generatedBy": "ci"
}
```

The new identity's selector proof is therefore distinct from — and does not
reuse — the 0.0.23 evidence: 62/62 selectors present for
`0.0.24+24 @ 3e43138`, produced by CI.
