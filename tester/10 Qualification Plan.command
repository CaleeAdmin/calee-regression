#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Qualification Plan (Mac handoff) ==="
echo "Read-only. Prints this host's capabilities and a concrete, secret-free"
echo "plan to move the QUALIFICATION measure on this Mac. It NEVER resets the"
echo "fixture, NEVER runs a product test, NEVER prompts for a password, and"
echo "NEVER touches git. Review the plan, then run the ordered steps yourself."
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -r -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

echo "--- Host capabilities (readiness) ---"
"$CALEE_PYTHON" -m calee_regression host-capabilities --format text

echo ""
echo "--- Qualification plan ---"
"$CALEE_PYTHON" -m calee_regression qualification-plan --config "$CALEE_TEST_CONFIG" --format markdown
STATUS=$?

echo ""
echo "Nothing was executed. Run the ordered steps above on this Mac to qualify."
read -r -p "Press Enter to close..."
exit $STATUS
