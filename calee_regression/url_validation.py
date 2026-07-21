"""Structured backend/publication URL validation (Priority 7).

Replaces a bare ``url.startswith("https://")`` test -- which accepts any
amount of deceptive structure after the scheme (embedded userinfo, a
fragment, whitespace/control characters, a malformed port, ...) as long as
the string happens to start with the right seven characters -- with a real
parse via :mod:`urllib.parse` and an explicit checklist of what a trusted
backend/publication URL may and may not contain.

Deliberately its own tiny module (mirrors ``identity_format.py``'s framing)
so every caller that needs to validate a backend/publication URL --
``release_installer.py``'s schema-v2 manifest ``backend`` field,
``machine_config.py``'s ``backend_url``, ``subscribed_publisher.py``'s
``public_url`` -- shares ONE validator, rather than each re-implementing (or
skipping) its own ad-hoc check.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Any of these appearing literally in the URL string is rejected outright,
# before any parsing is attempted -- urlsplit tolerates/strips some of these
# silently, which is exactly the kind of "looks fine, isn't" gap a deceptive
# URL can exploit.
_CONTROL_OR_WHITESPACE = frozenset(chr(c) for c in range(0x00, 0x21)) | {chr(0x7F)}


def validate_backend_url(url: "str | None") -> "list[str]":
    """Validate a backend/publication URL. Returns a list of problems (empty
    == accepted). Never raises.

    Requires:
      * a non-empty string with no leading/trailing whitespace and no
        whitespace/control character anywhere in it;
      * scheme exactly ``https`` (case-insensitive, e.g. ``HTTPS://`` is
        accepted; ``http://`` is not);
      * a non-empty host;
      * no embedded username/password (``https://user:pass@host/`` is
        rejected -- a classic deceptive-URL vector, and never a legitimate
        shape for a backend/publication URL);
      * no fragment (``#...``);
      * a syntactically valid port when one is given (1-65535).
    """
    problems: "list[str]" = []
    if url is None or not isinstance(url, str):
        return ["URL must be a non-empty string."]
    if not url.strip():
        return ["URL must not be empty."]
    if url != url.strip():
        problems.append("URL must not have leading/trailing whitespace.")
    if any(ch in _CONTROL_OR_WHITESPACE for ch in url):
        problems.append("URL must not contain whitespace or control characters.")
    if problems:
        # Malformed at the character level -- parsing further would only
        # produce confusing secondary errors.
        return problems

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        return [f"URL could not be parsed: {exc}"]

    if (parts.scheme or "").lower() != "https":
        problems.append(f"URL scheme must be exactly 'https' (got {parts.scheme!r}).")

    try:
        host = parts.hostname
    except ValueError as exc:
        problems.append(f"URL host could not be parsed: {exc}")
        host = None
    if not host:
        problems.append("URL must have a non-empty host.")

    if parts.username is not None or parts.password is not None:
        problems.append("URL must not contain a username/password (userinfo).")

    if parts.fragment:
        problems.append("URL must not contain a fragment ('#...').")

    try:
        port = parts.port
    except ValueError:
        problems.append("URL port is not a valid integer.")
    else:
        if port is not None and not (1 <= port <= 65535):
            problems.append(f"URL port {port} is out of range (must be 1-65535).")

    return problems


def is_valid_backend_url(url: "str | None") -> bool:
    return not validate_backend_url(url)


def normalize_backend_url(url: str) -> str:
    """Canonical form: no trailing slash (``https://host/`` ->
    ``https://host``), so two otherwise-identical URLs that differ only in a
    trailing slash always compare equal. Does not itself validate ``url`` --
    call :func:`validate_backend_url` first."""
    text = url.strip()
    if text.endswith("/") and len(text) > len(text.rstrip("/")):
        stripped = text.rstrip("/")
        # Never strip down to a bare scheme (e.g. "https://" -> "https:") --
        # a backend URL always has at least a host after the scheme, so this
        # only triggers on a URL malformed enough to already have failed
        # validation; kept as a safety floor, not a normal code path.
        return stripped if "://" in stripped else text
    return text
