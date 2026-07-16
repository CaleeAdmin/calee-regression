#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/../.." || exit 1

# shellcheck source=../../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

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
    echo "FAILED/BLOCKED: release-technical — open the report ('06 Open Latest Report') for details."
fi

read -p "Press Enter to close..."
exit $STATUS
