#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/../.." || exit 1

echo "=== Calee Regression — Calendar Only ==="
echo "Only run this on a PREPARED tablet/emulator with a logged-in demo account"
echo "and the regression fixture reset ('01 Prepare Test Environment')."
echo ""

# shellcheck source=../../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

bash scripts/run_calendar.sh
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo ""
    echo "PASSED: calendar"
else
    echo ""
    echo "FAILED/BLOCKED: calendar — open the report ('07 Open Latest Report') for details."
fi

read -p "Press Enter to close..."
exit $STATUS
