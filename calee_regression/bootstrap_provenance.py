"""Interpreter / bootstrap provenance (Workstream 1).

A small, secret-free description of the Python interpreter and virtualenv a run
executed under, so a report consumer can tell EXACTLY which interpreter
produced it -- part of proving the hermetic-bootstrap contract end to end. It
NEVER contains a credential or an environment dump; only interpreter/venv
identity and the bootstrap-contract version exported by
``scripts/ensure_environment.sh``.
"""

from __future__ import annotations

import os
import sys

BOOTSTRAP_VERSION_ENV = "CALEE_BOOTSTRAP_VERSION"


def _virtualenv() -> "str | None":
    """The active virtualenv prefix, or ``None`` if not in one.

    Prefers the interpreter's OWN recorded prefixes (``sys.prefix`` differs from
    ``sys.base_prefix`` inside a venv) over ``$VIRTUAL_ENV``, which can be stale
    or point at a foreign environment the hermetic interpreter deliberately
    ignores.
    """
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return sys.prefix
    env = os.environ.get("VIRTUAL_ENV")
    return env or None


def interpreter_provenance() -> dict:
    """Return a secret-free record of the current interpreter/venv/bootstrap.

    Keys are stable and safe to embed in any report:
    ``pythonExecutable``, ``pythonVersion``, ``virtualEnvironment``,
    ``inVirtualEnvironment`` and ``bootstrapVersion``.
    """
    venv = _virtualenv()
    return {
        "pythonExecutable": sys.executable or "",
        "pythonVersion": ".".join(str(x) for x in sys.version_info[:3]),
        "virtualEnvironment": venv,
        "inVirtualEnvironment": venv is not None,
        "bootstrapVersion": os.environ.get(BOOTSTRAP_VERSION_ENV) or None,
    }
