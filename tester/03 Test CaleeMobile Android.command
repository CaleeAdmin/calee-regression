#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Test CaleeMobile (Android) ==="
echo "If you haven't run '01 Prepare Test Environment' for this run yet, this"
echo "will prepare and verify the regression fixture automatically first."
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

# The ONE canonical report root (Priority 3), resolved once here (this
# launcher's own bootstrap above guarantees the CLI is available) and
# exported so scripts/test_caleemobile.sh inherits it instead of resolving
# its own -- see calee_regression/report_root.py.
if [ -z "${CALEE_REPORT_ROOT:-}" ]; then
    if ! CALEE_REPORT_ROOT="$("$CALEE_PYTHON" -m calee_regression report-root)"; then
        echo "BLOCKED: the configured report root could not be resolved." >&2
        read -p "Press Enter to close..."
        exit 3
    fi
fi
export CALEE_REPORT_ROOT

# Priority 5: credentials come through the single secure boundary (environment
# OR macOS Keychain), placed only in the child environment -- so a Keychain-only
# machine needs no exported CALEE_TEST_EMAIL / CALEE_TEST_PASSWORD.
"$CALEE_PYTHON" -m calee_regression run-with-credentials -- bash scripts/test_caleemobile.sh android
STATUS=$?

read -p "Press Enter to close..."
exit $STATUS
