"""Authenticated verification of MERGED-MAIN CI artifacts (Priority 6).

``main_ci_evidence.py`` (Priority 5) only proves a downloaded JSON file is
INTERNALLY consistent -- correct schema, matching SHA, every canonical gate
accounted for. It never proves GitHub Actions actually produced that file:
an internally-consistent ``ci-summary.json`` is trivial to hand-author. This
module is the authenticated origin layer, reusing ``github_artifact.py``'s
GitHub-API primitives (the SAME chain design Priority 2 built for selector
evidence: a pure, network-free verification core plus a thin live-acquisition
layer that is BLOCKED, never faked, without credentials) instead of
duplicating them.

The chain differs from selector evidence's in the ways that matter for
"proof this is what actually landed on the target branch":

  * the triggering event must be ``push`` (to ``refs/heads/main``) or
    ``merge_group`` -- NOT ``workflow_dispatch`` (selector evidence wants a
    deliberate dispatch; main-CI evidence wants an ORGANIC merge/push, and
    explicitly distrusts anything else, mirroring
    ``main_ci_evidence.verify_main_ci_evidence``'s own event/ref checks);
  * the artifact name is expected to CONTAIN the exact merge SHA (GitHub
    Actions' own ``upload-artifact`` step names it
    ``ci-summary-${{ github.sha }}`` / ``framework-test-summary-${{
    github.sha }}``), not a fixed name;
  * there is no single "job marker" to require -- the run's overall
    ``conclusion`` plus the extracted evidence's own ``gates`` breakdown
    (independently validated against the canonical required-gate set by
    :func:`main_ci_evidence.verify_main_ci_evidence`) together prove every
    required gate passed, so this module never needs a separate per-job API
    call.

Composition, not duplication: once the ZIP is authenticated (repository,
workflow path, run success, artifact ownership, digest match) and the single
expected summary is safely extracted, the extracted JSON is handed to
:func:`main_ci_evidence.verify_main_ci_evidence` for the FULL content/schema/
gate verdict -- the same canonical verifier the offline (structural-only)
command uses. A structural fault (unreadable metadata, missing credentials,
a malformed ZIP) raises :class:`MainCiArtifactError` (BLOCKED); a rule
violation (wrong repo, wrong SHA, a failed gate) is returned in the verdict's
``problems`` list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import github_artifact as ga
from . import main_ci_evidence as mce
from .identity_format import is_full_git_sha

# The only two events that can describe what's ACTUALLY landing on the
# target branch -- see main_ci_evidence.py's module docstring. Deliberately
# the OPPOSITE requirement from github_artifact.PRODUCTION_DISPATCH_EVENTS
# (selector evidence wants a deliberate workflow_dispatch; this wants an
# organic push/merge-queue event).
MAIN_CI_APPROVED_EVENTS = frozenset({mce.MAIN_EVENT_PUSH, mce.MAIN_EVENT_MERGE_GROUP})

# Known repository -> (workflow path, artifact-name prefix, in-ZIP result
# filename) profiles, so the common case only needs --repository. An
# unrecognised repository requires the three overrides to be given
# explicitly (see the CLI command) rather than guessing.
KNOWN_PROFILES = {
    mce.CALEEMOBILE_REGRESSION_REPOSITORY: {
        "workflow_path": mce.CALEEMOBILE_REGRESSION_WORKFLOW_FILE,
        "artifact_prefix": "ci-summary-",
        "result_filename": "ci-summary.json",
    },
    "CaleeAdmin/calee-regression": {
        "workflow_path": ".github/workflows/framework-tests.yml",
        "artifact_prefix": "framework-test-summary-",
        "result_filename": "framework-test-summary.json",
    },
}


class MainCiArtifactError(Exception):
    """The chain could not be *evaluated* -- missing credentials, unreadable
    metadata, a malformed ZIP. A framework/pipeline fault (BLOCKED), never a
    silent pass. A *rule* violation is a verdict, not this."""


@dataclass
class MainCiArtifactChain:
    """The result of evaluating the authenticated merged-main artifact
    chain."""

    ok: bool
    problems: "list[str]" = field(default_factory=list)
    run: "ga.WorkflowRunMetadata | None" = None
    artifact: "ga.ArtifactMetadata | None" = None
    zip_bytes: "bytes | None" = None
    zip_sha256: "str | None" = None
    result_bytes: "bytes | None" = None
    result_sha256: "str | None" = None
    result: "dict[str, Any] | None" = None

    def summary(self) -> str:
        if self.ok:
            sha = (self.result or {}).get("commitSha", "?")
            run_id = self.run.run_id if self.run else "?"
            return f"Authenticated merged-main CI artifact verified (commit {sha}, run {run_id})."
        return "Authenticated merged-main CI artifact REJECTED: " + " ".join(self.problems)


def verify_main_ci_artifact_chain(
    run: "ga.WorkflowRunMetadata",
    artifact: "ga.ArtifactMetadata",
    zip_bytes: bytes,
    *,
    expected_repository: str,
    expected_workflow_path: str,
    expected_merge_sha: str,
    expected_artifact_name: str,
    expected_result_filename: str,
    expected_run_id: "str | None" = None,
    expected_artifact_id: "str | None" = None,
    required_gates: "list[str] | None" = None,
    canonical_required_gates: "tuple[str, ...] | list[str] | None" = None,
    max_zip_bytes: int = ga.MAX_ARTIFACT_ZIP_BYTES,
) -> MainCiArtifactChain:
    """Enforce the full authenticity + content chain over already-fetched
    inputs. Pure and fully unit-testable: no network, no filesystem.

    Enforces, in order: run belongs to the expected repository; workflow is
    the expected file (by PATH, never by display name); the triggering event
    is push-or-merge_group (never a workflow_dispatch/pull_request run); the
    run completed with conclusion success; the run's head_sha equals the
    expected merge SHA; the artifact records this run as its owner; the
    artifact's name equals the expected (SHA-embedding) name; GitHub's
    recorded digest matches the downloaded ZIP's actual sha256; the ZIP
    contains EXACTLY the one expected summary file (hardened extraction,
    Priority 2's existing rules: no path traversal, no duplicates, no extra
    files); and finally the extracted summary itself passes
    :func:`main_ci_evidence.verify_main_ci_evidence` -- the same canonical
    schema/gate verdict the offline (structural-only) command produces.
    """
    problems: "list[str]" = []

    if not is_full_git_sha((expected_merge_sha or "").strip()):
        problems.append(f"expected merge SHA {expected_merge_sha!r} is not a full 40-character SHA.")

    if expected_run_id is not None and run.run_id is not None and str(run.run_id) != str(expected_run_id):
        problems.append(f"workflow run id {run.run_id!r} != requested run id {expected_run_id!r}.")
    if (run.repo_full_name or "").strip() != expected_repository:
        problems.append(f"workflow run repository {run.repo_full_name!r} != expected {expected_repository!r}.")
    # The workflow is identified by PATH exactly -- a workflow *name* never
    # substitutes for it (any repo can name a workflow "ci"), mirroring
    # github_artifact.py's P7.1 rule.
    if (run.workflow_path or "").strip() != expected_workflow_path:
        problems.append(
            f"workflow path {run.workflow_path!r} != expected {expected_workflow_path!r} -- a workflow "
            f"NAME never substitutes for its path."
        )
    if (run.event or "").strip() not in MAIN_CI_APPROVED_EVENTS:
        problems.append(
            f"workflow run event {run.event!r} is not push-to-main or merge_group "
            f"({sorted(MAIN_CI_APPROVED_EVENTS)}) -- a workflow_dispatch/pull_request/schedule run's "
            f"trigger does not describe what actually landed on the target branch."
        )
    if (run.status or "").strip().lower() != "completed":
        problems.append(f"workflow run has not completed (status={run.status!r}).")
    if (run.conclusion or "").strip().lower() != "success":
        problems.append(f"workflow run conclusion {run.conclusion!r} != 'success'.")
    if not (run.head_sha or "").strip():
        problems.append("workflow run has no head_sha -- cannot tie this run to a commit.")
    elif run.head_sha.strip().lower() != (expected_merge_sha or "").strip().lower():
        problems.append(
            f"workflow run head_sha {run.head_sha!r} != expected merge SHA {expected_merge_sha!r} -- "
            f"this run did not test the commit being verified."
        )

    # --- artifact ownership + identity --------------------------------
    if expected_artifact_id is not None and artifact.artifact_id is not None and str(artifact.artifact_id) != str(expected_artifact_id):
        problems.append(f"artifact id {artifact.artifact_id!r} != requested artifact id {expected_artifact_id!r}.")
    if artifact.workflow_run_id is None:
        problems.append(
            "artifact metadata does not record its workflow_run id -- cannot confirm the artifact "
            "belongs to the verified run."
        )
    elif run.run_id is not None and str(artifact.workflow_run_id) != str(run.run_id):
        problems.append(f"artifact belongs to run {artifact.workflow_run_id!r}, not the verified run {run.run_id!r}.")
    if (artifact.name or "").strip() != expected_artifact_name:
        problems.append(
            f"artifact name {artifact.name!r} != expected {expected_artifact_name!r} (the expected name "
            f"embeds the exact merge SHA)."
        )
    if artifact.expired is True:
        problems.append("artifact is expired -- its bytes are no longer retrievable/trustworthy.")
    digest_hex = ga._normalise_digest(artifact.digest)
    if digest_hex is None:
        problems.append("artifact has no GitHub digest -- cannot content-address the downloaded bytes.")

    # --- ZIP bytes: size + digest --------------------------------------
    zip_sha = ga.sha256_hex(zip_bytes)
    if len(zip_bytes) > max_zip_bytes:
        problems.append(f"downloaded artifact ZIP is {len(zip_bytes)} bytes, over the {max_zip_bytes}-byte limit.")
    if artifact.size_in_bytes is not None and len(zip_bytes) != artifact.size_in_bytes:
        problems.append(
            f"downloaded ZIP is {len(zip_bytes)} bytes but GitHub records size_in_bytes="
            f"{artifact.size_in_bytes} -- the download is incomplete or altered."
        )
    if digest_hex is not None and zip_sha != digest_hex:
        problems.append(
            f"downloaded ZIP sha256 {zip_sha} != GitHub artifact digest sha256:{digest_hex} -- "
            f"the bytes do not match what GitHub stored."
        )

    # --- hardened extraction of exactly one summary --------------------
    result_bytes: "bytes | None" = None
    result: "dict[str, Any] | None" = None
    result_sha: "str | None" = None
    try:
        result_bytes, result = ga.extract_single_result(zip_bytes, expected_name=expected_result_filename)
        result_sha = ga.sha256_hex(result_bytes)
    except ga.GithubArtifactError as exc:
        problems.append(str(exc))

    # --- content/schema/gate verdict (Priority 5's canonical verifier) -
    if result is not None:
        problems.extend(mce.verify_main_ci_evidence(
            result, expected_sha=expected_merge_sha, required_gates=required_gates,
            expected_repository=expected_repository, expected_workflow_file=expected_workflow_path,
            canonical_required_gates=canonical_required_gates,
        ))

    return MainCiArtifactChain(
        ok=not problems,
        problems=problems,
        run=run,
        artifact=artifact,
        zip_bytes=zip_bytes,
        zip_sha256=zip_sha,
        result_bytes=result_bytes,
        result_sha256=result_sha,
        result=result,
    )


def acquire_main_ci_artifact(
    *,
    repository: str,
    workflow_path: str,
    run_id: "str | None",
    artifact_id: "str | None",
    expected_merge_sha: str,
    expected_artifact_name: str,
    expected_result_filename: str,
    local_zip_path: "str | None" = None,
    required_gates: "list[str] | None" = None,
    canonical_required_gates: "tuple[str, ...] | list[str] | None" = None,
    json_fetcher: "ga.JsonFetcher | None" = None,
    bytes_fetcher: "ga.BytesFetcher | None" = None,
    token: "str | None" = None,
    env: "dict[str, str] | None" = None,
) -> MainCiArtifactChain:
    """Acquire and verify authenticated merged-main CI evidence from a
    GitHub run + artifact. Mirrors
    ``github_artifact.acquire_github_artifact``'s shape/credential-BLOCKED
    behaviour exactly, reusing its live HTTP fetchers and token resolution
    rather than duplicating them.

    Requires BOTH a run id and an artifact id -- evidence with no artifact id
    or no run id cannot be authenticated and is refused. Raises
    :class:`MainCiArtifactError` (mapped to BLOCKED by the caller) when a
    required id is missing, when no credentials/fetcher is available (naming
    the exact missing secret), or when the ZIP is structurally unreadable.
    """
    if not ga._opt_str(run_id):
        raise MainCiArtifactError(
            "authenticated merged-main CI evidence requires a GitHub workflow run id (--workflow-run-id); "
            "a self-declared runId in a JSON file is not proof."
        )
    if not ga._opt_str(artifact_id):
        raise MainCiArtifactError(
            "authenticated merged-main CI evidence requires a GitHub artifact id (--artifact-id); "
            "a bare --summary JSON cannot be authenticated."
        )

    effective_token = token if token is not None else ga.resolve_token(env)
    if json_fetcher is None or bytes_fetcher is None:
        if not effective_token:
            missing = " or ".join(ga.TOKEN_ENV_VARS)
            raise MainCiArtifactError(
                f"BLOCKED: no GitHub API credentials available to authenticate the artifact (set one of "
                f"{missing} to a token with read access to {repository}). Without it the run/artifact "
                f"ownership and the artifact digest cannot be verified, so the evidence cannot be accepted "
                f"as authenticated merged-main evidence."
            )
        if json_fetcher is None:
            json_fetcher = ga._make_live_json_fetcher(effective_token)
        if bytes_fetcher is None:
            bytes_fetcher = ga._make_live_bytes_fetcher(effective_token)

    base = ga._api_base()
    run_data = json_fetcher(f"{base}/repos/{repository}/actions/runs/{run_id}")
    run = ga.WorkflowRunMetadata.from_api(run_data)
    art_data = json_fetcher(f"{base}/repos/{repository}/actions/artifacts/{artifact_id}")
    artifact = ga.ArtifactMetadata.from_api(art_data)

    if local_zip_path:
        try:
            with open(local_zip_path, "rb") as fh:
                zip_bytes = fh.read(ga.MAX_ARTIFACT_ZIP_BYTES + 1)
        except OSError as exc:
            raise MainCiArtifactError(f"could not read artifact ZIP {local_zip_path}: {exc}") from exc
    else:
        download_url = artifact.archive_download_url or f"{base}/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        zip_bytes = bytes_fetcher(download_url)

    return verify_main_ci_artifact_chain(
        run, artifact, zip_bytes,
        expected_repository=repository, expected_workflow_path=workflow_path,
        expected_merge_sha=expected_merge_sha, expected_artifact_name=expected_artifact_name,
        expected_result_filename=expected_result_filename,
        expected_run_id=run_id, expected_artifact_id=artifact_id,
        required_gates=required_gates, canonical_required_gates=canonical_required_gates,
    )
