#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Test Full Calee Solution ==="
echo "This runs Prepare (incl. starting Appium automatically if needed),"
echo "the Calee tablet suite, CaleeMobile (API + UI for each platform this"
echo "release includes), guided manual checks, then combines everything"
echo "into one report."
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

# Determine which platforms this release actually includes (technical-owner
# config/release-platforms.yaml; defaults to "every platform" if absent --
# see release_platforms.py). Never silently narrowed by what happens to be
# convenient to run right now.
eval "$(python -m calee_regression release-platforms)"

echo ""
echo "--- Step 1: Prepare Test Environment (incl. Appium) ---"
python -m calee_regression prepare --config "$CALEE_TEST_CONFIG" --suite tablet-full
PREPARE_STATUS=$?

echo ""
echo "--- Step 2: Calee Tablet ---"
python -m calee_regression suite --config "$CALEE_TEST_CONFIG" --suite full-tester
TABLET_REPORT_DIR="$(ls -1dt reports/full-tester-*/ 2>/dev/null | head -n1)"

ANDROID_UI_REPORT=""
if [ "$RELEASE_PLATFORM_ANDROID" = "true" ]; then
    echo ""
    echo "--- Step 3: CaleeMobile Android ---"
    bash scripts/test_caleemobile.sh android
    ANDROID_UI_REPORT_DIR="$(ls -1dt reports/mobile-android-*/ 2>/dev/null | head -n1)"
    if [ -n "$ANDROID_UI_REPORT_DIR" ] && [ -f "${ANDROID_UI_REPORT_DIR}results.json" ]; then
        ANDROID_UI_REPORT="${ANDROID_UI_REPORT_DIR}results.json"
    fi
else
    echo ""
    echo "--- Step 3: CaleeMobile Android — SKIPPED (not part of this release; see config/release-platforms.yaml) ---"
fi

IOS_UI_REPORT=""
if [ "$RELEASE_PLATFORM_IOS" = "true" ]; then
    echo ""
    echo "--- Step 4: CaleeMobile iPhone ---"
    bash scripts/test_caleemobile.sh ios
    IOS_UI_REPORT_DIR="$(ls -1dt reports/mobile-ios-*/ 2>/dev/null | head -n1)"
    if [ -n "$IOS_UI_REPORT_DIR" ] && [ -f "${IOS_UI_REPORT_DIR}results.json" ]; then
        IOS_UI_REPORT="${IOS_UI_REPORT_DIR}results.json"
    fi
else
    echo ""
    echo "--- Step 4: CaleeMobile iPhone — SKIPPED (not part of this release; see config/release-platforms.yaml) ---"
fi

echo ""
echo "--- Step 5: Manual Checks ---"
python -m calee_regression record-manual-checks

echo ""
echo "--- Combining into one report ---"
CONSOLIDATE_ARGS=(--build-version "${CALEE_BUILD_VERSION:-unknown}")
if [ -n "$TABLET_REPORT_DIR" ] && [ -f "${TABLET_REPORT_DIR}results.json" ]; then
    CONSOLIDATE_ARGS+=(--tablet-report "${TABLET_REPORT_DIR}results.json")
fi
if [ -f "reports/mobile-api-latest.json" ]; then
    CONSOLIDATE_ARGS+=(--mobile-api-report "reports/mobile-api-latest.json")
fi
if [ -n "$ANDROID_UI_REPORT" ]; then
    CONSOLIDATE_ARGS+=(--mobile-android-report "$ANDROID_UI_REPORT")
fi
if [ -n "$IOS_UI_REPORT" ]; then
    CONSOLIDATE_ARGS+=(--mobile-ios-report "$IOS_UI_REPORT")
fi
if [ -f "reports/manual-checks-latest.json" ]; then
    CONSOLIDATE_ARGS+=(--manual-checks "reports/manual-checks-latest.json")
fi
if [ -f "reports/environment-status-latest.json" ]; then
    CONSOLIDATE_ARGS+=(--environment-report "reports/environment-status-latest.json")
fi
if [ "$RELEASE_PLATFORM_ANDROID" = "true" ]; then
    CONSOLIDATE_ARGS+=(--android-mandatory)
else
    CONSOLIDATE_ARGS+=(--android-optional)
fi
if [ "$RELEASE_PLATFORM_IOS" = "true" ]; then
    CONSOLIDATE_ARGS+=(--ios-mandatory)
else
    CONSOLIDATE_ARGS+=(--ios-optional)
fi
# Optional build/commit metadata -- only included when a technical owner has
# set these (never fabricated); see docs/SETUP_MAC.md.
[ -n "${CALEE_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--calee-build-version "$CALEE_BUILD_VERSION")
[ -n "${CALEEMOBILE_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--caleemobile-build-version "$CALEEMOBILE_BUILD_VERSION")
[ -n "${CALEE_EXPECTED_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--expected-calee-build-version "$CALEE_EXPECTED_BUILD_VERSION")
[ -n "${CALEEMOBILE_EXPECTED_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--expected-caleemobile-build-version "$CALEEMOBILE_EXPECTED_BUILD_VERSION")
[ -n "${CALEESHELL_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--caleeshell-version "$CALEESHELL_VERSION")
[ -n "${CALEE_GIT_SHA:-}" ] && CONSOLIDATE_ARGS+=(--calee-git-sha "$CALEE_GIT_SHA")
[ -n "${CALEEMOBILE_GIT_SHA:-}" ] && CONSOLIDATE_ARGS+=(--caleemobile-git-sha "$CALEEMOBILE_GIT_SHA")

python -m calee_regression consolidate "${CONSOLIDATE_ARGS[@]}"
STATUS=$?

echo ""
echo "--- Stopping Appium (only if this run started it) ---"
python -m calee_regression stop-appium

echo ""
case $STATUS in
    0) echo "PASS: Full Calee Solution" ;;
    1) echo "FAIL: Full Calee Solution — a real problem was found. Open the report ('07 Open Latest Report') for details." ;;
    *) echo "BLOCKED: Full Calee Solution — see the messages above and in the report. This is NOT necessarily a product failure." ;;
esac
if [ "$PREPARE_STATUS" -ne 0 ] && [ "$STATUS" -eq 0 ]; then
    # Should not be reachable in practice (a blocked prepare means the
    # mandatory tablet suite could not have produced a real pass either),
    # but never let a passing consolidate mask a failed prepare step in
    # the summary line the tester reads last.
    echo "NOTE: Prepare Test Environment reported a problem earlier in this run — see Step 1 above."
fi

read -p "Press Enter to close..."
exit $STATUS
