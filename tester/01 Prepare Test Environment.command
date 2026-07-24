#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Prepare Test Environment ==="
echo "Run this first, every time, before testing the tablet or CaleeMobile."
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

"$CALEE_PYTHON" -m calee_regression prepare --config "$CALEE_TEST_CONFIG"
STATUS=$?

echo ""
case $STATUS in
    0) echo "READY: the test environment is prepared." ;;
    3) echo "BLOCKED: the test environment is not ready yet — see the messages above, and ask your technical owner if you're stuck." ;;
    *) echo "BLOCKED: could not finish preparing the test environment — see the messages above." ;;
esac

read -p "Press Enter to close..."
exit $STATUS
