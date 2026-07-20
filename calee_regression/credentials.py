"""Credential provider abstraction and log redaction (Phase 4).

The framework needs a small number of secrets (the regression account
username/password, optional API tokens, optional AI-analysis credentials).
This module resolves them from one of three sources, in a caller-chosen
order, and never lets them leak:

  * **Environment variables** -- the default for a technical owner's machine
    and for `--dart-define`-style injection.
  * **macOS Keychain** -- `security find-generic-password -w`, so a technical
    owner can store the regression password in the login keychain instead of
    an exported env var. The subprocess runner is injectable, so this is
    testable with no real Keychain.
  * **Injected values** -- for CI and unit tests, a plain in-memory map.

Hard rules this module enforces (and tests):

  * A required secret that cannot be resolved raises ``CredentialError``,
    which callers map to **BLOCKED** -- never a product failure, never a
    silent empty string. An *optional* secret simply resolves to ``None``.
  * Secrets are never placed on a command line. ``build_env`` returns an
    environment mapping to hand to a subprocess; ``redact`` scrubs secret
    values out of any log/report text before it is written or printed.
  * The resolver's ``repr`` never contains a secret value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable


class CredentialError(Exception):
    """A required credential could not be resolved. Callers must treat this as
    BLOCKED (an environment/config problem), never as a product FAIL."""


@dataclass(frozen=True)
class CredentialRequest:
    """Describes one secret and where to look for it.

    ``name`` is the logical key used by injected providers and by ``redact``;
    ``env_var`` is the environment variable to read; ``keychain_service``/
    ``keychain_account`` locate it in the macOS login keychain (both required
    for the Keychain provider to attempt a lookup)."""

    name: str
    env_var: str
    keychain_service: "str | None" = None
    keychain_account: "str | None" = None
    required: bool = True


# The canonical secrets the framework uses. Kept here so callers reference a
# single source of truth and redaction knows every logical secret name.
REGRESSION_USERNAME = CredentialRequest(
    name="regression_username", env_var="CALEE_TEST_EMAIL",
    keychain_service="calee-regression", keychain_account="regression-username",
)
REGRESSION_PASSWORD = CredentialRequest(
    name="regression_password", env_var="CALEE_TEST_PASSWORD",
    keychain_service="calee-regression", keychain_account="regression-password",
)
API_TOKEN = CredentialRequest(
    name="api_token", env_var="CALEE_API_TOKEN",
    keychain_service="calee-regression", keychain_account="api-token", required=False,
)
AI_ANALYSIS_KEY = CredentialRequest(
    name="ai_analysis_key", env_var="CALEE_AI_ANALYSIS_KEY",
    keychain_service="calee-regression", keychain_account="ai-analysis-key", required=False,
)

REQUIRED_SECRETS = (REGRESSION_USERNAME, REGRESSION_PASSWORD)
OPTIONAL_SECRETS = (API_TOKEN, AI_ANALYSIS_KEY)


class EnvironmentProvider:
    """Resolves a secret from an environment mapping (``os.environ`` by
    default; injectable for tests). An empty value is treated as absent."""

    def __init__(self, environ: "dict | None" = None):
        self._environ = environ if environ is not None else os.environ

    def get(self, request: CredentialRequest) -> "str | None":
        value = self._environ.get(request.env_var)
        return value if value else None


class KeychainProvider:
    """Resolves a secret from the macOS login keychain via
    ``security find-generic-password -s <service> -a <account> -w``.

    The subprocess runner is injected (defaulting to a real one) so this is
    fully testable without a Keychain. A non-macOS host, a missing ``security``
    binary, or a not-found item all resolve to ``None`` (fall through to the
    next provider), never an exception."""

    def __init__(self, runner: "Callable[[list[str]], tuple[int, str]] | None" = None):
        self._runner = runner or _default_security_runner

    def get(self, request: CredentialRequest) -> "str | None":
        if not (request.keychain_service and request.keychain_account):
            return None
        argv = [
            "security", "find-generic-password",
            "-s", request.keychain_service,
            "-a", request.keychain_account,
            "-w",
        ]
        try:
            code, out = self._runner(argv)
        except Exception:
            return None
        if code != 0:
            return None
        value = (out or "").strip()
        return value or None


def _default_security_runner(argv: "list[str]") -> "tuple[int, str]":
    import subprocess

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout or ""


class InjectedProvider:
    """Resolves a secret from an in-memory ``{logical_name: value}`` map -- for
    CI and unit tests, where no real env/Keychain should be consulted."""

    def __init__(self, values: "dict[str, str]"):
        self._values = dict(values)

    def get(self, request: CredentialRequest) -> "str | None":
        value = self._values.get(request.name)
        return value if value else None


class CredentialResolver:
    """Resolves credentials by trying an ordered chain of providers. The first
    provider that returns a value wins. Resolved secret values are cached so
    ``redact`` can scrub every value that was ever resolved, but the cache is
    never exposed via ``repr``/``str``."""

    def __init__(self, providers: "list"):
        if not providers:
            raise ValueError("CredentialResolver needs at least one provider.")
        self._providers = list(providers)
        self._resolved: "dict[str, str]" = {}

    def get(self, request: CredentialRequest) -> "str | None":
        for provider in self._providers:
            value = provider.get(request)
            if value:
                self._resolved[request.name] = value
                return value
        return None

    def require(self, request: CredentialRequest) -> str:
        """Resolve a required secret or raise CredentialError (BLOCKED)."""
        value = self.get(request)
        if value is None:
            raise CredentialError(
                f"Required credential {request.name!r} could not be resolved. Set the "
                f"{request.env_var} environment variable, or store it in the macOS keychain "
                f"(service {request.keychain_service!r}, account {request.keychain_account!r}). "
                f"This BLOCKS the run -- it is never treated as a product failure."
            )
        return value

    def resolve_all(self, requests: "list[CredentialRequest]") -> "dict[str, str]":
        """Resolve a batch: required ones raise on absence, optional ones are
        simply omitted from the result if absent."""
        out: "dict[str, str]" = {}
        for request in requests:
            if request.required:
                out[request.name] = self.require(request)
            else:
                value = self.get(request)
                if value is not None:
                    out[request.name] = value
        return out

    def secret_values(self) -> "set[str]":
        """Every secret value resolved so far -- feed this to ``redact`` before
        writing any log/report text."""
        return set(self._resolved.values())

    def __repr__(self) -> str:  # never leak secret values
        return f"CredentialResolver(providers={len(self._providers)}, resolved={len(self._resolved)})"

    __str__ = __repr__


def default_resolver(*, environ: "dict | None" = None, keychain_runner=None, injected: "dict | None" = None) -> CredentialResolver:
    """The standard chain: injected (CI/tests) -> environment -> Keychain.

    Injected values win first so CI never accidentally reads a developer's
    real environment/Keychain; environment beats Keychain for an interactive
    override."""
    providers: "list" = []
    if injected is not None:
        providers.append(InjectedProvider(injected))
    providers.append(EnvironmentProvider(environ))
    providers.append(KeychainProvider(keychain_runner))
    return CredentialResolver(providers)


_REDACTED = "***REDACTED***"


def redact(text: str, secrets) -> str:
    """Replace every occurrence of every secret value in ``text`` with a
    fixed marker. Longer secrets are replaced first so a secret that contains
    a shorter one is fully scrubbed. Empty/None secrets are ignored (they must
    never blank out the whole string)."""
    if not text:
        return text
    values = sorted((s for s in secrets if s), key=len, reverse=True)
    for value in values:
        text = text.replace(value, _REDACTED)
    return text


def build_env(base: "dict | None", resolved: "dict[str, str]", mapping: "dict[str, str]") -> "dict[str, str]":
    """Build an environment mapping for a subprocess, placing secrets in the
    environment (never on the command line). ``mapping`` maps a logical secret
    name to the env-var name the child expects (e.g.
    ``{"regression_password": "CALEE_TEST_PASSWORD"}``)."""
    env = dict(base if base is not None else os.environ)
    for logical_name, env_var in mapping.items():
        if logical_name in resolved:
            env[env_var] = resolved[logical_name]
    return env
