#!/usr/bin/env bash
# Shared by "03 Test CaleeMobile Android.command" and
# "04 Test CaleeMobile iPhone.command" so the two launchers don't duplicate
# the same orchestration logic. Runs CaleeMobile-Regression's backend API
# checks (always possible, no device needed) and its per-platform UI
# checks via run_ui_suite.py, which resolves a real device (never a
# hardcoded "-d android"/"-d ios"), passes test credentials through
# --dart-define, and writes a structured results.json for consolidation.
set -uo pipefail

PLATFORM="${1:-}"
if [ "$PLATFORM" != "android" ] && [ "$PLATFORM" != "ios" ]; then
    echo "Usage: test_caleemobile.sh <android|ios>" >&2
    exit 2
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR" || exit 1
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
REPORT_DIR="reports/mobile-${PLATFORM}-${TIMESTAMP}"
mkdir -p "$REPORT_DIR"

SIBLING="../CaleeMobile-Regression"
if [ ! -d "$SIBLING/api" ]; then
    echo "BLOCKED: CaleeMobile-Regression was not found next to this folder."
    echo "Ask your technical owner to check out CaleeMobile-Regression alongside calee-regression."
    exit 3
fi
echo "[OK] CaleeMobile source found"

API_REPORT="reports/mobile-api-latest.json"
echo "Running the CaleeMobile backend API checks..."
( cd "$SIBLING/api" && python3 run_regression.py --report "$SCRIPT_DIR/$API_REPORT" )
API_STATUS=$?

UI_STATUS=3
UI_REPORT="$REPORT_DIR/results.json"

# CALEE_TEST_EMAIL/CALEE_TEST_PASSWORD are read from the environment by
# run_ui_suite.py itself (never passed as a bare CLI argument here), so
# they never appear in a process listing (ps) or in any echoed command.
if [ -z "${CALEE_TEST_EMAIL:-}" ] || [ -z "${CALEE_TEST_PASSWORD:-}" ]; then
    echo ""
    echo "BLOCKED: the CaleeMobile $PLATFORM UI checks need CALEE_TEST_EMAIL and CALEE_TEST_PASSWORD to be"
    echo "configured. Ask your technical owner to set these (see docs/SETUP_MAC.md). Skipping — the backend"
    echo "API checks above still ran."
elif ! command -v flutter >/dev/null 2>&1; then
    echo ""
    if [ "$PLATFORM" = "ios" ]; then
        echo "BLOCKED: the CaleeMobile iPhone UI checks need a Mac with Flutter and Xcode installed. Skipping — the backend API checks above still ran."
    else
        echo "BLOCKED: the CaleeMobile Android UI checks need Flutter installed. Skipping — the backend API checks above still ran."
    fi
elif [ ! -d "$SIBLING/ui" ]; then
    echo ""
    echo "BLOCKED: CaleeMobile-Regression/ui was not found next to this folder. Skipping — the backend API checks above still ran."
else
    echo ""
    echo "Preparing the CaleeMobile $PLATFORM UI checks..."
    if ! ( cd "$SIBLING/ui" && flutter pub get ) > "$SCRIPT_DIR/$REPORT_DIR/pub-get.log" 2>&1; then
        echo "BLOCKED: \`flutter pub get\` failed in CaleeMobile-Regression/ui — a Flutter toolchain/dependency"
        echo "problem, not a product failure. See $REPORT_DIR/pub-get.log"
        UI_STATUS=3
    else
        ( cd "$SIBLING/ui" && python3 run_ui_suite.py \
            --platform "$PLATFORM" \
            --report "$SCRIPT_DIR/$UI_REPORT" \
            --log "$SCRIPT_DIR/$REPORT_DIR/flutter.log" )
        UI_STATUS=$?
    fi
fi

echo ""
if [ "$API_STATUS" -eq 1 ] || [ "$UI_STATUS" -eq 1 ]; then
    echo "FAIL: CaleeMobile $PLATFORM — a real problem was found (see messages above)."
    exit 1
elif [ "$API_STATUS" -ne 0 ] || [ "$UI_STATUS" -ne 0 ]; then
    echo "BLOCKED: CaleeMobile $PLATFORM — see messages above. This is NOT necessarily a product failure."
    exit 3
else
    echo "PASS: CaleeMobile $PLATFORM"
    exit 0
fi
