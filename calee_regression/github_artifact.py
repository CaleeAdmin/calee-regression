"""GitHub artifact authenticity chain for production selector evidence
(Priority 2).

Production must NOT accept an arbitrary JSON file that merely *claims* to have
come from CI:

    {"generatedBy": "ci", "workflowRunId": "123", "regressionSha": "aaaa..."}

Any file can say that. Real proof that selector evidence came from the expected
CI run requires tying it back to GitHub's own record of the run and the artifact
it produced, and to the artifact's content-addressed digest. This module is that
chain. It has two layers, deliberately separated so the *policy* is fully
unit-testable without any network:

  * a pure verification core -- :func:`verify_github_artifact_chain` -- that,
    given already-fetched workflow-run metadata, the run's jobs, artifact
    metadata and the downloaded ZIP bytes, enforces every Priority-2 rule and
    returns a verdict (never raises for a *rule* failure -- that is a verdict,
    like an identity mismatch);
  * a thin live-acquisition layer -- :func:`acquire_github_artifact` -- that
    fetches those inputs from the GitHub REST API (or accepts an
    operator-supplied ``--github-artifact-zip`` for the byte layer) and then runs
    the same core. When API credentials are unavailable it returns **BLOCKED**
    naming the exact missing secret; it never fabricates a pass.

Chain enforced (all must hold, else the verdict is not ok):

  workflow run
    * repository is exactly ``CaleeAdmin/CaleeMobile-Regression``;
    * the workflow is the expected ``ci.yml`` **by path exactly** -- a workflow
      *name* of ``ci`` is diagnostic only and never substitutes for the path
      (P7.1: any repo can name a workflow ``ci``);
    * the triggering event is ``workflow_dispatch``; ``repository_dispatch`` is
      accepted ONLY in explicit legacy-evidence mode
      (``allow_legacy_repository_dispatch=True``) (P7.2);
    * the run has ``status == completed`` and ``conclusion == success`` (P7.3);
    * the run's ``head_sha`` equals the evidence ``regressionSha`` (which must be
      a full 40-char SHA) (P7.7);
    * EXACTLY ONE selector-contract job is present, and it is ``completed`` +
      ``success`` -- duplicate selector-like jobs are refused (P7.3/P7.4).
  artifact
    * the artifact metadata RECORDS the workflow run it belongs to, and that is
      the verified run -- a missing relationship is refused (P7.5);
    * its name is exactly ``selector-contract-result``;
    * it is not expired;
    * a GitHub ``digest`` (``sha256:...``) is present.
  bytes
    * the downloaded ZIP is not oversized and (when known) matches the
      artifact's ``size_in_bytes``;
    * ``sha256(zip bytes)`` equals GitHub's ``digest``.
  extraction (hardened -- see :func:`extract_single_result`)
    * exactly one expected result file, no extra files, no duplicate entries,
      no path traversal, not oversized, not a malformed ZIP.
  extracted identity
    * the extracted evidence's ``regressionSha`` equals the run ``head_sha``;
    * (when a release target is supplied) the extracted ``testedSha`` /
      ``pubspecVersion`` match it.

A *structural* problem (unreadable metadata, missing credentials, a malformed
ZIP) is raised as :class:`GithubArtifactError` (BLOCKED). A *rule* failure is
returned in the verdict's ``problems`` -- both keep a release from PASSing, but
only the latter is a legitimate "this evidence is not for this build" verdict.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .identity_format import is_full_git_sha

# --- expected identities (the one CI pipeline this chain trusts) -------------

EXPECTED_WORKFLOW_REPO = "CaleeAdmin/CaleeMobile-Regression"
EXPECTED_WORKFLOW_PATH = ".github/workflows/ci.yml"
# DIAGNOSTIC ONLY (P7.1): the run is identified by EXPECTED_WORKFLOW_PATH. A
# workflow *name* of "ci" never substitutes for the path -- any repository can
# name a workflow "ci".
EXPECTED_WORKFLOW_NAME = "ci"
# P7.2: new production evidence must come from a deliberate workflow_dispatch.
# A push/pull_request/schedule run is not an approved dispatch event.
PRODUCTION_DISPATCH_EVENTS = frozenset({"workflow_dispatch"})
# repository_dispatch is accepted ONLY in explicit legacy-evidence mode
# (allow_legacy_repository_dispatch=True). The older cross-repo trigger used it;
# new evidence must not.
LEGACY_DISPATCH_EVENTS = frozenset({"workflow_dispatch", "repository_dispatch"})
# Backwards-compatible alias (the legacy set) for any external reader.
APPROVED_DISPATCH_EVENTS = LEGACY_DISPATCH_EVENTS
# The selector-contract job's name contains this (case-insensitive). Matching a
# substring rather than the full title survives cosmetic name edits while still
# pinning the *selector contract* job specifically, not just any green job.
SELECTOR_CONTRACT_JOB_MARKER = "selector contract"
EXPECTED_ARTIFACT_NAME = "selector-contract-result"
# The single JSON the artifact is expected to contain.
EXPECTED_RESULT_FILENAME = "selector-contract-result.json"

# Oversized guards. The real artifact is <500 bytes; a selector-contract result
# is a small JSON. A ZIP or member far larger than this is not our artifact and
# is refused rather than read into memory (a zip-bomb / wrong-artifact guard).
MAX_ARTIFACT_ZIP_BYTES = 1_000_000
MAX_EXTRACTED_MEMBER_BYTES = 4_000_000

# Environment variables a live fetch will read a token from, in order. Named so
# a BLOCKED message can tell the operator exactly which secret to provide.
TOKEN_ENV_VARS = ("REGRESSION_API_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


class GithubArtifactError(Exception):
    """The chain could not be *evaluated* -- missing credentials, unreadable
    metadata, a malformed ZIP. A framework/pipeline fault (BLOCKED), never a
    silent pass. A *rule* violation is a verdict, not this."""


# --- metadata value objects (decoupled from the GitHub JSON shape) ----------


@dataclass
class WorkflowRunMetadata:
    run_id: "str | None" = None
    repo_full_name: "str | None" = None
    workflow_path: "str | None" = None
    workflow_name: "str | None" = None
    event: "str | None" = None
    head_sha: "str | None" = None
    # The run's actual branch, as GitHub's own "get a workflow run" API
    # response exposes it (``head_branch``) -- an authenticated signal
    # independent of (and cross-checked against) the caller-supplied
    # ``event``/expected-ref assumptions. ``None`` only when the API response
    # itself omits it (never guessed/defaulted).
    head_branch: "str | None" = None
    status: "str | None" = None
    conclusion: "str | None" = None

    @classmethod
    def from_api(cls, data: "dict[str, Any]") -> "WorkflowRunMetadata":
        if not isinstance(data, dict):
            raise GithubArtifactError("workflow-run metadata is not a JSON object.")
        repo = data.get("repository")
        repo_full = None
        if isinstance(repo, dict):
            repo_full = repo.get("full_name")
        return cls(
            run_id=_opt_str(data.get("id")),
            repo_full_name=_opt_str(repo_full),
            workflow_path=_opt_str(data.get("path")),
            workflow_name=_opt_str(data.get("name")),
            event=_opt_str(data.get("event")),
            head_sha=_opt_str(data.get("head_sha")),
            head_branch=_opt_str(data.get("head_branch")),
            status=_opt_str(data.get("status")),
            conclusion=_opt_str(data.get("conclusion")),
        )


@dataclass
class JobMetadata:
    name: "str | None" = None
    status: "str | None" = None
    conclusion: "str | None" = None

    @classmethod
    def from_api(cls, data: "dict[str, Any]") -> "JobMetadata":
        return cls(
            name=_opt_str(data.get("name")),
            status=_opt_str(data.get("status")),
            conclusion=_opt_str(data.get("conclusion")),
        )


@dataclass
class ArtifactMetadata:
    artifact_id: "str | None" = None
    name: "str | None" = None
    expired: "bool | None" = None
    size_in_bytes: "int | None" = None
    digest: "str | None" = None
    workflow_run_id: "str | None" = None
    archive_download_url: "str | None" = None

    @classmethod
    def from_api(cls, data: "dict[str, Any]") -> "ArtifactMetadata":
        if not isinstance(data, dict):
            raise GithubArtifactError("artifact metadata is not a JSON object.")
        wr = data.get("workflow_run")
        wr_id = wr.get("id") if isinstance(wr, dict) else None
        expired = data.get("expired")
        return cls(
            artifact_id=_opt_str(data.get("id")),
            name=_opt_str(data.get("name")),
            expired=bool(expired) if expired is not None else None,
            size_in_bytes=_opt_int(data.get("size_in_bytes")),
            digest=_opt_str(data.get("digest")),
            workflow_run_id=_opt_str(wr_id),
            archive_download_url=_opt_str(data.get("archive_download_url")),
        )


@dataclass
class GithubArtifactChain:
    """The result of evaluating the authenticity chain."""

    ok: bool
    problems: "list[str]" = field(default_factory=list)
    run: "WorkflowRunMetadata | None" = None
    artifact: "ArtifactMetadata | None" = None
    # Raw, unmodified bytes and their raw-byte digests (feed Priority 3).
    zip_bytes: "bytes | None" = None
    zip_sha256: "str | None" = None
    result_bytes: "bytes | None" = None
    result_sha256: "str | None" = None
    # The extracted JSON, parsed (for identity verification / adoption).
    result: "dict[str, Any] | None" = None

    def summary(self) -> str:
        if self.ok:
            where = (self.result or {}).get("testedSha", "?")
            return f"GitHub artifact authenticity chain verified (artifact {self.artifact.artifact_id if self.artifact else '?'}, tested {where})."
        return "GitHub artifact authenticity chain REJECTED: " + " ".join(self.problems)


# --- hardened ZIP extraction -------------------------------------------------


def _member_is_safe(name: str) -> bool:
    """A ZIP member name is safe only when it is a single, plain filename in the
    archive root: no directory components, no traversal, no absolute path, no
    drive/backslash. The artifact is a single flat JSON, so anything else is a
    tampered or wrong artifact and is refused."""
    if not name or name != name.strip():
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    if name.startswith(("/", "~")):
        return False
    if ".." in name:
        return False
    # A drive-letter or NTFS ADS colon has no place in our artifact.
    if ":" in name:
        return False
    return True


def extract_single_result(
    zip_bytes: bytes,
    *,
    expected_name: str = EXPECTED_RESULT_FILENAME,
    max_member_bytes: int = MAX_EXTRACTED_MEMBER_BYTES,
) -> "tuple[bytes, dict[str, Any]]":
    """Extract *exactly one* expected result file from an artifact ZIP, safely.

    Returns ``(raw_bytes, parsed_json)``. The raw bytes are the member's exact
    stored content -- never reparsed/reserialised -- so Priority 3 can hash and
    preserve them byte-for-byte.

    Raises :class:`GithubArtifactError` (BLOCKED) for a malformed ZIP, a member
    that fails the path-safety check, a duplicate entry, an oversized member,
    missing/extra files, or content that is not the expected JSON object.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise GithubArtifactError(f"artifact is not a valid ZIP archive: {exc}") from exc

    with zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]

        # Path-traversal / unsafe-name guard, before anything is read.
        for name in names:
            if not _member_is_safe(name):
                raise GithubArtifactError(
                    f"artifact ZIP member {name!r} is unsafe (path traversal, directory, "
                    f"or absolute path) -- refusing to extract."
                )

        # Duplicate-entry guard: a ZIP may carry two members with the same name;
        # a later one could shadow an inspected earlier one. Reject outright.
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise GithubArtifactError(f"artifact ZIP has duplicate entries: {dupes}.")

        # Exactly the one expected result file -- no more, no fewer. Extra files
        # (even harmless-looking ones) mean this is not the clean single-result
        # artifact the contract emits.
        if names != [expected_name]:
            raise GithubArtifactError(
                f"artifact ZIP must contain exactly one file {expected_name!r}; "
                f"found {names!r}."
            )

        info = infos[0]
        # Oversized guard using the declared size first (cheap), then enforce
        # again on the actual read (a lying header can't smuggle a bomb through).
        if info.file_size > max_member_bytes:
            raise GithubArtifactError(
                f"artifact member {expected_name!r} declares {info.file_size} bytes, over the "
                f"{max_member_bytes}-byte limit."
            )
        with zf.open(info) as fh:
            raw = fh.read(max_member_bytes + 1)
        if len(raw) > max_member_bytes:
            raise GithubArtifactError(
                f"artifact member {expected_name!r} exceeds the {max_member_bytes}-byte limit when read."
            )

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GithubArtifactError(f"artifact member {expected_name!r} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise GithubArtifactError(f"artifact member {expected_name!r} is not a JSON object.")
    return raw, parsed


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalise_digest(digest: "str | None") -> "str | None":
    """Return the bare hex of a ``sha256:<hex>`` (or plain hex) digest, lowercased."""
    if not digest:
        return None
    text = str(digest).strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    return text or None


# --- pure verification core --------------------------------------------------


def verify_github_artifact_chain(
    run: WorkflowRunMetadata,
    jobs: "list[JobMetadata]",
    artifact: ArtifactMetadata,
    zip_bytes: bytes,
    *,
    expected_regression_sha: "str | None" = None,
    expected_tested_sha: "str | None" = None,
    expected_version: "str | None" = None,
    expected_run_id: "str | None" = None,
    expected_artifact_id: "str | None" = None,
    allow_legacy_repository_dispatch: bool = False,
    max_zip_bytes: int = MAX_ARTIFACT_ZIP_BYTES,
) -> GithubArtifactChain:
    """Enforce the full authenticity chain over already-fetched inputs.

    Pure and fully unit-testable: no network, no filesystem. Returns a verdict
    listing every problem. A malformed ZIP (a structural fault) is still raised
    as :class:`GithubArtifactError` from :func:`extract_single_result`; callers
    that want a verdict-only surface catch it and treat it as a BLOCK.
    """
    problems: "list[str]" = []

    # --- workflow run identity ---------------------------------------------
    if expected_run_id is not None and run.run_id is not None and str(run.run_id) != str(expected_run_id):
        problems.append(
            f"workflow run id {run.run_id!r} != requested run id {expected_run_id!r}."
        )
    if (run.repo_full_name or "").strip() != EXPECTED_WORKFLOW_REPO:
        problems.append(
            f"workflow run repository {run.repo_full_name!r} != expected {EXPECTED_WORKFLOW_REPO!r}."
        )
    # P7.1: the workflow is identified by PATH exactly. A workflow *name* of
    # "ci" is diagnostic only and must NOT substitute for the path -- any repo
    # can name a workflow "ci".
    if (run.workflow_path or "").strip() != EXPECTED_WORKFLOW_PATH:
        problems.append(
            f"workflow path {run.workflow_path!r} != expected {EXPECTED_WORKFLOW_PATH!r} "
            f"(a workflow name of {EXPECTED_WORKFLOW_NAME!r} is diagnostic only and does not "
            f"substitute for the path)."
        )
    # P7.2: new production evidence must come from a deliberate workflow_dispatch;
    # repository_dispatch is accepted only in explicit legacy-evidence mode.
    approved_events = LEGACY_DISPATCH_EVENTS if allow_legacy_repository_dispatch else PRODUCTION_DISPATCH_EVENTS
    if (run.event or "").strip() not in approved_events:
        legacy_hint = "" if allow_legacy_repository_dispatch else (
            " (repository_dispatch is accepted only in explicit legacy-evidence mode)"
        )
        problems.append(
            f"workflow run event {run.event!r} is not an approved dispatch event "
            f"({sorted(approved_events)}) -- production evidence must come from a deliberate "
            f"workflow_dispatch, not an incidental push/PR/schedule run{legacy_hint}."
        )
    # P7.3: the run must have COMPLETED and concluded SUCCESS. A missing status
    # or conclusion is not acceptable for production evidence.
    if (run.status or "").strip().lower() != "completed":
        problems.append(f"workflow run has not completed (status={run.status!r}).")
    if (run.conclusion or "").strip().lower() != "success":
        problems.append(f"workflow run conclusion {run.conclusion!r} != 'success'.")
    if not (run.head_sha or "").strip():
        problems.append("workflow run has no head_sha -- cannot tie evidence to a commit.")

    # --- selector-contract job: EXACTLY ONE, completed + success -----------
    selector_jobs = [j for j in jobs if SELECTOR_CONTRACT_JOB_MARKER in (j.name or "").lower()]
    if not selector_jobs:
        problems.append(
            f"no selector-contract job (name containing {SELECTOR_CONTRACT_JOB_MARKER!r}) found in the run -- "
            f"cannot confirm the contract actually ran."
        )
    elif len(selector_jobs) > 1:
        # P7.4: duplicate selector-like jobs are ambiguous -- a second job could
        # shadow a failing first. Refuse rather than accept "any success".
        names = [j.name for j in selector_jobs]
        problems.append(
            f"multiple selector-contract jobs match {SELECTOR_CONTRACT_JOB_MARKER!r} ({names!r}) -- "
            f"ambiguous; exactly one is required."
        )
    else:
        job = selector_jobs[0]
        # P7.3: that single job must have completed AND concluded success.
        if (job.status or "").strip().lower() != "completed":
            problems.append(f"selector-contract job has not completed (status={job.status!r}).")
        if (job.conclusion or "").strip().lower() != "success":
            problems.append(
                f"selector-contract job did not conclude success (conclusion={job.conclusion!r})."
            )

    # --- artifact identity + freshness -------------------------------------
    if expected_artifact_id is not None and artifact.artifact_id is not None and str(artifact.artifact_id) != str(expected_artifact_id):
        problems.append(
            f"artifact id {artifact.artifact_id!r} != requested artifact id {expected_artifact_id!r}."
        )
    # P7.5: the artifact MUST record the workflow run it belongs to, and it must
    # be the verified run. A missing relationship is refused, not accepted.
    if artifact.workflow_run_id is None:
        problems.append(
            "artifact metadata does not record its workflow_run id -- cannot confirm the artifact "
            "belongs to the verified run."
        )
    elif run.run_id is not None and str(artifact.workflow_run_id) != str(run.run_id):
        problems.append(
            f"artifact belongs to run {artifact.workflow_run_id!r}, not the verified run {run.run_id!r}."
        )
    if (artifact.name or "").strip() != EXPECTED_ARTIFACT_NAME:
        problems.append(f"artifact name {artifact.name!r} != expected {EXPECTED_ARTIFACT_NAME!r}.")
    if artifact.expired is True:
        problems.append("artifact is expired -- its bytes are no longer retrievable/trustworthy.")
    digest_hex = _normalise_digest(artifact.digest)
    if digest_hex is None:
        problems.append("artifact has no GitHub digest -- cannot content-address the downloaded bytes.")

    # --- ZIP bytes: size + digest ------------------------------------------
    zip_sha = sha256_hex(zip_bytes)
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

    # --- hardened extraction of exactly one result -------------------------
    result_bytes: "bytes | None" = None
    result: "dict[str, Any] | None" = None
    result_sha: "str | None" = None
    try:
        result_bytes, result = extract_single_result(zip_bytes)
        result_sha = sha256_hex(result_bytes)
    except GithubArtifactError as exc:
        problems.append(str(exc))

    # --- extracted evidence ties back to the run + release target ----------
    if result is not None:
        ev_regression = _opt_str(result.get("regressionSha"))
        # P7.7: regressionSha must be PRESENT, a full 40-char SHA, and equal to
        # the run's own head_sha.
        if ev_regression is None:
            problems.append("extracted evidence has no regressionSha -- cannot tie it to the run's own commit.")
        else:
            if not is_full_git_sha(ev_regression):
                problems.append(
                    f"extracted evidence regressionSha {ev_regression!r} is not a full 40-character SHA."
                )
            if run.head_sha and ev_regression.lower() != run.head_sha.strip().lower():
                problems.append(
                    f"extracted evidence regressionSha {ev_regression!r} != run head_sha {run.head_sha!r} -- "
                    f"the evidence was not produced by this run."
                )
        if expected_regression_sha is not None:
            if not is_full_git_sha(expected_regression_sha):
                problems.append(f"expected regressionSha {expected_regression_sha!r} is not a full 40-char SHA.")
            elif ev_regression and ev_regression.lower() != expected_regression_sha.strip().lower():
                problems.append(
                    f"extracted evidence regressionSha {ev_regression!r} != expected {expected_regression_sha!r}."
                )
        # P7.6: workflowRunId must be PRESENT and equal to the verified run -- a
        # CI-produced result must record which run produced it.
        ev_workflow_run = _opt_str(result.get("workflowRunId"))
        if ev_workflow_run is None:
            problems.append(
                "extracted evidence has no workflowRunId -- a CI-produced result must record which "
                "workflow run produced it."
            )
        elif run.run_id and str(ev_workflow_run) != str(run.run_id):
            problems.append(
                f"extracted evidence workflowRunId {ev_workflow_run!r} != verified run {run.run_id!r}."
            )
        if expected_tested_sha is not None:
            ev_tested = _opt_str(result.get("testedSha"))
            if ev_tested and ev_tested.lower() != expected_tested_sha.strip().lower():
                problems.append(
                    f"extracted evidence testedSha {ev_tested!r} != release target {expected_tested_sha!r}."
                )
        if expected_version is not None:
            ev_version = _opt_str(result.get("pubspecVersion"))
            if ev_version and ev_version != expected_version.strip():
                problems.append(
                    f"extracted evidence pubspecVersion {ev_version!r} != release target {expected_version!r}."
                )

    return GithubArtifactChain(
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


# --- live acquisition (BLOCKED without credentials) -------------------------

# A "fetcher" fetches a JSON document from a GitHub REST URL. Injected so the
# acquisition flow is testable with a fake and so the live implementation (which
# needs a token) is the only part that touches the network.
JsonFetcher = Callable[[str], "dict[str, Any]"]
BytesFetcher = Callable[[str], bytes]


def resolve_token(env: "dict[str, str] | None" = None) -> "str | None":
    env = env if env is not None else dict(os.environ)
    for var in TOKEN_ENV_VARS:
        value = (env.get(var) or "").strip()
        if value:
            return value
    return None


def _api_base() -> str:
    return (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")


def acquire_github_artifact(
    *,
    run_id: "str | None",
    artifact_id: "str | None",
    local_zip_path: "str | None" = None,
    expected_regression_sha: "str | None" = None,
    expected_tested_sha: "str | None" = None,
    expected_version: "str | None" = None,
    owner_repo: str = EXPECTED_WORKFLOW_REPO,
    allow_legacy_repository_dispatch: bool = False,
    json_fetcher: "JsonFetcher | None" = None,
    bytes_fetcher: "BytesFetcher | None" = None,
    token: "str | None" = None,
    env: "dict[str, str] | None" = None,
) -> GithubArtifactChain:
    """Acquire and verify production selector evidence from a GitHub run+artifact.

    Modes (mirroring ``--github-run-id`` / ``--github-artifact-id`` /
    ``--github-artifact-zip``):

      * ``run_id`` + ``artifact_id`` with API access -> fetch run/jobs/artifact
        metadata and the ZIP over the API, then verify the full chain;
      * ``local_zip_path`` -> the operator supplies an already-downloaded ZIP for
        the byte + extraction layer; metadata is still fetched over the API when
        credentials exist, so the run/job/artifact ownership is verified too.

    Requires BOTH a run id and an artifact id (Priority 2.1-2.2): evidence with no
    artifact id or no run id cannot be authenticated and is refused.

    Raises :class:`GithubArtifactError` (mapped to BLOCKED by the caller) when a
    required id is missing, when metadata is needed but no credentials/fetcher is
    available (message names the exact missing secret), or when the ZIP is
    structurally unreadable.
    """
    if not _opt_str(run_id):
        raise GithubArtifactError(
            "production CI evidence requires a GitHub workflow run id (--github-run-id); "
            "a self-declared workflowRunId in a JSON file is not proof."
        )
    if not _opt_str(artifact_id):
        raise GithubArtifactError(
            "production CI evidence requires a GitHub artifact id (--github-artifact-id); "
            "a bare --source JSON cannot be authenticated."
        )

    have_metadata_fetcher = json_fetcher is not None
    effective_token = token if token is not None else resolve_token(env)
    if json_fetcher is None or bytes_fetcher is None:
        if not effective_token:
            missing = " or ".join(TOKEN_ENV_VARS)
            raise GithubArtifactError(
                "BLOCKED: no GitHub API credentials available to authenticate the artifact "
                f"(set one of {missing} to a token with read access to {owner_repo}). "
                "Without it the run/job/artifact ownership and the artifact digest cannot be "
                "verified, so the evidence cannot be accepted for a production release."
            )
        # Default live implementations (only reached with a real token).
        if json_fetcher is None:
            json_fetcher = _make_live_json_fetcher(effective_token)
        if bytes_fetcher is None:
            bytes_fetcher = _make_live_bytes_fetcher(effective_token)
        have_metadata_fetcher = True

    base = _api_base()
    run_data = json_fetcher(f"{base}/repos/{owner_repo}/actions/runs/{run_id}")
    run = WorkflowRunMetadata.from_api(run_data)
    jobs_data = json_fetcher(f"{base}/repos/{owner_repo}/actions/runs/{run_id}/jobs")
    jobs_list = jobs_data.get("jobs") if isinstance(jobs_data, dict) else None
    jobs = [JobMetadata.from_api(j) for j in (jobs_list or []) if isinstance(j, dict)]
    art_data = json_fetcher(f"{base}/repos/{owner_repo}/actions/artifacts/{artifact_id}")
    artifact = ArtifactMetadata.from_api(art_data)

    # ZIP bytes: operator-supplied local file, else download via the API.
    if local_zip_path:
        try:
            with open(local_zip_path, "rb") as fh:
                zip_bytes = fh.read(MAX_ARTIFACT_ZIP_BYTES + 1)
        except OSError as exc:
            raise GithubArtifactError(f"could not read --github-artifact-zip {local_zip_path}: {exc}") from exc
    else:
        download_url = artifact.archive_download_url or f"{base}/repos/{owner_repo}/actions/artifacts/{artifact_id}/zip"
        zip_bytes = bytes_fetcher(download_url)

    _ = have_metadata_fetcher  # (documented for readers; both fetchers are set above)
    return verify_github_artifact_chain(
        run, jobs, artifact, zip_bytes,
        expected_regression_sha=expected_regression_sha,
        expected_tested_sha=expected_tested_sha,
        expected_version=expected_version,
        expected_run_id=run_id,
        expected_artifact_id=artifact_id,
        allow_legacy_repository_dispatch=allow_legacy_repository_dispatch,
    )


def _make_live_json_fetcher(token: str) -> JsonFetcher:
    import urllib.request

    def _fetch(url: str) -> "dict[str, Any]":
        req = urllib.request.Request(url, headers=_api_headers(token))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - api.github.com only
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surfaced as BLOCKED by the caller
            raise GithubArtifactError(f"GitHub API request failed for {url}: {exc}") from exc

    return _fetch


def _make_live_bytes_fetcher(token: str) -> BytesFetcher:
    import urllib.request

    def _fetch(url: str) -> bytes:
        req = urllib.request.Request(url, headers=_api_headers(token))
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
                return resp.read(MAX_ARTIFACT_ZIP_BYTES + 1)
        except Exception as exc:  # noqa: BLE001
            raise GithubArtifactError(f"GitHub artifact download failed for {url}: {exc}") from exc

    return _fetch


def _api_headers(token: str) -> "dict[str, str]":
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "calee-regression-artifact-verifier",
    }


def _opt_str(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: "Any") -> "int | None":
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
