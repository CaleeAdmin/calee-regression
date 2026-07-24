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

echo "============================================================" >&2
echo " release-technical requires a REAL PHYSICAL TABLET." >&2
echo " It includes kiosk/admin and system-receiver scenarios that" >&2
echo " are not safe or meaningful on an emulator, and are not for" >&2
echo " non-technical testers. If you have not read" >&2
echo " docs/TROUBLESHOOTING.md and docs/CALEE_LAUNCH_MODEL.md," >&2
echo " stop now and read them first." >&2
echo "============================================================" >&2

"$CALEE_PYTHON" -m calee_regression suite --config "$CONFIG" --suite release-technical --confirm-technical
