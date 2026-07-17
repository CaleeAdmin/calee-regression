# Branch-protection status (Workstream 3 verification)

_Last verified: 2026-07-17, from the regression session (read-only GitHub access)._

Workstream 3 requires verifying whether branch protection **actually** requires the
`Format, Analyze & Test` status check (job name under the **Flutter CI** workflow,
`CaleeMobile/.github/workflows/flutter-ci.yml`) on `dev`, `stage` and `main`, and reporting a
configuration gap clearly if repository settings cannot be changed from the session.

## Finding — CONFIGURATION GAP (BLOCKED)

`CaleeAdmin/CaleeMobile` branch protection queried this session:

| Branch | Protected? | `Format, Analyze & Test` required to merge? |
|---|---|---|
| `dev`   | **No** (`protected: false`) | **No** |
| `stage` | **No** (`protected: false`) | **No** |
| `main`  | **No** (`protected: false`) | **No** |

None of the three integration branches has any branch-protection rule, so the required
status check is **not** enforced: a pull request (or a direct push) can merge into `dev`,
`stage` or `main` without a green `Format, Analyze & Test` run. The CI workflow itself runs on
PRs and pushes to all three branches (so the signal exists), but nothing **blocks a merge** on it.

This is a repository-settings gap, not a code gap — it **cannot be fixed from this session**
(changing branch protection needs repository-admin access to
`Settings → Branches → Branch protection rules`, which the GitHub App scope available here does
not grant, and the GitHub tooling available here exposes no branch-protection write API).

## Required remediation (repository administrator)

For each of `dev`, `stage`, and `main` on `CaleeAdmin/CaleeMobile`:

1. `Settings → Branches → Branch protection rules → Add rule` (branch name pattern = the branch).
2. Enable **Require status checks to pass before merging**.
3. Add the status check **`Format, Analyze & Test`** (it appears once the Flutter CI workflow has
   run at least once on a PR to that branch).
4. Recommended: also enable **Require a pull request before merging** so the push-time gate can't
   be bypassed by a direct push.

Until this is applied, treat "CI is green on the PR" as advisory, not enforced, and do not rely on
branch protection to keep unformatted/failing code off `dev`/`stage`/`main`.
