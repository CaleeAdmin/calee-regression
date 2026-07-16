#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/../.." || exit 1

echo "=== Calee Regression — Full Tester (tablet only) ==="
echo "Only run this on a PREPARED tablet/emulator with a logged-in demo account."
echo "(Use '02 Test Calee Tablet' instead unless you specifically want this exact suite name.)"
echo ""

# shellcheck source=../../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

bash scripts/run_full_tester.sh
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo ""
    echo "PASSED: full-tester"
else
    echo ""
    echo "FAILED/BLOCKED: full-tester — open the report ('06 Open Latest Report') for details."
fi

read -p "Press Enter to close..."
exit $STATUS
