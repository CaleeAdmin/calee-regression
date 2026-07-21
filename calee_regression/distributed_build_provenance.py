"""Authenticated distributed-build acceptance provenance (Priority 3, this
session).

``distributed_build_acceptance.py``'s original evidence shape trusted the
CLI operator's own ``--verified-via`` label: nothing there proved an App
Store Connect/TestFlight/Play Console check ever actually happened. This
module adds a provenance layer -- mirroring ``selector_provenance.py``'s
proven raw-byte-digest + envelope-digest + adoption-record pattern, already
tamper-tested in this codebase for a different (selector-contract) evidence
stream -- so a PASS can be produced only from:

  * an authenticated App Store Connect/TestFlight API result;
  * an authenticated Play Console API result;
  * a signed store-export evidence package; or
  * a retained CI artifact that itself contains authenticated provider
    evidence.

A manually-typed identity claim (``distributed_build_acceptance.py``'s
original flat, unprovenanced shape) can never reach PASS any more -- see
that module's own docstring and ``consolidated_report.
component_from_distributed_build_acceptance_report``.

IMPORTANT -- what THIS module alone can and cannot prove: ``validate_
distributed_evidence`` below only checks that a ``sourceEvidence`` dict is
well-FORMED (schema, provider/channel allow-lists, a ``generatedBy``-
specific proof-shape requirement like "signed-export needs a non-empty
signatureOrArtifactProvenance object"). A hand-typed JSON file that simply
claims ``generatedBy: "provider-api"`` with plausible-looking fields passes
every one of those checks -- this module has no way to know the claim
wasn't backed by a real API call. The actual "was this ever independently
authenticated" gate lives one layer up, in ``provider_evidence.py``: only
its live collectors / real signature verification / authenticated-artifact
chain may set a record's ``evidenceTier`` to one of ``provider_evidence.
AUTHENTICATED_TIERS`` (via :func:`build_provenance_record`'s
``evidence_tier`` parameter -- see that parameter's own docstring). ``cli.
py``'s ``record-distributed-build-acceptance`` stamps ``manual-unverified``
unconditionally on its ``--source`` path, never from the file's own claimed
content, and ``consolidated_report.component_from_distributed_build_
acceptance_report`` independently re-checks ``evidenceTier`` membership at
consolidation -- never trusting a report's recorded ``status`` alone. Treat
this module as the tamper-evidence + content-shape layer; treat
``provider_evidence.py`` as the origin-authentication layer they compose
with.

Two independent digests, same distinction ``selector_provenance.py`` draws:

  * ``sourceContentDigest`` -- a *semantic* digest over the canonical JSON of
    the parsed evidence (sorted keys, compact) -- logical equality, not
    byte-for-byte;
  * ``sourceRawSha256`` -- a *raw-byte* digest of the exact preserved evidence
    file bytes, hashed as stored, never reparsed. This is what "byte-for-byte"
    means, and what catches an altered source file after adoption.

The whole record is then protected by ``envelopeDigest``: a SHA-256 over
every field except itself, so tampering with ANY field -- provider, channel,
provider-record identifiers, the adoption block, or the raw-byte digest --
BLOCKS at consolidation.

Bundle layout (see :func:`write_evidence_bundle`), written under
``reports/runs/<run-id>/distributed-build-acceptance/``:

    distributed-build-source.json      (raw evidence bytes, unmodified)
    distributed-build-source.sha256    (raw-byte digest sidecar)
    distributed-build-provenance.json  (this record, envelope-protected)
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from .distributed_build_acceptance import VALID_CHANNELS
from .identity_format import is_full_git_sha, is_wellformed_version

DISTRIBUTED_PROVENANCE_SCHEMA_VERSION = 2
SUPPORTED_PROVENANCE_SCHEMA_VERSIONS = frozenset({DISTRIBUTED_PROVENANCE_SCHEMA_VERSION})

DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT = "caleemobile-distributed-build-acceptance"

# How the underlying source evidence was actually produced -- the load-bearing
# anti-fabrication field, exactly analogous to selector_provenance.py's
# generatedBy ("ci"/"local"), but distinguishing the authentic sources this
# module accepts.
GENERATED_BY_PROVIDER_API = "provider-api"
GENERATED_BY_SIGNED_EXPORT = "signed-export"
GENERATED_BY_CI_ARTIFACT = "ci-artifact"
# Priority 1 (this session): the distributed-build IDENTITY CHAIN join --
# stamped only by build_provenance.join_provider_and_build_provenance, once
# BOTH an independently-authenticated provider observation AND an
# independently-authenticated build-provenance record have named the SAME
# immutable platform build. This is the ONLY generatedBy value whose
# testedGitSha/testedVersion are proven by an authenticated SOURCE (never the
# provider) -- see that function's docstring.
GENERATED_BY_PROVIDER_BUILD_JOIN = "provider-build-provenance-join"
VALID_GENERATED_BY = frozenset({
    GENERATED_BY_PROVIDER_API, GENERATED_BY_SIGNED_EXPORT, GENERATED_BY_CI_ARTIFACT, GENERATED_BY_PROVIDER_BUILD_JOIN,
})
# Explicitly-named rejected sources, so a rejection message is precise rather
# than a generic "not recognised" -- mirrors distributed_build_acceptance.py's
# REJECTED_VERIFIED_VIA (kept in sync so both the legacy and provenance paths
# refuse the same fabricated claims).
REJECTED_GENERATED_BY = frozenset({"local_checkout", "unsigned_build", "manual_claim"})

VALID_PROVIDERS = frozenset({"app_store_connect", "play_console", "custom_signed_export"})

DEFAULT_FRESHNESS = datetime.timedelta(days=30)
FUTURE_SKEW = datetime.timedelta(minutes=5)

# Bundle filenames.
BUNDLE_SOURCE_JSON = "distributed-build-source.json"
BUNDLE_SOURCE_SHA = "distributed-build-source.sha256"
BUNDLE_PROVENANCE = "distributed-build-provenance.json"


class DistributedProvenanceError(Exception):
    """A provenance record is structurally unusable (no sourceEvidence, an
    unreadable digest) -- a framework fault, never a verdict."""


def raw_sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def content_digest(evidence: "dict[str, Any]") -> str:
    """Semantic sha256: digest over canonical JSON, excluding the
    self-referential ``sourceDigest`` field."""
    payload = {k: v for k, v in evidence.items() if k != "sourceDigest"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_bytes(obj: "Any") -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def envelope_digest(record: "dict[str, Any]") -> str:
    payload = {k: v for k, v in record.items() if k != "envelopeDigest"}
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _norm(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_utc_iso8601(value: "str | None") -> "datetime.datetime | None":
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    if parsed.utcoffset() != datetime.timedelta(0):
        return None
    return parsed.astimezone(datetime.timezone.utc)


def validate_distributed_evidence(
    evidence: "dict[str, Any]",
    *,
    expected_git_sha: "str | None" = None,
    expected_version: "str | None" = None,
    expected_release_id: "str | None" = None,
    now: "datetime.datetime | None" = None,
    max_age: "datetime.timedelta | None" = DEFAULT_FRESHNESS,
    future_skew: datetime.timedelta = FUTURE_SKEW,
) -> "list[str]":
    """Validate one distributed-build provenance evidence envelope (the
    minimum schema: schemaVersion/component/provider/channel/
    distributedBuildId/releaseId/testedGitSha/testedVersion/
    providerAccountOrProject/providerRecordId/providerObservedAt/
    generatedBy/sourceDigest/timestamp). Returns problems (empty == accepted).

    Never accepts on faith: every identity field is format-checked, every
    provenance field is required and cross-checked, and a
    ``generatedBy``-specific proof requirement is enforced so a
    self-declared provider label with nothing backing it is rejected (Priority
    3.9: "self-declared provider labels without source proof")."""
    problems: "list[str]" = []
    if not isinstance(evidence, dict):
        return ["distributed-build evidence must be a JSON object."]

    schema_version = evidence.get("schemaVersion")
    if schema_version is None:
        problems.append("no schemaVersion recorded in the distributed-build evidence.")
    elif schema_version not in SUPPORTED_PROVENANCE_SCHEMA_VERSIONS:
        problems.append(
            f"schemaVersion {schema_version!r} is not a supported distributed-build provenance version "
            f"(supported: {sorted(SUPPORTED_PROVENANCE_SCHEMA_VERSIONS)})."
        )

    component = _norm(evidence.get("component"))
    if component is not None and component != DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT:
        problems.append(f"unexpected component {component!r} (expected {DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT!r}).")

    provider = _norm(evidence.get("provider"))
    if not provider:
        problems.append("no provider recorded -- distributed-build evidence must name which store/provider produced it.")
    elif provider not in VALID_PROVIDERS:
        problems.append(f"provider {provider!r} is not a recognised provider (expected one of {sorted(VALID_PROVIDERS)}).")

    channel = _norm(evidence.get("channel"))
    if not channel:
        problems.append("no distribution channel recorded in the distributed-build evidence.")
    elif channel not in VALID_CHANNELS:
        problems.append(f"channel {channel!r} is not a recognised distribution channel (expected one of {sorted(VALID_CHANNELS)}).")

    if not _norm(evidence.get("distributedBuildId")):
        problems.append(
            "no distributed build identifier (TestFlight build number / App Store Connect version / "
            "Play Console release id) recorded."
        )

    provider_account = _norm(evidence.get("providerAccountOrProject"))
    if not provider_account:
        problems.append("no providerAccountOrProject recorded -- cannot prove WHICH provider account/project produced this evidence.")

    provider_record_id = _norm(evidence.get("providerRecordId"))
    if not provider_record_id:
        problems.append("no providerRecordId recorded -- cannot prove a specific provider record backs this evidence.")

    provider_observed_at = _norm(evidence.get("providerObservedAt"))
    if not provider_observed_at:
        problems.append("no providerObservedAt recorded -- cannot prove WHEN the provider actually observed this build.")
    elif _parse_utc_iso8601(provider_observed_at) is None:
        problems.append(f"providerObservedAt {provider_observed_at!r} is not a valid UTC ISO-8601 instant.")

    generated_by = _norm(evidence.get("generatedBy"))
    if not generated_by:
        problems.append(
            "no generatedBy recorded -- distributed-build acceptance must be verified through a real "
            "provider API, a signed store export, or a retained CI artifact, never fabricated."
        )
    elif generated_by in REJECTED_GENERATED_BY:
        problems.append(
            f"generatedBy {generated_by!r} is explicitly rejected -- acceptance can never be fabricated "
            f"from a local checkout, an unsigned build, or a manual claim."
        )
    elif generated_by not in VALID_GENERATED_BY:
        problems.append(f"generatedBy {generated_by!r} is not a recognised authentic source (expected one of {sorted(VALID_GENERATED_BY)}).")
    else:
        # generatedBy-specific proof requirements -- Priority 3.9: a
        # self-declared label alone is never enough.
        if generated_by == GENERATED_BY_PROVIDER_API and provider not in ("app_store_connect", "play_console"):
            problems.append(
                f"generatedBy 'provider-api' requires provider to be 'app_store_connect' or 'play_console' "
                f"(got {provider!r})."
            )
        if generated_by == GENERATED_BY_SIGNED_EXPORT:
            sig = evidence.get("signatureOrArtifactProvenance")
            if not isinstance(sig, dict) or not sig:
                problems.append(
                    "generatedBy 'signed-export' requires a non-empty signatureOrArtifactProvenance object "
                    "(e.g. signer identity + signature) -- a signed-export label with no signature attached "
                    "proves nothing."
                )
        if generated_by == GENERATED_BY_CI_ARTIFACT and not provider_record_id:
            problems.append("generatedBy 'ci-artifact' requires providerRecordId to name the retained CI run/artifact.")
        if generated_by == GENERATED_BY_PROVIDER_BUILD_JOIN:
            # Priority 1: a self-declared join label alone is never enough --
            # both halves of the chain must actually be present and shaped
            # like what build_provenance.join_provider_and_build_provenance
            # produces (full re-verification of each half's own authenticity
            # is the CALLER's responsibility -- e.g. consolidated_report.py
            # only ever trusts evidenceTier, never this label; this is a
            # structural presence check only).
            provider_observation = evidence.get("providerObservation")
            build_provenance = evidence.get("buildProvenance")
            if not isinstance(provider_observation, dict) or not provider_observation:
                problems.append(
                    "generatedBy 'provider-build-provenance-join' requires a non-empty providerObservation "
                    "object -- a join label with no provider-side record attached proves nothing."
                )
            if not isinstance(build_provenance, dict) or not build_provenance:
                problems.append(
                    "generatedBy 'provider-build-provenance-join' requires a non-empty buildProvenance "
                    "object -- a join label with no build-provenance-side record attached proves nothing."
                )

    tested_git_sha = _norm(evidence.get("testedGitSha"))
    if not tested_git_sha:
        problems.append("no tested CaleeMobile SHA recorded in the distributed-build evidence.")
    elif not is_full_git_sha(tested_git_sha):
        problems.append(f"tested SHA {tested_git_sha!r} is abbreviated/ambiguous (need the full 40-character SHA).")

    tested_version = _norm(evidence.get("testedVersion"))
    if not tested_version:
        problems.append("no tested CaleeMobile version recorded in the distributed-build evidence.")
    elif not is_wellformed_version(tested_version):
        problems.append(f"tested version {tested_version!r} is not a well-formed version identity.")

    if not _norm(evidence.get("sourceDigest")):
        problems.append("no sourceDigest recorded -- the evidence envelope is not self-describing its own content digest.")

    timestamp = _norm(evidence.get("timestamp"))
    if not timestamp:
        problems.append("no timestamp recorded in the distributed-build evidence.")
    else:
        parsed_ts = _parse_utc_iso8601(timestamp)
        if parsed_ts is None:
            problems.append(f"timestamp {timestamp!r} is not a valid UTC ISO-8601 instant.")
        else:
            reference = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=datetime.timezone.utc)
            if parsed_ts > reference + future_skew:
                problems.append(f"timestamp {timestamp!r} is in the future relative to now ({reference.isoformat()}).")
            elif max_age is not None and (reference - parsed_ts) > max_age:
                age = reference - parsed_ts
                problems.append(f"timestamp {timestamp!r} is stale: {age.days}d old, older than the {max_age.days}d freshness window.")

    if expected_git_sha is not None:
        if not is_full_git_sha(expected_git_sha):
            problems.append(f"expected CaleeMobile SHA {expected_git_sha!r} is abbreviated/ambiguous; configure the full SHA.")
        elif tested_git_sha and tested_git_sha.lower() != expected_git_sha.strip().lower():
            problems.append(
                f"tested SHA {tested_git_sha!r} != expected release SHA {expected_git_sha!r} -- this evidence "
                f"is for a different CaleeMobile commit than the one being released."
            )

    if expected_version is not None and tested_version and tested_version != expected_version.strip():
        problems.append(
            f"tested version {tested_version!r} != expected release version {expected_version!r} -- this "
            f"evidence is for a different CaleeMobile version than the one being released."
        )

    release_id = _norm(evidence.get("releaseId"))
    if expected_release_id is not None:
        if not (expected_release_id or "").strip():
            problems.append("expected release ID is empty.")
        elif not release_id:
            problems.append("no releaseId recorded in the distributed-build evidence -- it is not bound to this release.")
        elif release_id != str(expected_release_id).strip():
            problems.append(
                f"evidence releaseId {release_id!r} != expected release {expected_release_id!r} -- refusing "
                f"to accept distributed-build evidence for another release."
            )

    return problems


def build_provenance_record(
    source_evidence: "dict[str, Any]",
    *,
    release_run_id: str,
    adopted_at: str,
    adopted_by: str,
    source_path: str,
    raw_source_bytes: "bytes | None" = None,
    evidence_tier: "str | None" = None,
) -> "dict[str, Any]":
    """Build the immutable-source + adoption provenance record (Priority
    3.4-3.6), envelope-protected. Mirrors
    ``selector_provenance.build_provenance_record``.

    ``evidence_tier``, when given, is stamped into the record (and so
    covered by ``envelopeDigest`` like every other field -- tampering with
    it after the fact is detected exactly like tampering with any other
    field). It must be set by the CALLER's own control flow -- e.g. which
    of ``provider_evidence.py``'s live-collection/signed-export/CI-artifact
    paths actually ran and verified successfully -- never copied from
    ``source_evidence`` content itself, or an operator-supplied claim could
    simply declare its own tier. See ``provider_evidence.AUTHENTICATED_
    TIERS`` for which tiers this can ever justify a PASS."""
    preserved = json.loads(json.dumps(source_evidence))
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
    if raw_source_bytes is not None:
        record["sourceRawSha256"] = raw_sha256(raw_source_bytes)
    if evidence_tier is not None:
        record["evidenceTier"] = evidence_tier
    record["envelopeDigest"] = envelope_digest(record)
    return record


def verify_provenance_record(
    record: "dict[str, Any]",
    *,
    source_bytes: "bytes | None" = None,
    trusted_envelope_digest: "str | None" = None,
    expected_release_run_id: "str | None" = None,
    **validate_kwargs: Any,
) -> "list[str]":
    """Re-verify a recorded distributed-build provenance record at
    consolidation. Recomputes and BLOCKS (returns a problem) on any mismatch
    of the envelope digest, the semantic content digest, and (when supplied)
    the raw-byte digest of the preserved source file -- then re-runs the
    evidence validation rules. Empty list == intact and accepted.

    ``trusted_envelope_digest``: see selector_provenance.verify_provenance_
    record's identical tamper-evidence-limitation docstring -- a plain
    recompute only proves internal consistency; a digest anchored outside
    this mutable bundle (e.g. a signed release-config) is needed to defeat a
    coordinated edit+rehash."""
    problems: "list[str]" = []
    source_evidence = record.get("sourceEvidence")
    if not isinstance(source_evidence, dict):
        raise DistributedProvenanceError("distributed-build provenance record has no sourceEvidence object.")

    recorded_envelope = _norm(record.get("envelopeDigest"))
    if recorded_envelope is None:
        problems.append("distributed-build provenance record has no envelopeDigest -- the envelope is not integrity-protected.")
    else:
        actual_envelope = envelope_digest(record)
        if actual_envelope != recorded_envelope:
            problems.append(
                f"distributed-build provenance envelope digest mismatch: recorded {recorded_envelope}, "
                f"actual {actual_envelope} -- a provenance/adoption field was modified."
            )

    if trusted_envelope_digest is not None:
        anchored = _norm(trusted_envelope_digest)
        if anchored is not None and recorded_envelope is not None and anchored != recorded_envelope:
            problems.append(
                f"distributed-build provenance envelopeDigest {recorded_envelope} does not match the "
                f"trusted anchored digest {anchored} -- the bundle was replaced or re-hashed after adoption."
            )

    recorded_digest = _norm(record.get("sourceContentDigest"))
    if recorded_digest is None:
        problems.append("distributed-build provenance record has no sourceContentDigest -- source evidence cannot be integrity-checked.")
    else:
        actual = content_digest(source_evidence)
        if actual != recorded_digest:
            problems.append(
                f"distributed-build source evidence digest mismatch: recorded {recorded_digest}, actual "
                f"{actual} -- the preserved source evidence was modified after adoption."
            )

    if source_bytes is not None:
        recorded_raw = _norm(record.get("sourceRawSha256"))
        if recorded_raw is None:
            problems.append("distributed-build provenance record has no sourceRawSha256 -- the raw evidence bytes are not protected.")
        elif raw_sha256(source_bytes) != recorded_raw:
            problems.append(
                f"distributed-build-source.json raw-byte digest mismatch: recorded {recorded_raw}, actual "
                f"{raw_sha256(source_bytes)} -- the preserved evidence bytes were altered."
            )

    adoption = record.get("adoption") if isinstance(record.get("adoption"), dict) else {}
    adopted_run = _norm(adoption.get("releaseRunId"))
    if expected_release_run_id is not None:
        if not adopted_run:
            problems.append("distributed-build provenance adoption has no releaseRunId -- evidence is not tied to this release run.")
        elif adopted_run != str(expected_release_run_id).strip():
            problems.append(
                f"distributed-build adoption releaseRunId {adopted_run!r} != current release run "
                f"{str(expected_release_run_id).strip()!r} -- this evidence was adopted by a different run."
            )

    problems.extend(validate_distributed_evidence(source_evidence, **validate_kwargs))
    return problems


def write_evidence_bundle(
    directory: "Path | str",
    record: "dict[str, Any]",
    *,
    source_bytes: "bytes | None" = None,
) -> "list[str]":
    """Write the distributed-build evidence bundle into ``directory``. The raw
    source bytes are written verbatim (never reserialised), alongside their
    raw-byte ``.sha256`` sidecar and the envelope-protected provenance
    record."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written: "list[str]" = []

    if source_bytes is not None:
        (directory / BUNDLE_SOURCE_JSON).write_bytes(source_bytes)
        (directory / BUNDLE_SOURCE_SHA).write_text(
            raw_sha256(source_bytes) + "  " + BUNDLE_SOURCE_JSON + "\n", encoding="utf-8"
        )
        written += [BUNDLE_SOURCE_JSON, BUNDLE_SOURCE_SHA]

    (directory / BUNDLE_PROVENANCE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    written.append(BUNDLE_PROVENANCE)
    return written


def source_evidence_of(record: "dict[str, Any] | None") -> "dict[str, Any] | None":
    if not isinstance(record, dict):
        return None
    if isinstance(record.get("sourceEvidence"), dict):
        return record["sourceEvidence"]
    return None
