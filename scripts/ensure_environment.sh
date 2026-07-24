#!/usr/bin/env bash
# Sourced by every tester-facing .command launcher. Guarantees a working,
# repository-owned Python virtualenv and a tester config, then exports the
# hermetic interpreter (CALEE_PYTHON) every launcher uses -- without ever
# asking a non-technical tester to run venv/pip commands themselves. Safe to
# source more than once (each step is a no-op once already done). On failure it
# prints a plain-language message and a log path, then returns/exits non-zero;
# it never prints a raw command for the tester to type, and it never echoes a
# credential (only interpreter/venv paths are logged).
set -uo pipefail

# Bootstrap-contract version. Recorded in interpreter provenance so a report
# consumer can tell which bootstrap produced a run. Bump when the contract
# below changes in a way consumers care about.
CALEE_BOOTSTRAP_VERSION="2"
export CALEE_BOOTSTRAP_VERSION

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT" || exit 1

mkdir -p "$REPO_ROOT/reports"
BOOTSTRAP_LOG="$REPO_ROOT/reports/setup.log"

VENV_DIR="$REPO_ROOT/.venv"

# shellcheck source=scripts/lib/hermetic_python.sh
. "$REPO_ROOT/scripts/lib/hermetic_python.sh"

_ensure_environment_fail() {
    echo ""
    echo "$1"
    echo "If this keeps happening, send $BOOTSTRAP_LOG to your technical owner."
    return 1 2>/dev/null || exit 1
}

# _calee_has_deps PATH -> 0 iff that interpreter can import a real third-party
# dependency (click). "import calee_regression" is deliberately NOT used: from
# the repo root it would succeed even in an empty venv, because the current
# directory is implicitly on sys.path.
_calee_has_deps() {
    [ -n "${1:-}" ] && [ -x "$1" ] && "$1" -c "import click" >/dev/null 2>&1
}

# 1) Validated override: honour an already-pinned, working CALEE_PYTHON. This
#    is the explicit path advanced users and test harnesses use; it is never
#    accidental PATH leakage.
if _calee_has_deps "${CALEE_PYTHON:-}"; then
    CALEE_PIP="${CALEE_PIP:-$CALEE_PYTHON -m pip}"
    export CALEE_PYTHON CALEE_PIP
else
    # 2) Create the repo-owned venv once, keyed on the activate script (NOT on
    #    the interpreter): a pre-seeded/stub .venv/bin/activate means "already
    #    bootstrapped, do not create". Uses "<python> -m venv" -- never a bare
    #    tool from PATH.
    if [ ! -f "$VENV_DIR/bin/activate" ]; then
        echo "Setting up the test environment (first run only, this can take a minute)..."
        PYTHON_BIN=""
        for candidate in python3.11 python3 python; do
            if command -v "$candidate" >/dev/null 2>&1; then
                PYTHON_BIN="$candidate"
                break
            fi
        done
        if [ -z "$PYTHON_BIN" ]; then
            _ensure_environment_fail "No Python 3 installation was found on this computer." || return 1
        fi
        if ! "$PYTHON_BIN" -m venv "$VENV_DIR" >"$BOOTSTRAP_LOG" 2>&1; then
            _ensure_environment_fail "Could not set up the test environment automatically." || return 1
        fi
    fi

    # 3) Backward-compatible activation so an interactive shell inherits the
    #    venv. The CANONICAL interpreter every launcher uses is "$CALEE_PYTHON".
    if [ -f "$VENV_DIR/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$VENV_DIR/bin/activate"
    fi

    # 4) Resolve the interpreter, distinguishing a BROKEN repo venv from an
    #    ABSENT one:
    #      * <venv>/bin/python present but not runnable => a stale/broken/moved
    #        venv. Diagnose EXPLICITLY and stop -- never silently fall back to a
    #        system python (that silent fallback is the original footgun) and
    #        never blindly recreate it (that hides the real cause and can loop).
    #      * <venv>/bin/python absent (e.g. a pre-seeded stub venv, or an
    #        interpreter this bootstrap did not build) => resolve the best
    #        available interpreter via the shared resolver.
    VENV_PYTHON="$VENV_DIR/bin/python"
    if [ -e "$VENV_PYTHON" ] && ! _calee_python_runs "$VENV_PYTHON"; then
        _ensure_environment_fail \
"The test environment's Python at $VENV_PYTHON is present but will not run (a stale, moved or broken .venv).
Delete the .venv folder inside $REPO_ROOT, then run this again to rebuild it once." || return 1
    fi
    _calee_resolve_python "$REPO_ROOT"
    if [ -z "${CALEE_PYTHON:-}" ]; then
        _ensure_environment_fail "No usable Python interpreter could be resolved for the test environment." || return 1
    fi
    CALEE_PIP="$CALEE_PYTHON -m pip"

    # 5) Ensure dependencies -- installed ONLY into the resolved interpreter,
    #    always via "<python> -m pip", never a bare `pip` from PATH.
    if ! _calee_has_deps "$CALEE_PYTHON"; then
        echo "Installing test dependencies (first run only, this can take a minute)..."
        if ! "$CALEE_PYTHON" -m pip install -e ".[dev]" >"$BOOTSTRAP_LOG" 2>&1; then
            _ensure_environment_fail "Could not install test dependencies automatically." || return 1
        fi
    fi

    export CALEE_PYTHON CALEE_PIP
fi

export CALEE_TEST_CONFIG="${CALEE_TEST_CONFIG:-config/tester.local.yaml}"

if [ ! -f "$CALEE_TEST_CONFIG" ]; then
    echo ""
    echo "This computer hasn't been set up for testing yet."
    echo "Ask your technical owner to complete the one-time setup (see docs/SETUP_MAC.md)."
    return 1 2>/dev/null || exit 1
fi

return 0 2>/dev/null || exit 0
