"""Offline tests for the regression-owned subscribed-calendar ICS publisher
(Priority 5/6/7). No real network/device/backend is used anywhere: every
publisher adapter goes through an injected opener/runner, and polling uses
test_polling.py's fake-clock pattern.

Covers the specific offline-orchestration scenarios this session's task
enumerates: WebDAV publisher success through an injected client (#14),
publisher credential absent from argv/reports (#15), published ICS digest
matches evidence (#16), bounded polling success (#17), polling timeout
blocks (#18), fixed-date mode never asserts Today (#19), offline-only mode
never claims publication (#20).
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from calee_regression import credentials as credentials_mod
from calee_regression import subscribed_publisher as sp
from calee_regression import ics_contract


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ── #14: WebDAV publisher success through an injected client ───────────────


def test_webdav_publisher_puts_ics_with_basic_auth_through_injected_opener():
    captured = {}

    class _Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        return _Resp()

    publisher = sp.webdav_publisher(
        "https://fixtures.example.com/calee/regression-calendar.ics",
        username="reg-user", password="reg-pass", opener=_opener,
    )
    result = publisher("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    assert result.ok, result.detail
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://fixtures.example.com/calee/regression-calendar.ics"
    assert captured["body"] == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    # Basic auth header present, but the RAW credential never appears bare in the URL.
    assert "Authorization" in captured["headers"]
    assert "reg-user" not in captured["url"] and "reg-pass" not in captured["url"]


def test_webdav_publisher_non_2xx_status_is_not_ok():
    class _Resp:
        status = 403

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    publisher = sp.webdav_publisher("https://x/y.ics", username="u", password="p", opener=lambda req, timeout=None: _Resp())
    result = publisher("ICS")
    assert not result.ok
    assert "403" in result.detail


# ── #15: publisher credential absent from argv and reports ─────────────────


def test_webdav_publisher_exception_message_is_redacted_of_credentials():
    secret_password = "hunter2-VERY-SECRET"

    def _raising_opener(req, timeout=None):
        # Simulate a library/proxy that echoes the raw password into its
        # exception message (e.g. a connection-string debug dump) -- a
        # realistic leak path distinct from the (already-encoded) Basic auth
        # header, which redact() cannot un-base64 to find a raw match in.
        raise RuntimeError(f"auth failed for password={secret_password!r}")

    publisher = sp.webdav_publisher("https://x/y.ics", username="reg-user", password=secret_password, opener=_raising_opener)
    result = publisher("ICS")
    assert not result.ok
    assert secret_password not in result.detail
    assert "REDACTED" in result.detail


def test_webdav_publisher_basic_auth_header_never_contains_raw_password_as_plaintext_json():
    # The Authorization header value is base64(user:pass) -- never the raw
    # password in a form redact() (or a log scraper) could miss by being
    # ALREADY encoded; this test locks in that the header is base64, not a
    # plain "username:password" string a naive log line might emit unencoded.
    captured = {}

    class _Resp:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _opener(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return _Resp()

    sp.webdav_publisher("https://x/y.ics", username="reg-user", password="hunter2", opener=_opener)("ICS")
    assert captured["headers"]["Authorization"].startswith("Basic ")
    assert "hunter2" not in captured["headers"]["Authorization"]


def test_presigned_put_publisher_never_puts_url_in_a_separate_credential_field():
    # The presigned URL itself carries the authorisation; this adapter must
    # not additionally require/accept a separate secret parameter at all --
    # confirmed structurally via its signature.
    import inspect
    sig = inspect.signature(sp.presigned_put_publisher)
    for name in sig.parameters:
        assert "password" not in name.lower() and "token" not in name.lower() and "secret" not in name.lower()


def test_s3_cli_publisher_pipes_ics_over_stdin_never_argv():
    calls = []

    class _FakeCompleted:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = b""
            self.stderr = b""

    def _fake_run(argv, *, input=None, capture_output=None, env=None, timeout=None):
        calls.append({"argv": list(argv), "input": input, "env": env})
        return _FakeCompleted(0)

    publisher = sp.s3_cli_publisher(bucket="calee-fixtures", key="calee/regression-calendar.ics", runner=_fake_run,
                                     env={"AWS_ACCESS_KEY_ID": "AKIA...", "AWS_SECRET_ACCESS_KEY": "shh"})
    result = publisher("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    assert result.ok, result.detail
    call = calls[0]
    # The ICS body travels via stdin, never as a command-line argument.
    assert call["input"] == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    assert "BEGIN:VCALENDAR" not in " ".join(call["argv"])
    # No AWS secret appears in the command array itself (only in the child env).
    assert "shh" not in " ".join(call["argv"]) and "AKIA..." not in " ".join(call["argv"])
    assert call["env"]["AWS_SECRET_ACCESS_KEY"] == "shh"  # credential reached the child via env, as intended


def test_s3_cli_publisher_redacts_secret_from_nonzero_exit_stderr():
    # aws CLI can echo the credential it rejected straight into stderr on a
    # normal (non-exception) failure -- this never raises, so
    # _redacted_publisher's except-clause never sees it; s3_cli_publisher
    # must scrub it itself using its own `secrets` param (Priority 8).
    secret_key = "AKIA-FAKE-REJECTED-KEY"

    class _FakeCompleted:
        returncode = 1
        stdout = b""
        stderr = f"An error occurred (InvalidAccessKeyId): The AWS Access Key Id {secret_key} does not exist".encode("utf-8")

    def _fake_run(argv, *, input=None, capture_output=None, env=None, timeout=None):
        return _FakeCompleted()

    publisher = sp.s3_cli_publisher(
        bucket="calee-fixtures", key="calee/regression-calendar.ics", runner=_fake_run,
        env={"AWS_ACCESS_KEY_ID": secret_key, "AWS_SECRET_ACCESS_KEY": "shh"},
        secrets=[secret_key, "shh"],
    )
    result = publisher("ICS")
    assert not result.ok
    assert secret_key not in result.detail
    assert "REDACTED" in result.detail


def test_s3_cli_publisher_stderr_unredacted_when_no_secrets_given():
    # Without a `secrets` list (e.g. an ad-hoc/offline caller that never
    # wired credentials), s3_cli_publisher must not crash trying to redact --
    # it simply has nothing to scrub.
    class _FakeCompleted:
        returncode = 1
        stdout = b""
        stderr = b"some non-secret failure detail"

    def _fake_run(argv, *, input=None, capture_output=None, env=None, timeout=None):
        return _FakeCompleted()

    publisher = sp.s3_cli_publisher(bucket="calee-fixtures", key="k.ics", runner=_fake_run)
    result = publisher("ICS")
    assert not result.ok
    assert "some non-secret failure detail" in result.detail


# ── Priority 8: build_publisher_from_config actually resolves AWS credentials


def test_build_publisher_from_config_s3_cli_resolves_aws_credentials_and_builds_child_env(monkeypatch):
    import subprocess

    calls = []

    class _FakeCompleted:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(argv, *, input=None, capture_output=None, env=None, timeout=None):
        calls.append({"argv": list(argv), "input": input, "env": dict(env) if env else None})
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({
        "subscribed_s3_bucket": "calee-fixtures",
        "subscribed_s3_access_key_id": "AKIA_FAKE_ID",
        "subscribed_s3_secret_access_key": "sh-s3-secret",
    })])
    publisher, kind, url = sp.build_publisher_from_config(
        {"publisher": "s3-cli", "s3_key": "calee/regression-calendar.ics"}, resolver=resolver,
    )
    assert publisher is not None and kind == "s3-cli"

    result = publisher("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
    assert result.ok, result.detail
    call = calls[0]
    # No AWS secret ever appears in the command array itself.
    assert "sh-s3-secret" not in " ".join(call["argv"]) and "AKIA_FAKE_ID" not in " ".join(call["argv"])
    # It DID reach the child env, under the standard names "aws s3 cp" reads.
    assert call["env"]["AWS_ACCESS_KEY_ID"] == "AKIA_FAKE_ID"
    assert call["env"]["AWS_SECRET_ACCESS_KEY"] == "sh-s3-secret"
    # The base environment (PATH, HOME, ...) is preserved, not replaced.
    assert call["env"].get("PATH") == os.environ.get("PATH")


def test_build_publisher_from_config_s3_cli_resolves_optional_session_token():
    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({
        "subscribed_s3_bucket": "calee-fixtures",
        "subscribed_s3_access_key_id": "AKIA_FAKE_ID",
        "subscribed_s3_secret_access_key": "sh-s3-secret",
        "subscribed_s3_session_token": "sts-token-value",
    })])
    publisher, kind, url = sp.build_publisher_from_config({"publisher": "s3-cli"}, resolver=resolver)
    assert publisher is not None and kind == "s3-cli"


def test_build_publisher_from_config_s3_cli_blocks_honestly_when_access_key_missing():
    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({
        "subscribed_s3_bucket": "calee-fixtures",
        "subscribed_s3_secret_access_key": "sh-s3-secret",
        # no access key id
    })])
    publisher, kind, url = sp.build_publisher_from_config({"publisher": "s3-cli"}, resolver=resolver)
    assert publisher is None and kind is None and url is None


def test_build_publisher_from_config_s3_cli_blocks_honestly_when_secret_key_missing():
    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({
        "subscribed_s3_bucket": "calee-fixtures",
        "subscribed_s3_access_key_id": "AKIA_FAKE_ID",
        # no secret access key
    })])
    publisher, kind, url = sp.build_publisher_from_config({"publisher": "s3-cli"}, resolver=resolver)
    assert publisher is None and kind is None and url is None


def test_build_publisher_from_config_s3_cli_wraps_a_raised_exception_never_crashes(monkeypatch):
    # A missing `aws` binary/timeout/permission error raises inside the
    # subprocess call -- this must never propagate out of the Publisher, and
    # must never leak the resolved secret in the exception text (Priority 8:
    # "wrap in _redacted_publisher").
    import subprocess

    secret = "sh-s3-secret-in-exception"

    def _raising_run(argv, **kwargs):
        raise RuntimeError(f"aws cli invocation failed; debug env dump included secret={secret!r}")

    monkeypatch.setattr(subprocess, "run", _raising_run)

    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({
        "subscribed_s3_bucket": "calee-fixtures",
        "subscribed_s3_access_key_id": "AKIA_FAKE_ID",
        "subscribed_s3_secret_access_key": secret,
    })])
    publisher, kind, url = sp.build_publisher_from_config({"publisher": "s3-cli"}, resolver=resolver)
    result = publisher("ICS")  # must not raise
    assert not result.ok
    assert secret not in result.detail
    assert "REDACTED" in result.detail


def test_build_publisher_from_config_s3_cli_redacts_secret_from_nonzero_exit_stderr(monkeypatch):
    import subprocess

    secret = "sh-s3-secret-rejected"

    class _FakeCompleted:
        returncode = 1
        stdout = b""
        stderr = f"An error occurred (InvalidAccessKeyId): key rejected, saw secret={secret}".encode("utf-8")

    def _fake_run(argv, **kwargs):
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({
        "subscribed_s3_bucket": "calee-fixtures",
        "subscribed_s3_access_key_id": "AKIA_FAKE_ID",
        "subscribed_s3_secret_access_key": secret,
    })])
    publisher, kind, url = sp.build_publisher_from_config({"publisher": "s3-cli"}, resolver=resolver)
    result = publisher("ICS")
    assert not result.ok
    assert secret not in result.detail
    assert "REDACTED" in result.detail


def test_build_publisher_from_config_never_raises_on_missing_credential_and_blocks_honestly():
    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({})])  # nothing resolvable
    publisher, kind, url = sp.build_publisher_from_config(
        {"publisher": "webdav", "public_url": "https://x/y.ics"}, resolver=resolver,
    )
    assert publisher is None and kind is None and url is None


def test_build_publisher_from_config_resolves_credentials_via_injected_provider():
    resolver = credentials_mod.CredentialResolver([credentials_mod.InjectedProvider({
        "subscribed_webdav_username": "reg-user", "subscribed_webdav_password": "reg-pass",
    })])
    publisher, kind, url = sp.build_publisher_from_config(
        {"publisher": "webdav", "public_url": "https://fixtures.example.com/x.ics"}, resolver=resolver,
    )
    assert publisher is not None and kind == "webdav" and url == "https://fixtures.example.com/x.ics"


# ── Priority 5: `local` is rejected in published mode ──────────────────────


def test_build_publisher_from_config_rejects_local_adapter_in_published_mode():
    publisher, kind, url = sp.build_publisher_from_config(
        {"publisher": "local", "local_path": "/tmp/reg-sub-calendar.ics"}, mode=sp.MODE_PUBLISHED,
    )
    assert publisher is None and kind is None and url is None


def test_build_publisher_from_config_allows_local_adapter_outside_published_mode():
    publisher, kind, url = sp.build_publisher_from_config(
        {"publisher": "local", "local_path": "/tmp/reg-sub-calendar.ics"}, mode=sp.MODE_OFFLINE_ONLY,
    )
    assert publisher is not None and kind == "local"


# ── #16: published ICS digest matches evidence ──────────────────────────────


def _timed_title(run_id: str) -> str:
    titles = sp.scenario_variables(sp.resolve_target_date(None), run_token=sp.build_run_token(run_id))
    return titles["REG_SUB_TIMED_TITLE"]


def test_published_mode_records_content_digest_matching_the_actual_ics():
    published_bytes = {}
    run_id = "release-20260720-101010-digest1"

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True, detail="ok")

    # Priority 5: the poll_check must return the EXACT published bytes for
    # the read-back to verify (byte SHA-256 + both titles + target date) --
    # a bare truthy value can no longer pass.
    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: published_bytes["ics"].encode("utf-8"),
        poll_interval_seconds=0.01, poll_timeout_seconds=1,
        # Priority 6: a second, separate ingestion phase must ALSO succeed.
        ingestion_check=lambda: {"found": True, "id": "evt_1", "title": _timed_title(run_id), "calendarId": "cal_1"},
        ingestion_interval_seconds=0.01, ingestion_timeout_seconds=1, ingestion_api_label="fake-api",
        clock=fake.clock, sleep=fake.sleep,
    )
    assert result.ok, result.detail
    expected_digest = hashlib.sha256(published_bytes["ics"].encode("utf-8")).hexdigest()
    assert result.content_sha256 == expected_digest
    assert result.to_dict()["contentSha256"] == expected_digest
    assert result.public_read_verification_status == sp.STATUS_OK
    assert result.public_read_observed_sha256 == expected_digest
    assert result.public_read_verified_at is not None
    assert result.ingestion_status == sp.STATUS_OK
    assert result.ingestion_observed_event == {"id": "evt_1", "title": _timed_title(run_id), "calendarId": "cal_1"}
    assert result.ingestion_timestamp is not None
    # The ICS body itself is never in the JSON evidence (provisioning input, not a result).
    assert "ics" not in result.to_dict()


# ── #17/#18: bounded polling success / timeout blocks ───────────────────────


def test_published_mode_polls_until_visible_then_succeeds():
    attempts = {"n": 0}
    published_bytes = {}
    run_id = "release-20260720-101010-poll1"

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True)

    def _poll_check():
        attempts["n"] += 1
        if attempts["n"] < 3:
            return b"stale-unrelated-content"  # not yet visible
        return published_bytes["ics"].encode("utf-8")  # visible on the 3rd attempt

    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=_poll_check, poll_interval_seconds=10, poll_timeout_seconds=300,
        ingestion_check=lambda: {"found": True, "id": "evt_1", "title": _timed_title(run_id)},
        ingestion_interval_seconds=10, ingestion_timeout_seconds=300,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert result.ok, result.detail
    assert result.publication_status == sp.STATUS_OK
    assert result.observation_status == sp.STATUS_OK
    assert result.public_read_verification_status == sp.STATUS_OK
    assert result.poll_attempts == 3
    assert result.public_read_attempts == 3
    assert result.ingestion_status == sp.STATUS_OK
    assert result.run_relative_publication_used is True


# ── Priority 6: Calee ingestion verification (separate from public-URL) ────


def test_published_mode_blocks_when_no_ingestion_api_is_configured():
    # Public URL verifies fine, but no ingestion_check is available -- must
    # BLOCK with a precise reason, never silently pass from public-URL
    # readability alone.
    published_bytes = {}
    run_id = "release-20260720-101010-noingest1"

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True)

    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: published_bytes["ics"].encode("utf-8"),
        poll_interval_seconds=0.01, poll_timeout_seconds=1,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert not result.ok
    assert result.public_read_verification_status == sp.STATUS_OK  # phase 1 genuinely passed
    assert result.ingestion_status == sp.STATUS_BLOCKED
    assert "no existing authenticated regression API operation" in result.detail[0]
    assert result.run_relative_publication_used is False


def test_published_mode_ingestion_delayed_then_succeeds():
    # Fake-client test: the event isn't ingested/visible for the first two
    # attempts, then appears -- proves the SEPARATE ingestion poll is
    # genuinely bounded-and-retried, not a single check.
    published_bytes = {}
    run_id = "release-20260720-101010-ingestdelay1"
    attempts = {"n": 0}

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True)

    def _ingestion_check():
        attempts["n"] += 1
        if attempts["n"] < 3:
            return {"found": False, "title": _timed_title(run_id)}
        return {"found": True, "id": "evt_delayed", "title": _timed_title(run_id), "calendarId": "cal_1"}

    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: published_bytes["ics"].encode("utf-8"),
        poll_interval_seconds=0.01, poll_timeout_seconds=1,
        ingestion_check=_ingestion_check, ingestion_interval_seconds=10, ingestion_timeout_seconds=300,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert result.ok, result.detail
    assert result.ingestion_status == sp.STATUS_OK
    assert result.ingestion_attempts == 3
    assert result.ingestion_observed_event["id"] == "evt_delayed"


def test_published_mode_ingestion_timeout_blocks():
    # The ingestion API never reports the event as found within the bound.
    published_bytes = {}
    run_id = "release-20260720-101010-ingesttimeout1"

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True)

    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: published_bytes["ics"].encode("utf-8"),
        poll_interval_seconds=0.01, poll_timeout_seconds=1,
        ingestion_check=lambda: {"found": False, "title": _timed_title(run_id)},
        ingestion_interval_seconds=10, ingestion_timeout_seconds=30,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert not result.ok
    assert result.public_read_verification_status == sp.STATUS_OK
    assert result.ingestion_status == sp.STATUS_BLOCKED
    assert result.run_relative_publication_used is False
    assert "ingestion was not observed" in result.detail[0]


def test_published_mode_ingestion_wrong_title_never_matches():
    # An unrelated event exists (e.g. a different test's leftover) but the
    # run-specific title never appears -- must never false-match.
    published_bytes = {}
    run_id = "release-20260720-101010-wrongtitle1"

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True)

    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: published_bytes["ics"].encode("utf-8"),
        poll_interval_seconds=0.01, poll_timeout_seconds=1,
        ingestion_check=lambda: {"found": True, "id": "evt_other", "title": "REG-SUB-TIMED-SOMEOTHERRUN", "calendarId": "cal_1"},
        ingestion_interval_seconds=10, ingestion_timeout_seconds=30,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert not result.ok
    assert result.ingestion_status == sp.STATUS_BLOCKED
    assert result.run_relative_publication_used is False


def test_published_mode_ingestion_stale_event_wrong_calendar_never_matches():
    # A same-titled event exists but in the WRONG calendar (e.g. a stale
    # leftover from a manual test) -- when the caller pins the expected
    # fixture calendar id, this must not be accepted as this run's evidence.
    published_bytes = {}
    run_id = "release-20260720-101010-stale1"

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True)

    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: published_bytes["ics"].encode("utf-8"),
        poll_interval_seconds=0.01, poll_timeout_seconds=1,
        ingestion_check=lambda: {
            "found": True, "id": "evt_stale", "title": _timed_title(run_id), "calendarId": "some-other-calendar",
        },
        ingestion_expected_calendar_id="regression:regsub",
        ingestion_interval_seconds=10, ingestion_timeout_seconds=30,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert not result.ok
    assert result.ingestion_status == sp.STATUS_BLOCKED
    assert result.run_relative_publication_used is False


def test_published_mode_polling_timeout_blocks_never_asserts_run_relative_scenario():
    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-poll2", mode=sp.MODE_PUBLISHED,
        publisher=lambda ics: sp.PublishResult(ok=True), publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: False,  # never becomes visible
        poll_interval_seconds=10, poll_timeout_seconds=30,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert not result.ok
    assert result.publication_status == sp.STATUS_OK
    assert result.observation_status == "timeout"
    assert result.public_read_verification_status.startswith("blocked")
    assert result.run_relative_publication_used is False


# ── Priority 5: a stale-but-nonempty ICS at the public URL must not pass ───


def test_published_mode_stale_nonempty_ics_from_a_prior_run_is_rejected():
    # A genuinely well-formed, nonempty ICS is sitting at the public URL --
    # just not THIS run's. The old "any nonempty response passes" contract
    # would have accepted this; byte-SHA verification must not.
    stale_ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:stale@calee\r\nSUMMARY:REG-SUB-TIMED-OLDRUN123\r\n"
        "DTSTART:20260101T090000Z\r\nDTEND:20260101T093000Z\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode("utf-8")
    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-stale1", mode=sp.MODE_PUBLISHED,
        publisher=lambda ics: sp.PublishResult(ok=True), publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: stale_ics,
        poll_interval_seconds=1, poll_timeout_seconds=2,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert not result.ok
    assert result.public_read_verification_status == "blocked-mismatch"
    assert result.public_read_observed_sha256 != result.content_sha256
    assert "SHA-256 does not match" in result.detail[0]
    assert result.run_relative_publication_used is False


def test_published_mode_rejects_when_titles_present_but_wrong_date():
    # A response carrying the correct run-specific SUMMARY text (so a naive
    # substring-only title check would wrongly accept it) but dated on a
    # completely different day -- the target-date check must still block it,
    # in addition to the (also-failing) byte SHA-256 check.
    fake = _FakeClock()
    run_id = "release-20260720-101010-wrongdate1"

    def _poll_check():
        titles = sp.scenario_variables(sp.resolve_target_date(None), run_token=sp.build_run_token(run_id))
        wrong_dated_ics = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            f"BEGIN:VEVENT\r\nUID:t@calee\r\nSUMMARY:{titles['REG_SUB_TIMED_TITLE']}\r\n"
            "DTSTART:20200101T090000Z\r\nDTEND:20200101T093000Z\r\nEND:VEVENT\r\n"
            f"BEGIN:VEVENT\r\nUID:a@calee\r\nSUMMARY:{titles['REG_SUB_ALLDAY_TITLE']}\r\n"
            "DTSTART;VALUE=DATE:20200101\r\nDTEND;VALUE=DATE:20200102\r\nEND:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )
        return wrong_dated_ics.encode("utf-8")

    result = sp.prepare_subscribed_fixture(
        run_id=run_id, mode=sp.MODE_PUBLISHED,
        publisher=lambda ics: sp.PublishResult(ok=True), publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=_poll_check, poll_interval_seconds=1, poll_timeout_seconds=2,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert not result.ok
    assert result.public_read_verification_status == "blocked-mismatch"
    joined = "; ".join(result.detail)
    assert "target date is not present" in joined
    # Sanity: the titles WERE textually present -- proving this specifically
    # exercises the date check, not a title miss.
    assert "timed-event title is not present" not in joined
    assert "all-day-event title is not present" not in joined


def test_published_mode_publish_failure_never_polls():
    poll_calls = []
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-pubfail", mode=sp.MODE_PUBLISHED,
        publisher=lambda ics: sp.PublishResult(ok=False, detail="403 Forbidden"),
        publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: poll_calls.append(1) or True,
    )
    assert not result.ok
    assert result.publication_status == sp.STATUS_BLOCKED
    assert result.observation_status == sp.NOT_ATTEMPTED
    assert poll_calls == []  # never polled after a failed publish


def test_published_mode_with_no_publisher_configured_blocks_honestly():
    result = sp.prepare_subscribed_fixture(run_id="release-20260720-101010-nopub", mode=sp.MODE_PUBLISHED)
    assert not result.ok
    assert result.publication_status == sp.STATUS_BLOCKED
    assert "no publisher is configured" in result.detail[0]


# ── #19: fixed-date mode never asserts Today ────────────────────────────────


def test_fixed_date_mode_uses_static_date_and_records_run_relative_not_used():
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-fixed1", mode=sp.MODE_FIXED_DATE,
        fixed_date="2026-08-05", fixed_date_titles={"REG_SUB_TIMED_TITLE": "REG-SUB-TIMED-STATIC"},
    )
    assert result.ok
    assert result.resolved_date == "2026-08-05"
    assert result.generated_titles == {"REG_SUB_TIMED_TITLE": "REG-SUB-TIMED-STATIC"}
    assert result.run_relative_publication_used is False
    assert result.publication_status == sp.NOT_ATTEMPTED
    assert any("does not assert Today" in d for d in result.detail)
    assert any("NOT used" in d for d in result.detail)


# ── #20: offline-only mode never claims publication ─────────────────────────


def test_offline_only_mode_generates_and_validates_but_claims_nothing():
    result = sp.prepare_subscribed_fixture(run_id="release-20260720-101010-offline1", mode=sp.MODE_OFFLINE_ONLY)
    assert result.ok
    assert result.publication_status == sp.NOT_ATTEMPTED
    assert result.observation_status == sp.NOT_ATTEMPTED
    assert result.run_relative_publication_used is False
    assert result.public_url is None and result.publisher_type is None
    assert any("no publication was attempted or claimed" in d for d in result.detail)
    # The generated ICS is genuinely date-correct (round-trips through the
    # existing offline ICS contract validator).
    occurrences = ics_contract.expand(result.ics)
    assert occurrences, "offline-only mode's ICS did not validate against ics_contract"
    import datetime as _dt
    target = _dt.date.fromisoformat(result.resolved_date)
    visible_dates = {(o.start if o.all_day else o.start.date()) for o in occurrences}
    assert target in visible_dates


def test_unknown_mode_is_rejected():
    with pytest.raises(sp.PublisherError):
        sp.prepare_subscribed_fixture(run_id="r1", mode="silently-fallback")


def test_offline_only_never_silently_becomes_published_or_fixed_date():
    # Explicit-mode contract: offline-only stays offline-only even when a
    # publisher/poll_check happen to be supplied (defensive -- a caller bug
    # must never silently upgrade the mode).
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-noescalate", mode=sp.MODE_OFFLINE_ONLY,
        publisher=lambda ics: sp.PublishResult(ok=True), poll_check=lambda: True,
    )
    assert result.mode == sp.MODE_OFFLINE_ONLY
    assert result.publication_status == sp.NOT_ATTEMPTED


# ── Priority 7: release identity + generatedAt travel in every mode's result ─


def test_offline_only_records_release_id_and_generated_at():
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-relid1", mode=sp.MODE_OFFLINE_ONLY,
        release_id="2026.07.20-rc1", now="2026-07-20T10:00:00+00:00",
    )
    assert result.release_id == "2026.07.20-rc1"
    assert result.generated_at == "2026-07-20T10:00:00+00:00"
    assert result.to_dict()["releaseId"] == "2026.07.20-rc1"
    assert result.to_dict()["generatedAt"] == "2026-07-20T10:00:00+00:00"


def test_fixed_date_records_release_id_and_generated_at():
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-relid2", mode=sp.MODE_FIXED_DATE,
        release_id="2026.07.20-rc1", now="2026-07-20T10:00:00+00:00",
        fixed_date="2026-08-05", fixed_date_titles={"REG_SUB_TIMED_TITLE": "REG-SUB-TIMED-STATIC"},
    )
    assert result.release_id == "2026.07.20-rc1"
    assert result.generated_at == "2026-07-20T10:00:00+00:00"


def test_published_mode_records_release_id_and_generated_at():
    fake = _FakeClock()
    fixed_ics = {}

    def poll_check():
        return fixed_ics.get("bytes")

    def publisher(ics):
        fixed_ics["bytes"] = ics.encode("utf-8")
        return sp.PublishResult(ok=True)

    titled_ics = _timed_title("release-20260720-101010-relid3")
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-relid3", mode=sp.MODE_PUBLISHED,
        release_id="2026.07.20-rc1", now="2026-07-20T10:00:00+00:00",
        publisher=publisher, poll_check=poll_check,
        ingestion_check=lambda: {"found": True, "title": titled_ics, "calendarId": None},
        clock=fake.clock, sleep=fake.sleep,
    )
    assert result.release_id == "2026.07.20-rc1"
    assert result.generated_at == "2026-07-20T10:00:00+00:00"
