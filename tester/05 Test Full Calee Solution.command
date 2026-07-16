#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Test Full Calee Solution ==="
echo "This runs Prepare, the Calee tablet suite, and CaleeMobile (Android),"
echo "then combines everything into one report."
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

echo ""
echo "--- Step 1 of 3: Prepare Test Environment ---"
python -m calee_regression prepare --config "$CALEE_TEST_CONFIG"

echo ""
echo "--- Step 2 of 3: Calee Tablet ---"
python -m calee_regression suite --config "$CALEE_TEST_CONFIG" --suite full-tester
TABLET_REPORT_DIR="$(ls -1dt reports/full-tester-*/ 2>/dev/null | head -n1)"

echo ""
echo "--- Step 3 of 3: CaleeMobile (Android) ---"
bash scripts/test_caleemobile.sh android
MOBILE_API_REPORT="reports/mobile-api-latest.json"

echo ""
echo "--- Combining into one report ---"
CONSOLIDATE_ARGS=(--build-version "${CALEE_BUILD_VERSION:-unknown}")
if [ -n "$TABLET_REPORT_DIR" ] && [ -f "${TABLET_REPORT_DIR}results.json" ]; then
    CONSOLIDATE_ARGS+=(--tablet-report "${TABLET_REPORT_DIR}results.json")
fi
if [ -f "$MOBILE_API_REPORT" ]; then
    CONSOLIDATE_ARGS+=(--mobile-api-report "$MOBILE_API_REPORT")
fi

python -m calee_regression consolidate "${CONSOLIDATE_ARGS[@]}"
STATUS=$?

echo ""
case $STATUS in
    0) echo "PASS: Full Calee Solution" ;;
    1) echo "FAIL: Full Calee Solution — a real problem was found. Open the report ('06 Open Latest Report') for details." ;;
    *) echo "BLOCKED: Full Calee Solution — see the messages above. This is NOT necessarily a product failure. Manual guided checks (see docs/NON_TECH_TESTER_GUIDE.md) still need to be recorded before this can be an overall PASS." ;;
esac

read -p "Press Enter to close..."
exit $STATUS
