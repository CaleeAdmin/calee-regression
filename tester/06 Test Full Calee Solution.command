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

# One run ID for this entire release run, generated once at startup and
# shared by every component (Prepare, tablet, CaleeMobile API/UI, manual
# checks, consolidation). Every component writes to a fixed path inside
# reports/runs/$CALEE_RUN_ID/ -- never a timestamped directory a later
# step has to rediscover by listing and sorting, and never a shared
# always-overwritten file another run could be racing against. See
# calee_regression/run_context.py.
CALEE_RUN_ID="release-$(date +%Y%m%d-%H%M%S)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
export CALEE_RUN_ID
echo "Run ID: $CALEE_RUN_ID"
echo "Workspace: reports/runs/$CALEE_RUN_ID/"
echo ""

# Determine which platforms this release actually includes (technical-owner
# config/release-platforms.yaml; defaults to "every platform" if absent --
# see release_platforms.py). Never silently narrowed by what happens to be
# convenient to run right now.
eval "$(python -m calee_regression release-platforms)"

echo ""
echo "--- Step 1: Prepare Test Environment (incl. Appium) ---"
python -m calee_regression prepare --config "$CALEE_TEST_CONFIG" --suite tablet-full --run-id "$CALEE_RUN_ID"
PREPARE_STATUS=$?

echo ""
echo "--- Step 2: Calee Tablet ---"
python -m calee_regression suite --config "$CALEE_TEST_CONFIG" --suite full-tester --run-id "$CALEE_RUN_ID"

if [ "$RELEASE_PLATFORM_ANDROID" = "true" ]; then
    echo ""
    echo "--- Step 3: CaleeMobile Android ---"
    bash scripts/test_caleemobile.sh android
else
    echo ""
    echo "--- Step 3: CaleeMobile Android — SKIPPED (not part of this release; see config/release-platforms.yaml) ---"
fi

if [ "$RELEASE_PLATFORM_IOS" = "true" ]; then
    echo ""
    echo "--- Step 4: CaleeMobile iPhone ---"
    bash scripts/test_caleemobile.sh ios
else
    echo ""
    echo "--- Step 4: CaleeMobile iPhone — SKIPPED (not part of this release; see config/release-platforms.yaml) ---"
fi

echo ""
echo "--- Step 5: Manual Checks ---"
python -m calee_regression record-manual-checks --run-id "$CALEE_RUN_ID"

echo ""
echo "--- Combining into one report ---"
# No per-component report path flags here: consolidate auto-discovers
# each one from this run's fixed workspace paths
# (reports/runs/$CALEE_RUN_ID/<component>/results.json) and rejects
# anything that doesn't carry this exact run ID -- see
# calee_regression/run_context.py and docs/RELEASE_POLICY.md.
CONSOLIDATE_ARGS=(--run-id "$CALEE_RUN_ID" --build-version "${CALEE_BUILD_VERSION:-unknown}")
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
[ -n "${CALEE_TESTER_ID:-}" ] && CONSOLIDATE_ARGS+=(--tester "$CALEE_TESTER_ID")

python -m calee_regression consolidate "${CONSOLIDATE_ARGS[@]}"
STATUS=$?

echo ""
echo "--- Stopping Appium (only if this run started it) ---"
python -m calee_regression stop-appium

echo ""
case $STATUS in
    0) echo "PASS: Full Calee Solution (run $CALEE_RUN_ID)" ;;
    1) echo "FAIL: Full Calee Solution (run $CALEE_RUN_ID) — a real problem was found. Open the report ('07 Open Latest Report') for details." ;;
    *) echo "BLOCKED: Full Calee Solution (run $CALEE_RUN_ID) — see the messages above and in the report. This is NOT necessarily a product failure." ;;
esac
if [ "$PREPARE_STATUS" -ne 0 ] && [ "$STATUS" -eq 0 ]; then
    # Not reachable in practice any more: Prepare is now a mandatory
    # consolidated component (see consolidated_report.py), so a failed
    # Prepare always makes $STATUS non-zero too. Kept as a hard backstop —
    # a passing consolidate must never mask a failed Prepare step.
    echo "NOTE: Prepare Test Environment reported a problem earlier in this run — see Step 1 above."
    STATUS=3
fi
echo "Report workspace: reports/runs/$CALEE_RUN_ID/"

read -p "Press Enter to close..."
exit $STATUS
