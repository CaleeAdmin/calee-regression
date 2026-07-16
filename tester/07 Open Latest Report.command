#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Open Latest Report ==="
echo ""

bash scripts/open_latest_report.sh
STATUS=$?

if [ $STATUS -ne 0 ]; then
    echo ""
    echo "No report could be opened. Run '02 Test Calee Tablet' or '06 Test Full Calee Solution' first."
fi

read -p "Press Enter to close..."
exit $STATUS
