#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/../.." || exit 1

echo "=== Calee Regression — Check Setup ==="
echo "(This only checks local tooling. Use '01 Prepare Test Environment' for the full check + fixture reset.)"
echo ""

# shellcheck source=../../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

bash scripts/doctor.sh
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo ""
    echo "PASSED: setup looks good."
else
    echo ""
    echo "FAILED: setup has problems — see the [ERROR]/[WARN] lines above."
fi

read -p "Press Enter to close..."
exit $STATUS
