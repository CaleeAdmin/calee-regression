"""Immutable source provenance vs. release adoption for selector evidence
(Priority 1 Problem B; raw-byte preservation + envelope integrity, Priority 3).

The release gate previously *mutated* an adopted selector artifact in place --
stamping the current run's ``releaseRunId`` and a ``generatedBy`` onto it and
then hashing the mutated result. That destroys the artifact's original
provenance. This module keeps source and adoption separate and verifiable.

Priority 3 adds genuine raw-byte preservation. Two digests are kept, and they
are NOT the same thing:

  * ``sourceContentDigest`` -- a *semantic* SHA-256 over the canonical JSON of
    the parsed evidence (sorted keys, compact). Useful for comparing two results
    for logical equality regardless of key order/whitespace. This is a digest of
    a *reparsed* object -- it must never be described as "byte-for-byte".
  * ``sourceResultSha256`` / ``sourceArtifactZipSha256`` -- *raw-byte* SHA-256 of
    the exact extracted ``source-result.json`` bytes and the exact downloaded
    ``source-artifact.zip`` bytes, hashed as stored, never reparsed/reserialised.
    These are what "byte-for-byte" means here.

The whole provenance record is then protected by ``envelopeDigest``: a SHA-256
over the entire envelope (every field except the digest itself), so tampering
with ANY provenance, adoption, local-verification, artifact-id, workflow-run-id
or regression-SHA field BLOCKS at consolidation. The raw sidecar files
(``source-result.sha256`` / ``source-artifact.sha256``) let a human or a later
tool re-verify the raw bytes against the digests the envelope protects.

Bundle layout (see :func:`write_evidence_bundle`):

    selector-contract/source-artifact.zip     (raw ZIP bytes, unmodified)
    selector-contract/source-result.json      (raw JSON bytes, unmodified)
    selector-contract/source-result.sha256     (raw-byte digest of the JSON)
    selector-contract/source-artifact.sha256    (raw-byte digest of the ZIP)
    selector-contract/provenance.json          (this record, envelope-protected)

Provenance rules enforced (``validate_source_provenance``):
  * ``generatedBy`` must be exactly ``ci`` or ``local``;
  * ``regressionSha`` must be a full 40-character SHA (when present);
  * ``workflowRunId`` is required when ``generatedBy == "ci"``;
  * verified local command evidence is required when ``generatedBy == "local"``;
  * contradictory provenance (``ci`` with a local-verification block, or a
    self-declared ``artifactDigest`` disagreeing with the actual content) fails.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .identity_format import is_full_git_sha

GENERATED_BY_CI = "ci"
GENERATED_BY_LOCAL = "local"
GENERATED_BY_VALUES = frozenset({GENERATED_BY_CI, GENERATED_BY_LOCAL})

# Provenance keys that belong to the *source* artifact.
_SOURCE_PROVENANCE_KEYS = (
    "releaseRunId",
    "workflowRunId",
    "regressionSha",
    "generatedBy",
    "artifactId",
    "artifactDigest",
)

# Bundle filenames (Priority 3 layout).
BUNDLE_ARTIFACT_ZIP = "source-artifact.zip"
BUNDLE_RESULT_JSON = "source-result.json"
BUNDLE_RESULT_SHA = "source-result.sha256"
BUNDLE_ARTIFACT_SHA = "source-artifact.sha256"
BUNDLE_PROVENANCE = "provenance.json"


class ProvenanceError(Exception):
    """A provenance record is structurally unusable (missing sourceEvidence,
    unreadable digest). A *rule* violation is returned as a problem list, not
    raised -- it is a verdict, like an identity mismatch."""


def raw_sha256(data: bytes) -> str:
    """``sha256:<hex>`` of exact bytes, hashed as-is (never reparsed)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_digest(evidence: "dict[str, Any]") -> str:
    """Deterministic ``sha256:`` *semantic* digest over the canonical JSON of
    ``evidence`` (sorted keys, compact separators). A self-referential
    ``artifactDigest`` field is excluded so a digest can be embedded in the same
    object it describes without changing its own value.

    NOTE: this is a digest of a reparsed object, not of raw bytes -- it proves
    logical equality, and must not be called "byte-for-byte". For byte identity
    use :func:`raw_sha256` over the preserved source bytes.
    """
    payload = {k: v for k, v in evidence.items() if k != "artifactDigest"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_bytes(obj: "Any") -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def envelope_digest(record: "dict[str, Any]") -> str:
    """``sha256:`` over the entire provenance envelope EXCEPT its own
    ``envelopeDigest`` field.

    Because the envelope embeds the source-evidence object (with its
    ``workflowRunId`` / ``regressionSha``), the source content + raw-byte
    digests, the artifact id, the adoption block and the local-verification
    block, this single digest covers every mutable field Priority 3 requires be
    protected. Any change to any of them changes this digest.
    """
    payload = {k: v for k, v in record.items() if k != "envelopeDigest"}
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


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
    for its declared origin.
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
    raw_result_bytes: "bytes | None" = None,
    raw_zip_bytes: "bytes | None" = None,
) -> "dict[str, Any]":
    """Build the immutable-source + adoption provenance record, envelope-protected.

    ``source_evidence`` is preserved as a semantic deep copy (``sourceEvidence``)
    with a canonical ``sourceContentDigest``. When the exact source bytes are
    supplied (from the GitHub artifact chain), their *raw-byte* digests are
    recorded too:

      * ``raw_result_bytes`` -> ``sourceResultSha256`` (the exact JSON bytes);
      * ``raw_zip_bytes``    -> ``sourceArtifactZipSha256`` (the exact ZIP bytes).

    Finally an ``envelopeDigest`` over the whole record protects every field.
    """
    preserved = json.loads(json.dumps(source_evidence))  # semantic deep copy (parsed view)
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
    # Raw-byte digests of the exact preserved files (Priority 3.1-3.3). These are
    # what "byte-for-byte" means -- distinct from the semantic content digest.
    if raw_result_bytes is not None:
        record["sourceResultSha256"] = raw_sha256(raw_result_bytes)
    if raw_zip_bytes is not None:
        record["sourceArtifactZipSha256"] = raw_sha256(raw_zip_bytes)
    # GitHub artifact identity, retained for traceability when the caller has it.
    if _norm(source_artifact_id) is not None:
        record["sourceArtifactId"] = _norm(source_artifact_id)
    if _norm(source_artifact_digest) is not None:
        record["sourceArtifactDigest"] = _norm(source_artifact_digest)
    if local_verification is not None:
        record["localVerification"] = local_verification
    # Envelope digest LAST, over everything above (Priority 3.5).
    record["envelopeDigest"] = envelope_digest(record)
    return record


def verify_provenance_record(
    record: "dict[str, Any]",
    *,
    result_bytes: "bytes | None" = None,
    zip_bytes: "bytes | None" = None,
) -> "list[str]":
    """Re-verify a recorded provenance record at consolidation (Priority 3.6-3.7).

    Recomputes, and BLOCKS (returns a problem) on any mismatch of:
      * the envelope digest over the whole record;
      * the semantic content digest of the preserved ``sourceEvidence``;
      * (when the raw files are supplied) the raw-byte digests of
        ``source-result.json`` and ``source-artifact.zip``.
    Then re-runs the source provenance rules. Empty list == intact.
    """
    problems: "list[str]" = []
    source_evidence = record.get("sourceEvidence")
    if not isinstance(source_evidence, dict):
        raise ProvenanceError("provenance record has no sourceEvidence object.")

    # --- envelope integrity: any tampered field changes this digest ---------
    recorded_envelope = _norm(record.get("envelopeDigest"))
    if recorded_envelope is None:
        problems.append("provenance record has no envelopeDigest -- the envelope is not integrity-protected.")
    else:
        actual_envelope = envelope_digest(record)
        if actual_envelope != recorded_envelope:
            problems.append(
                f"provenance envelope digest mismatch: recorded {recorded_envelope}, actual "
                f"{actual_envelope} -- a provenance/adoption/local-verification field was modified."
            )

    # --- semantic content digest of the preserved evidence ------------------
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

    # --- raw-byte digests of the preserved files ----------------------------
    if result_bytes is not None:
        recorded_result = _norm(record.get("sourceResultSha256"))
        if recorded_result is None:
            problems.append("provenance record has no sourceResultSha256 -- the raw JSON bytes are not protected.")
        elif raw_sha256(result_bytes) != recorded_result:
            problems.append(
                f"source-result.json raw-byte digest mismatch: recorded {recorded_result}, "
                f"actual {raw_sha256(result_bytes)} -- the preserved JSON bytes were altered."
            )
    if zip_bytes is not None:
        recorded_zip = _norm(record.get("sourceArtifactZipSha256"))
        if recorded_zip is None:
            problems.append("provenance record has no sourceArtifactZipSha256 -- the raw ZIP bytes are not protected.")
        elif raw_sha256(zip_bytes) != recorded_zip:
            problems.append(
                f"source-artifact.zip raw-byte digest mismatch: recorded {recorded_zip}, "
                f"actual {raw_sha256(zip_bytes)} -- the preserved ZIP bytes were altered."
            )

    local_verification = record.get("localVerification") if isinstance(record.get("localVerification"), dict) else None
    problems.extend(validate_source_provenance(source_evidence, local_verification=local_verification))
    return problems


def write_evidence_bundle(
    directory: "Path | str",
    record: "dict[str, Any]",
    *,
    result_bytes: "bytes | None" = None,
    zip_bytes: "bytes | None" = None,
) -> "list[str]":
    """Write the Priority-3 evidence bundle into ``directory`` and return the
    filenames written.

    The raw bytes are written verbatim -- ``source-result.json`` / and
    ``source-artifact.zip`` are the exact preserved bytes, never reserialised --
    alongside their raw-byte ``.sha256`` sidecars and the envelope-protected
    ``provenance.json``.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: "list[str]" = []

    if zip_bytes is not None:
        (directory / BUNDLE_ARTIFACT_ZIP).write_bytes(zip_bytes)
        (directory / BUNDLE_ARTIFACT_SHA).write_text(
            raw_sha256(zip_bytes) + "  " + BUNDLE_ARTIFACT_ZIP + "\n", encoding="utf-8"
        )
        written += [BUNDLE_ARTIFACT_ZIP, BUNDLE_ARTIFACT_SHA]

    if result_bytes is not None:
        (directory / BUNDLE_RESULT_JSON).write_bytes(result_bytes)
        (directory / BUNDLE_RESULT_SHA).write_text(
            raw_sha256(result_bytes) + "  " + BUNDLE_RESULT_JSON + "\n", encoding="utf-8"
        )
        written += [BUNDLE_RESULT_JSON, BUNDLE_RESULT_SHA]

    (directory / BUNDLE_PROVENANCE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    written.append(BUNDLE_PROVENANCE)
    return written


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
