"""Shared build-identity *format* validators (Workstreams 1 & 2).

A release's build identity has two independent failure modes:

  * the *wrong* identity was tested (expected != detected) -- enforced at
    consolidation by ``component_from_build_identity`` /
    ``component_from_release_intent``; and
  * a *malformed* identity was configured in the first place -- an
    abbreviated Git SHA (``abc1234`` names more than one commit) or a
    version string that isn't a recognisable version at all (``""``,
    ``"latest"``, ``"0.3"``). A malformed expectation can never be safely
    matched against a detected value, so it must be rejected up front
    rather than silently "not matching".

These predicates are the single source of truth for the second failure
mode. They are pure and unit-tested (``framework_tests/test_identity_format.py``)
and deliberately live in their own tiny module so both the config loader
(``release_platforms.py``) and the consolidator
(``consolidated_report.py``) can share them without importing each other.
"""

from __future__ import annotations

import re
from typing import Any

# A full Git SHA-1 is exactly 40 hex characters. An abbreviated SHA
# (``abc1234``) is ambiguous -- it can name more than one commit -- so a
# release, which must prove *which* commit was tested, requires the full form.
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# Accepts the version shapes actually used across the Calee solution:
#   * CaleeMobile pubspec ``version:``  -> ``0.0.23+23`` (semver + build)
#   * Calee tablet ``versionName``      -> ``founder-v0.3.24`` (stage-prefixed)
#   * CaleeShell ``versionName``        -> ``founder-v0.2.11``
#   * a bare ``0.3.24`` (some configs record the numeric part only)
# and rejects anything that isn't a recognisable version -- ``""``,
# ``"latest"``, ``"0.3"`` (too few components), ``"v1.2.3"`` (no stage word
# before ``-v``), ``"1.2.3.4"`` (too many components), ``"0.0.22+"`` (empty
# build), etc.
_VERSION_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9]*-v)?\d+\.\d+\.\d+(?:\+\d+)?$")


def is_full_git_sha(value: "Any | None") -> bool:
    """True only for a full 40-character hexadecimal Git SHA.

    False for None, the empty string, an abbreviated SHA (``abc1234``), or a
    40-character string that isn't hex (``"g" * 40``).
    """
    return bool(value) and bool(_FULL_GIT_SHA_RE.match(str(value).strip()))


def is_wellformed_version(value: "Any | None") -> bool:
    """True for a recognisable Calee-solution version string.

    Accepts ``0.0.23+23`` (CaleeMobile pubspec), ``founder-v0.3.24`` /
    ``founder-v0.2.11`` (Calee tablet / CaleeShell versionName), and a bare
    ``0.3.24``. Rejects None, empty/whitespace, ``latest``, ``0.3`` (too few
    components), ``v1.2.3`` (no stage word), ``1.2.3.4`` (too many
    components), and ``0.0.22+`` (empty build number).
    """
    return bool(value) and bool(_VERSION_RE.match(str(value).strip()))
