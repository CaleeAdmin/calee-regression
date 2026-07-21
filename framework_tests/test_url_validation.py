"""Priority 7 (this session) -- structured backend/publication URL
validation, replacing a bare ``startswith("https://")`` test."""

from __future__ import annotations

import pytest

from calee_regression import url_validation as uv


@pytest.mark.parametrize("url", [
    "https://hub.calee.com.au",
    "https://hub.calee.com.au/",
    "https://hub-dev.calee.com.au:8443/api",
    "https://hub.calee.com.au/api/v1",
    "HTTPS://hub.calee.com.au",  # scheme is case-insensitive per RFC 3986
    "https://192.168.1.1",
    "https://[::1]:8443/",
])
def test_valid_urls_accepted(url):
    assert uv.validate_backend_url(url) == [], url


@pytest.mark.parametrize("url,expected_fragment", [
    ("http://hub.calee.com.au", "scheme"),
    ("ftp://hub.calee.com.au", "scheme"),
    ("hub.calee.com.au", "scheme"),
    ("//hub.calee.com.au", "scheme"),
    ("HTTP://hub.calee.com.au", "scheme"),
])
def test_wrong_scheme_rejected(url, expected_fragment):
    problems = uv.validate_backend_url(url)
    assert problems
    assert any(expected_fragment in p for p in problems)


@pytest.mark.parametrize("url", [
    "https://",
    "https:///path",
    "https:///",
])
def test_empty_host_rejected(url):
    problems = uv.validate_backend_url(url)
    assert any("host" in p for p in problems)


@pytest.mark.parametrize("url", [
    "https://user:pass@hub.calee.com.au/",
    "https://user@hub.calee.com.au/",
    # Deceptive: looks like it points at "real.com" but the actual host is
    # "evil.com" -- everything before '@' is userinfo, not the host.
    "https://real.com@evil.com/",
    "https://admin:s3cr3t@internal-backend.example/",
])
def test_userinfo_rejected(url):
    problems = uv.validate_backend_url(url)
    assert any("username/password" in p for p in problems), (url, problems)


def test_fragment_rejected():
    problems = uv.validate_backend_url("https://hub.calee.com.au/path#section")
    assert any("fragment" in p for p in problems)


@pytest.mark.parametrize("url", [
    "https://hub.calee.com.au:99999/",
    "https://hub.calee.com.au:0/",
    "https://hub.calee.com.au:abc/",
    "https://hub.calee.com.au:-1/",
])
def test_invalid_port_rejected(url):
    problems = uv.validate_backend_url(url)
    assert problems, url


@pytest.mark.parametrize("url", [
    " https://hub.calee.com.au",
    "https://hub.calee.com.au ",
    " https://hub.calee.com.au ",
    "https://hub.calee.com.au/\t",
    "https://hub.calee.com.au/\n",
    "https://ho st.calee.com.au",
    "https://hub.calee.com.au/\x00",
    "https://hub.calee.com.au/\x07",
    "https://hub.calee.com.au/\x7f",
])
def test_whitespace_and_control_chars_rejected(url):
    problems = uv.validate_backend_url(url)
    assert problems, url


@pytest.mark.parametrize("url", [None, "", "   ", 123, ["https://x"]])
def test_empty_or_non_string_rejected(url):
    assert uv.validate_backend_url(url) != []


def test_is_valid_backend_url_matches_validate():
    assert uv.is_valid_backend_url("https://hub.calee.com.au") is True
    assert uv.is_valid_backend_url("http://hub.calee.com.au") is False


# ── normalisation ────────────────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("https://hub.calee.com.au/", "https://hub.calee.com.au"),
    ("https://hub.calee.com.au", "https://hub.calee.com.au"),
    ("https://hub.calee.com.au/api/", "https://hub.calee.com.au/api"),
    ("https://hub.calee.com.au/api", "https://hub.calee.com.au/api"),
])
def test_normalize_strips_exactly_one_trailing_slash(url, expected):
    assert uv.normalize_backend_url(url) == expected


def test_normalize_two_urls_differing_only_by_trailing_slash_compare_equal():
    a = uv.normalize_backend_url("https://hub.calee.com.au/api/")
    b = uv.normalize_backend_url("https://hub.calee.com.au/api")
    assert a == b
