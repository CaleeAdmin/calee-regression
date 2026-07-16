#!/usr/bin/env bash
# Sourced by every tester-facing .command launcher. Makes sure the Python
# virtual environment and dependencies exist, and that a tester config is
# present -- without ever asking a non-technical tester to run a venv/pip
# command themselves. Safe to source more than once (each step is a no-op
# once already done). On failure, prints a plain-language message and a log
# path, then returns/exits non-zero; it never prints a raw command for the
# tester to type.
set -uo pipefail

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT" || exit 1

mkdir -p "$REPO_ROOT/reports"
BOOTSTRAP_LOG="$REPO_ROOT/reports/setup.log"

_ensure_environment_fail() {
    echo ""
    echo "$1"
    echo "If this keeps happening, send $BOOTSTRAP_LOG to your technical owner."
    return 1 2>/dev/null || exit 1
}

if [ ! -f .venv/bin/activate ]; then
    echo "Setting up the test environment (first run only, this can take a minute)..."
    PYTHON_BIN=""
    for candidate in python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
    if [ -z "$PYTHON_BIN" ]; then
        _ensure_environment_fail "No Python 3 installation was found on this Mac." || return 1
    fi
    if ! "$PYTHON_BIN" -m venv .venv >"$BOOTSTRAP_LOG" 2>&1; then
        _ensure_environment_fail "Could not set up the test environment automatically." || return 1
    fi
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Check a genuine third-party dependency (click), not calee_regression
# itself: running from the repo root, "import calee_regression" would
# succeed even in a brand-new venv where nothing has been pip-installed,
# because Python implicitly puts the current directory on sys.path.
if ! python -c "import click" >/dev/null 2>&1; then
    echo "Installing test dependencies (first run only, this can take a minute)..."
    if ! pip install -e ".[dev]" >"$BOOTSTRAP_LOG" 2>&1; then
        _ensure_environment_fail "Could not install test dependencies automatically." || return 1
    fi
fi

export CALEE_TEST_CONFIG="${CALEE_TEST_CONFIG:-config/tester.local.yaml}"

if [ ! -f "$CALEE_TEST_CONFIG" ]; then
    echo ""
    echo "This Mac hasn't been set up for testing yet."
    echo "Ask your technical owner to complete the one-time setup (see docs/SETUP_MAC.md)."
    return 1 2>/dev/null || exit 1
fi

return 0 2>/dev/null || exit 0
