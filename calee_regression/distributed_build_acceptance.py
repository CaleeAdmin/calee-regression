"""Distributed-build acceptance evidence (Priority 3).

When a schema-v2 release manifest declares
``caleeMobile.distributedBuildAcceptanceRequired: true``, the release must
carry explicit, externally-verifiable proof that a distributed/TestFlight/
store build's identity matches the release candidate -- never a fabricated
claim derived from a local checkout or an unsigned build. This module is the
*consuming* side, mirroring selector_evidence.py's shape: a schema, a parser,
and a verifier that rejects evidence that does not prove the expected
CaleeMobile identity, or whose ``verifiedVia`` provenance is not a real
distributed/store verification source.

Evidence is rejected (the verdict is not ``ok``) when:

  * the schema version is unknown/unsupported;
  * the component marker is missing or wrong;
  * the distribution channel is missing or not one of the recognised
    channels (TestFlight, App Store, Play Console internal testing,
    enterprise distribution);
  * no distributed build identifier (TestFlight build number / App Store
    Connect version / Play Console release id) is recorded;
  * ``verifiedVia`` is missing, or names a rejected local/unsigned/manual
    source, or is not a recognised real verification source;
  * the tested SHA is missing/abbreviated, or the tested version is
    malformed;
  * the timestamp is missing, invalid, in the future, or stale;
  * the tested SHA/version differs from the expected release identity; or
  * the evidence is bound to a different release ID.

With no physical/distributed evidence recorded at all, the caller (see
``consolidated_report.component_from_distributed_build_acceptance_report``)
records BLOCKED -- this module never fabricates an acceptance verdict from
absent evidence.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity_format import is_full_git_sha, is_wellformed_version

DISTRIBUTED_BUILD_ACCEPTANCE_SCHEMA_VERSION = 1
# Schema versions this consumer knows how to read -- an evidence file
# declaring a version outside this set was produced by an emitter this
# consumer does not understand yet, and must be refused rather than guessed.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT = "caleemobile-distributed-build-acceptance"

# Recognised distribution channels a distributed build can come from.
VALID_CHANNELS = frozenset({
    "testflight", "app_store", "play_console_internal", "enterprise_distribution",
})

# How the acceptance evidence was actually verified. Only a real, externally
# observable check counts -- a local checkout or an unsigned build must never
# stand in for distributed/store acceptance (see the module docstring).
VALID_VERIFIED_VIA = frozenset({
    "testflight_api", "app_store_connect_api", "play_console_api", "signed_store_export",
})
# Explicitly-named rejected provenance markers, so a rejection message is
# precise ("fabricated from a local checkout") rather than a generic
# "not a recognised source".
REJECTED_VERIFIED_VIA = frozenset({"local_checkout", "unsigned_build", "manual_claim"})

DEFAULT_FRESHNESS = datetime.timedelta(days=30)
FUTURE_SKEW = datetime.timedelta(minutes=5)


class DistributedBuildAcceptanceError(Exception):
    """Distributed-build-acceptance evidence is missing, unreadable, or
    malformed (a framework/pipeline fault), as opposed to a genuine identity
    mismatch (reported as a verdict, not raised)."""


@dataclass
class DistributedBuildAcceptanceResult:
    """One distributed-build acceptance record's machine-readable result."""

    schema_version: "int | None" = None
    component: "str | None" = DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT
    channel: "str | None" = None
    # TestFlight build number / App Store Connect version string / Play
    # Console release id -- whatever the channel's own store uses to name
    # this exact distributed build.
    distributed_build_id: "str | None" = None
    tested_git_sha: "str | None" = None
    tested_version: "str | None" = None
    # How this record was actually verified -- see VALID_VERIFIED_VIA /
    # REJECTED_VERIFIED_VIA above. The load-bearing anti-fabrication field.
    verified_via: "str | None" = None
    release_id: "str | None" = None
    timestamp: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "schemaVersion": (
                self.schema_version if self.schema_version is not None
                else DISTRIBUTED_BUILD_ACCEPTANCE_SCHEMA_VERSION
            ),
            "component": self.component or DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT,
            "channel": self.channel,
            "distributedBuildId": self.distributed_build_id,
            "testedGitSha": self.tested_git_sha,
            "testedVersion": self.tested_version,
            "verifiedVia": self.verified_via,
            "releaseId": self.release_id,
            "timestamp": self.timestamp,
        }


def parse_distributed_build_acceptance_result(data: "Any") -> DistributedBuildAcceptanceResult:
    """Build a DistributedBuildAcceptanceResult from a decoded JSON mapping.

    Raises DistributedBuildAcceptanceError for anything that isn't the
    expected shape -- a malformed evidence file is a framework/pipeline
    fault, never silently treated as a passing (or failing) product result.
    """
    if not isinstance(data, dict):
        raise DistributedBuildAcceptanceError("distributed-build-acceptance result must be a JSON object.")

    schema_version = _opt_int(data.get("schemaVersion"))
    if schema_version is not None and schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise DistributedBuildAcceptanceError(
            f"unsupported distributed-build-acceptance schemaVersion {schema_version!r}; this consumer "
            f"supports only {sorted(SUPPORTED_SCHEMA_VERSIONS)} -- refusing to read evidence produced by "
            f"an unknown emitter version."
        )

    component = _opt_str(data.get("component"))
    if component is not None and component != DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT:
        raise DistributedBuildAcceptanceError(
            f"unexpected component {component!r} (expected {DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT!r})."
        )

    return DistributedBuildAcceptanceResult(
        schema_version=schema_version,
        component=component,
        channel=_opt_str(data.get("channel")),
        distributed_build_id=_opt_str(data.get("distributedBuildId")),
        tested_git_sha=_opt_str(data.get("testedGitSha")),
        tested_version=_opt_str(data.get("testedVersion")),
        verified_via=_opt_str(data.get("verifiedVia")),
        release_id=_opt_str(data.get("releaseId")),
        timestamp=_opt_str(data.get("timestamp")),
    )


def load_distributed_build_acceptance_result(path: "Path | str") -> DistributedBuildAcceptanceResult:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DistributedBuildAcceptanceError(
            f"could not read distributed-build-acceptance result at {path}: {exc}"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DistributedBuildAcceptanceError(f"{path} is not valid JSON: {exc}") from exc
    return parse_distributed_build_acceptance_result(data)


@dataclass
class DistributedBuildAcceptanceVerdict:
    ok: bool
    problems: "list[str]" = field(default_factory=list)
    result: "DistributedBuildAcceptanceResult | None" = None

    def summary(self) -> str:
        if self.ok:
            r = self.result
            where = f"{r.tested_version} @ {r.tested_git_sha}" if r else "?"
            return f"Distributed-build acceptance evidence accepted for CaleeMobile {where}."
        return "Distributed-build acceptance evidence REJECTED: " + " ".join(self.problems)


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


def verify_distributed_build_acceptance_evidence(
    result: DistributedBuildAcceptanceResult,
    *,
    expected_git_sha: "str | None" = None,
    expected_version: "str | None" = None,
    expected_release_id: "str | None" = None,
    now: "datetime.datetime | None" = None,
    max_age: "datetime.timedelta | None" = DEFAULT_FRESHNESS,
    future_skew: datetime.timedelta = FUTURE_SKEW,
) -> DistributedBuildAcceptanceVerdict:
    """Reject distributed-build-acceptance evidence that doesn't prove the
    *expected* CaleeMobile release build was actually accepted from a real
    distributed/store channel. Returns a verdict listing every problem."""
    problems: "list[str]" = []

    if result.schema_version is None:
        problems.append("no schemaVersion recorded in the evidence.")
    elif result.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        problems.append(
            f"schemaVersion {result.schema_version!r} is not supported "
            f"(this consumer supports {sorted(SUPPORTED_SCHEMA_VERSIONS)})."
        )
    if not result.component:
        problems.append("no component marker recorded in the evidence.")
    elif result.component != DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT:
        problems.append(
            f"component {result.component!r} != expected {DISTRIBUTED_BUILD_ACCEPTANCE_COMPONENT!r}."
        )

    if not result.channel:
        problems.append("no distribution channel recorded in the evidence.")
    elif result.channel not in VALID_CHANNELS:
        problems.append(
            f"channel {result.channel!r} is not a recognised distribution channel "
            f"(expected one of {sorted(VALID_CHANNELS)})."
        )

    if not result.distributed_build_id:
        problems.append(
            "no distributed build identifier (TestFlight build number / App Store Connect "
            "version / Play Console release id) recorded."
        )

    verified_via = (result.verified_via or "").strip()
    if not verified_via:
        problems.append(
            "no verifiedVia recorded -- distributed-build acceptance must be verified through a "
            "real TestFlight/App Store Connect/Play Console API or a signed store export, never "
            "fabricated."
        )
    elif verified_via in REJECTED_VERIFIED_VIA:
        problems.append(
            f"verifiedVia {verified_via!r} is explicitly rejected -- acceptance can never be "
            f"fabricated from a local checkout or an unsigned build."
        )
    elif verified_via not in VALID_VERIFIED_VIA:
        problems.append(
            f"verifiedVia {verified_via!r} is not a recognised distributed/store verification "
            f"source (expected one of {sorted(VALID_VERIFIED_VIA)})."
        )

    if not result.tested_git_sha:
        problems.append("no tested CaleeMobile SHA recorded in the evidence.")
    elif not is_full_git_sha(result.tested_git_sha):
        problems.append(
            f"tested SHA {result.tested_git_sha!r} is abbreviated/ambiguous "
            f"(need the full 40-character SHA)."
        )

    if not result.tested_version:
        problems.append("no tested CaleeMobile version recorded in the evidence.")
    elif not is_wellformed_version(result.tested_version):
        problems.append(f"tested version {result.tested_version!r} is not a well-formed version identity.")

    if not result.timestamp:
        problems.append("no timestamp recorded in the evidence.")
    else:
        parsed_ts = _parse_utc_iso8601(result.timestamp)
        if parsed_ts is None:
            problems.append(f"timestamp {result.timestamp!r} is not a valid UTC ISO-8601 instant.")
        else:
            reference = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=datetime.timezone.utc)
            if parsed_ts > reference + future_skew:
                problems.append(
                    f"timestamp {result.timestamp!r} is in the future relative to now "
                    f"({reference.isoformat()})."
                )
            elif max_age is not None and (reference - parsed_ts) > max_age:
                age = reference - parsed_ts
                problems.append(
                    f"timestamp {result.timestamp!r} is stale: {age.days}d old, older than the "
                    f"{max_age.days}d freshness window."
                )

    if expected_git_sha is not None:
        if not is_full_git_sha(expected_git_sha):
            problems.append(
                f"expected CaleeMobile SHA {expected_git_sha!r} is abbreviated/ambiguous; "
                f"configure the full SHA."
            )
        elif result.tested_git_sha and result.tested_git_sha.strip().lower() != expected_git_sha.strip().lower():
            problems.append(
                f"tested SHA {result.tested_git_sha!r} != expected release SHA {expected_git_sha!r} -- "
                f"this evidence is for a different CaleeMobile commit than the one being released."
            )

    if expected_version is not None and result.tested_version:
        if result.tested_version.strip() != expected_version.strip():
            problems.append(
                f"tested version {result.tested_version!r} != expected release version "
                f"{expected_version!r} -- this evidence is for a different CaleeMobile version than "
                f"the one being released."
            )

    if expected_release_id is not None:
        if not (expected_release_id or "").strip():
            problems.append("expected release ID is empty.")
        elif not result.release_id:
            problems.append(
                "no releaseId recorded in the evidence -- distributed-build acceptance must be "
                "bound to a release ID."
            )
        elif result.release_id.strip() != expected_release_id.strip():
            problems.append(
                f"evidence releaseId {result.release_id!r} != expected release "
                f"{expected_release_id!r} -- refusing to accept distributed-build evidence for "
                f"another release."
            )

    return DistributedBuildAcceptanceVerdict(ok=not problems, problems=problems, result=result)


def _opt_str(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: "Any") -> "int | None":
    if value is None:
        return None
    if isinstance(value, bool):
        raise DistributedBuildAcceptanceError(f"expected an integer, got boolean {value!r}.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DistributedBuildAcceptanceError(f"expected an integer, got {value!r}.") from exc
