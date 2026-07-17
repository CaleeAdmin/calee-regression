#!/usr/bin/env bash
# Shared by "03 Test CaleeMobile Android.command" and
# "04 Test CaleeMobile iPhone.command" so the two launchers don't duplicate
# the same orchestration logic. Runs CaleeMobile-Regression's backend API
# checks (always possible, no device needed) and its per-platform UI
# checks via run_ui_suite.py, which resolves a real device (never a
# hardcoded "-d android"/"-d ios"), passes test credentials to the
# CaleeMobile process securely (never as a bare CLI argument another
# process could read off the process list), and writes a structured
# results.json for consolidation.
set -uo pipefail

PLATFORM="${1:-}"
if [ "$PLATFORM" != "android" ] && [ "$PLATFORM" != "ios" ]; then
    echo "Usage: test_caleemobile.sh <android|ios>" >&2
    exit 2
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR" || exit 1

# Every invocation belongs to a run workspace, whether or not it's part of
# an orchestrated "06 Test Full Calee Solution" run -- a standalone
# "03/04 Test CaleeMobile ..." run just gets its own run ID instead of
# writing to shared "-latest" files (see calee_regression/run_context.py
# and docs/RELEASE_POLICY.md).
CALEE_RUN_ID="${CALEE_RUN_ID:-mobile-standalone-$(date +%Y%m%d-%H%M%S)}"
export CALEE_RUN_ID
RUN_DIR="reports/runs/$CALEE_RUN_ID"
API_DIR="$RUN_DIR/mobile-api"
UI_DIR="$RUN_DIR/mobile-$PLATFORM"
mkdir -p "$API_DIR" "$UI_DIR"
echo "Run ID: $CALEE_RUN_ID"

SIBLING="../CaleeMobile-Regression"
if [ ! -d "$SIBLING/api" ]; then
    echo "BLOCKED: CaleeMobile-Regression was not found next to this folder."
    echo "Ask your technical owner to check out CaleeMobile-Regression alongside calee-regression."
    exit 3
fi
echo "[OK] CaleeMobile source found"

API_REPORT="$API_DIR/results.json"
echo "Running the CaleeMobile backend API checks..."
( cd "$SIBLING/api" && python3 run_regression.py --report "$SCRIPT_DIR/$API_REPORT" )
API_STATUS=$?

UI_STATUS=3
UI_REPORT="$UI_DIR/results.json"

# --- Phase 2: same-run Prepare enforcement -------------------------------
# A standalone "03/04 Test CaleeMobile ..." run must exercise the app
# against a VERIFIED, same-run environment -- never the old "not checked"
# behaviour. If this run has no environment report yet (i.e. it is not a
# "06 Test Full Calee Solution" run, where Prepare already ran under this
# same run ID), run Prepare now with THIS run ID, then require: Prepare
# passed, it wrote an environment report, that report belongs to THIS run,
# and the regression fixture verified "ok". Any of those failing BLOCKS the
# mobile UI checks -- see docs/RELEASE_POLICY.md and
# docs/TEST_DATA_RESET_CONTRACT.md.
ENV_REPORT="$RUN_DIR/environment/results.json"
PREPARE_BLOCK=""

if [ ! -f "$ENV_REPORT" ]; then
    echo ""
    echo "No same-run environment report yet — preparing the environment first (run $CALEE_RUN_ID)..."
    if [ -z "${CALEE_TEST_CONFIG:-}" ] || [ ! -f "${CALEE_TEST_CONFIG:-}" ]; then
        PREPARE_BLOCK="cannot auto-prepare the test environment: no tester config is set (CALEE_TEST_CONFIG). Ask your technical owner to finish setup (docs/SETUP_MAC.md)."
    elif [ -z "${CALEE_API_BASE:-}" ] || [ -z "${CALEE_TEST_EMAIL:-}" ] || [ -z "${CALEE_TEST_PASSWORD:-}" ]; then
        PREPARE_BLOCK="cannot auto-prepare the test environment: CALEE_API_BASE, CALEE_TEST_EMAIL and CALEE_TEST_PASSWORD must all be configured so the regression fixture can be reset and verified."
    else
        python -m calee_regression prepare --config "$CALEE_TEST_CONFIG" --run-id "$CALEE_RUN_ID"
        PREPARE_STATUS=$?
        if [ "$PREPARE_STATUS" -ne 0 ]; then
            PREPARE_BLOCK="Prepare did not pass (exit $PREPARE_STATUS) — refusing to run the mobile UI checks against an unprepared environment."
        elif [ ! -f "$ENV_REPORT" ]; then
            PREPARE_BLOCK="Prepare reported success but wrote no environment report at $ENV_REPORT — refusing to proceed."
        fi
    fi
fi

if [ -z "$PREPARE_BLOCK" ] && [ -f "$ENV_REPORT" ]; then
    # Read this run's environment report (prepare's output). It must belong
    # to THIS run and its fixture must have verified "ok".
    eval "$(python3 -c "
import json
import shlex
with open('$ENV_REPORT', encoding='utf-8') as f:
    data = json.load(f)
run_id = data.get('runId', '') or ''
status = data.get('fixtureVerificationStatus', '') or ''
backend = data.get('targetEnvironment', '') or ''
print(f'ENV_RUN_ID={shlex.quote(run_id)}')
print(f'ENV_FIXTURE_STATUS={shlex.quote(status)}')
print(f'ENV_BACKEND={shlex.quote(backend)}')
")"
    if [ "$ENV_RUN_ID" != "$CALEE_RUN_ID" ]; then
        PREPARE_BLOCK="the environment report's run ID ('${ENV_RUN_ID:-missing}') does not match this run ('$CALEE_RUN_ID') — refusing to trust a report from another run."
    elif [ "$ENV_FIXTURE_STATUS" != "ok" ]; then
        PREPARE_BLOCK="the regression fixture was not verified (status: '${ENV_FIXTURE_STATUS:-missing}') — refusing to run the mobile UI checks against unverified data."
    else
        # The verified backend flows to run_ui_suite.py both as the backend to
        # build CaleeMobile against (CALEE_MOBILE_BACKEND -> --dart-define=
        # CALEE_API_BASE) and as the fixture backend to confirm against
        # (CALEE_EXPECTED_BACKEND); run_ui_suite then verifies the app
        # actually resolved it. See run_ui_suite.py.
        CALEE_FIXTURE_STATUS="$ENV_FIXTURE_STATUS"
        CALEE_EXPECTED_BACKEND="$ENV_BACKEND"
        CALEE_MOBILE_BACKEND="$ENV_BACKEND"
        export CALEE_FIXTURE_STATUS CALEE_EXPECTED_BACKEND CALEE_MOBILE_BACKEND
        echo "[OK] Environment verified for run $CALEE_RUN_ID (backend: ${ENV_BACKEND:-unknown})"
    fi
fi

# CALEE_TEST_EMAIL/CALEE_TEST_PASSWORD are read from the environment by
# run_ui_suite.py itself (never passed as a bare CLI argument here), so
# they never appear in a process listing (ps) or in any echoed command.
if [ -n "$PREPARE_BLOCK" ]; then
    echo ""
    echo "BLOCKED: the CaleeMobile $PLATFORM UI checks — $PREPARE_BLOCK"
    echo "The backend API checks above still ran."
    UI_STATUS=3
elif [ -z "${CALEE_TEST_EMAIL:-}" ] || [ -z "${CALEE_TEST_PASSWORD:-}" ]; then
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
    if ! ( cd "$SIBLING/ui" && flutter pub get ) > "$SCRIPT_DIR/$UI_DIR/pub-get.log" 2>&1; then
        echo "BLOCKED: \`flutter pub get\` failed in CaleeMobile-Regression/ui — a Flutter toolchain/dependency"
        echo "problem, not a product failure. See $UI_DIR/pub-get.log"
        UI_STATUS=3
    else
        # CALEE_MOBILE_BACKEND / CALEE_EXPECTED_BACKEND / CALEE_FIXTURE_STATUS
        # are exported above and read by run_ui_suite.py from the environment.
        ( cd "$SIBLING/ui" && python3 run_ui_suite.py \
            --platform "$PLATFORM" \
            --report "$SCRIPT_DIR/$UI_REPORT" \
            --log "$SCRIPT_DIR/$UI_DIR/flutter.log" )
        UI_STATUS=$?
    fi
fi

python -m calee_regression record-component --run-id "$CALEE_RUN_ID" --component mobile-api \
    --report-path "$API_REPORT" --exit-code "$API_STATUS" || true
python -m calee_regression record-component --run-id "$CALEE_RUN_ID" --component "mobile-$PLATFORM" \
    --report-path "$UI_REPORT" --exit-code "$UI_STATUS" || true

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
