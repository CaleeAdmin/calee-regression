#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Test Calee Tablet ==="
echo "Only run this on a PREPARED tablet/emulator with a logged-in demo account."
echo "(Use '01 Prepare Test Environment' first if you haven't already today.)"
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

python -m calee_regression suite --config "$CALEE_TEST_CONFIG" --suite full-tester
STATUS=$?

echo ""
case $STATUS in
    0) echo "PASS: Calee tablet" ;;
    1) echo "FAIL: Calee tablet — a real problem was found. Open the report ('07 Open Latest Report') for details." ;;
    3) echo "BLOCKED: Calee tablet — the test could not run (see messages above). This is NOT a product failure — check the device/tablet state and try again." ;;
    *) echo "BLOCKED: Calee tablet — could not finish (see messages above)." ;;
esac

read -p "Press Enter to close..."
exit $STATUS
