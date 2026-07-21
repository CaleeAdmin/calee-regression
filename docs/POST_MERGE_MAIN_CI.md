# Post-Merge Main-CI Evidence — Technical-Owner Procedure

A coding/PR session can only ever verify **PR-head CI**. The merge commit on
`main` does not exist until after the merge, so authenticated *post-merge*
main-CI evidence can never be produced from inside the PR itself — any claim
otherwise is fabricated. This document is the technical-owner procedure for
authenticating merged-main CI for **both** regression repositories after a
merge, using the existing authenticated verifier
(`calee_regression/main_ci_artifact.py`, surfaced through
`qualification-preflight`'s `--calee-regression-main-*` /
`--caleemobile-regression-main-*` flags and `verify-main-ci-artifact`).

## Procedure (run once per repository, after the merge)

For each of `CaleeAdmin/calee-regression` and
`CaleeAdmin/CaleeMobile-Regression`:

1. **Retrieve the exact push-to-main (or merge-group) workflow run** for the
   merge commit — from the repository's Actions tab or
   `GET /repos/<repo>/actions/runs?branch=main&head_sha=<merge-sha>`.
2. **Retrieve its retained summary artifact** (the run's own uploaded
   main-CI summary artifact ID).
3. Run the authenticated verifier with those IDs. It verifies, fail-closed:
   - the **repository** matches the expected profile;
   - the **workflow path** matches the profile's pinned workflow file (a
     workflow *name* never substitutes for its path);
   - the run's **branch/ref is `main`**;
   - the run's **head SHA is the exact merge SHA**;
   - the run **completed with conclusion `success`**;
   - the **artifact belongs to that run**, is not expired, and its
     downloaded bytes match the **GitHub-recorded digest**;
   - the contained **summary schema and required gates** (for
     CaleeMobile-Regression, the canonical required-gate list in
     `main_ci_evidence.py`).

Example (both repositories in one preflight):

```bash
python -m calee_regression qualification-preflight \
  --calee-regression-main-sha <merge-sha-1> \
  --calee-regression-main-workflow-run-id <run-id-1> \
  --calee-regression-main-artifact-id <artifact-id-1> \
  --caleemobile-regression-main-sha <merge-sha-2> \
  --caleemobile-regression-main-workflow-run-id <run-id-2> \
  --caleemobile-regression-main-artifact-id <artifact-id-2>
```

A GitHub token with read access must be resolvable
(`REGRESSION_API_TOKEN`/`GITHUB_TOKEN`/`GH_TOKEN`).

## What a session report must say

Until the post-merge run genuinely exists and its artifacts have been
retrieved and verified as above, the honest status line is:

```
PR-head CI: verified
Post-merge main CI: pending until after merge
```

Never record run IDs or artifact IDs that were not actually retrieved.
