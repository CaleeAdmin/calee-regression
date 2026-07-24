# shellcheck shell=bash
# Hermetic interpreter resolution (Workstream 1). This file is SOURCED, never
# executed, and defines helpers only -- it must not `set` shell options or run
# side effects, so a caller can source it safely more than once.
#
# It resolves CALEE_PYTHON: an ABSOLUTE path to the Python interpreter every
# tester launcher must use to run `-m calee_regression`. Resolving this once,
# from an absolute path, is what stops a stripped PATH (e.g. PATH=/usr/bin:/bin)
# or a foreign activated virtualenv from silently selecting a different
# interpreter that lacks this framework's dependencies -- the interpreter
# portability weakness a cloud session exposed (a launcher picked a system
# python without `click` even though the repo .venv had the right deps).
#
# Precedence, highest first:
#   1. An already-exported CALEE_PYTHON that actually runs -- a validated
#      override used by ensure_environment.sh, advanced users and test
#      harnesses (an explicit choice, never accidental PATH leakage).
#   2. The repository-owned virtualenv: <repo>/.venv/bin/python.
#   3. A system python3/python (last resort; a bootstrap step is expected to
#      have created the .venv before real work runs).
#
# macOS Bash 3.2 compatible: no associative arrays, no `mapfile`, no `local -n`.

# _calee_python_runs PATH -> returns 0 iff PATH is an executable interpreter
# that starts and can import the standard library.
_calee_python_runs() {
    [ -n "${1:-}" ] && [ -x "$1" ] && "$1" -c "import sys" >/dev/null 2>&1
}

# _calee_resolve_python REPO_ROOT
#   Sets and exports CALEE_PYTHON to the best available interpreter using the
#   precedence above. Best-effort: if nothing is found CALEE_PYTHON is empty
#   and the caller is expected to fail closed with a clear message.
_calee_resolve_python() {
    _calee_repo_root="${1:-.}"

    # 1) Honour a working, explicitly-pinned interpreter.
    if _calee_python_runs "${CALEE_PYTHON:-}"; then
        export CALEE_PYTHON
        return 0
    fi

    # 2) Prefer the repository-owned virtualenv interpreter.
    _calee_venv_python="$_calee_repo_root/.venv/bin/python"
    if _calee_python_runs "$_calee_venv_python"; then
        CALEE_PYTHON="$_calee_venv_python"
        export CALEE_PYTHON
        return 0
    fi

    # 3) Last resort: a system interpreter on PATH (prefer python3).
    if command -v python3 >/dev/null 2>&1; then
        CALEE_PYTHON="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        CALEE_PYTHON="$(command -v python)"
    else
        CALEE_PYTHON=""
    fi
    export CALEE_PYTHON
}
