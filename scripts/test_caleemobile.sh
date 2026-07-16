#!/usr/bin/env bash
# Shared by "03 Test CaleeMobile Android.command" and
# "04 Test CaleeMobile iPhone.command" so the two launchers don't duplicate
# the same orchestration logic. Runs CaleeMobile-Regression's backend API
# checks (always possible, no device needed) and, if Flutter + a device are
# available, its per-platform UI checks too.
set -uo pipefail

PLATFORM="${1:-}"
if [ "$PLATFORM" != "android" ] && [ "$PLATFORM" != "ios" ]; then
    echo "Usage: test_caleemobile.sh <android|ios>" >&2
    exit 2
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR" || exit 1
mkdir -p reports

SIBLING="../CaleeMobile-Regression"
if [ ! -d "$SIBLING/api" ]; then
    echo "BLOCKED: CaleeMobile-Regression was not found next to this folder."
    echo "Ask your technical owner to check out CaleeMobile-Regression alongside calee-regression."
    exit 3
fi

API_REPORT="reports/mobile-api-latest.json"
echo "Running the CaleeMobile backend API checks..."
( cd "$SIBLING/api" && python3 run_regression.py --report "$SCRIPT_DIR/$API_REPORT" )
API_STATUS=$?

UI_STATUS=3
if command -v flutter >/dev/null 2>&1 && [ -d "$SIBLING/ui" ]; then
    echo ""
    echo "Running the CaleeMobile $PLATFORM UI checks (requires a connected device/emulator/simulator)..."
    ( cd "$SIBLING/ui" && flutter pub get && flutter test integration_test -d "$PLATFORM" )
    UI_STATUS=$?
else
    echo ""
    if [ "$PLATFORM" = "ios" ]; then
        echo "BLOCKED: the CaleeMobile iPhone UI checks need a Mac with Flutter and Xcode installed, plus a connected iPhone or simulator. Skipping — the backend API checks above still ran."
    else
        echo "BLOCKED: the CaleeMobile Android UI checks need Flutter installed, plus a connected Android device/emulator. Skipping — the backend API checks above still ran."
    fi
fi

echo ""
if [ "$API_STATUS" -eq 1 ]; then
    echo "FAIL: CaleeMobile $PLATFORM — the backend API checks found a real problem."
    exit 1
elif [ "$API_STATUS" -ne 0 ] || [ "$UI_STATUS" -ne 0 ]; then
    echo "BLOCKED: CaleeMobile $PLATFORM — see messages above. This is NOT necessarily a product failure."
    exit 3
else
    echo "PASS: CaleeMobile $PLATFORM"
    exit 0
fi
