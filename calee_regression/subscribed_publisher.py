"""Regression-owned subscribed-calendar ICS publisher (Priority 5/6/7).

Replaces Hub-based provisioning (``subscribed_provision.py``'s
``http_provisioner``, which POSTs to a ``calee-hub-core`` admin endpoint that
does not exist -- see that module's docstring) with a pluggable publisher
entirely inside this repository. No new backend endpoint is added or
required.

The runner:

  1. generates the today-relative run-specific ICS (reusing
     ``subscribed_fixture.py``, unchanged);
  2. publishes it to a stable external URL already subscribed by the
     regression account, through one of several adapters (WebDAV PUT, HTTP
     PUT to a pre-signed/narrowly-authenticated object URL, an installed
     S3-compatible CLI, or local filesystem for offline validation only);
  3. polls the EXISTING, already-authenticated Calee API/UI (an injected
     ``poll_check`` callable -- this module never talks to the product API
     itself) until the run-specific event is visible, using the existing
     bounded ``polling.poll_until`` (never an arbitrary fixed sleep);
  4. records publication + observation evidence;
  5. exposes the generated event titles as scenario variables, exactly like
     the fixture generator already did.

Three explicit modes (Priority 6) -- never a silent fallback between them:

  * ``published``  -- publish + poll; the run-relative subscribed scenario is
    enabled only after BOTH publication and observation succeed.
  * ``fixed-date``  -- uses the existing static fixture and its known date;
    never asserts against "Today"; records that run-relative publication was
    NOT used.
  * ``offline-only`` -- generates and validates the ICS only; never claims
    provisioning; the physical scenario stays blocked.

Publisher security (Priority 5): every adapter resolves its credential
through the existing ``credentials`` provider chain (environment ->
Keychain, or injected for tests) -- never placed in argv, never written into
a report/exception, never persisted in plaintext. See ``credentials.py``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import credentials as credentials_mod
from . import ics_contract
from .polling import PollResult, poll_until
from .subscribed_fixture import (
    DEFAULT_TIMEZONE,
    allday_event_name,
    fixture_evidence,
    generate_today_relative_ics,
    resolve_target_date,
    timed_event_name,
)

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"

MODE_PUBLISHED = "published"
MODE_FIXED_DATE = "fixed-date"
MODE_OFFLINE_ONLY = "offline-only"
VALID_MODES = frozenset({MODE_PUBLISHED, MODE_FIXED_DATE, MODE_OFFLINE_ONLY})

NOT_ATTEMPTED = "not-attempted"

# Publisher credentials (Priority 5): resolved through the existing
# credentials.py provider chain, one CredentialRequest per adapter secret.
WEBDAV_USERNAME = credentials_mod.CredentialRequest(
    name="subscribed_webdav_username", env_var="CALEE_SUBSCRIBED_WEBDAV_USERNAME",
    keychain_service="calee-regression", keychain_account="subscribed-webdav-username", required=True,
)
WEBDAV_PASSWORD = credentials_mod.CredentialRequest(
    name="subscribed_webdav_password", env_var="CALEE_SUBSCRIBED_WEBDAV_PASSWORD",
    keychain_service="calee-regression", keychain_account="subscribed-webdav-password", required=True,
)
PRESIGNED_PUT_URL = credentials_mod.CredentialRequest(
    name="subscribed_presigned_put_url", env_var="CALEE_SUBSCRIBED_PRESIGNED_PUT_URL",
    keychain_service="calee-regression", keychain_account="subscribed-presigned-put-url", required=True,
)
S3_BUCKET = credentials_mod.CredentialRequest(
    name="subscribed_s3_bucket", env_var="CALEE_SUBSCRIBED_S3_BUCKET",
    keychain_service="calee-regression", keychain_account="subscribed-s3-bucket", required=True,
)
# Priority 8: the AWS credentials the "aws s3 cp" child process itself needs.
# Namespaced under CALEE_SUBSCRIBED_* (not the bare AWS_* names) so this never
# silently picks up an operator's own ambient AWS session/SSO credentials for
# something unrelated -- resolution is always explicit, exactly like every
# other adapter's secrets.
S3_ACCESS_KEY_ID = credentials_mod.CredentialRequest(
    name="subscribed_s3_access_key_id", env_var="CALEE_SUBSCRIBED_AWS_ACCESS_KEY_ID",
    keychain_service="calee-regression", keychain_account="subscribed-s3-access-key-id", required=True,
)
S3_SECRET_ACCESS_KEY = credentials_mod.CredentialRequest(
    name="subscribed_s3_secret_access_key", env_var="CALEE_SUBSCRIBED_AWS_SECRET_ACCESS_KEY",
    keychain_service="calee-regression", keychain_account="subscribed-s3-secret-access-key", required=True,
)
# Optional -- only needed for temporary/STS-issued credentials.
S3_SESSION_TOKEN = credentials_mod.CredentialRequest(
    name="subscribed_s3_session_token", env_var="CALEE_SUBSCRIBED_AWS_SESSION_TOKEN",
    keychain_service="calee-regression", keychain_account="subscribed-s3-session-token", required=False,
)


class PublisherError(Exception):
    """A publisher configuration/usage problem (e.g. an unknown publisher
    type). Distinct from a publish ATTEMPT failing, which is reported as a
    structured PublishResult, never raised."""


@dataclass
class PublishResult:
    ok: bool
    detail: str = ""


# A Publisher takes the ICS text and returns a PublishResult. Injected so
# every adapter is offline-testable; a real one performs one network PUT or
# shells an installed CLI. Never raises for a failed publish (network/auth/
# tool problems become `ok=False`, never an uncaught exception reaching a
# report/exception message that could carry a credential).
Publisher = Callable[[str], PublishResult]


def _redacted_publisher(inner: Publisher, secrets: "list[str]") -> Publisher:
    """Wraps a Publisher so any exception's message is scrubbed of every
    resolved secret before it can reach a caller/report -- the last line of
    defence alongside each adapter never putting a secret in argv/URL/log."""

    def _wrapped(ics: str) -> PublishResult:
        try:
            return inner(ics)
        except Exception as exc:  # noqa: BLE001 - must never leak a raw secret-bearing message
            return PublishResult(ok=False, detail=credentials_mod.redact(f"{type(exc).__name__}: {exc}", secrets))

    return _wrapped


def webdav_publisher(public_url: str, *, username: str, password: str, timeout: float = 30.0, opener=None) -> Publisher:
    """An HTTP PUT (WebDAV) publisher, HTTP Basic-authenticated. Credentials
    are placed only in the Authorization header, never in the URL/argv/log."""
    import base64
    import urllib.request

    _open = opener or urllib.request.urlopen
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")

    def _publish(ics: str) -> PublishResult:
        req = urllib.request.Request(
            public_url, data=ics.encode("utf-8"), method="PUT",
            headers={"Content-Type": "text/calendar; charset=utf-8", "Authorization": f"Basic {auth}"},
        )
        with _open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
        if status not in (200, 201, 204):
            return PublishResult(ok=False, detail=f"WebDAV PUT returned status {status}.")
        return PublishResult(ok=True, detail=f"WebDAV PUT to {public_url} succeeded (status {status}).")

    return _redacted_publisher(_publish, [username, password])


def presigned_put_publisher(put_url: str, *, public_url: "str | None" = None, timeout: float = 30.0, opener=None) -> Publisher:
    """An HTTP PUT to a pre-signed or narrowly-authenticated object URL (S3
    presigned URL, Azure SAS URL, etc.) -- the authorisation is embedded in
    the URL itself by whoever generated it; this adapter never appends or
    logs it separately. ``put_url`` is resolved from the credential provider
    (never a CLI argument); ``public_url`` (if different, e.g. a presigned
    upload URL vs. the stable public read URL) is recorded in evidence
    instead of the presigned one."""
    import urllib.request

    _open = opener or urllib.request.urlopen

    def _publish(ics: str) -> PublishResult:
        req = urllib.request.Request(
            put_url, data=ics.encode("utf-8"), method="PUT",
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        with _open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
        if status not in (200, 201, 204):
            return PublishResult(ok=False, detail=f"Pre-signed PUT returned status {status}.")
        return PublishResult(ok=True, detail=f"Pre-signed PUT to {public_url or '<presigned URL>'} succeeded.")

    return _redacted_publisher(_publish, [put_url])


def s3_cli_publisher(
    *, bucket: str, key: str, cli_path: str = "aws", region: "str | None" = None,
    env: "dict | None" = None, runner=None, secrets: "list[str] | None" = None,
) -> Publisher:
    """Uploads via an already-installed S3-compatible CLI (``aws s3 cp``),
    piping the ICS over stdin so it never touches a temp file or argv.
    Credentials belong in the CHILD environment only (AWS_* env vars) --
    never appended to the command array itself -- so this function accepts an
    already-built ``env`` mapping rather than resolving credentials itself;
    ``build_publisher_from_config`` is what actually resolves
    S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY/S3_SESSION_TOKEN via credentials.py
    and builds that mapping with ``credentials.build_env`` (Priority 8), then
    wraps the returned Publisher in ``_redacted_publisher`` so a subprocess
    exception (e.g. a missing ``aws`` binary, a timeout) can never leak a
    resolved secret.

    ``secrets`` (Priority 8) is the same resolved-value list, used here to
    scrub the CLI's own stderr before it is embedded in a normal (non-
    exception) ``PublishResult.detail`` on a nonzero exit -- ``aws`` can echo
    the credential it rejected (e.g. an AccessDenied message quoting the key
    id) straight to stderr, and that path never raises an exception for
    ``_redacted_publisher`` to catch, so it must be redacted here directly.

    ``runner`` is injectable (``subprocess.run``-shaped) for offline tests."""
    import subprocess as _subprocess

    _run = runner or _subprocess.run
    target = f"s3://{bucket}/{key}"

    def _publish(ics: str) -> PublishResult:
        argv = [cli_path, "s3", "cp", "-", target]
        if region:
            argv += ["--region", region]
        proc = _run(argv, input=ics.encode("utf-8"), capture_output=True, env=env, timeout=60)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
            detail = f"{cli_path} s3 cp exited {proc.returncode}: {stderr.strip()[:300]}"
            if secrets:
                detail = credentials_mod.redact(detail, secrets)
            return PublishResult(ok=False, detail=detail)
        return PublishResult(ok=True, detail=f"Uploaded to {target} via {cli_path}.")

    return _publish


def local_filesystem_publisher(path) -> Publisher:
    """Writes the ICS to a local file. OFFLINE-ONLY VALIDATION -- this never
    makes the ICS reachable at any externally-subscribed URL, so it must
    never be used for the ``published`` mode's "publish + poll" contract;
    only ``offline-only`` mode (which never claims provisioning at all) may
    use it."""
    from pathlib import Path as _Path

    target = _Path(path)

    def _publish(ics: str) -> PublishResult:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ics, encoding="utf-8")
        return PublishResult(ok=True, detail=f"Wrote ICS to local file {target} (offline validation only).")

    return _publish


@dataclass
class SubscribedFixtureResult:
    """First-class run evidence for the subscribed-fixture component
    (Priority 7). Every field Priority 7's evidence contract lists."""

    status: str
    mode: str
    run_id: "str | None" = None
    release_id: "str | None" = None
    # Priority 7: when this evidence was produced, independent of any
    # individual phase's own timestamp -- lets consolidation reject a stale
    # report (e.g. reused from a previous execution) even for fixed-date/
    # offline-only modes, which never set publication/observation/ingestion
    # timestamps at all.
    generated_at: "str | None" = None
    resolved_date: "str | None" = None
    timezone: str = DEFAULT_TIMEZONE
    generated_titles: dict = field(default_factory=dict)
    public_url: "str | None" = None
    publisher_type: "str | None" = None
    content_sha256: "str | None" = None
    publication_status: str = NOT_ATTEMPTED
    publication_timestamp: "str | None" = None
    observation_status: str = NOT_ATTEMPTED
    observation_timestamp: "str | None" = None
    poll_attempts: int = 0
    # Priority 5: exact public-content verification, distinct from the
    # coarse observation_status above -- publicReadVerificationStatus is
    # "ok" ONLY when the downloaded bytes' SHA-256 matches the generated
    # ICS's, both run-specific titles are present, AND the expected target
    # date is present. A stale-but-nonempty ICS records blocked-mismatch
    # here even though the raw HTTP fetch itself succeeded.
    public_read_verification_status: str = NOT_ATTEMPTED
    public_read_attempts: int = 0
    public_read_observed_sha256: "str | None" = None
    public_read_verified_at: "str | None" = None
    # Priority 6: a SECOND, separate phase -- proving Calee actually
    # INGESTED the published feed via an existing authenticated regression
    # API operation, never inferred from public-URL readability alone.
    # ingestion_observed_event carries only identity fields (id/title/
    # calendarId) -- never credentials or authorization headers.
    ingestion_status: str = NOT_ATTEMPTED
    ingestion_attempts: int = 0
    ingestion_elapsed_seconds: "float | None" = None
    ingestion_timestamp: "str | None" = None
    ingestion_api: "str | None" = None
    ingestion_observed_event: "dict | None" = None
    run_relative_publication_used: bool = False
    detail: "list[str]" = field(default_factory=list)
    ics: "str | None" = None  # provisioning input, never written to the JSON evidence

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "runId": self.run_id,
            "releaseId": self.release_id,
            "generatedAt": self.generated_at,
            "resolvedDate": self.resolved_date,
            "timezone": self.timezone,
            "generatedTitles": dict(self.generated_titles),
            "publicUrl": self.public_url,
            "publisherType": self.publisher_type,
            "contentSha256": self.content_sha256,
            "publicationStatus": self.publication_status,
            "publicationTimestamp": self.publication_timestamp,
            "observationStatus": self.observation_status,
            "observationTimestamp": self.observation_timestamp,
            "pollAttempts": self.poll_attempts,
            "publicReadVerificationStatus": self.public_read_verification_status,
            "publicReadAttempts": self.public_read_attempts,
            "publicReadObservedSha256": self.public_read_observed_sha256,
            "publicReadVerifiedAt": self.public_read_verified_at,
            "ingestionStatus": self.ingestion_status,
            "ingestionAttempts": self.ingestion_attempts,
            "ingestionElapsedSeconds": self.ingestion_elapsed_seconds,
            "ingestionTimestamp": self.ingestion_timestamp,
            "ingestionApi": self.ingestion_api,
            "ingestionObservedEvent": dict(self.ingestion_observed_event) if self.ingestion_observed_event else None,
            "runRelativePublicationUsed": self.run_relative_publication_used,
            "detail": list(self.detail),
        }


def build_run_token(run_id: "str | None") -> str:
    if not run_id:
        return "LOCAL"
    token = "".join(ch for ch in str(run_id) if ch.isalnum())
    return (token[-12:] or "LOCAL").upper()


def scenario_variables(target_date: _dt.date, *, run_token: str) -> dict:
    return {
        "REG_SUB_TIMED_TITLE": timed_event_name(run_token),
        "REG_SUB_ALLDAY_TITLE": allday_event_name(run_token),
        "REG_SUB_DATE": target_date.isoformat(),
    }


def prepare_subscribed_fixture(
    *,
    run_id: "str | None",
    mode: str,
    release_id: "str | None" = None,
    target_date: "_dt.date | None" = None,
    timezone: str = DEFAULT_TIMEZONE,
    publisher: "Publisher | None" = None,
    publisher_type: "str | None" = None,
    public_url: "str | None" = None,
    poll_check=None,
    poll_interval_seconds: float = 10.0,
    poll_timeout_seconds: float = 300.0,
    # Priority 6: the SECOND, separate phase -- an existing authenticated
    # regression API operation's zero-arg lookup, returning
    # {"found": bool, "id":..., "title":..., "calendarId":...}. None means
    # "no such operation is available for this run" -- published mode then
    # returns BLOCKED with a precise reason, never a silent pass from the
    # public-URL phase alone.
    ingestion_check=None,
    ingestion_interval_seconds: float = 10.0,
    ingestion_timeout_seconds: float = 300.0,
    ingestion_api_label: "str | None" = None,
    ingestion_expected_calendar_id: "str | None" = None,
    fixed_date_titles: "dict | None" = None,
    fixed_date: "str | None" = None,
    now=None,
    clock=None,
    sleep=None,
) -> SubscribedFixtureResult:
    """Runs exactly ONE of the three explicit modes (Priority 6) -- never a
    silent fallback between them. Always generates the today-relative ICS
    first (cheap, pure, needed for its own evidence/validation regardless of
    mode); only ``published`` mode actually publishes+polls."""
    if mode not in VALID_MODES:
        raise PublisherError(f"Unknown subscribed-fixture mode {mode!r}; must be one of {sorted(VALID_MODES)}.")

    token = build_run_token(run_id)
    date_ = resolve_target_date(target_date)
    ics = generate_today_relative_ics(date_, run_token=token)
    generated_titles = scenario_variables(date_, run_token=token)
    content_sha256 = hashlib.sha256(ics.encode("utf-8")).hexdigest()
    timestamp = (now or _dt.datetime.now(_dt.timezone.utc).isoformat())

    base_kwargs = dict(
        run_id=run_id, release_id=release_id, mode=mode, generated_at=timestamp,
        resolved_date=date_.isoformat(), timezone=timezone,
        generated_titles=generated_titles, content_sha256=content_sha256, ics=ics,
    )

    if mode == MODE_OFFLINE_ONLY:
        # Generate + validate only -- NEVER claims provisioning; the physical
        # scenario stays blocked regardless of how clean the ICS is.
        return SubscribedFixtureResult(
            status=STATUS_OK, publication_status=NOT_ATTEMPTED, observation_status=NOT_ATTEMPTED,
            run_relative_publication_used=False,
            detail=["offline-only mode: ICS generated and validated locally; no publication was attempted or claimed."],
            **base_kwargs,
        )

    if mode == MODE_FIXED_DATE:
        # Uses the EXISTING static fixture and its own known date -- never
        # asserts against "Today", and explicitly records that run-relative
        # publication was NOT used (Priority 6 requirement).
        fixed_date_kwargs = dict(base_kwargs)
        fixed_date_kwargs["resolved_date"] = fixed_date or base_kwargs["resolved_date"]
        fixed_date_kwargs["generated_titles"] = fixed_date_titles or {}
        return SubscribedFixtureResult(
            status=STATUS_OK, publication_status=NOT_ATTEMPTED, observation_status=NOT_ATTEMPTED,
            run_relative_publication_used=False,
            detail=[
                "fixed-date mode: navigates to the existing static subscribed fixture's known date; "
                "does not assert Today; run-relative publication was NOT used this run.",
            ],
            **fixed_date_kwargs,
        )

    # mode == MODE_PUBLISHED
    if publisher is None:
        return SubscribedFixtureResult(
            status=STATUS_BLOCKED, publication_status=STATUS_BLOCKED, observation_status=NOT_ATTEMPTED,
            run_relative_publication_used=False,
            detail=["published mode: no publisher is configured for this machine -- see config/tester.local.yaml's subscribed_fixture section."],
            **base_kwargs,
        )
    publish_outcome = publisher(ics)
    if not publish_outcome.ok:
        return SubscribedFixtureResult(
            status=STATUS_BLOCKED, publication_status=STATUS_BLOCKED, observation_status=NOT_ATTEMPTED,
            run_relative_publication_used=False, public_url=public_url, publisher_type=publisher_type,
            publication_timestamp=timestamp,
            detail=[f"published mode: publication failed: {publish_outcome.detail}"],
            **base_kwargs,
        )

    if poll_check is None:
        return SubscribedFixtureResult(
            status=STATUS_BLOCKED, publication_status=STATUS_OK, observation_status=STATUS_BLOCKED,
            public_read_verification_status=STATUS_BLOCKED,
            run_relative_publication_used=False, public_url=public_url, publisher_type=publisher_type,
            publication_timestamp=timestamp,
            detail=["published mode: publication succeeded, but no observation check (Calee API/UI poll) was configured."],
            **base_kwargs,
        )

    # Priority 5: published mode must not pass merely because the public URL
    # returns nonempty bytes. Every poll attempt re-derives, from the exact
    # downloaded bytes: their raw SHA-256 (must equal content_sha256, the
    # generated ICS's own hash), whether both run-specific titles are
    # textually present, and whether the expected target date is a visible
    # occurrence date once the downloaded text is expanded through
    # ics_contract.expand -- so a stale-but-nonempty (or unrelated) ICS at
    # the same URL can never count as success.
    timed_title = generated_titles.get("REG_SUB_TIMED_TITLE", "")
    allday_title = generated_titles.get("REG_SUB_ALLDAY_TITLE", "")

    def _verified_check():
        raw = poll_check()
        if raw is None:
            return {"ok": False, "sha256": None, "hasTimedTitle": False, "hasAlldayTitle": False, "hasTargetDate": False}
        raw_bytes = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
        text = raw_bytes.decode("utf-8", errors="replace")
        observed_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        has_target_date = False
        try:
            occurrences = ics_contract.expand(text)
            visible_dates = {d for occ in occurrences for d in occ.visible_dates}
            has_target_date = date_ in visible_dates
        except ics_contract.IcsContractError:
            has_target_date = False
        return {
            "sha256": observed_sha256,
            "hasTimedTitle": bool(timed_title) and timed_title in text,
            "hasAlldayTitle": bool(allday_title) and allday_title in text,
            "hasTargetDate": has_target_date,
        }

    def _is_verified(observation) -> bool:
        return bool(
            observation
            and observation.get("sha256") == content_sha256
            and observation.get("hasTimedTitle")
            and observation.get("hasAlldayTitle")
            and observation.get("hasTargetDate")
        )

    poll_result: PollResult = poll_until(
        check=_verified_check, is_success=_is_verified,
        timeout_seconds=poll_timeout_seconds, interval_seconds=poll_interval_seconds,
        clock=clock, sleep=sleep,
    )
    last_observed = poll_result.last_observed or {}
    if not poll_result.succeeded:
        reasons = []
        if last_observed.get("sha256") is None:
            reasons.append("the public URL could not be retrieved")
        elif last_observed.get("sha256") != content_sha256:
            reasons.append("the downloaded content's SHA-256 does not match the generated ICS (stale or unrelated content)")
        if not last_observed.get("hasTimedTitle"):
            reasons.append("the run-specific timed-event title is not present")
        if not last_observed.get("hasAlldayTitle"):
            reasons.append("the run-specific all-day-event title is not present")
        if not last_observed.get("hasTargetDate"):
            reasons.append("the expected target date is not present")
        verification_status = "blocked-timeout" if last_observed.get("sha256") is None else "blocked-mismatch"
        return SubscribedFixtureResult(
            status=STATUS_BLOCKED, publication_status=STATUS_OK, observation_status="timeout",
            public_read_verification_status=verification_status, public_read_attempts=poll_result.attempts,
            public_read_observed_sha256=last_observed.get("sha256"),
            run_relative_publication_used=False, public_url=public_url, publisher_type=publisher_type,
            publication_timestamp=timestamp, poll_attempts=poll_result.attempts,
            detail=[
                f"published mode: publication succeeded, but the public read content could not be verified "
                f"within {poll_timeout_seconds}s ({poll_result.attempts} attempt(s)): " + "; ".join(reasons)
                + (f". Last error: {poll_result.last_error}" if poll_result.last_error else ""),
            ],
            **base_kwargs,
        )

    verified_at = now or _dt.datetime.now(_dt.timezone.utc).isoformat()
    public_read_kwargs = dict(
        publication_status=STATUS_OK, observation_status=STATUS_OK,
        public_read_verification_status=STATUS_OK, public_read_attempts=poll_result.attempts,
        public_read_observed_sha256=last_observed.get("sha256"), public_read_verified_at=verified_at,
        public_url=public_url, publisher_type=publisher_type,
        publication_timestamp=timestamp, observation_timestamp=verified_at,
        poll_attempts=poll_result.attempts,
    )

    # Priority 6: a SECOND, separate bounded poll -- through an EXISTING
    # authenticated regression API operation -- proving Calee actually
    # INGESTED the published feed. Public-URL readability (verified above)
    # is never substituted for this; both phases must succeed for published
    # mode to return status: ok.
    if ingestion_check is None:
        return SubscribedFixtureResult(
            status=STATUS_BLOCKED, ingestion_status=STATUS_BLOCKED,
            run_relative_publication_used=False,
            detail=[
                "published mode: the public URL content was verified, but no existing authenticated "
                "regression API operation is configured to observe Calee's ingestion of the subscribed "
                "calendar -- cannot prove the hub actually parsed the published feed. BLOCKED, never a "
                "silent pass from public-URL readability alone.",
            ],
            **public_read_kwargs, **base_kwargs,
        )

    timed_title_for_ingestion = timed_title

    def _is_ingested(observation) -> bool:
        if not observation or not observation.get("found"):
            return False
        if observation.get("title") != timed_title_for_ingestion:
            return False
        if ingestion_expected_calendar_id is not None and observation.get("calendarId") != ingestion_expected_calendar_id:
            return False
        return True

    ingestion_poll_result: PollResult = poll_until(
        check=ingestion_check, is_success=_is_ingested,
        timeout_seconds=ingestion_timeout_seconds, interval_seconds=ingestion_interval_seconds,
        clock=clock, sleep=sleep,
    )
    ingestion_observed = ingestion_poll_result.last_observed or {}
    # Never record credentials/authorization headers -- only identity fields
    # the API itself returned.
    observed_event_summary = None
    if isinstance(ingestion_observed, dict) and ingestion_observed.get("found"):
        observed_event_summary = {
            "id": ingestion_observed.get("id"),
            "title": ingestion_observed.get("title"),
            "calendarId": ingestion_observed.get("calendarId"),
        }

    if not ingestion_poll_result.succeeded:
        return SubscribedFixtureResult(
            status=STATUS_BLOCKED, ingestion_status=STATUS_BLOCKED,
            ingestion_attempts=ingestion_poll_result.attempts,
            ingestion_elapsed_seconds=ingestion_poll_result.elapsed_seconds,
            ingestion_api=ingestion_api_label, ingestion_observed_event=observed_event_summary,
            run_relative_publication_used=False,
            detail=[
                f"published mode: public URL content verified, but Calee ingestion was not observed via "
                f"{ingestion_api_label or 'the configured API'} within {ingestion_timeout_seconds}s "
                f"({ingestion_poll_result.attempts} attempt(s))."
                + (f" Last observed: {observed_event_summary}." if observed_event_summary else " No matching event was ever observed.")
                + (f" Last error: {ingestion_poll_result.last_error}" if ingestion_poll_result.last_error else ""),
            ],
            **public_read_kwargs, **base_kwargs,
        )

    ingested_at = now or _dt.datetime.now(_dt.timezone.utc).isoformat()
    return SubscribedFixtureResult(
        status=STATUS_OK, ingestion_status=STATUS_OK,
        ingestion_attempts=ingestion_poll_result.attempts,
        ingestion_elapsed_seconds=ingestion_poll_result.elapsed_seconds, ingestion_timestamp=ingested_at,
        ingestion_api=ingestion_api_label, ingestion_observed_event=observed_event_summary,
        run_relative_publication_used=True,
        detail=[
            f"published mode: verified the EXACT public content (byte SHA-256, both run-specific titles, "
            f"expected target date) after {poll_result.attempts} attempt(s), AND confirmed Calee ingestion "
            f"via {ingestion_api_label or 'the configured API'} after {ingestion_poll_result.attempts} "
            f"attempt(s) -- the run-relative subscribed scenario may now be enabled.",
        ],
        **public_read_kwargs, **base_kwargs,
    )


def build_publisher_from_config(
    section: "dict | None", *, resolver: "credentials_mod.CredentialResolver | None" = None, mode: "str | None" = None,
):
    """Builds a (Publisher, publisher_type, public_url) triple from a
    ``subscribed_fixture:`` config section (see config/tester.local.example.
    yaml), resolving any credential the chosen adapter needs through the
    given resolver (defaults to credentials.default_resolver()). Returns
    (None, None, None) when no publisher is configured (published mode then
    BLOCKS honestly rather than fabricating a publisher). Never raises for a
    missing credential -- a required-but-absent credential makes this return
    (None, ...) too (published mode BLOCKS with a clear reason), exactly like
    any other missing-provisioner case; it never crashes the run.

    ``mode`` (Priority 5): the ``local`` adapter is REJECTED (returns
    (None, None, None), the same honest BLOCKED path as a missing credential)
    whenever ``mode == MODE_PUBLISHED`` -- writing a file to local disk never
    makes the ICS reachable at any externally-subscribed URL, so it can never
    satisfy the published mode's publish+poll contract. ``local`` remains
    valid for every other mode (offline-only)."""
    if not section:
        return None, None, None
    resolver = resolver or credentials_mod.default_resolver()
    publisher_kind = section.get("publisher")
    public_url = section.get("public_url")
    if publisher_kind == "local" and mode == MODE_PUBLISHED:
        return None, None, None
    try:
        if publisher_kind == "webdav":
            username = resolver.require(WEBDAV_USERNAME)
            password = resolver.require(WEBDAV_PASSWORD)
            return webdav_publisher(public_url, username=username, password=password), "webdav", public_url
        if publisher_kind == "presigned-put":
            put_url = resolver.require(PRESIGNED_PUT_URL)
            return presigned_put_publisher(put_url, public_url=public_url), "presigned-put", public_url
        if publisher_kind == "s3-cli":
            bucket = resolver.require(S3_BUCKET)
            access_key_id = resolver.require(S3_ACCESS_KEY_ID)
            secret_access_key = resolver.require(S3_SECRET_ACCESS_KEY)
            session_token = resolver.get(S3_SESSION_TOKEN)
            key = section.get("s3_key", "calee/regression-calendar.ics")
            # Priority 8: the child "aws s3 cp" process's own AWS_* env vars,
            # built the same way every other credential-bearing subprocess in
            # this framework builds its child env (credentials.build_env --
            # never on argv). Wrapped in _redacted_publisher so a subprocess
            # exception (missing aws binary, timeout, permission error) or a
            # stderr message can never leak a resolved secret -- the same
            # protection webdav/presigned-put already have.
            resolved = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
            mapping = {"access_key_id": "AWS_ACCESS_KEY_ID", "secret_access_key": "AWS_SECRET_ACCESS_KEY"}
            secrets = [access_key_id, secret_access_key]
            if session_token:
                resolved["session_token"] = session_token
                mapping["session_token"] = "AWS_SESSION_TOKEN"
                secrets.append(session_token)
            child_env = credentials_mod.build_env(None, resolved, mapping)
            inner = s3_cli_publisher(
                bucket=bucket, key=key, cli_path=section.get("s3_cli_path", "aws"),
                env=child_env, secrets=secrets,
            )
            return _redacted_publisher(inner, secrets), "s3-cli", public_url
        if publisher_kind == "local":
            return local_filesystem_publisher(section.get("local_path", "/tmp/reg-sub-calendar.ics")), "local", public_url
    except credentials_mod.CredentialError:
        return None, None, None
    return None, None, None
