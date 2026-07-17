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
echo "--- Recording build identity ---"
# Automatic build-identity collection (Phase 3). Detect the CaleeMobile
# version/commit/dirty state from its checkout, and the Calee tablet
# identity from adb/source where available, so a release PASS can prove
# exactly which builds were tested. A technical owner can still override any
# value by exporting the matching env var; the AUTO_* values only fill the
# gaps. Never fabricated: an undetectable identity stays unavailable, which
# the consolidator turns into BLOCKED for an in-scope app.
eval "$(python -m calee_regression build-identity)"
CALEEMOBILE_BUILD_VERSION="${CALEEMOBILE_BUILD_VERSION:-${AUTO_CALEEMOBILE_BUILD_VERSION:-}}"
CALEEMOBILE_GIT_SHA="${CALEEMOBILE_GIT_SHA:-${AUTO_CALEEMOBILE_GIT_SHA:-}}"
CALEEMOBILE_DIRTY="${CALEEMOBILE_DIRTY:-${AUTO_CALEEMOBILE_DIRTY:-false}}"
CALEEMOBILE_IDENTITY_AVAILABLE="${CALEEMOBILE_IDENTITY_AVAILABLE:-${AUTO_CALEEMOBILE_IDENTITY_AVAILABLE:-false}}"
CALEE_BUILD_VERSION="${CALEE_BUILD_VERSION:-${AUTO_CALEE_BUILD_VERSION:-}}"
CALEE_GIT_SHA="${CALEE_GIT_SHA:-${AUTO_CALEE_GIT_SHA:-}}"
CALEE_DIRTY="${CALEE_DIRTY:-${AUTO_CALEE_DIRTY:-false}}"
CALEE_IDENTITY_AVAILABLE="${CALEE_IDENTITY_AVAILABLE:-${AUTO_CALEE_IDENTITY_AVAILABLE:-false}}"
CALEE_VERSION_CODE="${CALEE_VERSION_CODE:-${AUTO_CALEE_VERSION_CODE:-}}"
CALEE_APPLICATION_ID="${CALEE_APPLICATION_ID:-${AUTO_CALEE_APPLICATION_ID:-}}"
CALEESHELL_VERSION="${CALEESHELL_VERSION:-${AUTO_CALEE_CALEESHELL_VERSION:-}}"
echo "CaleeMobile: ${CALEEMOBILE_BUILD_VERSION:-unknown} @ ${CALEEMOBILE_GIT_SHA:-unknown} (dirty=$CALEEMOBILE_DIRTY, available=$CALEEMOBILE_IDENTITY_AVAILABLE)"
echo "Calee tablet: ${CALEE_BUILD_VERSION:-unknown} @ ${CALEE_GIT_SHA:-unknown} (dirty=$CALEE_DIRTY, available=$CALEE_IDENTITY_AVAILABLE)"

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
# Build/commit identity -- auto-collected above (a technical owner can still
# override any value via the matching env var). The detected identity is
# always passed so the consolidator can gate on it; see Phase 3.
[ -n "${CALEE_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--calee-build-version "$CALEE_BUILD_VERSION")
[ -n "${CALEEMOBILE_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--caleemobile-build-version "$CALEEMOBILE_BUILD_VERSION")
[ -n "${CALEE_EXPECTED_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--expected-calee-build-version "$CALEE_EXPECTED_BUILD_VERSION")
[ -n "${CALEEMOBILE_EXPECTED_BUILD_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--expected-caleemobile-build-version "$CALEEMOBILE_EXPECTED_BUILD_VERSION")
[ -n "${CALEESHELL_VERSION:-}" ] && CONSOLIDATE_ARGS+=(--caleeshell-version "$CALEESHELL_VERSION")
[ -n "${CALEE_GIT_SHA:-}" ] && CONSOLIDATE_ARGS+=(--calee-git-sha "$CALEE_GIT_SHA")
[ -n "${CALEEMOBILE_GIT_SHA:-}" ] && CONSOLIDATE_ARGS+=(--caleemobile-git-sha "$CALEEMOBILE_GIT_SHA")
[ -n "${CALEE_EXPECTED_GIT_SHA:-}" ] && CONSOLIDATE_ARGS+=(--expected-calee-git-sha "$CALEE_EXPECTED_GIT_SHA")
[ -n "${CALEEMOBILE_EXPECTED_GIT_SHA:-}" ] && CONSOLIDATE_ARGS+=(--expected-caleemobile-git-sha "$CALEEMOBILE_EXPECTED_GIT_SHA")
[ -n "${CALEE_VERSION_CODE:-}" ] && CONSOLIDATE_ARGS+=(--calee-version-code "$CALEE_VERSION_CODE")
[ -n "${CALEE_APPLICATION_ID:-}" ] && CONSOLIDATE_ARGS+=(--calee-application-id "$CALEE_APPLICATION_ID")
[ "${CALEEMOBILE_DIRTY:-false}" = "true" ] && CONSOLIDATE_ARGS+=(--caleemobile-dirty)
[ "${CALEE_DIRTY:-false}" = "true" ] && CONSOLIDATE_ARGS+=(--calee-dirty)
[ "${CALEEMOBILE_IDENTITY_AVAILABLE:-false}" = "true" ] || CONSOLIDATE_ARGS+=(--caleemobile-identity-unavailable)
[ "${CALEE_IDENTITY_AVAILABLE:-false}" = "true" ] || CONSOLIDATE_ARGS+=(--calee-identity-unavailable)
[ "${CALEE_ALLOW_DIRTY:-false}" = "true" ] && CONSOLIDATE_ARGS+=(--allow-dirty)
[ "${CALEE_ALLOW_UNKNOWN_BUILD_IDENTITY:-false}" = "true" ] && CONSOLIDATE_ARGS+=(--allow-unknown-build-identity)
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
