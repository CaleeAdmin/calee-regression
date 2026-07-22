# CI / validation tiers (Priority 10)

Three different things in this system produce a "green check," and they are
not interchangeable proof. This document names each tier, what it actually
proves, and where its evidence lives. It covers both **this repo's own CI**
(which tests the regression framework's own Python code) and **the actual
release run** (which certifies a product build) -- the two are easy to
conflate because both use the word "release."

## 1. PR validation

**Where:** `.github/workflows/framework-tests.yml`, triggered on
`pull_request` (and ordinary `push` to a non-`main` branch).
**Proves:** the PR *head* commit passes `python -m pytest`,
`coverage-report --check`, scenario-file validation, and `shellcheck`.
**Does NOT prove:** anything about what later lands on `main` -- GitHub's
merge queue can create a synthetic merge commit that differs from the PR
head, and a stale approved PR can be merged without CI re-running on the
actual merge result.

## 2. Main-commit validation

**Where:** the same `framework-tests.yml` job, on `push` to `refs/heads/main`
or `merge_group` (the merge-queue's merge commit). One unconditional job runs
identical gates on every trigger (see
`framework_tests/test_ci_workflow_evidence.py::test_push_to_main_runs_the_same_required_gates`),
so there's no separate "main-only" gate set to drift from PR checks.
**Proves:** the *exact* commit that landed on `main` (or the exact merge
commit the queue is about to land) ran those same gates, and the evidence
embeds that exact SHA:

- "Record framework-test evidence" writes `commitSha` (`GITHUB_SHA`),
  `runId`, `event`, `ref` to `framework-test-summary.json`.
- "Upload framework-test summary" retains it as
  `framework-test-summary-${{ github.sha }}` (90-day retention,
  `if-no-files-found: error`).
- "Merge-commit / main smoke check" (gated to `merge_group` /
  `refs/heads/main`) re-reads that summary and hard-fails if `commitSha`
  doesn't match `GITHUB_SHA`.

**Does NOT prove:** anything about a *product* release (Calee/CaleeShell/
CaleeMobile) -- this evidence is about the regression framework's own code
passing its own tests, not about any tablet/mobile build being certified.

**Independent re-verification (Priority 8).** The in-workflow "Merge-commit /
main smoke check" step above only proves what THAT SPECIFIC RUN believed
about itself -- it is not something a technical owner can re-check after the
fact without re-reading the workflow's own live log. `python -m
calee_regression verify-main-ci-evidence --expected-sha <full-main-sha>
--summary <downloaded-framework-test-summary.json>`
(`calee_regression/main_ci_evidence.py`) re-derives the SAME verdict
independently and offline, from a downloaded copy of the retained artifact:
exact commit SHA, a genuine `push`-to-`refs/heads/main` or `merge_group`
event (a `pull_request` event's evidence is rejected outright, however
clean), and every gate the evidence lists (there is only one, unconditional
job here; CaleeMobile-Regression's richer multi-gate `ci-summary.json` is
verified with the same command/module and its own `--required-gate` flags).

**This command must be run AFTER the pull request has actually merged** --
using the artifact from the Actions run that executed for the real merge
commit on `main` (or the merge-queue's synthetic merge commit), never a
PR-head run's evidence and never a SHA merely predicted before the merge
happened. A Claude Code session working on a not-yet-merged PR cannot
truthfully claim merged-main evidence was verified during that session; it
can only state that this command exists and name it as the next step for a
human (or a later, post-merge run) to execute against the real merge-commit
artifact.

## 3. Release-candidate certification

This tier is **not a GitHub Actions job on this repo**. It is the actual
release run, starting from `00 Run Calee Release Regression.command` (whose
own header comment documents the order: the release bundle is verified and
the effective release configuration is composed *before* any `adb install`,
reboot, or HOME mutation may occur -- see the `verify-release-bundle` CLI
command (`calee_regression/cli.py`, backed by
`calee_regression/release_installer.py`) and `docs/RELEASE_INSTALLER.md` for
what that verification checks), delegating to `06 Test Full Calee Solution.command`
and ending in `python -m calee_regression consolidate`. Its PASS/FAIL/BLOCKED
decision rule is `docs/RELEASE_POLICY.md`; per-build identity evidence (exact
SHAs, pubspec versions, artifact digests) is `docs/RELEASE_VERSION_IDENTITY.md`.

Certification is release-**bound**, not just commit-exact: the effective
release config (`release-config/results.json`) carries a `releaseId`, and
`python -m calee_regression selector-contract --expected-release-id <id>`
(auto-derived from that same run's own `release-config/results.json`)
rejects CaleeMobile selector evidence bound to a *different* release ID even
when the SHA/version happen to match (`calee_regression/selector_evidence.py`,
`framework_tests/test_selector_evidence.py`).

The CaleeMobile half of that evidence is itself produced by a tier-3 check in
the **sibling repo**: `CaleeMobile-Regression`'s `ci.yml` `release-certification-guard`
job, run via `workflow_dispatch`/`repository_dispatch` with `expected_sha` +
`expected_version` + `release_id` all supplied, which fails (never skips) on
any mismatch or on a missing `release_id`. See, in the sibling
`CaleeAdmin/CaleeMobile-Regression` repo (these two files live only there,
not in this repo): its own `docs/CI_VALIDATION_TIERS.md` for the full
three-tier breakdown on that side, and its `docs/CI_CROSS_REPO_TRIGGER.md`
for how the two repos connect.

## Summary

| Tier | Where enforced | Commit-exact? | Release-bound? | Fails loud on mismatch? |
|---|---|---|---|---|
| 1. PR validation | `framework-tests.yml` on `pull_request` | PR head only | no | n/a |
| 2. Main-commit validation | `framework-tests.yml` on `push`(main)/`merge_group` | yes (`framework-test-summary-<sha>`) | no | yes (smoke check) |
| 3. Release-candidate certification | the actual release run (`06 ...command` → `consolidate`), consuming CaleeMobile-Regression's own tier-3 CI evidence | yes | yes (`releaseId`) | yes (BLOCKED/FAIL, never silently PASS) |

A green tier-1 or tier-2 check on this repo's own commits is never, on its
own, evidence that a product release is certified -- certification requires
an actual release run whose evidence is bound to that release's ID.

## How release runs find tier-2/tier-3 evidence

A release run no longer needs a technical owner to hand-copy workflow run
IDs and artifact IDs: `acquire-release-evidence` derives the expected
identities from the verified release bundle, finds the exact matching runs
(tier 2 for both regression repos by exact merged-main SHA; tier 3 selector
certification by exact CaleeMobile SHA + version + release ID), and
authenticates each artifact against its run and GitHub-recorded digest. A
tier-2/tier-3 run that merely *exists* but doesn't match the exact identity
is rejected; "the latest successful run" is never used. Manual run/artifact
IDs remain available as diagnostic overrides only. See
`docs/CONFIGURATION_AND_QUALIFICATION.md` §"Automatic exact-identity
evidence acquisition".
