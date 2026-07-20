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


# ── #16: published ICS digest matches evidence ──────────────────────────────


def test_published_mode_records_content_digest_matching_the_actual_ics():
    published_bytes = {}

    def _publisher(ics):
        published_bytes["ics"] = ics
        return sp.PublishResult(ok=True, detail="ok")

    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-digest1", mode=sp.MODE_PUBLISHED,
        publisher=_publisher, publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=lambda: True, poll_interval_seconds=0.01, poll_timeout_seconds=1,
        clock=_FakeClock().clock, sleep=lambda s: None,
    )
    assert result.ok, result.detail
    expected_digest = hashlib.sha256(published_bytes["ics"].encode("utf-8")).hexdigest()
    assert result.content_sha256 == expected_digest
    assert result.to_dict()["contentSha256"] == expected_digest
    # The ICS body itself is never in the JSON evidence (provisioning input, not a result).
    assert "ics" not in result.to_dict()


# ── #17/#18: bounded polling success / timeout blocks ───────────────────────


def test_published_mode_polls_until_visible_then_succeeds():
    attempts = {"n": 0}

    def _poll_check():
        attempts["n"] += 1
        return attempts["n"] >= 3  # visible on the 3rd attempt

    fake = _FakeClock()
    result = sp.prepare_subscribed_fixture(
        run_id="release-20260720-101010-poll1", mode=sp.MODE_PUBLISHED,
        publisher=lambda ics: sp.PublishResult(ok=True), publisher_type="webdav", public_url="https://x/y.ics",
        poll_check=_poll_check, poll_interval_seconds=10, poll_timeout_seconds=300,
        clock=fake.clock, sleep=fake.sleep,
    )
    assert result.ok
    assert result.publication_status == sp.STATUS_OK
    assert result.observation_status == sp.STATUS_OK
    assert result.poll_attempts == 3
    assert result.run_relative_publication_used is True


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
    assert result.run_relative_publication_used is False


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
