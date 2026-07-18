"""Immutable source provenance vs. release adoption for selector evidence
(Priority 1, Problem B).

The release gate previously *mutated* an adopted selector artifact in place --
stamping the current run's ``releaseRunId`` and a ``generatedBy`` onto it and
then hashing the mutated result. That destroys the artifact's original
provenance: after adoption there is no way to tell what the CI run actually
recorded, and the digest proves nothing about the artifact as produced.

This module keeps the two concerns separate and verifiable:

  * ``sourceEvidence``  -- the adopted artifact, preserved byte-for-byte, with
    its ORIGINAL provenance (releaseRunId / workflowRunId / regressionSha /
    generatedBy / artifactId / artifactDigest) untouched;
  * ``sourceContentDigest`` -- a SHA-256 this framework computes over the
    canonical source evidence, verified BEFORE adoption and again at
    consolidation (so tampering with any field after the fact BLOCKS);
  * ``adoption`` -- the release run adopting it: this run's releaseRunId, when,
    by what, and from which path.

Provenance rules enforced here (``validate_source_provenance``):
  * ``generatedBy`` must be exactly ``ci`` or ``local``;
  * ``regressionSha`` must be a full 40-character SHA (when present);
  * ``workflowRunId`` is required when ``generatedBy == "ci"``;
  * verified local command evidence is required when ``generatedBy == "local"``;
  * contradictory provenance (e.g. ``ci`` with a local-verification block, or a
    self-declared ``artifactDigest`` that disagrees with the actual content) is
    rejected.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .identity_format import is_full_git_sha

GENERATED_BY_CI = "ci"
GENERATED_BY_LOCAL = "local"
GENERATED_BY_VALUES = frozenset({GENERATED_BY_CI, GENERATED_BY_LOCAL})

# Provenance keys that belong to the *source* artifact. Kept in one place so the
# byte-for-byte source view and the digest agree on what "the evidence" is.
_SOURCE_PROVENANCE_KEYS = (
    "releaseRunId",
    "workflowRunId",
    "regressionSha",
    "generatedBy",
    "artifactId",
    "artifactDigest",
)


class ProvenanceError(Exception):
    """A provenance record is structurally unusable (missing sourceEvidence,
    unreadable digest). A *rule* violation is returned as a problem list, not
    raised -- it is a verdict, like an identity mismatch."""


def content_digest(evidence: "dict[str, Any]") -> str:
    """Deterministic ``sha256:`` digest over the canonical JSON of ``evidence``.

    A self-referential ``artifactDigest`` field is excluded so a digest can be
    embedded in the same object it describes without changing its own value.
    Canonical = sorted keys, compact separators, so the digest depends only on
    content, not on key order or incidental whitespace.
    """
    payload = {k: v for k, v in evidence.items() if k != "artifactDigest"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _norm(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_source_provenance(
    source_evidence: "dict[str, Any]",
    *,
    local_verification: "dict[str, Any] | None" = None,
) -> "list[str]":
    """Return every provenance-rule violation for an adopted source artifact.

    An empty list means the source provenance is self-consistent and complete
    for its declared origin. ``local_verification`` is the verified local
    command evidence (from ``toolchain_verify``); it is required when the source
    declares ``generatedBy == "local"`` and must be absent/consistent otherwise.
    """
    problems: "list[str]" = []

    generated_by = _norm(source_evidence.get("generatedBy"))
    if generated_by is None:
        problems.append("source evidence has no generatedBy -- cannot tell how it was produced (expected 'ci' or 'local').")
    elif generated_by not in GENERATED_BY_VALUES:
        problems.append(f"source evidence generatedBy {generated_by!r} is not exactly 'ci' or 'local'.")

    regression_sha = _norm(source_evidence.get("regressionSha"))
    if regression_sha is not None and not is_full_git_sha(regression_sha):
        problems.append(
            f"source evidence regressionSha {regression_sha!r} is not a full 40-character SHA."
        )

    workflow_run_id = _norm(source_evidence.get("workflowRunId"))
    if generated_by == GENERATED_BY_CI:
        if workflow_run_id is None:
            problems.append("CI-generated source evidence must carry a workflowRunId (which CI run produced it).")
        if regression_sha is None:
            problems.append("CI-generated source evidence must carry a regressionSha (which CaleeMobile-Regression commit produced it).")
        if local_verification is not None:
            problems.append("contradictory provenance: generatedBy='ci' but a local-verification block is attached.")

    if generated_by == GENERATED_BY_LOCAL:
        if local_verification is None:
            problems.append("locally-generated source evidence requires verified local command evidence (Flutter toolchain was not actually run).")
        elif not local_verification.get("ok"):
            local_problems = local_verification.get("problems") or ["toolchain verification did not pass."]
            problems.append("local toolchain verification failed: " + "; ".join(str(p) for p in local_problems))

    # A self-declared artifactDigest that disagrees with the actual content is a
    # contradiction: the artifact claims a fingerprint it does not have.
    declared_digest = _norm(source_evidence.get("artifactDigest"))
    if declared_digest is not None and declared_digest.startswith("sha256:"):
        actual = content_digest(source_evidence)
        if declared_digest != actual:
            problems.append(
                f"source evidence's self-declared artifactDigest {declared_digest} "
                f"does not match its actual content digest {actual}."
            )

    return problems


def build_provenance_record(
    source_evidence: "dict[str, Any]",
    *,
    release_run_id: str,
    adopted_at: str,
    adopted_by: str,
    source_path: str,
    source_artifact_id: "str | None" = None,
    source_artifact_digest: "str | None" = None,
    local_verification: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """Build the immutable-source + adoption provenance record.

    ``source_evidence`` is preserved byte-for-byte (a deep copy, never mutated);
    its content digest is computed here so it can be re-verified downstream. The
    adoption block carries THIS run's context, kept strictly separate from the
    source's own provenance fields.
    """
    preserved = json.loads(json.dumps(source_evidence))  # deep copy, byte-for-byte content
    record: "dict[str, Any]" = {
        "sourceEvidence": preserved,
        "sourceContentDigest": content_digest(preserved),
        "adoption": {
            "releaseRunId": release_run_id,
            "adoptedAt": adopted_at,
            "adoptedBy": adopted_by,
            "sourcePath": source_path,
        },
    }
    # GitHub artifact identity, retained for traceability when the caller has it
    # (opaque to us -- our own sourceContentDigest is the one we verify).
    if _norm(source_artifact_id) is not None:
        record["sourceArtifactId"] = _norm(source_artifact_id)
    if _norm(source_artifact_digest) is not None:
        record["sourceArtifactDigest"] = _norm(source_artifact_digest)
    if local_verification is not None:
        record["localVerification"] = local_verification
    return record


def verify_provenance_record(record: "dict[str, Any]") -> "list[str]":
    """Re-verify a recorded provenance record at consolidation.

    Recomputes the content digest of the preserved ``sourceEvidence`` and
    compares it to the recorded ``sourceContentDigest`` (so tampering with any
    field after the digest was generated BLOCKS), and re-runs the source
    provenance rules. Returns every problem found (empty == intact).
    """
    problems: "list[str]" = []
    source_evidence = record.get("sourceEvidence")
    if not isinstance(source_evidence, dict):
        raise ProvenanceError("provenance record has no sourceEvidence object.")

    recorded_digest = _norm(record.get("sourceContentDigest"))
    if recorded_digest is None:
        problems.append("provenance record has no sourceContentDigest -- source evidence cannot be integrity-checked.")
    else:
        actual = content_digest(source_evidence)
        if actual != recorded_digest:
            problems.append(
                f"source evidence digest mismatch: recorded {recorded_digest}, "
                f"actual {actual} -- the preserved source evidence was modified after adoption."
            )

    local_verification = record.get("localVerification") if isinstance(record.get("localVerification"), dict) else None
    problems.extend(validate_source_provenance(source_evidence, local_verification=local_verification))
    return problems


def source_evidence_of(record_or_report: "dict[str, Any]") -> "dict[str, Any] | None":
    """Best-effort extraction of the source evidence dict from either a
    provenance record or a full gate report (which nests it under
    ``provenance``). Returns None when neither shape carries one."""
    if not isinstance(record_or_report, dict):
        return None
    if isinstance(record_or_report.get("sourceEvidence"), dict):
        return record_or_report["sourceEvidence"]
    prov = record_or_report.get("provenance")
    if isinstance(prov, dict) and isinstance(prov.get("sourceEvidence"), dict):
        return prov["sourceEvidence"]
    return None
