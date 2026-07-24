#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Record Manual Checks ==="
echo "You'll be shown each manual check one at a time. Just type the number"
echo "next to your answer and press Enter -- nothing here needs typing a"
echo "file, a command, or JSON/YAML."
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

"$CALEE_PYTHON" -m calee_regression record-manual-checks
STATUS=$?

echo ""
case $STATUS in
    0) echo "All manual checks recorded: PASS." ;;
    1) echo "FAIL: at least one mandatory manual check was recorded as Fail." ;;
    3) echo "BLOCKED: at least one mandatory manual check is missing or was recorded as Blocked. Run this again to finish it." ;;
    *) echo "Could not finish recording manual checks — see the messages above." ;;
esac

read -p "Press Enter to close..."
exit $STATUS
