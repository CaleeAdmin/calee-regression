#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/../.."

if [ ! -f .venv/bin/activate ]; then
    echo "No .venv found. Run this first in Terminal:"
    echo "  python3.11 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'"
    read -p "Press Enter to close..."
    exit 1
fi

source .venv/bin/activate
export CALEE_TEST_CONFIG=config/tester.local.yaml

echo "============================================================"
echo " release-technical requires a REAL PHYSICAL TABLET."
echo " It includes kiosk/admin and system-receiver scenarios that"
echo " are not safe or meaningful on an emulator, and are not for"
echo " non-technical testers. Only run this if you know why you"
echo " are here."
echo "============================================================"

bash scripts/run_release_technical.sh
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo ""
    echo "PASSED: release-technical"
else
    echo ""
    echo "FAILED: release-technical — open the report (Open Latest Report.command) for details."
fi

read -p "Press Enter to close..."
exit $STATUS
