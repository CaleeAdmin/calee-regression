#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Test CaleeMobile (Android) ==="
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

bash scripts/test_caleemobile.sh android
STATUS=$?

read -p "Press Enter to close..."
exit $STATUS
