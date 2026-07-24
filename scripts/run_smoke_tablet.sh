#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR"

# Hermetic interpreter (Workstream 1): run the framework through the
# repository-owned "$CALEE_PYTHON", never a bare python from PATH.
# shellcheck source=scripts/lib/hermetic_python.sh
. "$SCRIPT_DIR/scripts/lib/hermetic_python.sh"
_calee_resolve_python "$SCRIPT_DIR"

CONFIG="${CALEE_TEST_CONFIG:-config/tester.local.yaml}"

"$CALEE_PYTHON" -m calee_regression suite --config "$CONFIG" --suite smoke-tablet
