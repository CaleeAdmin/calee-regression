"""Authenticated build provenance -- the SOURCE side of the distributed-build
identity chain (Priority 1, this session).

``provider_evidence.py``'s provider observation proves only the STORE's side
of a distributed build: which build record a provider API shows, and
provider-owned facts about it (marketing version, platform build number,
processing/release state, ...). It can never prove which Git commit produced
that build -- a provider response alone must never prove source Git
identity. This module is the other, independently-authenticated half: proof
of the Git SHA/ref/application version/platform/bundle id/platform build
number a specific CI build produced, from one of two accepted origins:

  * an authenticated GitHub Actions artifact (:func:`acquire_build_provenance_
    artifact`, reusing ``github_artifact.py``'s chain primitives -- the same
    pattern ``main_ci_artifact.py``/``provider_evidence.py``'s own CI-artifact
    paths already use, rather than duplicating them); or
  * a cryptographically signed build-provenance export, verified against the
    PINNED trust root (:func:`build_signed_build_provenance`, reusing
    ``provider_evidence.resolve_pinned_trusted_public_key``/``verify_signed_
    export`` -- never a per-command key override, see Priority 4).

A plain local JSON file -- unauthenticated by either of the above -- can
never pass; see :func:`parse_build_provenance` (structural parsing only,
raises for a malformed shape but proves nothing about origin) versus the two
acquisition functions above (the only ones that can ever justify a PASS).

:func:`join_provider_and_build_provenance` is the actual identity-chain join:
it requires BOTH sides independently authenticated, requires them to name
the SAME immutable platform build (exact application/package match, exact
platform match, exact platform build-number/versionCode match), requires the
build provenance's own Git SHA/version to match the schema-v2 release-config
expectation, and requires the provider record not to be expired/invalid.
Only when every one of those holds does it produce a joined evidence dict
(schemaVersion 2, ``generatedBy: "provider-build-provenance-join"``) that
``distributed_build_provenance.build_provenance_record`` can wrap and
``provider_evidence.TIER_PROVIDER_BUILD_PROVENANCE_JOIN`` can stamp as
authenticated. When either side is unavailable or fails to authenticate, the
caller (``cli.py``'s ``record-distributed-build-acceptance``) records
BLOCKED with a precise reason -- this module never fabricates a join.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import github_artifact as ga
from .identity_format import (
    compose_full_version,
    is_full_git_sha,
    is_wellformed_version,
    split_marketing_version_and_build_number,
)

BUILD_PROVENANCE_SCHEMA_VERSION = 1
# Schema version 2 (Priority 3, this session): distinct provenance-artifact
# and build-artifact identities -- the ambiguous v1 ``artifactId``/
# ``artifactDigest`` pair could refer either to the provenance JSON artifact
# or to the real IPA/AAB/APK. v1 remains parseable (structural-only), but a
# v1 record can no longer produce a release-gating PASS through the
# GitHub-artifact path -- see verify_build_provenance_artifact_chain.
BUILD_PROVENANCE_SCHEMA_VERSION_2 = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})

# How the distributed BINARY artifact's identity was actually established
# (Priority 3 requirement 7 -- the trust level is always explicit):
#   * github-recorded-digest: the binary artifact's metadata (id/name/digest/
#     run ownership/expiry) was fetched and authenticated live from the
#     GitHub API and matched the record; the (possibly very large) binary
#     bytes themselves were NOT downloaded.
#   * downloaded-bytes: the binary ZIP itself was downloaded and its SHA-256
#     recomputed against the GitHub-recorded digest.
BUILD_ARTIFACT_TRUST_GITHUB_METADATA = "github-recorded-digest"
BUILD_ARTIFACT_TRUST_DOWNLOADED = "downloaded-bytes"
BUILD_PROVENANCE_COMPONENT = "caleemobile-build-provenance"

PLATFORM_IOS = "ios"
PLATFORM_ANDROID = "android"
VALID_PLATFORMS = frozenset({PLATFORM_IOS, PLATFORM_ANDROID})

GENERATED_BY_GITHUB_ARTIFACT = "github-actions-artifact"
GENERATED_BY_SIGNED_EXPORT = "signed-export"
VALID_GENERATED_BY = frozenset({GENERATED_BY_GITHUB_ARTIFACT, GENERATED_BY_SIGNED_EXPORT})

DEFAULT_FRESHNESS = datetime.timedelta(days=30)
FUTURE_SKEW = datetime.timedelta(minutes=5)


class BuildProvenanceError(Exception):
    """Build provenance is missing, unreadable, or not a JSON object -- a
    framework/pipeline fault, never a verdict. A real CONTENT problem (a
    present-but-malformed field) is returned as a problem list, not raised."""


def _opt_str(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_utc_iso8601(value: "Any") -> "datetime.datetime | None":
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        return None
    return parsed.astimezone(datetime.timezone.utc)


@dataclass
class BuildProvenanceRecord:
    """Priority 1: everything the build-provenance side of the identity
    chain must prove -- all source-owned facts, populated only from an
    authenticated origin (see the module docstring)."""

    repository: "str | None"
    workflow_run_id: "str | None"
    workflow_file: "str | None"
    source_git_sha: "str | None"
    source_ref: "str | None"
    application_version: "str | None"
    platform: "str | None"
    bundle_id: "str | None"
    platform_build_number: "str | None"
    artifact_id: "str | None"
    artifact_digest: "str | None"
    build_timestamp: "str | None"
    generated_by: "str | None"
    raw_bytes: "bytes | None" = None
    # Schema version this record actually declared (defaults to 1 for a
    # record that omits it, matching the historical emitter).
    schema_version: int = BUILD_PROVENANCE_SCHEMA_VERSION
    # Schema v2 (Priority 3): the two DISTINCT artifact identities --
    # {"id", "name", "sha256"} for the provenance JSON artifact, and
    # {"id", "name", "sha256", "platform"} for the real IPA/AAB/APK.
    provenance_artifact: "dict[str, Any] | None" = None
    build_artifact: "dict[str, Any] | None" = None
    # Populated by verify_build_provenance_artifact_chain (never from the
    # record's own content): the authenticated GitHub run metadata this
    # record was verified against, and how the binary artifact's identity
    # was established (BUILD_ARTIFACT_TRUST_*).
    authenticated_run: "dict[str, Any] | None" = None
    build_artifact_trust: "str | None" = None

    def raw_sha256(self) -> "str | None":
        if self.raw_bytes is None:
            return None
        return "sha256:" + hashlib.sha256(self.raw_bytes).hexdigest()

    def to_dict(self) -> "dict[str, Any]":
        data = {
            "schemaVersion": self.schema_version,
            "component": BUILD_PROVENANCE_COMPONENT,
            "repository": self.repository,
            "workflowRunId": self.workflow_run_id,
            "workflowFile": self.workflow_file,
            "sourceGitSha": self.source_git_sha,
            "sourceRef": self.source_ref,
            "applicationVersion": self.application_version,
            "platform": self.platform,
            "bundleId": self.bundle_id,
            "platformBuildNumber": self.platform_build_number,
            "artifactId": self.artifact_id,
            "artifactDigest": self.artifact_digest,
            "buildTimestamp": self.build_timestamp,
            "generatedBy": self.generated_by,
        }
        if self.provenance_artifact is not None:
            data["provenanceArtifact"] = self.provenance_artifact
        if self.build_artifact is not None:
            data["buildArtifact"] = self.build_artifact
        if self.authenticated_run is not None:
            data["authenticatedRun"] = self.authenticated_run
        if self.build_artifact_trust is not None:
            data["buildArtifactTrust"] = self.build_artifact_trust
        return data


def parse_build_provenance(data: "Any") -> BuildProvenanceRecord:
    """Parse a build-provenance JSON mapping into a
    :class:`BuildProvenanceRecord`. STRUCTURAL parsing only -- raises
    :class:`BuildProvenanceError` for a shape this consumer can't even read
    (not a dict, unsupported schemaVersion, wrong component); proves NOTHING
    about origin (see the module docstring -- only the two acquisition
    functions below authenticate a record; a hand-typed JSON parses here
    just as cleanly as a genuine one, exactly like ``distributed_build_
    acceptance.parse_distributed_build_acceptance_result``)."""
    if not isinstance(data, dict):
        raise BuildProvenanceError("build provenance must be a JSON object.")
    schema_version = data.get("schemaVersion")
    if schema_version is not None and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BuildProvenanceError(
            f"unsupported build-provenance schemaVersion {schema_version!r}; this consumer supports only "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}."
        )
    component = data.get("component")
    if component is not None and component != BUILD_PROVENANCE_COMPONENT:
        raise BuildProvenanceError(f"unexpected component {component!r} (expected {BUILD_PROVENANCE_COMPONENT!r}).")

    def _artifact_identity(key: str) -> "dict[str, Any] | None":
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise BuildProvenanceError(f"build provenance {key} must be a JSON object, got {type(value).__name__}.")
        return {k: value.get(k) for k in ("id", "name", "sha256", "platform") if value.get(k) is not None}

    return BuildProvenanceRecord(
        schema_version=schema_version if schema_version is not None else BUILD_PROVENANCE_SCHEMA_VERSION,
        provenance_artifact=_artifact_identity("provenanceArtifact"),
        build_artifact=_artifact_identity("buildArtifact"),
        repository=_opt_str(data.get("repository")),
        workflow_run_id=_opt_str(data.get("workflowRunId")),
        workflow_file=_opt_str(data.get("workflowFile")),
        source_git_sha=_opt_str(data.get("sourceGitSha")),
        source_ref=_opt_str(data.get("sourceRef")),
        application_version=_opt_str(data.get("applicationVersion")),
        platform=_opt_str(data.get("platform")),
        bundle_id=_opt_str(data.get("bundleId")),
        platform_build_number=_opt_str(data.get("platformBuildNumber")),
        artifact_id=_opt_str(data.get("artifactId")),
        artifact_digest=_opt_str(data.get("artifactDigest")),
        build_timestamp=_opt_str(data.get("buildTimestamp")),
        generated_by=_opt_str(data.get("generatedBy")),
    )


def validate_build_provenance(record: BuildProvenanceRecord) -> "list[str]":
    """Format/consistency validation for a parsed build-provenance record --
    every field Priority 1 requires must be present and well-formed. Returns
    a problem list (empty == well-formed). Never checks origin/authenticity
    -- that is the acquisition functions' job."""
    problems: "list[str]" = []
    if not record.repository:
        problems.append("build provenance has no repository recorded.")
    if not record.workflow_run_id:
        problems.append("build provenance has no workflowRunId recorded.")
    if not record.workflow_file:
        problems.append("build provenance has no workflowFile recorded.")
    if not record.source_git_sha:
        problems.append("build provenance has no sourceGitSha recorded.")
    elif not is_full_git_sha(record.source_git_sha):
        problems.append(f"build provenance sourceGitSha {record.source_git_sha!r} is abbreviated/ambiguous (need the full 40-character SHA).")
    if not record.source_ref:
        problems.append("build provenance has no sourceRef recorded.")
    if not record.application_version:
        problems.append("build provenance has no applicationVersion recorded.")
    elif not is_wellformed_version(record.application_version):
        problems.append(f"build provenance applicationVersion {record.application_version!r} is not a well-formed version identity.")
    if record.platform not in VALID_PLATFORMS:
        problems.append(f"build provenance platform {record.platform!r} is not one of {sorted(VALID_PLATFORMS)}.")
    if not record.bundle_id:
        problems.append("build provenance has no bundleId recorded.")
    if not record.platform_build_number:
        problems.append("build provenance has no platformBuildNumber recorded.")
    if record.schema_version >= BUILD_PROVENANCE_SCHEMA_VERSION_2:
        # Schema v2: DISTINCT provenance-artifact and build-artifact
        # identities (Priority 3) -- the ambiguous v1 artifactId/
        # artifactDigest pair is no longer accepted as sufficient.
        problems.extend(_validate_artifact_identity(
            # sha256 is optional here (compared when present): the provenance
            # JSON is inside the provenance artifact, so it cannot know its
            # own artifact digest without circularity.
            record.provenance_artifact, label="provenanceArtifact", require_platform=False, require_sha=False,
        ))
        problems.extend(_validate_artifact_identity(
            record.build_artifact, label="buildArtifact", require_platform=True, require_sha=True,
        ))
        if (
            record.build_artifact is not None and record.platform is not None
            and _opt_str(record.build_artifact.get("platform")) not in (None, record.platform)
        ):
            problems.append(
                f"buildArtifact platform {record.build_artifact.get('platform')!r} != build provenance "
                f"platform {record.platform!r}."
            )
    else:
        if not record.artifact_id:
            problems.append("build provenance has no artifactId recorded.")
        if not record.artifact_digest:
            problems.append("build provenance has no artifactDigest recorded.")
    if not record.build_timestamp:
        problems.append("build provenance has no buildTimestamp recorded.")
    elif _parse_utc_iso8601(record.build_timestamp) is None:
        problems.append(f"build provenance buildTimestamp {record.build_timestamp!r} is not a valid UTC ISO-8601 instant.")
    if not record.generated_by:
        problems.append("build provenance has no generatedBy recorded -- cannot tell how it was produced.")
    elif record.generated_by not in VALID_GENERATED_BY:
        problems.append(
            f"build provenance generatedBy {record.generated_by!r} is not a recognised authenticated origin "
            f"(expected one of {sorted(VALID_GENERATED_BY)}) -- a plain local JSON file cannot pass."
        )
    return problems


def _validate_artifact_identity(
    identity: "dict[str, Any] | None", *, label: str, require_platform: bool, require_sha: bool = True,
) -> "list[str]":
    """Format checks for one schema-v2 artifact identity object."""
    problems: "list[str]" = []
    if not isinstance(identity, dict) or not identity:
        return [f"build provenance schemaVersion 2 requires a {label} object with id/name/sha256."]
    if not _opt_str(identity.get("id")):
        problems.append(f"build provenance {label} has no id recorded.")
    if not _opt_str(identity.get("name")):
        problems.append(f"build provenance {label} has no name recorded.")
    sha = _opt_str(identity.get("sha256"))
    if not sha:
        if require_sha:
            problems.append(f"build provenance {label} has no sha256 recorded.")
    elif not _looks_like_sha256(sha):
        problems.append(f"build provenance {label} sha256 {sha!r} is not a 64-hex-character SHA-256 digest.")
    if require_platform and _opt_str(identity.get("platform")) not in VALID_PLATFORMS:
        problems.append(
            f"build provenance {label} platform {identity.get('platform')!r} is not one of "
            f"{sorted(VALID_PLATFORMS)}."
        )
    return problems


def _looks_like_sha256(value: str) -> bool:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def _bare_sha256(value: "str | None") -> "str | None":
    """Normalise a possibly ``sha256:``-prefixed digest to bare lowercase hex
    (None for anything that isn't a plausible digest)."""
    if not value:
        return None
    text = str(value).strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    if len(text) != 64 or not all(c in "0123456789abcdef" for c in text):
        return None
    return text


# --- authenticated origin 1: GitHub Actions artifact -------------------------


@dataclass
class BuildProvenanceArtifactChain:
    """The result of evaluating an authenticated GitHub Actions artifact
    containing a build-provenance record."""

    ok: bool
    problems: "list[str]" = field(default_factory=list)
    run: "Any" = None
    artifact: "Any" = None
    zip_bytes: "bytes | None" = None
    zip_sha256: "str | None" = None
    result_bytes: "bytes | None" = None
    record: "BuildProvenanceRecord | None" = None

    def summary(self) -> str:
        if self.ok:
            return f"Authenticated build provenance verified (commit {self.record.source_git_sha if self.record else '?'})."
        return "Authenticated build provenance REJECTED: " + "; ".join(self.problems)


def verify_build_provenance_artifact_chain(
    run: "ga.WorkflowRunMetadata",
    artifact: "ga.ArtifactMetadata",
    zip_bytes: bytes,
    *,
    expected_repository: str,
    expected_workflow_path: str,
    expected_artifact_name: str,
    expected_result_filename: str,
    expected_run_id: "str | None" = None,
    expected_artifact_id: "str | None" = None,
    max_zip_bytes: "int | None" = None,
    build_artifact_metadata: "ga.ArtifactMetadata | None" = None,
) -> BuildProvenanceArtifactChain:
    """Pure core: authenticate that ``zip_bytes`` genuinely is the named
    artifact from the named GitHub Actions run (repository, workflow path,
    run success, artifact ownership/name/digest -- the same chain
    ``main_ci_artifact.py``/``provider_evidence.py`` enforce for their own
    artifact origins), then parse+validate the single extracted file as a
    :class:`BuildProvenanceRecord`. Deliberately does NOT require any
    particular ``event`` -- unlike merged-main CI evidence, a CaleeMobile
    build workflow may legitimately run on a schedule/dispatch/tag-push; the
    identity binding comes from the record's own fields (checked by the
    caller via :func:`join_provider_and_build_provenance`), not the run's
    trigger."""
    problems: "list[str]" = []
    effective_max = max_zip_bytes if max_zip_bytes is not None else ga.MAX_ARTIFACT_ZIP_BYTES

    if expected_run_id is not None and run.run_id is not None and str(run.run_id) != str(expected_run_id):
        problems.append(f"workflow run id {run.run_id!r} != requested run id {expected_run_id!r}.")
    if (run.repo_full_name or "").strip() != expected_repository:
        problems.append(f"workflow run repository {run.repo_full_name!r} != expected {expected_repository!r}.")
    if (run.workflow_path or "").strip() != expected_workflow_path:
        problems.append(
            f"workflow path {run.workflow_path!r} != expected {expected_workflow_path!r} -- a workflow "
            f"NAME never substitutes for its path."
        )
    if (run.status or "").strip().lower() != "completed":
        problems.append(f"workflow run has not completed (status={run.status!r}).")
    if (run.conclusion or "").strip().lower() != "success":
        problems.append(f"workflow run conclusion {run.conclusion!r} != 'success'.")

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
        problems.append(f"artifact name {artifact.name!r} != expected {expected_artifact_name!r}.")
    if artifact.expired is True:
        problems.append("artifact is expired -- its bytes are no longer retrievable/trustworthy.")
    digest_hex = ga._normalise_digest(artifact.digest)
    if digest_hex is None:
        problems.append("artifact has no GitHub digest -- cannot content-address the downloaded bytes.")

    zip_sha = ga.sha256_hex(zip_bytes)
    if len(zip_bytes) > effective_max:
        problems.append(f"downloaded artifact ZIP is {len(zip_bytes)} bytes, over the {effective_max}-byte limit.")
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

    result_bytes: "bytes | None" = None
    record: "BuildProvenanceRecord | None" = None
    try:
        result_bytes, parsed = ga.extract_single_result(zip_bytes, expected_name=expected_result_filename)
    except ga.GithubArtifactError as exc:
        problems.append(str(exc))
        parsed = None

    if parsed is not None:
        try:
            record = parse_build_provenance(parsed)
        except BuildProvenanceError as exc:
            problems.append(str(exc))
        else:
            record.raw_bytes = result_bytes
            record.generated_by = GENERATED_BY_GITHUB_ARTIFACT
            problems.extend(validate_build_provenance(record))
            problems.extend(_bind_record_to_authenticated_run(record, run))
            record.authenticated_run = {
                "id": run.run_id,
                "attempt": run.run_attempt,
                "event": run.event,
                "headSha": run.head_sha,
                "headBranch": run.head_branch,
                "workflowPath": run.workflow_path,
                "repository": run.repo_full_name,
            }
            if record.schema_version < BUILD_PROVENANCE_SCHEMA_VERSION_2:
                # Priority 3 requirement 11: v1 stays parseable (structural
                # only) but must NEVER produce a release-gating PASS through
                # the GitHub-artifact path -- its single artifactId/
                # artifactDigest cannot distinguish the provenance JSON from
                # the real distributed binary.
                problems.append(
                    "build provenance schemaVersion 1 cannot produce a release-gating PASS through the "
                    "GitHub-artifact path -- migrate the emitting workflow to schemaVersion 2, which "
                    "identifies the provenance artifact and the distributed binary artifact "
                    "(provenanceArtifact/buildArtifact) separately."
                )
            else:
                problems.extend(_verify_v2_artifact_identities(
                    record, provenance_artifact=artifact, provenance_zip_sha256=zip_sha,
                    run=run, build_artifact_metadata=build_artifact_metadata,
                ))

    return BuildProvenanceArtifactChain(
        ok=not problems, problems=problems, run=run, artifact=artifact,
        zip_bytes=zip_bytes, zip_sha256=zip_sha, result_bytes=result_bytes, record=record,
    )


def _bind_record_to_authenticated_run(record: BuildProvenanceRecord, run: "ga.WorkflowRunMetadata") -> "list[str]":
    """Priority 2: the record's own provenance claims must agree with the
    AUTHENTICATED workflow-run metadata that uploaded it -- a genuine GitHub
    artifact containing hand-authored provenance for another repository/run/
    workflow/SHA/ref must BLOCK. Each claim is compared independently;
    missing authenticated metadata never silently passes a claim."""
    problems: "list[str]" = []
    if record.repository and (run.repo_full_name or "").strip() != record.repository:
        problems.append(
            f"build provenance record.repository {record.repository!r} != authenticated run repository "
            f"{run.repo_full_name!r} -- the record describes another repository."
        )
    if record.workflow_run_id and run.run_id is not None and str(record.workflow_run_id) != str(run.run_id):
        problems.append(
            f"build provenance record.workflowRunId {record.workflow_run_id!r} != authenticated run id "
            f"{run.run_id!r} -- the record names another workflow run."
        )
    if record.workflow_file and (run.workflow_path or "").strip() != record.workflow_file:
        problems.append(
            f"build provenance record.workflowFile {record.workflow_file!r} != authenticated workflow "
            f"path {run.workflow_path!r} -- the record names another workflow file."
        )
    if record.source_git_sha:
        if not run.head_sha:
            problems.append(
                "authenticated run metadata has no head SHA -- cannot bind the record's sourceGitSha "
                "to the run that actually built it."
            )
        elif record.source_git_sha.strip().lower() != run.head_sha.strip().lower():
            problems.append(
                f"build provenance record.sourceGitSha {record.source_git_sha!r} != authenticated run "
                f"head SHA {run.head_sha!r} -- the record claims a commit the authenticated run did not build."
            )
    if record.source_ref and run.head_branch:
        claimed = record.source_ref.strip()
        branch = run.head_branch.strip()
        acceptable = {branch, f"refs/heads/{branch}", f"refs/tags/{branch}"}
        if claimed not in acceptable:
            problems.append(
                f"build provenance record.sourceRef {record.source_ref!r} does not agree with the "
                f"authenticated run's head branch {run.head_branch!r}."
            )
    return problems


def _verify_v2_artifact_identities(
    record: BuildProvenanceRecord,
    *,
    provenance_artifact: "ga.ArtifactMetadata",
    provenance_zip_sha256: "str | None",
    run: "ga.WorkflowRunMetadata",
    build_artifact_metadata: "ga.ArtifactMetadata | None",
) -> "list[str]":
    """Priority 3: verify a schema-v2 record's two artifact identities.

    The provenance-artifact identity must match the artifact the chain
    actually authenticated/downloaded; the build-artifact identity must be
    independently authenticated against live GitHub metadata (same run, same
    id/name/digest, not expired) -- and must genuinely be a DIFFERENT
    artifact than the provenance ZIP (a record describing its own small
    provenance ZIP as the distributed application artifact is rejected).
    Sets ``record.build_artifact_trust`` when the binary identity
    authenticates via GitHub metadata + GitHub-recorded digest."""
    problems: "list[str]" = []
    pa = record.provenance_artifact or {}
    ba = record.build_artifact or {}

    if _opt_str(pa.get("id")) and provenance_artifact.artifact_id is not None \
            and str(pa.get("id")) != str(provenance_artifact.artifact_id):
        problems.append(
            f"record provenanceArtifact.id {pa.get('id')!r} != authenticated provenance artifact id "
            f"{provenance_artifact.artifact_id!r}."
        )
    if _opt_str(pa.get("name")) and (provenance_artifact.name or "").strip() != str(pa.get("name")).strip():
        problems.append(
            f"record provenanceArtifact.name {pa.get('name')!r} != authenticated provenance artifact "
            f"name {provenance_artifact.name!r}."
        )
    recorded_pa_sha = _bare_sha256(_opt_str(pa.get("sha256")))
    if recorded_pa_sha and provenance_zip_sha256 and recorded_pa_sha != provenance_zip_sha256.lower():
        problems.append(
            f"record provenanceArtifact.sha256 {pa.get('sha256')!r} != downloaded provenance ZIP sha256 "
            f"{provenance_zip_sha256}."
        )

    recorded_ba_sha = _bare_sha256(_opt_str(ba.get("sha256")))
    # Masquerade guard (Priority 3 requirement 10): the record's "build
    # artifact" must not simply be its own provenance ZIP under another name.
    if _opt_str(ba.get("id")) and _opt_str(pa.get("id")) and str(ba.get("id")) == str(pa.get("id")):
        problems.append(
            "record buildArtifact.id equals provenanceArtifact.id -- the provenance ZIP cannot "
            "masquerade as the distributed application artifact."
        )
    if recorded_ba_sha and provenance_zip_sha256 and recorded_ba_sha == provenance_zip_sha256.lower():
        problems.append(
            "record buildArtifact.sha256 equals the provenance ZIP's own sha256 -- the provenance ZIP "
            "cannot masquerade as the distributed application artifact."
        )

    if build_artifact_metadata is None:
        problems.append(
            "the distributed binary artifact's GitHub metadata was not fetched/authenticated -- cannot "
            "confirm the buildArtifact identity belongs to the authenticated workflow run."
        )
        return problems

    bm = build_artifact_metadata
    if _opt_str(ba.get("id")) and bm.artifact_id is not None and str(ba.get("id")) != str(bm.artifact_id):
        problems.append(
            f"record buildArtifact.id {ba.get('id')!r} != authenticated GitHub build-artifact id "
            f"{bm.artifact_id!r}."
        )
    if _opt_str(ba.get("name")) and (bm.name or "").strip() != str(ba.get("name")).strip():
        problems.append(
            f"record buildArtifact.name {ba.get('name')!r} != authenticated GitHub build-artifact name "
            f"{bm.name!r}."
        )
    if bm.workflow_run_id is None:
        problems.append(
            "GitHub build-artifact metadata does not record its workflow_run id -- cannot confirm the "
            "binary artifact belongs to the authenticated run."
        )
    elif run.run_id is not None and str(bm.workflow_run_id) != str(run.run_id):
        problems.append(
            f"the binary build artifact belongs to run {bm.workflow_run_id!r}, not the authenticated "
            f"run {run.run_id!r}."
        )
    if bm.expired is True:
        problems.append("the binary build artifact is expired -- its bytes are no longer retrievable/trustworthy.")
    github_ba_sha = _bare_sha256(bm.digest)
    if github_ba_sha is None:
        problems.append(
            "GitHub records no digest for the binary build artifact -- its content cannot be "
            "content-addressed."
        )
    elif recorded_ba_sha is None:
        problems.append("record buildArtifact.sha256 is missing/unparseable -- cannot compare with the GitHub-recorded digest.")
    elif recorded_ba_sha != github_ba_sha:
        problems.append(
            f"record buildArtifact.sha256 {ba.get('sha256')!r} != GitHub-recorded digest sha256:"
            f"{github_ba_sha} -- the record does not describe the artifact GitHub actually stored."
        )
    if github_ba_sha is not None and provenance_zip_sha256 and github_ba_sha == provenance_zip_sha256.lower():
        problems.append(
            "the GitHub-recorded binary-artifact digest equals the provenance ZIP's own sha256 -- the "
            "provenance ZIP cannot masquerade as the distributed application artifact."
        )
    if not problems:
        # Priority 3 requirement 7: the binary bytes were not downloaded;
        # trust rests on authenticated GitHub metadata + the GitHub-recorded
        # digest, and that trust level is stated explicitly.
        record.build_artifact_trust = BUILD_ARTIFACT_TRUST_GITHUB_METADATA
    return problems


def acquire_build_provenance_artifact(
    *,
    repository: str,
    workflow_path: str,
    run_id: "str | None",
    artifact_id: "str | None",
    expected_artifact_name: str,
    expected_result_filename: str,
    local_zip_path: "str | None" = None,
    json_fetcher: "ga.JsonFetcher | None" = None,
    bytes_fetcher: "ga.BytesFetcher | None" = None,
    token: "str | None" = None,
    env: "dict[str, str] | None" = None,
) -> BuildProvenanceArtifactChain:
    """Acquire and verify authenticated build provenance from a GitHub
    run+artifact. Mirrors ``main_ci_artifact.acquire_main_ci_artifact``'s /
    ``provider_evidence.acquire_provider_ci_artifact``'s shape and credential-
    BLOCKED behaviour exactly, reusing ``github_artifact.py``'s live HTTP
    fetchers and token resolution rather than duplicating them. Raises
    :class:`BuildProvenanceError` (BLOCKED) when a required id is missing or
    no credentials/fetcher is available (naming the exact missing secret)."""
    if not ga._opt_str(run_id):
        raise BuildProvenanceError(
            "authenticated build provenance requires a GitHub workflow run id "
            "(--build-provenance-github-run-id); a self-declared workflowRunId in a JSON file is not proof."
        )
    if not ga._opt_str(artifact_id):
        raise BuildProvenanceError(
            "authenticated build provenance requires a GitHub artifact id (--build-provenance-github-artifact-id)."
        )

    effective_token = token if token is not None else ga.resolve_token(env)
    if json_fetcher is None or bytes_fetcher is None:
        if not effective_token:
            missing = " or ".join(ga.TOKEN_ENV_VARS)
            raise BuildProvenanceError(
                f"BLOCKED: no GitHub API credentials available to authenticate the build-provenance artifact "
                f"(set one of {missing} to a token with read access to {repository}). Without it the "
                f"run/artifact ownership and the artifact digest cannot be verified."
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
            raise BuildProvenanceError(f"could not read build-provenance artifact ZIP {local_zip_path}: {exc}") from exc
    else:
        download_url = artifact.archive_download_url or f"{base}/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        zip_bytes = bytes_fetcher(download_url)

    # Priority 3: for a schema-v2 record, the DISTINCT binary build
    # artifact's metadata is fetched and authenticated through the GitHub
    # API too. The record has to be (tolerantly) pre-parsed here just to
    # learn which artifact id to fetch; verify_build_provenance_artifact_
    # chain re-parses and performs every actual check.
    build_artifact_metadata: "ga.ArtifactMetadata | None" = None
    try:
        _peek_bytes, peek_parsed = ga.extract_single_result(zip_bytes, expected_name=expected_result_filename)
        peek_record = parse_build_provenance(peek_parsed)
    except (ga.GithubArtifactError, BuildProvenanceError):
        peek_record = None
    if peek_record is not None and peek_record.schema_version >= BUILD_PROVENANCE_SCHEMA_VERSION_2 \
            and isinstance(peek_record.build_artifact, dict):
        build_artifact_id = ga._opt_str(peek_record.build_artifact.get("id"))
        if build_artifact_id:
            try:
                ba_data = json_fetcher(f"{base}/repos/{repository}/actions/artifacts/{build_artifact_id}")
                build_artifact_metadata = ga.ArtifactMetadata.from_api(ba_data)
            except (ga.GithubArtifactError, OSError):
                build_artifact_metadata = None

    return verify_build_provenance_artifact_chain(
        run, artifact, zip_bytes,
        expected_repository=repository, expected_workflow_path=workflow_path,
        expected_artifact_name=expected_artifact_name, expected_result_filename=expected_result_filename,
        expected_run_id=run_id, expected_artifact_id=artifact_id,
        build_artifact_metadata=build_artifact_metadata,
    )


# --- authenticated origin 2: signed export -----------------------------------


def build_signed_build_provenance(
    *, payload: "dict[str, Any]", signature_bytes: bytes, trusted_public_key_pem: str,
) -> "tuple[BuildProvenanceRecord | None, list[str]]":
    """Verify a signed build-provenance payload against the (caller-resolved
    -- see ``provider_evidence.resolve_pinned_trusted_public_key``, Priority
    4) trusted public key. Returns ``(record_or_none, problems)`` -- ``None``
    with a non-empty problems list on signature failure, never a half-
    trusted result."""
    from . import provider_evidence as pe

    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    problems = pe.verify_signed_export(payload_bytes=payload_bytes, signature_bytes=signature_bytes, trusted_public_key_pem=trusted_public_key_pem)
    if problems:
        return None, problems

    try:
        record = parse_build_provenance(payload)
    except BuildProvenanceError as exc:
        return None, [str(exc)]
    record.raw_bytes = payload_bytes
    record.generated_by = GENERATED_BY_SIGNED_EXPORT
    problems = validate_build_provenance(record)
    if problems:
        return None, problems
    return record, []


# --- the join: the actual distributed-build identity chain -------------------


@dataclass
class JoinVerdict:
    ok: bool
    problems: "list[str]" = field(default_factory=list)
    evidence: "dict[str, Any] | None" = None

    def summary(self) -> str:
        if self.ok:
            ev = self.evidence or {}
            return f"Distributed-build identity chain PASS for CaleeMobile {ev.get('testedVersion')} @ {ev.get('testedGitSha')}."
        return "Distributed-build identity chain REJECTED: " + "; ".join(self.problems)


def join_provider_and_build_provenance(
    provider_observation: "dict[str, Any]",
    build_provenance: "BuildProvenanceRecord",
    *,
    expected_release_config_git_sha: "str | None" = None,
    expected_release_config_version: "str | None" = None,
    expected_release_id: "str | None" = None,
    release_run_id: "str | None" = None,
    provider_raw_sha256: "str | None" = None,
    build_raw_sha256: "str | None" = None,
    now: "datetime.datetime | None" = None,
    max_provider_age: "datetime.timedelta | None" = DEFAULT_FRESHNESS,
) -> JoinVerdict:
    """Priority 1's actual identity-chain join: succeeds only when BOTH the
    provider observation and the build provenance independently authenticate
    AND name the SAME immutable platform build. Every requirement below is
    checked independently; missing/mismatched values are never treated as a
    match by omission.

    Chain requirements (all must hold):
      1. Provider observation authentication (format-validated here via
         ``provider_evidence.validate_provider_observation``; the CALLER is
         responsible for having actually authenticated its origin -- see
         that function's docstring).
      2. Build-provenance authentication (format-validated here via
         :func:`validate_build_provenance`; same caveat).
      3. Exact application/package match (provider bundleId == build
         bundleId).
      4. Exact platform match.
      5. Exact platform build-number/versionCode match.
      6. Build-provenance Git SHA equals the schema-v2 release-config Git
         SHA.
      7. Build-provenance application version equals the schema-v2
         release-config version (decomposed via
         ``identity_format.split_marketing_version_and_build_number`` into
         marketing-version + build-number, per Priority 2 -- rejecting
         ambiguous parsing of a value like ``0.0.24+24``).
      8. Provider record is not expired, invalid, or unavailable.
      9. (Handled by the caller, not this pure function: both source
         bundles and this chain's evidence must be included in the release
         ZIP -- see ``cli.py``'s join wiring.)
    """
    from . import provider_evidence as pe

    problems: "list[str]" = []

    problems.extend(pe.validate_provider_observation(provider_observation, expected_release_id=expected_release_id))
    problems.extend(validate_build_provenance(build_provenance))

    provider_bundle_id = provider_observation.get("bundleId")
    if not provider_bundle_id or not build_provenance.bundle_id:
        problems.append("cannot confirm exact application/package match -- bundleId missing on one or both sides.")
    elif str(provider_bundle_id).strip() != str(build_provenance.bundle_id).strip():
        problems.append(
            f"provider bundleId {provider_bundle_id!r} != build-provenance bundleId {build_provenance.bundle_id!r} "
            f"-- these two authenticated records do not name the same application."
        )

    provider_platform = provider_observation.get("platform")
    if not provider_platform or not build_provenance.platform:
        problems.append("cannot confirm exact platform match -- platform missing on one or both sides.")
    elif str(provider_platform).strip() != str(build_provenance.platform).strip():
        problems.append(
            f"provider platform {provider_platform!r} != build-provenance platform {build_provenance.platform!r}."
        )

    provider_build_number = provider_observation.get("buildNumber")
    if not provider_build_number or not build_provenance.platform_build_number:
        problems.append("cannot confirm exact platform build-number match -- buildNumber missing on one or both sides.")
    elif str(provider_build_number).strip() != str(build_provenance.platform_build_number).strip():
        problems.append(
            f"provider buildNumber {provider_build_number!r} != build-provenance platformBuildNumber "
            f"{build_provenance.platform_build_number!r} -- these two authenticated records do not name the "
            f"same immutable platform build."
        )

    if expected_release_config_git_sha is not None:
        if not is_full_git_sha(expected_release_config_git_sha):
            problems.append(f"expected release-config Git SHA {expected_release_config_git_sha!r} is abbreviated/ambiguous.")
        elif build_provenance.source_git_sha and build_provenance.source_git_sha.strip().lower() != expected_release_config_git_sha.strip().lower():
            problems.append(
                f"build-provenance sourceGitSha {build_provenance.source_git_sha!r} != schema-v2 release-config "
                f"Git SHA {expected_release_config_git_sha!r}."
            )

    # Priority 1: ONE canonical CaleeMobile version identity. The join's
    # testedVersion is DERIVED (never copied from an expectation) from the
    # authenticated build-provenance fields: applicationVersion (marketing)
    # + platformBuildNumber, composed via the canonical helper. A pair that
    # cannot be composed unambiguously blocks the join outright.
    canonical_tested_version = compose_full_version(
        build_provenance.application_version, build_provenance.platform_build_number,
    )
    if canonical_tested_version is None:
        problems.append(
            f"cannot compose a canonical CaleeMobile full version from build-provenance "
            f"applicationVersion {build_provenance.application_version!r} + platformBuildNumber "
            f"{build_provenance.platform_build_number!r} -- ambiguous or noncanonical version forms "
            f"are rejected, never guessed."
        )

    if expected_release_config_version is not None:
        split = split_marketing_version_and_build_number(expected_release_config_version)
        if split is None:
            problems.append(
                f"expected release-config version {expected_release_config_version!r} cannot be unambiguously "
                f"split into marketing-version + build-number -- refusing to guess."
            )
        else:
            expected_marketing, expected_build_number = split
            if build_provenance.application_version and build_provenance.application_version.strip() != expected_marketing.strip():
                problems.append(
                    f"build-provenance applicationVersion {build_provenance.application_version!r} != schema-v2 "
                    f"release-config marketing version {expected_marketing!r}."
                )
            if build_provenance.platform_build_number and build_provenance.platform_build_number.strip() != expected_build_number.strip():
                problems.append(
                    f"build-provenance platformBuildNumber {build_provenance.platform_build_number!r} != "
                    f"schema-v2 release-config build number {expected_build_number!r}."
                )
            if canonical_tested_version is not None and canonical_tested_version != expected_release_config_version.strip():
                problems.append(
                    f"canonical joined testedVersion {canonical_tested_version!r} != schema-v2 release-config "
                    f"full version {expected_release_config_version!r}."
                )

    # Priority 5: explicit provider marketing-version confirmation. For iOS
    # (App Store Connect) the provider's own marketing version must equal the
    # build-provenance applicationVersion; a provider value that is absent is
    # recorded as unavailable, NEVER reported as a provider-confirmed match.
    # For Play, the release "name" is a display label (releaseLabel), not
    # independently established as the application semantic version by the
    # API contract, so no marketing comparison applies there.
    provider_name = _opt_str(provider_observation.get("provider"))
    provider_marketing = _opt_str(provider_observation.get("marketingVersion"))
    if provider_name == "app_store_connect":
        if provider_marketing is None:
            provider_marketing_confirmation = "unavailable-from-provider"
        elif build_provenance.application_version and provider_marketing != build_provenance.application_version.strip():
            provider_marketing_confirmation = "mismatch"
            problems.append(
                f"provider marketingVersion {provider_marketing!r} != build-provenance applicationVersion "
                f"{build_provenance.application_version!r} -- the store's marketing version does not "
                f"confirm the authenticated build's version."
            )
        else:
            provider_marketing_confirmation = "verified"
    else:
        provider_marketing_confirmation = "not-applicable"

    # Requirement 8: provider record not expired/invalid/unavailable.
    processing_state = provider_observation.get("processingState")
    if processing_state in ("FAILED", "INVALID"):
        problems.append(f"provider record processingState is {processing_state!r} -- not a valid/available build.")
    release_status = provider_observation.get("releaseStatus")
    if release_status == "draft":
        problems.append("provider record releaseStatus is 'draft' -- this build was never actually released.")
    observed_at = provider_observation.get("providerObservedAt")
    parsed_observed_at = _parse_utc_iso8601(observed_at) if observed_at else None
    if observed_at and parsed_observed_at is None:
        pass  # already reported by validate_provider_observation
    elif parsed_observed_at is not None:
        reference = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=datetime.timezone.utc)
        if parsed_observed_at > reference + FUTURE_SKEW:
            problems.append(f"provider observation providerObservedAt {observed_at!r} is in the future.")
        elif max_provider_age is not None and (reference - parsed_observed_at) > max_provider_age:
            age = reference - parsed_observed_at
            problems.append(f"provider observation is stale: {age.days}d old, older than the {max_provider_age.days}d freshness window -- the provider record may no longer be valid.")

    evidence = _build_joined_evidence(
        provider_observation, build_provenance,
        canonical_tested_version=canonical_tested_version,
        provider_marketing_confirmation=provider_marketing_confirmation,
        release_run_id=release_run_id,
        provider_raw_sha256=provider_raw_sha256,
        build_raw_sha256=build_raw_sha256,
    )
    return JoinVerdict(ok=not problems, problems=problems, evidence=evidence)


def _build_joined_evidence(
    provider_observation: "dict[str, Any]",
    build_provenance: BuildProvenanceRecord,
    *,
    canonical_tested_version: "str | None" = None,
    provider_marketing_confirmation: "str | None" = None,
    release_run_id: "str | None" = None,
    provider_raw_sha256: "str | None" = None,
    build_raw_sha256: "str | None" = None,
) -> "dict[str, Any]":
    """The MERGED distributed-build-acceptance-shaped evidence dict
    (schemaVersion 2) this join produces on success -- ready for
    ``distributed_build_provenance.build_provenance_record``. ``testedGitSha``/
    ``testedVersion`` come EXCLUSIVELY from the authenticated build-provenance
    side, never the provider observation (Priority 1's core requirement)."""
    provider_raw_digest = _opt_str(provider_observation.get("sourceDigest")) or ""
    build_raw_digest = build_provenance.artifact_digest or build_provenance.raw_sha256() or ""
    combined_digest = "sha256:" + hashlib.sha256((provider_raw_digest + "|" + build_raw_digest).encode("utf-8")).hexdigest()
    evidence = {
        "schemaVersion": 2,
        "component": "caleemobile-distributed-build-acceptance",
        "provider": provider_observation.get("provider"),
        "channel": provider_observation.get("channel"),
        "distributedBuildId": provider_observation.get("providerRecordId"),
        "releaseId": provider_observation.get("releaseId"),
        "testedGitSha": build_provenance.source_git_sha,
        # Priority 1: ONE canonical full-version identity, derived from the
        # authenticated build-provenance fields (marketing + build number)
        # via identity_format.compose_full_version -- never the marketing-
        # only value, and never copied from an expectation.
        "testedVersion": canonical_tested_version,
        "marketingVersion": build_provenance.application_version,
        "platformBuildNumber": build_provenance.platform_build_number,
        # Priority 5: whether the provider's own marketing version confirmed
        # the build's version -- "verified" / "unavailable-from-provider" /
        # "mismatch" / "not-applicable" (Play: the release name is only a
        # display label, recorded as releaseLabel below).
        "providerMarketingVersionConfirmation": provider_marketing_confirmation,
        "providerReleaseLabel": (
            _opt_str(provider_observation.get("marketingVersion"))
            if provider_observation.get("provider") == "play_console" else None
        ),
        "providerAccountOrProject": provider_observation.get("providerAccountOrProject"),
        "providerRecordId": provider_observation.get("providerRecordId"),
        "providerObservedAt": provider_observation.get("providerObservedAt"),
        "generatedBy": "provider-build-provenance-join",
        "sourceDigest": combined_digest,
        "timestamp": build_provenance.build_timestamp or provider_observation.get("providerObservedAt"),
        "providerObservation": provider_observation,
        "buildProvenance": build_provenance.to_dict(),
    }
    # Priority 6: each retained raw source file's SHA-256, and the identity
    # binding of this joined evidence to the exact run/release/build.
    source_files: "dict[str, str]" = {}
    if provider_raw_sha256:
        source_files["provider-response.bin"] = provider_raw_sha256
    if build_raw_sha256:
        source_files["build-provenance-source.bin"] = build_raw_sha256
    if source_files:
        evidence["sourceFiles"] = source_files
    evidence["binding"] = {
        "runId": release_run_id,
        "releaseId": provider_observation.get("releaseId"),
        "productGitSha": build_provenance.source_git_sha,
        "platform": build_provenance.platform,
        "platformBuildNumber": build_provenance.platform_build_number,
    }
    return evidence
