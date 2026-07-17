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

# Before running any UI assertion, check this run's own Prepare outcome
# (fixture readiness) and target backend, if this run has an environment
# report yet -- see run_ui_suite.py::check_fixture_and_backend_alignment
# and docs/RELEASE_POLICY.md. A standalone invocation with no Prepare step
# yet (no environment/results.json) leaves these unset, which
# run_ui_suite.py treats as "not checked", never as "verified ready".
ENV_REPORT="$RUN_DIR/environment/results.json"
if [ -f "$ENV_REPORT" ]; then
    eval "$(python3 -c "
import json
import shlex
with open('$ENV_REPORT', encoding='utf-8') as f:
    data = json.load(f)
status = data.get('fixtureVerificationStatus', '') or ''
backend = data.get('targetEnvironment', '') or ''
print(f'CALEE_FIXTURE_STATUS={shlex.quote(status)}')
print(f'CALEE_EXPECTED_BACKEND={shlex.quote(backend)}')
")"
    export CALEE_FIXTURE_STATUS CALEE_EXPECTED_BACKEND
fi

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
    if ! ( cd "$SIBLING/ui" && flutter pub get ) > "$SCRIPT_DIR/$UI_DIR/pub-get.log" 2>&1; then
        echo "BLOCKED: \`flutter pub get\` failed in CaleeMobile-Regression/ui — a Flutter toolchain/dependency"
        echo "problem, not a product failure. See $UI_DIR/pub-get.log"
        UI_STATUS=3
    else
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
