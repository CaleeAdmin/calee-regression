"""Offline tests for the credential provider abstraction and log redaction
(Phase 4). No real environment, Keychain, or subprocess is required: the env
map and the Keychain subprocess runner are both injected.
"""

from __future__ import annotations

import pytest

from calee_regression import credentials as cred
from calee_regression.credentials import (
    API_TOKEN,
    REGRESSION_PASSWORD,
    REGRESSION_USERNAME,
    CredentialError,
    CredentialResolver,
    EnvironmentProvider,
    InjectedProvider,
    KeychainProvider,
    build_env,
    default_resolver,
    redact,
)


def test_environment_provider_resolves_and_empty_is_absent():
    provider = EnvironmentProvider({"CALEE_TEST_PASSWORD": "s3cret"})
    assert provider.get(REGRESSION_PASSWORD) == "s3cret"
    assert EnvironmentProvider({"CALEE_TEST_PASSWORD": ""}).get(REGRESSION_PASSWORD) is None
    assert EnvironmentProvider({}).get(REGRESSION_PASSWORD) is None


def test_injected_provider_resolves_by_logical_name():
    provider = InjectedProvider({"regression_password": "inj"})
    assert provider.get(REGRESSION_PASSWORD) == "inj"
    assert provider.get(REGRESSION_USERNAME) is None


def test_keychain_provider_builds_correct_command_and_parses_output():
    seen = {}

    def _runner(argv):
        seen["argv"] = argv
        return 0, "keychain-secret\n"

    provider = KeychainProvider(runner=_runner)
    value = provider.get(REGRESSION_PASSWORD)
    assert value == "keychain-secret"
    # The password is RETRIEVED, never passed in -- argv contains only the
    # lookup keys, and the -w flag (print password to stdout).
    assert seen["argv"] == [
        "security", "find-generic-password",
        "-s", "calee-regression", "-a", "regression-password", "-w",
    ]
    assert "keychain-secret" not in " ".join(seen["argv"])


def test_keychain_provider_not_found_returns_none():
    provider = KeychainProvider(runner=lambda argv: (44, ""))  # security exit 44 = item not found
    assert provider.get(REGRESSION_PASSWORD) is None


def test_keychain_provider_missing_binary_returns_none():
    def _raise(argv):
        raise FileNotFoundError("security not found")

    assert KeychainProvider(runner=_raise).get(REGRESSION_PASSWORD) is None


def test_keychain_provider_skips_when_no_service_configured():
    from calee_regression.credentials import CredentialRequest

    req = CredentialRequest(name="x", env_var="X")  # no keychain service/account
    called = []
    KeychainProvider(runner=lambda argv: called.append(argv) or (0, "v")).get(req)
    assert called == []  # never shelled out


def test_chain_order_injected_beats_env_beats_keychain():
    resolver = default_resolver(
        injected={"regression_password": "from-injected"},
        environ={"CALEE_TEST_PASSWORD": "from-env"},
        keychain_runner=lambda argv: (0, "from-keychain"),
    )
    assert resolver.get(REGRESSION_PASSWORD) == "from-injected"

    resolver2 = default_resolver(
        environ={"CALEE_TEST_PASSWORD": "from-env"},
        keychain_runner=lambda argv: (0, "from-keychain"),
    )
    assert resolver2.get(REGRESSION_PASSWORD) == "from-env"

    resolver3 = default_resolver(environ={}, keychain_runner=lambda argv: (0, "from-keychain"))
    assert resolver3.get(REGRESSION_PASSWORD) == "from-keychain"


def test_require_missing_required_credential_raises_blocked():
    resolver = default_resolver(environ={}, keychain_runner=lambda argv: (1, ""))
    with pytest.raises(CredentialError) as exc:
        resolver.require(REGRESSION_PASSWORD)
    assert "BLOCKS" in str(exc.value)
    # The error names where to set it, but not any secret value.
    assert "CALEE_TEST_PASSWORD" in str(exc.value)


def test_optional_missing_credential_is_none_not_error():
    resolver = default_resolver(environ={}, keychain_runner=lambda argv: (1, ""))
    assert resolver.get(API_TOKEN) is None
    resolved = resolver.resolve_all([API_TOKEN])
    assert resolved == {}  # optional + absent -> simply omitted


def test_resolve_all_returns_required_and_present_optional():
    resolver = default_resolver(
        environ={"CALEE_TEST_EMAIL": "u@x", "CALEE_TEST_PASSWORD": "p", "CALEE_API_TOKEN": "tok"},
        keychain_runner=lambda argv: (1, ""),
    )
    resolved = resolver.resolve_all([REGRESSION_USERNAME, REGRESSION_PASSWORD, API_TOKEN])
    assert resolved == {"regression_username": "u@x", "regression_password": "p", "api_token": "tok"}


def test_redact_replaces_all_occurrences_and_ignores_empty():
    resolver = default_resolver(environ={"CALEE_TEST_PASSWORD": "hunter2"})
    resolver.require(REGRESSION_PASSWORD)
    log = "logging in with hunter2 ... retry with hunter2"
    scrubbed = redact(log, resolver.secret_values())
    assert "hunter2" not in scrubbed
    assert scrubbed.count("***REDACTED***") == 2


def test_redact_empty_secret_does_not_blank_everything():
    assert redact("some text", {""}) == "some text"
    assert redact("some text", set()) == "some text"


def test_redact_longer_secret_first():
    # A secret that contains a shorter secret must be fully scrubbed.
    scrubbed = redact("value=abcdef", {"abc", "abcdef"})
    assert "abcdef" not in scrubbed and "abc" not in scrubbed


def test_resolver_repr_never_leaks_secret():
    resolver = default_resolver(environ={"CALEE_TEST_PASSWORD": "topsecret"})
    resolver.require(REGRESSION_PASSWORD)
    assert "topsecret" not in repr(resolver)
    assert "topsecret" not in str(resolver)


def test_build_env_places_secret_in_env_not_argv():
    resolver = default_resolver(environ={"CALEE_TEST_PASSWORD": "p", "CALEE_TEST_EMAIL": "u@x"})
    resolved = resolver.resolve_all([REGRESSION_USERNAME, REGRESSION_PASSWORD])
    env = build_env({}, resolved, {"regression_password": "CALEE_TEST_PASSWORD", "regression_username": "CALEE_TEST_EMAIL"})
    assert env["CALEE_TEST_PASSWORD"] == "p"
    assert env["CALEE_TEST_EMAIL"] == "u@x"


def test_credential_resolver_requires_a_provider():
    with pytest.raises(ValueError):
        CredentialResolver([])
