"""Authenticated distributed-build evidence collection AT ITS ORIGIN
(Priority 3, this session).

The previous ``record-distributed-build-acceptance`` path accepted ANY
``--source`` JSON file: a hand-typed ``{"generatedBy": "provider-api", ...}``
with plausible-looking fields passed every check ``distributed_build_
provenance.py`` runs, because those checks are all FORMAT/CONSISTENCY rules
over content the operator supplied directly -- none of them ever contact App
Store Connect, Play Console, or GitHub. A local digest only proves the file
was not changed after adoption; it proves nothing about where the bytes
originally came from.

This module is the missing origin-authentication layer. A PASS now requires
ONE of:

  * :func:`collect_app_store_connect_evidence` / :func:`collect_play_console_
    evidence` -- this process ITSELF makes the authenticated HTTPS request
    (JWT-signed for App Store Connect; OAuth2 bearer, exchanged from a
    service-account JSON when needed, for Play Console), using credentials
    resolved through ``credentials.py`` (environment/Keychain -- never a
    value on the command line, never in a log). Produces
    :class:`ProviderEvidenceRecord` (tier ``provider-api-live``).
  * an authenticated GitHub CI artifact (``github_artifact.py``'s existing
    chain, reused here) whose contained result is ITSELF a
    ``ProviderEvidenceRecord`` -- i.e. the CI run that produced the artifact
    ran one of the collectors above, not a hand-typed claim. Tier
    ``github-authenticated-artifact``.
  * :func:`verify_signed_export` -- a real detached signature (or signed
    envelope) cryptographically verified against a configured trusted public
    key. Tier ``verified-signed-export``.

Anything else -- including a perfectly well-formed, self-consistent,
hand-authored JSON file -- is, at best, ``manual-unverified`` (never a PASS;
see ``distributed_build_provenance.py``/``cli.py``'s
``record-distributed-build-acceptance``, which now maps this tier to
``blocked-unverified`` unconditionally).

Design mirrors the codebase's established patterns throughout: a pure core
(:func:`build_provider_record`, :func:`verify_signed_export`) that is fully
unit-testable with injected clients/keys and never touches the network, plus
a thin live layer (:class:`AppStoreConnectClient`, :class:`PlayConsoleClient`,
:func:`collect_*_evidence`) that is BLOCKED -- naming the exact missing
credential -- rather than ever fabricating a result. No unit test in this
codebase contacts a real provider; every test injects a fake client.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import credentials as credentials_mod
from . import github_artifact as ga
from .identity_format import is_full_git_sha, is_wellformed_version

COLLECTOR_VERSION = "1.0.0"

PROVIDER_APP_STORE_CONNECT = "app_store_connect"
PROVIDER_PLAY_CONSOLE = "play_console"

TIER_PROVIDER_API_LIVE = "provider-api-live"
TIER_GITHUB_AUTHENTICATED_ARTIFACT = "github-authenticated-artifact"
TIER_VERIFIED_SIGNED_EXPORT = "verified-signed-export"
TIER_MANUAL_UNVERIFIED = "manual-unverified"
# Priority 4 (this session): a signature that verified genuinely, but only
# against an operator-supplied --trusted-public-key-file rather than the
# pinned machine-config key -- real cryptography, but NOT anchored to a
# preconfigured trust root, so it can NEVER be treated as equivalent to
# TIER_VERIFIED_SIGNED_EXPORT. Kept distinct from TIER_MANUAL_UNVERIFIED
# (which has no cryptography behind it at all) purely for an honest audit
# trail; both are, identically, never in AUTHENTICATED_TIERS.
TIER_DIAGNOSTIC_UNPINNED_KEY = "diagnostic-unpinned-key"
# Priority 1 (this session): the distributed-build identity chain's join --
# an independently-authenticated provider observation AND an independently-
# authenticated build provenance record NAME THE SAME immutable platform
# build (see build_provenance.join_provider_and_build_provenance). Neither
# side alone is ever sufficient; this tier is stamped only once BOTH sides
# authenticated AND every chain requirement in that function passed.
TIER_PROVIDER_BUILD_PROVENANCE_JOIN = "provider-build-provenance-join"
KNOWN_TIERS = frozenset({
    TIER_PROVIDER_API_LIVE, TIER_GITHUB_AUTHENTICATED_ARTIFACT, TIER_VERIFIED_SIGNED_EXPORT, TIER_MANUAL_UNVERIFIED,
    TIER_DIAGNOSTIC_UNPINNED_KEY, TIER_PROVIDER_BUILD_PROVENANCE_JOIN,
})
# The tiers that can ever justify a PASS -- everything else (missing,
# TIER_MANUAL_UNVERIFIED, TIER_DIAGNOSTIC_UNPINNED_KEY, or any unrecognised
# string) is, at best, an explicit blocked-unverified record. Consulted both
# by cli.py (the command's own immediate verdict) and consolidated_report.py
# (re-derived independently at consolidation, never trusting a report's
# recorded ``status`` on faith -- see component_from_distributed_build_
# acceptance_report). Deliberately a property of the RECORD's
# ``evidenceTier`` field (set by this process's own control flow, inside the
# envelope-digest-protected provenance record -- see
# distributed_build_provenance.build_provenance_record's ``evidence_tier``
# parameter), never read from operator-supplied evidence content itself.
AUTHENTICATED_TIERS = frozenset({
    TIER_PROVIDER_API_LIVE, TIER_GITHUB_AUTHENTICATED_ARTIFACT, TIER_VERIFIED_SIGNED_EXPORT,
    TIER_PROVIDER_BUILD_PROVENANCE_JOIN,
})


class ProviderEvidenceError(Exception):
    """The collection/verification process itself could not be completed --
    missing credentials, an unreachable provider, a malformed response, an
    unverifiable signature. A framework/pipeline fault (BLOCKED), never a
    fabricated pass. A *content* problem in an otherwise-authenticated
    response is returned as a problem list by the validator, not raised
    here."""


# --- HTTP seams (injected; the only place real network I/O happens) --------

# A "fetcher" performs one authenticated HTTPS request and returns
# (status_code, raw_response_bytes, response_headers). Injected so the
# collection flow is fully testable with a fake and so the live
# implementation (which needs real credentials/network) is the only part
# that ever touches a socket.
#
# Priority 3 (this session): the seam supports method/body/timeout, not just
# a bare GET -- ``method``/``body``/``timeout`` are keyword-only with GET-
# compatible defaults, so App Store Connect's existing GET-only calls (and
# any test fake written before this session, taking just ``(url, headers)``)
# keep working unchanged; only Play Console's OAuth token exchange/edit-
# session calls actually pass a non-default ``method``/``body``.
HttpFetcher = Callable[..., "tuple[int, bytes, dict[str, str]]"]


def _default_https_fetcher(
    url: str, headers: "dict[str, str]", *, method: str = "GET", body: "bytes | None" = None, timeout: float = 30,
) -> "tuple[int, bytes, dict[str, str]]":
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - provider API hosts only
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --- JWT signing (real crypto; the ONLY thing that makes the bearer token
#     usable against the real provider APIs) ---------------------------------


def _sign_jwt_es256(*, header: "dict[str, Any]", claims: "dict[str, Any]", private_key_pem: str) -> str:
    """Sign a JWT with ES256 (P-256/SHA-256) -- App Store Connect's required
    algorithm for its API keys. Raises ProviderEvidenceError for a malformed
    key rather than silently producing an unusable token."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.exceptions import UnsupportedAlgorithm

    try:
        private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ProviderEvidenceError(f"App Store Connect private key is not a valid PEM EC private key: {exc}") from exc
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ProviderEvidenceError("App Store Connect private key must be an EC (P-256) private key for ES256.")

    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                     f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    der_signature = private_key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    # JWS ES256 wants the raw fixed-width (32+32 byte) r||s encoding, not DER.
    raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{_b64url(raw_signature)}"


def _sign_jwt_rs256(*, header: "dict[str, Any]", claims: "dict[str, Any]", private_key_pem: str) -> str:
    """Sign a JWT with RS256 -- Google service-account JWT-bearer auth's
    required algorithm."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.exceptions import UnsupportedAlgorithm

    try:
        private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ProviderEvidenceError(f"Play Console service-account private key is not a valid PEM RSA private key: {exc}") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ProviderEvidenceError("Play Console service-account private key must be an RSA private key for RS256.")

    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                     f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    signature = private_key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def build_app_store_connect_jwt(*, key_id: str, issuer_id: str, private_key_pem: str, now: "int | None" = None) -> str:
    """Build the ES256-signed JWT App Store Connect's API requires as a
    bearer token (20-minute max lifetime per Apple's documented limit)."""
    issued_at = now if now is not None else int(time.time())
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    claims = {"iss": issuer_id, "iat": issued_at, "exp": issued_at + 19 * 60, "aud": "appstoreconnect-v1"}
    return _sign_jwt_es256(header=header, claims=claims, private_key_pem=private_key_pem)


def build_play_console_assertion_jwt(*, service_account_email: str, private_key_pem: str, scope: str, token_uri: str, now: "int | None" = None) -> str:
    """Build the RS256-signed JWT assertion for a Google service-account
    OAuth2 JWT-bearer token exchange (RFC 7523)."""
    issued_at = now if now is not None else int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": service_account_email, "scope": scope, "aud": token_uri,
        "iat": issued_at, "exp": issued_at + 60 * 60,
    }
    return _sign_jwt_rs256(header=header, claims=claims, private_key_pem=private_key_pem)


# --- evidence record ---------------------------------------------------------

PLATFORM_IOS = "ios"
PLATFORM_ANDROID = "android"

PROVIDER_OBSERVATION_SCHEMA_VERSION = 1
PROVIDER_OBSERVATION_COMPONENT = "caleemobile-provider-observation"


@dataclass
class ProviderEvidenceRecord:
    """Priority 1/2 (this session): the PROVIDER OBSERVATION half of the
    distributed-build identity chain. Every field here is populated by the
    collector ITSELF from the actual authenticated response -- never by the
    operator, and -- the defect this closes -- never backfilled from
    ``requested_git_sha``/``requested_version``. A provider observation
    proves only provider-owned facts (the store build record and platform
    build identifier); it can NEVER prove source Git identity, so this
    record carries no ``tested_git_sha``/``tested_version`` field at all.
    Source Git SHA/version proof comes exclusively from an independently-
    authenticated :class:`build_provenance.BuildProvenanceRecord`, joined
    against this observation by
    :func:`build_provenance.join_provider_and_build_provenance` -- see that
    function for the two-source chain.

    ``requested_git_sha``/``requested_version`` ARE still recorded here, but
    strictly as an audit trail of what the caller asked to check for --
    :meth:`to_provider_observation_dict` never surfaces them as
    ``testedGitSha``/``testedVersion``, and no verifier ever treats them as
    proof of anything.
    """

    provider: str
    platform: str  # PLATFORM_IOS | PLATFORM_ANDROID -- derived from provider, never the response
    provider_account_or_project: str
    provider_endpoint: str
    provider_record_id: str  # "providerBuildId": ASC Build.id / Play versionCode
    http_status: int
    observed_at: str
    raw_response_bytes: bytes
    credential_source_name: str
    collector_version: str
    collection_run_id: str
    release_id: "str | None" = None
    channel: "str | None" = None
    # --- provider-owned facts (Priority 1/2) -- absent when the provider
    # itself doesn't supply them; NEVER populated from requested_git_sha/
    # requested_version below. ------------------------------------------
    bundle_id: "str | None" = None  # app/package identifier
    marketing_version: "str | None" = None  # ASC preReleaseVersion.version / Play release name
    build_number: "str | None" = None  # the PLATFORM BUILD NUMBER: iOS CFBundleVersion / Android versionCode
    processing_state: "str | None" = None  # ASC only
    uploaded_date: "str | None" = None  # ASC only
    track: "str | None" = None  # Play only
    release_status: "str | None" = None  # Play only
    # --- Play edit-session diagnostics (Priority 3) -- absent for App Store
    # Connect, which has no edit-session concept. ``edit_session_source`` is
    # "explicit" (caller supplied an existing edit id to read from) or
    # "created" (a temporary edit was created under
    # ``--allow-create-play-edit-session`` and must be cleaned up).
    # ``edit_session_cleanup`` is "not-applicable" (no temporary edit was
    # created), "succeeded", or "failed" -- see
    # :func:`collect_play_console_evidence`.
    edit_session_source: "str | None" = None
    edit_session_cleanup: "str | None" = None
    # --- audit trail ONLY: never proof, never surfaced as tested*Sha/Version.
    requested_git_sha: "str | None" = None
    requested_version: "str | None" = None

    def raw_response_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.raw_response_bytes).hexdigest()

    def to_provider_observation_dict(self) -> "dict[str, Any]":
        """Priority 1: the provider-observation evidence envelope -- proves
        ONLY provider-owned facts (the store build record and platform build
        identifier). Deliberately carries NO testedGitSha/testedVersion: a
        provider response alone must never prove source Git identity. This
        dict alone can never satisfy
        ``distributed_build_provenance.validate_distributed_evidence`` (which
        requires testedGitSha/testedVersion) -- by design, it can only ever
        be one half of the chain; see
        ``build_provenance.join_provider_and_build_provenance`` for the
        other half and the join."""
        return {
            "schemaVersion": PROVIDER_OBSERVATION_SCHEMA_VERSION,
            "component": PROVIDER_OBSERVATION_COMPONENT,
            "provider": self.provider,
            "platform": self.platform,
            "channel": self.channel or ("testflight" if self.provider == PROVIDER_APP_STORE_CONNECT else "play_console_internal"),
            "releaseId": self.release_id,
            "providerAccountOrProject": self.provider_account_or_project,
            "providerRecordId": self.provider_record_id,
            "providerObservedAt": self.observed_at,
            "bundleId": self.bundle_id,
            "marketingVersion": self.marketing_version,
            "buildNumber": self.build_number,
            "processingState": self.processing_state,
            "uploadedDate": self.uploaded_date,
            "track": self.track,
            "releaseStatus": self.release_status,
            "editSessionSource": self.edit_session_source,
            "editSessionCleanup": self.edit_session_cleanup,
            "generatedBy": "provider-api",
            "sourceDigest": self.raw_response_sha256(),
            "timestamp": self.observed_at,
            # The full authenticated-collection audit trail, preserved
            # alongside the fields above so a later verifier/human can see
            # exactly how this observation was obtained without re-deriving
            # it. requestedGitSha/requestedVersion are audit-only -- what the
            # CALLER asked to check for, never proof of anything.
            "providerEndpoint": self.provider_endpoint,
            "httpStatus": self.http_status,
            "credentialSourceName": self.credential_source_name,
            "collectorVersion": self.collector_version,
            "collectionRunId": self.collection_run_id,
            "requestedGitSha": self.requested_git_sha,
            "requestedVersion": self.requested_version,
        }


def _opt_str(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _find_included_attribute(
    parsed: "dict[str, Any]", *, resource_type: str, relationships: "dict[str, Any]",
    relationship_name: str, attribute: str,
) -> "str | None":
    """Look up ``attribute`` on the JSON:API ``included`` resource of
    ``resource_type`` that ``relationships[relationship_name]`` points to.
    Used to pull the App Store Connect App/PreReleaseVersion resources
    Priority 2 needs (bundleId, marketing version) out of a single
    ``include=...``-augmented response -- returns ``None`` (never a guess)
    when the relationship or the included resource isn't present."""
    if not isinstance(parsed, dict):
        return None
    included = parsed.get("included")
    if not isinstance(included, list):
        return None
    relationship = relationships.get(relationship_name) if isinstance(relationships, dict) else None
    related_data = relationship.get("data") if isinstance(relationship, dict) else None
    related_id = related_data.get("id") if isinstance(related_data, dict) else None
    if related_id is None:
        return None
    for item in included:
        if not isinstance(item, dict):
            continue
        if item.get("type") == resource_type and str(item.get("id")) == str(related_id):
            attrs = item.get("attributes")
            if isinstance(attrs, dict):
                return _opt_str(attrs.get(attribute))
    return None


# --- App Store Connect / TestFlight collector -------------------------------


class AppStoreConnectClient:
    """Thin authenticated-HTTPS client for the App Store Connect API.
    ``fetcher`` is injected (real by default); tests always inject a fake --
    no unit test in this codebase contacts Apple's real API."""

    API_BASE = "https://api.appstoreconnect.apple.com/v1"

    def __init__(self, *, key_id: str, issuer_id: str, private_key_pem: str, fetcher: "HttpFetcher | None" = None):
        self._key_id = key_id
        self._issuer_id = issuer_id
        self._private_key_pem = private_key_pem
        self._fetcher = fetcher or _default_https_fetcher

    def get_build(self, *, app_id: str, build_version: str) -> "tuple[int, bytes, str]":
        """GET the build matching ``app_id``+``build_version`` (the TestFlight
        build/version number). Returns (status, raw_bytes, endpoint_url).

        Priority 2: requests ``include=app,preReleaseVersion`` in the SAME
        authenticated call, so the response's ``included`` array carries the
        App resource's ``bundleId`` and the PreReleaseVersion resource's
        ``version`` (the actual MARKETING version -- Build.attributes.version
        is the build number/CFBundleVersion, never the marketing version;
        see the module docstring and Priority 2's requirement not to treat
        Build.attributes.version as the complete semantic version). No extra
        round trip: one request, two related resources inlined."""
        token = build_app_store_connect_jwt(key_id=self._key_id, issuer_id=self._issuer_id, private_key_pem=self._private_key_pem)
        endpoint = (
            f"{self.API_BASE}/builds?filter[app]={app_id}&filter[version]={build_version}"
            f"&include=app,preReleaseVersion&fields[apps]=bundleId&fields[preReleaseVersions]=version"
        )
        status, raw, _headers = self._fetcher(endpoint, {"Authorization": f"Bearer {token}", "Accept": "application/json"})
        return status, raw, endpoint


def collect_app_store_connect_evidence(
    *,
    app_id: str,
    build_version: str,
    requested_git_sha: "str | None" = None,
    requested_version: "str | None" = None,
    release_id: "str | None" = None,
    resolver: "credentials_mod.CredentialResolver | None" = None,
    client: "AppStoreConnectClient | None" = None,
    collection_run_id: str,
    now: "Callable[[], str] | None" = None,
) -> ProviderEvidenceRecord:
    """Perform the authenticated App Store Connect request and build the
    evidence record. BLOCKS (raises :class:`ProviderEvidenceError`) with the
    exact missing credential name if App Store Connect credentials aren't
    resolvable -- never falls back to an unauthenticated/fabricated result.
    """
    resolver = resolver or credentials_mod.default_resolver()
    if client is None:
        try:
            key_id = resolver.require(credentials_mod.APP_STORE_CONNECT_KEY_ID)
            issuer_id = resolver.require(credentials_mod.APP_STORE_CONNECT_ISSUER_ID)
            private_key_pem = resolver.require(credentials_mod.APP_STORE_CONNECT_PRIVATE_KEY)
        except credentials_mod.CredentialError as exc:
            # Translate to this module's own exception type -- every caller
            # (the CLI in particular) catches ProviderEvidenceError alone to
            # map missing credentials to BLOCKED; letting a raw
            # credentials.CredentialError escape here would bypass that,
            # exactly the inconsistency collect_play_console_evidence below
            # already avoids (it raises ProviderEvidenceError directly).
            raise ProviderEvidenceError(str(exc)) from exc
        client = AppStoreConnectClient(key_id=key_id, issuer_id=issuer_id, private_key_pem=private_key_pem)

    status, raw, endpoint = client.get_build(app_id=app_id, build_version=build_version)
    if status != 200:
        raise ProviderEvidenceError(
            f"App Store Connect request to {endpoint} returned HTTP {status} -- "
            f"cannot authenticate distributed-build evidence: {raw[:500]!r}"
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderEvidenceError(f"App Store Connect response from {endpoint} is not valid JSON: {exc}") from exc

    builds = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(builds, list) or not builds:
        raise ProviderEvidenceError(f"App Store Connect returned no build matching app={app_id!r} version={build_version!r}.")
    build = builds[0]
    build_id = str(build.get("id")) if isinstance(build, dict) else None
    if not build_id:
        raise ProviderEvidenceError("App Store Connect build record has no id.")
    attributes = build.get("attributes") if isinstance(build, dict) else {}
    attributes = attributes if isinstance(attributes, dict) else {}
    observed_at = (now() if now is not None else _utc_now_iso())

    # Priority 2: Build.attributes.version is the BUILD NUMBER (CFBundleVersion)
    # -- never the complete marketing/semantic version. The marketing version
    # (CFBundleShortVersionString equivalent) comes from the related
    # PreReleaseVersion resource, inlined via `include=preReleaseVersion`
    # above; the bundle id comes from the related App resource, inlined via
    # `include=app`. Both are looked up from the response's own `included`
    # array (never guessed, never defaulted from `build_version`/`app_id`).
    build_number = _opt_str(attributes.get("version"))
    processing_state = _opt_str(attributes.get("processingState"))
    uploaded_date = _opt_str(attributes.get("uploadedDate"))
    relationships = build.get("relationships") if isinstance(build, dict) else {}
    relationships = relationships if isinstance(relationships, dict) else {}
    bundle_id = _find_included_attribute(
        parsed, resource_type="apps", relationships=relationships, relationship_name="app", attribute="bundleId",
    )
    marketing_version = _find_included_attribute(
        parsed, resource_type="preReleaseVersions", relationships=relationships,
        relationship_name="preReleaseVersion", attribute="version",
    )

    return ProviderEvidenceRecord(
        provider=PROVIDER_APP_STORE_CONNECT,
        platform=PLATFORM_IOS,
        provider_account_or_project=app_id,
        provider_endpoint=endpoint,
        provider_record_id=build_id,
        http_status=status,
        observed_at=observed_at,
        raw_response_bytes=raw,
        bundle_id=bundle_id,
        marketing_version=marketing_version,
        build_number=build_number,
        processing_state=processing_state,
        uploaded_date=uploaded_date,
        credential_source_name=credentials_mod.APP_STORE_CONNECT_KEY_ID.env_var,
        collector_version=COLLECTOR_VERSION,
        collection_run_id=collection_run_id,
        release_id=release_id,
        channel="testflight",
        requested_git_sha=requested_git_sha,
        requested_version=requested_version,
    )


# --- Play Console collector ---------------------------------------------------


class PlayConsoleClient:
    """Thin authenticated-HTTPS client for the Google Play Developer API.
    Resolves an OAuth2 bearer token either directly (a pre-issued access
    token credential) or by exchanging a service-account JSON for one via
    Google's token endpoint (RFC 7523 JWT-bearer grant)."""

    TOKEN_URI = "https://oauth2.googleapis.com/token"
    API_BASE = "https://androidpublisher.googleapis.com/androidpublisher/v3"
    SCOPE = "https://www.googleapis.com/auth/androidpublisher"

    def __init__(self, *, access_token: "str | None" = None, service_account: "dict[str, Any] | None" = None, fetcher: "HttpFetcher | None" = None):
        if not access_token and not service_account:
            raise ProviderEvidenceError("PlayConsoleClient requires either an access_token or a service_account.")
        self._access_token = access_token
        self._service_account = service_account
        self._fetcher = fetcher or _default_https_fetcher

    def _bearer_token(self) -> str:
        if self._access_token:
            return self._access_token
        sa = self._service_account
        assertion = build_play_console_assertion_jwt(
            service_account_email=sa["client_email"], private_key_pem=sa["private_key"],
            scope=self.SCOPE, token_uri=self.TOKEN_URI,
        )
        # Priority 3: a REAL POST with a form-encoded body (grant_type,
        # assertion) -- not a GET with the body smuggled into the query
        # string, which is what this used to do.
        body = f"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={assertion}"
        status, raw, _headers = self._fetcher(
            self.TOKEN_URI, {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST", body=body.encode("ascii"),
        )
        if status != 200:
            raise ProviderEvidenceError(f"Google OAuth2 token exchange returned HTTP {status}: {raw[:500]!r}")
        try:
            token_response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderEvidenceError(f"Google OAuth2 token response is not valid JSON: {exc}") from exc
        token = token_response.get("access_token") if isinstance(token_response, dict) else None
        if not token:
            raise ProviderEvidenceError("Google OAuth2 token exchange did not return an access_token.")
        return token

    def get_track(self, *, package_name: str, edit_id: str, track: str) -> "tuple[int, bytes, str]":
        """GET the named release track within a (caller-supplied or
        caller-created, see :func:`collect_play_console_evidence`) edit
        session. Returns (status, raw_bytes, endpoint_url)."""
        token = self._bearer_token()
        endpoint = f"{self.API_BASE}/applications/{package_name}/edits/{edit_id}/tracks/{track}"
        status, raw, _headers = self._fetcher(
            endpoint, {"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="GET",
        )
        return status, raw, endpoint

    def create_edit(self, *, package_name: str) -> str:
        """A REAL POST (Priority 3 -- not an `X-HTTP-Method-Override` header
        pretending a GET is a POST) creating a new edit session, and return
        its id. Because creating an edit can invalidate another open edit,
        this must only ever be called under the caller's explicit
        ``--allow-create-play-edit-session`` opt-in -- see
        :func:`collect_play_console_evidence`, never unconditionally."""
        token = self._bearer_token()
        endpoint = f"{self.API_BASE}/applications/{package_name}/edits"
        status, raw, _headers = self._fetcher(
            endpoint, {"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="POST", body=b"",
        )
        if status not in (200, 201):
            raise ProviderEvidenceError(f"Play Console edit-session creation returned HTTP {status}: {raw[:500]!r}")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderEvidenceError(f"Play Console edit-session response is not valid JSON: {exc}") from exc
        edit_id = parsed.get("id") if isinstance(parsed, dict) else None
        if not edit_id:
            raise ProviderEvidenceError("Play Console edit-session response has no id.")
        return str(edit_id)

    def delete_edit(self, *, package_name: str, edit_id: str) -> bool:
        """A REAL DELETE (Priority 3) of a temporary edit session THIS
        process created. Returns True on a successful cleanup (HTTP
        200/204), False otherwise -- never raises, so a cleanup failure can
        never mask (or crash past) an already-successful collection; the
        caller is responsible for surfacing a False return prominently."""
        try:
            token = self._bearer_token()
        except ProviderEvidenceError:
            return False
        endpoint = f"{self.API_BASE}/applications/{package_name}/edits/{edit_id}"
        try:
            status, _raw, _headers = self._fetcher(
                endpoint, {"Authorization": f"Bearer {token}", "Accept": "application/json"}, method="DELETE", body=b"",
            )
        except Exception:
            return False
        return status in (200, 204)


def collect_play_console_evidence(
    *,
    package_name: str,
    track: str,
    requested_git_sha: "str | None" = None,
    requested_version: "str | None" = None,
    release_id: "str | None" = None,
    resolver: "credentials_mod.CredentialResolver | None" = None,
    client: "PlayConsoleClient | None" = None,
    collection_run_id: str,
    now: "Callable[[], str] | None" = None,
    edit_id: "str | None" = None,
    allow_create_edit_session: bool = False,
) -> ProviderEvidenceRecord:
    """Perform the authenticated Play Console request and build the evidence
    record. BLOCKS with the exact missing credential name if neither a
    service-account JSON nor an access token is resolvable.

    Priority 3 (this session): the Play Developer API only exposes track
    state through an edit session (``edits.tracks.get``) -- there is no
    edit-free read for a track. Silently calling ``edits.insert`` on every
    collection is unsafe: creating an edit can invalidate another edit
    already open (e.g. one a human is mid-way through in the Play Console
    UI), so this function never does that unconditionally. Instead:

      * ``edit_id`` -- an edit session the CALLER already controls (e.g. one
        created and tracked outside this process). Used strictly read-only;
        never deleted by this function, since this process didn't create it.
      * ``allow_create_edit_session=True`` -- explicit opt-in (wired to the
        CLI's ``--allow-create-play-edit-session``) to create a TEMPORARY
        edit for this single read, with an unmissable stderr warning, and
        guaranteed cleanup in a ``finally`` -- a cleanup failure is also
        surfaced prominently on stderr, since a leaked edit can block other
        tooling/humans from editing the app until it expires or is deleted
        manually.
      * Neither supplied -- BLOCKS. Refusing to silently create an edit
        session is the safe default; this is never treated as a product
        failure.
    """
    resolver = resolver or credentials_mod.default_resolver()
    credential_source_name = credentials_mod.PLAY_CONSOLE_ACCESS_TOKEN.env_var
    if client is None:
        access_token = resolver.get(credentials_mod.PLAY_CONSOLE_ACCESS_TOKEN)
        service_account = None
        if not access_token:
            raw_sa = resolver.get(credentials_mod.PLAY_CONSOLE_SERVICE_ACCOUNT_JSON)
            if raw_sa:
                try:
                    service_account = json.loads(raw_sa)
                except json.JSONDecodeError as exc:
                    raise ProviderEvidenceError(f"Play Console service-account credential is not valid JSON: {exc}") from exc
                credential_source_name = credentials_mod.PLAY_CONSOLE_SERVICE_ACCOUNT_JSON.env_var
        if not access_token and not service_account:
            raise ProviderEvidenceError(
                f"Required credential could not be resolved: set {credentials_mod.PLAY_CONSOLE_ACCESS_TOKEN.env_var} "
                f"or {credentials_mod.PLAY_CONSOLE_SERVICE_ACCOUNT_JSON.env_var}. This BLOCKS distributed-build "
                f"evidence collection -- it is never treated as a product failure."
            )
        client = PlayConsoleClient(access_token=access_token, service_account=service_account)

    edit_id = _opt_str(edit_id)
    created_edit_id: "str | None" = None
    if edit_id:
        active_edit_id = edit_id
        edit_session_source = "explicit"
        edit_session_cleanup = "not-applicable"
    elif allow_create_edit_session:
        print(
            "WARNING: --allow-create-play-edit-session is set. Creating a "
            f"TEMPORARY Play Console edit session to read track {track!r} for "
            f"{package_name!r}. Creating an edit can invalidate another edit "
            "session already open for this app (e.g. one in progress in the "
            "Play Console UI). This temporary edit will be deleted "
            "immediately after this read.",
            file=sys.stderr,
        )
        active_edit_id = client.create_edit(package_name=package_name)
        created_edit_id = active_edit_id
        edit_session_source = "created"
        edit_session_cleanup = "failed"  # overwritten in the finally block below
    else:
        raise ProviderEvidenceError(
            "Play Console live collection requires either an existing edit session "
            "id (--play-edit-id, read strictly read-only) or explicit approval to "
            "create a temporary one (--allow-create-play-edit-session). Refusing to "
            "silently create an edit session, since doing so can invalidate another "
            "edit already open for this app. This BLOCKS distributed-build evidence "
            "collection -- it is never treated as a product failure."
        )

    try:
        status, raw, endpoint = client.get_track(package_name=package_name, edit_id=active_edit_id, track=track)
        if status != 200:
            raise ProviderEvidenceError(
                f"Play Console request to {endpoint} returned HTTP {status} -- cannot authenticate "
                f"distributed-build evidence: {raw[:500]!r}"
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderEvidenceError(f"Play Console response from {endpoint} is not valid JSON: {exc}") from exc

        releases = parsed.get("releases") if isinstance(parsed, dict) else None
        if not isinstance(releases, list) or not releases:
            raise ProviderEvidenceError(f"Play Console track {track!r} for {package_name!r} has no releases.")
        release = releases[0]
        version_codes = release.get("versionCodes") if isinstance(release, dict) else None
        version_code = str(version_codes[0]) if isinstance(version_codes, list) and version_codes else None
        if not version_code:
            raise ProviderEvidenceError("Play Console release has no versionCodes.")
    finally:
        if created_edit_id is not None:
            cleanup_ok = client.delete_edit(package_name=package_name, edit_id=created_edit_id)
            edit_session_cleanup = "succeeded" if cleanup_ok else "failed"
            if not cleanup_ok:
                print(
                    f"WARNING: cleanup of the temporary Play Console edit {created_edit_id!r} "
                    f"for {package_name!r} FAILED. This edit may still exist server-side and "
                    "could block other edits until it expires or is deleted manually via the "
                    "Play Console UI or API.",
                    file=sys.stderr,
                )

    observed_at = (now() if now is not None else _utc_now_iso())

    # distributed_build_provenance.VALID_CHANNELS only defines one Play
    # Console channel value ("play_console_internal") -- the internal
    # testing track this framework actually uses for distributed-build
    # acceptance. A non-"internal" track still authenticates correctly
    # against the real API above; it just has no distinct channel name in
    # today's schema, so it is recorded under the same value.
    channel = "play_console_internal"

    # Priority 1/2: provider-owned facts only. `release.get("name")` is Play's
    # own "versionName where independently available" (the release's own
    # display version -- absent, never guessed, when Play doesn't set one);
    # `release.get("status")` is the real Play Developer API TrackRelease
    # field ("completed"/"inProgress"/"halted"/"draft").
    return ProviderEvidenceRecord(
        provider=PROVIDER_PLAY_CONSOLE,
        platform=PLATFORM_ANDROID,
        provider_account_or_project=package_name,
        provider_endpoint=endpoint,
        provider_record_id=version_code,
        http_status=status,
        observed_at=observed_at,
        raw_response_bytes=raw,
        bundle_id=package_name,
        marketing_version=_opt_str(release.get("name")),
        build_number=version_code,
        track=track,
        release_status=_opt_str(release.get("status")),
        edit_session_source=edit_session_source,
        edit_session_cleanup=edit_session_cleanup,
        credential_source_name=credential_source_name,
        collector_version=COLLECTOR_VERSION,
        collection_run_id=collection_run_id,
        release_id=release_id,
        channel=channel,
        requested_git_sha=requested_git_sha,
        requested_version=requested_version,
    )


# --- signed export (real cryptographic signature verification) -------------


def verify_signed_export(
    *,
    payload_bytes: bytes,
    signature_bytes: bytes,
    trusted_public_key_pem: str,
) -> "list[str]":
    """Cryptographically verify a DETACHED signature over ``payload_bytes``
    against a configured trusted public key (Priority 3: "a real detached
    signature ... verified cryptographically against a configured trusted
    public key/certificate", not merely a nonempty
    ``signatureOrArtifactProvenance`` object).

    Supports an RSA (PKCS#1 v1.5 + SHA-256) or EC (ECDSA + SHA-256) public
    key -- whichever the configured PEM actually contains; the signature
    bytes must be in the matching encoding (PKCS#1 for RSA, DER for EC).
    Returns a problem list (empty == verified); never raises for a genuine
    signature mismatch (a verdict, not a framework fault) -- only for an
    unparseable key/signature."""
    from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    try:
        public_key = serialization.load_pem_public_key(trusted_public_key_pem.encode("utf-8"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        return [f"configured trusted public key is not a valid PEM public key: {exc}"]

    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature_bytes, payload_bytes, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature_bytes, payload_bytes, ec.ECDSA(hashes.SHA256()))
        else:
            return [f"configured trusted public key type {type(public_key).__name__} is not supported (need RSA or EC)."]
    except InvalidSignature:
        return ["signature verification FAILED: the signature does not match the payload under the configured trusted public key."]
    except ValueError as exc:
        return [f"signature could not be verified: {exc}"]
    return []


def compute_public_key_sha256_fingerprint(trusted_public_key_pem: str) -> str:
    """SHA-256 fingerprint of the ACTUAL key (Priority 4): computed over the
    DER-encoded SubjectPublicKeyInfo, which is canonical regardless of how
    the PEM text happens to be wrapped/whitespaced -- unlike hashing the raw
    PEM text, two byte-different PEM encodings of the identical key always
    produce the SAME fingerprint here. Returns lowercase 64-character hex, no
    ``sha256:`` prefix (matching ``machine_config.py``'s pinned-fingerprint
    format and ``release_installer.py``'s existing ``signerSha256``
    precedent). Raises :class:`ProviderEvidenceError` for a key that doesn't
    even parse as a PEM public key."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization

    try:
        public_key = serialization.load_pem_public_key(trusted_public_key_pem.encode("utf-8"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise ProviderEvidenceError(f"configured trusted public key is not a valid PEM public key: {exc}") from exc
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER, format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()


def resolve_pinned_trusted_public_key(
    *,
    pinned_fingerprint: "str | None",
    resolver: "credentials_mod.CredentialResolver | None" = None,
) -> "tuple[str, str]":
    """Priority 4: resolve the trusted signed-export public key EXCLUSIVELY
    from preconfigured machine security configuration/Keychain (via
    ``credentials.SIGNED_EXPORT_TRUSTED_PUBLIC_KEY`` -- environment/Keychain
    -- never a per-command ``--trusted-public-key-file`` override), and
    require its computed SHA-256 fingerprint to match ``pinned_fingerprint``
    (a technical owner's non-secret ``machine_config.MachineConfig.
    trusted_signed_export_public_key_sha256``). Returns ``(pem,
    verified_fingerprint)`` -- ``verified_fingerprint`` is the ONLY signer
    identity a release-gating PASS may ever be derived from; it is computed
    HERE, from the resolved key itself, never accepted as an operator-typed
    label (see :func:`build_signed_export_evidence`).

    Raises :class:`ProviderEvidenceError` (BLOCKED) when no fingerprint is
    pinned, the key can't be resolved, or the fingerprints disagree -- a
    mismatch NEVER falls back to trusting the resolved key anyway; this is
    the one function a release-gating PASS must go through, and it fails
    closed on every ambiguity."""
    if not (pinned_fingerprint or "").strip():
        raise ProviderEvidenceError(
            "no trusted signed-export public-key fingerprint is pinned in machine configuration "
            "(trusted_signed_export_public_key_sha256) -- a release-gating signed-export PASS requires "
            "a pinned fingerprint configured in advance, never an ad-hoc/per-command key."
        )
    resolver = resolver or credentials_mod.default_resolver()
    try:
        pem = resolver.require(credentials_mod.SIGNED_EXPORT_TRUSTED_PUBLIC_KEY)
    except credentials_mod.CredentialError as exc:
        raise ProviderEvidenceError(str(exc)) from exc

    actual_fingerprint = compute_public_key_sha256_fingerprint(pem)
    if actual_fingerprint.strip().lower() != pinned_fingerprint.strip().lower():
        raise ProviderEvidenceError(
            f"the resolved trusted public key's fingerprint {actual_fingerprint} does not match the "
            f"fingerprint {pinned_fingerprint.strip().lower()} pinned in machine configuration -- "
            f"refusing to trust it. This BLOCKS the signed-export path; it is never treated as a "
            f"reason to fall back to trusting the resolved key anyway."
        )
    return pem, actual_fingerprint


def build_signed_export_evidence(
    *,
    payload: "dict[str, Any]",
    signature_bytes: bytes,
    trusted_public_key_pem: str,
    signer_fingerprint: "str | None" = None,
) -> "tuple[dict[str, Any], list[str]]":
    """Verify a signed-export payload and, if the signature is genuine,
    return the evidence dict (schemaVersion 2, generatedBy signed-export)
    ready for ``distributed_build_provenance.validate_distributed_evidence``.
    Returns (evidence_or_empty_dict, problems) -- an empty dict with a
    non-empty problems list on signature failure, never a half-trusted
    result.

    Priority 4: the recorded ``signerFingerprint`` is ALWAYS the verified
    key's OWN computed SHA-256 fingerprint (:func:`compute_public_key_sha256_
    fingerprint`) -- never the caller-supplied ``signer_fingerprint``, which
    (when given) is recorded separately as ``operatorDeclaredSignerLabel``,
    purely for human cross-reference, and is never consulted by any
    verifier or consolidator to determine a PASS."""
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    problems = verify_signed_export(payload_bytes=payload_bytes, signature_bytes=signature_bytes, trusted_public_key_pem=trusted_public_key_pem)
    if problems:
        return {}, problems

    verified_fingerprint = compute_public_key_sha256_fingerprint(trusted_public_key_pem)
    evidence = dict(payload)
    evidence["generatedBy"] = "signed-export"
    evidence["signatureOrArtifactProvenance"] = {
        "signerFingerprint": verified_fingerprint,
        "signatureSha256": "sha256:" + hashlib.sha256(signature_bytes).hexdigest(),
        "verifiedAt": _utc_now_iso(),
    }
    if signer_fingerprint:
        evidence["signatureOrArtifactProvenance"]["operatorDeclaredSignerLabel"] = signer_fingerprint
    return evidence, []


def validate_provider_observation(
    evidence: "dict[str, Any]", *, expected_release_id: "str | None" = None,
) -> "list[str]":
    """Priority 1 (this session): format/consistency validation for a
    :meth:`ProviderEvidenceRecord.to_provider_observation_dict`-shaped
    envelope -- schemaVersion 1, component ``caleemobile-provider-
    observation``. Deliberately does NOT require ``testedGitSha``/
    ``testedVersion`` (a provider observation never carries them -- see the
    module docstring); a provider observation that passes this is still,
    BY ITSELF, never sufficient for a PASS -- only
    ``build_provenance.join_provider_and_build_provenance`` can combine it
    with an independently-authenticated build-provenance record to reach
    one. Returns a problem list (empty == well-formed)."""
    from . import distributed_build_acceptance as dba

    problems: "list[str]" = []
    if not isinstance(evidence, dict):
        return ["provider observation must be a JSON object."]

    schema_version = evidence.get("schemaVersion")
    if schema_version != PROVIDER_OBSERVATION_SCHEMA_VERSION:
        problems.append(
            f"provider observation schemaVersion {schema_version!r} != expected "
            f"{PROVIDER_OBSERVATION_SCHEMA_VERSION!r}."
        )
    component = evidence.get("component")
    if component != PROVIDER_OBSERVATION_COMPONENT:
        problems.append(f"provider observation component {component!r} != expected {PROVIDER_OBSERVATION_COMPONENT!r}.")

    provider = evidence.get("provider")
    if provider not in (PROVIDER_APP_STORE_CONNECT, PROVIDER_PLAY_CONSOLE):
        problems.append(f"provider observation has no recognised provider (got {provider!r}).")

    platform = evidence.get("platform")
    if platform not in (PLATFORM_IOS, PLATFORM_ANDROID):
        problems.append(f"provider observation has no recognised platform (got {platform!r}).")

    channel = evidence.get("channel")
    if not channel or channel not in dba.VALID_CHANNELS:
        problems.append(f"provider observation channel {channel!r} is not a recognised distribution channel.")

    if not _opt_str(evidence.get("providerAccountOrProject")):
        problems.append("provider observation has no providerAccountOrProject recorded.")
    if not _opt_str(evidence.get("providerRecordId")):
        problems.append("provider observation has no providerRecordId recorded.")

    observed_at = evidence.get("providerObservedAt")
    if not observed_at or _valid_utc_timestamp(observed_at) is False:
        problems.append(f"provider observation has no valid providerObservedAt timestamp (got {observed_at!r}).")

    if not _opt_str(evidence.get("sourceDigest")):
        problems.append("provider observation has no sourceDigest recorded.")

    timestamp = evidence.get("timestamp")
    if not timestamp or _valid_utc_timestamp(timestamp) is False:
        problems.append(f"provider observation has no valid timestamp (got {timestamp!r}).")

    if expected_release_id is not None:
        release_id = evidence.get("releaseId")
        if not release_id:
            problems.append("provider observation has no releaseId recorded -- it is not bound to this release.")
        elif str(release_id).strip() != str(expected_release_id).strip():
            problems.append(
                f"provider observation releaseId {release_id!r} != expected release {expected_release_id!r}."
            )

    return problems


def _valid_utc_timestamp(value: "Any") -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == datetime.timedelta(0)


# --- GitHub CI artifact: the CONTAINED provider evidence must itself be
#     authentic (never merely a raw claim smuggled through a real artifact) -


def verify_nested_provider_evidence(extracted: "dict[str, Any]", **validate_kwargs: Any) -> "list[str]":
    """Priority 1 requirement: "verify that the contained provider evidence
    is itself authentic" -- a GitHub artifact whose payload is just a
    hand-typed ``generatedBy: provider-api`` claim (with nothing backing it)
    must not pass merely because the ARTIFACT was authenticated. This
    re-runs :func:`validate_provider_observation` (schema, provider/
    platform/channel allow-lists, and -- via ``validate_kwargs`` -- the
    expected release-id binding) over the artifact's contained payload, PLUS
    an additional restriction: the nested evidence's own ``generatedBy``
    must be exactly ``"provider-api"`` (i.e. produced by
    :func:`collect_app_store_connect_evidence` /
    :func:`collect_play_console_evidence` during that CI run). A nested
    ``signed-export`` or another ``ci-artifact`` claim is not accepted here
    -- the artifact chain proves WHERE the bytes came from; this proves WHAT
    they claim is both well-formed AND was itself live-collected, not a
    fabricated or merely-recursive claim laundered through a real artifact
    upload. A provider observation that passes this is still only ONE half
    of the identity chain -- see :func:`validate_provider_observation`."""
    problems = list(validate_provider_observation(extracted, **validate_kwargs))
    if isinstance(extracted, dict) and extracted.get("generatedBy") != "provider-api":
        problems.append(
            f"nested evidence generatedBy {extracted.get('generatedBy')!r} is not 'provider-api' -- a CI "
            f"artifact can only authenticate distributed-build evidence that the CI run itself collected "
            f"live from a provider API (see collect_app_store_connect_evidence/"
            f"collect_play_console_evidence); a nested signed-export or a further ci-artifact claim is not "
            f"accepted here."
        )
    return problems


# --- GitHub CI artifact: authenticate the ARTIFACT chain itself -------------
# (verify_nested_provider_evidence above verifies the CONTENT once extracted;
# this is the origin-authentication layer around it -- reusing
# github_artifact.py's chain primitives exactly like main_ci_artifact.py
# does for merged-main evidence, rather than duplicating them.)


@dataclass
class ProviderCiArtifactChain:
    """The result of evaluating an authenticated CI-artifact distributed-
    build evidence chain (Priority 3, tier ``github-authenticated-
    artifact``)."""

    ok: bool
    problems: "list[str]" = field(default_factory=list)
    run: "Any" = None
    artifact: "Any" = None
    zip_bytes: "bytes | None" = None
    zip_sha256: "str | None" = None
    result_bytes: "bytes | None" = None
    result_sha256: "str | None" = None
    result: "dict[str, Any] | None" = None

    def summary(self) -> str:
        if self.ok:
            build = (self.result or {}).get("providerRecordId", "?")
            return f"Authenticated CI-artifact provider observation verified (build {build})."
        return "Authenticated CI-artifact provider observation REJECTED: " + "; ".join(self.problems)


def verify_provider_ci_artifact_chain(
    run: "Any",
    artifact: "Any",
    zip_bytes: bytes,
    *,
    expected_repository: str,
    expected_workflow_path: str,
    expected_artifact_name: str,
    expected_result_filename: str,
    expected_run_id: "str | None" = None,
    expected_artifact_id: "str | None" = None,
    max_zip_bytes: "int | None" = None,
    **validate_kwargs: Any,
) -> ProviderCiArtifactChain:
    """Authenticate that ``zip_bytes`` genuinely is the named artifact from
    the named GitHub Actions run (repository, workflow path, run success,
    artifact ownership/name/digest -- the same chain ``main_ci_artifact.py``
    enforces for merged-main evidence), then verify the single extracted
    file is itself authentic provider evidence via
    :func:`verify_nested_provider_evidence`.

    Deliberately does NOT require ``run.head_sha`` to equal anything --
    unlike merged-main evidence, the CI run that executes a distributed-
    build collector is not tied to the CaleeMobile commit being tested; the
    binding to a specific release/SHA/version comes entirely from the
    nested evidence's own fields (enforced via ``validate_kwargs``,
    forwarded to ``validate_distributed_evidence``)."""
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
    result: "dict[str, Any] | None" = None
    result_sha: "str | None" = None
    try:
        result_bytes, result = ga.extract_single_result(zip_bytes, expected_name=expected_result_filename)
        result_sha = ga.sha256_hex(result_bytes)
    except ga.GithubArtifactError as exc:
        problems.append(str(exc))

    if result is not None:
        problems.extend(verify_nested_provider_evidence(result, **validate_kwargs))

    return ProviderCiArtifactChain(
        ok=not problems, problems=problems, run=run, artifact=artifact,
        zip_bytes=zip_bytes, zip_sha256=zip_sha, result_bytes=result_bytes,
        result_sha256=result_sha, result=result,
    )


def acquire_provider_ci_artifact(
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
    **validate_kwargs: Any,
) -> ProviderCiArtifactChain:
    """Acquire and verify an authenticated CI-artifact distributed-build
    evidence chain from a GitHub run + artifact. Mirrors
    ``main_ci_artifact.acquire_main_ci_artifact``'s shape/credential-BLOCKED
    behaviour exactly, reusing ``github_artifact.py``'s live HTTP fetchers
    and token resolution rather than duplicating them. Raises
    :class:`ProviderEvidenceError` (BLOCKED) when a required id is missing,
    when no credentials/fetcher is available (naming the exact missing
    secret), or when the ZIP is structurally unreadable."""
    if not ga._opt_str(run_id):
        raise ProviderEvidenceError(
            "authenticated CI-artifact distributed-build evidence requires a GitHub workflow run id "
            "(--github-run-id); a self-declared run id in a JSON file is not proof."
        )
    if not ga._opt_str(artifact_id):
        raise ProviderEvidenceError(
            "authenticated CI-artifact distributed-build evidence requires a GitHub artifact id "
            "(--github-artifact-id)."
        )

    effective_token = token if token is not None else ga.resolve_token(env)
    if json_fetcher is None or bytes_fetcher is None:
        if not effective_token:
            missing = " or ".join(ga.TOKEN_ENV_VARS)
            raise ProviderEvidenceError(
                f"BLOCKED: no GitHub API credentials available to authenticate the artifact (set one of "
                f"{missing} to a token with read access to {repository}). Without it the run/artifact "
                f"ownership and the artifact digest cannot be verified, so the evidence cannot be accepted "
                f"as authenticated CI-artifact distributed-build evidence."
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
            raise ProviderEvidenceError(f"could not read artifact ZIP {local_zip_path}: {exc}") from exc
    else:
        download_url = artifact.archive_download_url or f"{base}/repos/{repository}/actions/artifacts/{artifact_id}/zip"
        zip_bytes = bytes_fetcher(download_url)

    return verify_provider_ci_artifact_chain(
        run, artifact, zip_bytes,
        expected_repository=repository, expected_workflow_path=workflow_path,
        expected_artifact_name=expected_artifact_name, expected_result_filename=expected_result_filename,
        expected_run_id=run_id, expected_artifact_id=artifact_id,
        **validate_kwargs,
    )
