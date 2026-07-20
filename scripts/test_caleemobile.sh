#!/usr/bin/env bash
# Shared by "03 Test CaleeMobile Android.command",
# "04 Test CaleeMobile iPhone.command" and "06 Test Full Calee Solution"
# so the launchers don't duplicate the same orchestration logic. Runs
# CaleeMobile-Regression's backend Client API checks and its per-platform UI
# checks via run_ui_suite.py, which resolves a real device (never a hardcoded
# "-d android"/"-d ios"), passes test credentials to the CaleeMobile process
# securely (never as a bare CLI argument another process could read off the
# process list), and writes a structured results.json for consolidation.
#
# Modes (Phase 3 -- run the Client API regression EXACTLY ONCE per release):
#   test_caleemobile.sh <android|ios>              full: Prepare + API + UI
#   test_caleemobile.sh api-only                   Prepare + Client API only
#   test_caleemobile.sh <android|ios> --ui-only    Prepare + UI only (no API)
#
# The standalone "03/04 Test CaleeMobile ..." launchers still use the full
# mode (Prepare + API + selected UI). The full-solution launcher ("06 Test
# Full Calee Solution") instead runs `api-only` ONCE and then `--ui-only`
# per selected platform, so the device-independent Client API suite is never
# re-run once per platform and an Android or iOS run can never overwrite the
# one reports/runs/<run-id>/mobile-api/results.json. `--skip-api` is an alias
# for `--ui-only`.
#
# Execution order (see docs/RELEASE_POLICY.md and Phase 1 of the release
# plan). NEITHER the Client API regression NOR the mobile UI regression may
# run before Prepare has passed and this run's environment/fixture/backend
# have been verified:
#
#   1. validate local setup (platform/mode arg, CaleeMobile-Regression sibling)
#   2. create or reuse the run ID and its workspace
#   3. run Prepare when this run has no environment report yet
#   4. validate the same-run environment report (belongs to THIS run)
#   5. validate the fixture status ("ok") and resolve the verified backend
#   6. run the Client API regression (against the verified backend) -- when
#      this mode runs the API
#   7. run the mobile UI regression (against the same verified backend) --
#      when this mode runs the UI
#   8. record the component(s) this mode produced into the run manifest
#
# The old ordering ran the Client API regression FIRST -- before Prepare had
# even reset/verified the fixture -- so the API suite could exercise an
# unprepared or misdirected backend and still be consolidated. That is the
# defect this ordering closes: a verified, same-run environment now gates
# BOTH suites, and the one verified backend is passed consistently to each.
set -uo pipefail

# --- Argument parsing: a platform and/or an explicit mode ----------------
# Backward compatible: a bare "android"/"ios" is the full mode. "api-only"
# needs no platform (the Client API suite is device-independent). "--ui-only"
# (alias "--skip-api") runs only the per-platform UI checks.
MODE="full"
PLATFORM=""
for arg in "$@"; do
    case "$arg" in
        android | ios) PLATFORM="$arg" ;;
        api-only | --api-only) MODE="api-only" ;;
        ui-only | --ui-only | --skip-api) MODE="ui-only" ;;
        *)
            echo "Usage: test_caleemobile.sh <android|ios> [--ui-only] | api-only" >&2
            exit 2
            ;;
    esac
done

RUN_API=false
RUN_UI=false
case "$MODE" in
    full)
        RUN_API=true
        RUN_UI=true
        ;;
    api-only) RUN_API=true ;;
    ui-only) RUN_UI=true ;;
esac

# Any mode that runs the UI needs to know which platform; api-only does not.
if [ "$RUN_UI" = true ] && [ "$PLATFORM" != "android" ] && [ "$PLATFORM" != "ios" ]; then
    echo "Usage: test_caleemobile.sh <android|ios> [--ui-only] | api-only" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR" || exit 1

# --- Step 1/2: validate local setup, create/reuse the run ID -------------
# Every invocation belongs to a run workspace, whether or not it's part of
# an orchestrated "06 Test Full Calee Solution" run -- a standalone
# "03/04 Test CaleeMobile ..." run just gets its own run ID instead of
# writing to shared "-latest" files (see calee_regression/run_context.py
# and docs/RELEASE_POLICY.md).
CALEE_RUN_ID="${CALEE_RUN_ID:-mobile-standalone-$(date +%Y%m%d-%H%M%S)}"
export CALEE_RUN_ID
echo "Run ID: $CALEE_RUN_ID"
echo "Mode: $MODE${PLATFORM:+ ($PLATFORM)}"

# The sibling-repo check is a pure filesystem check -- it must not require a
# working Python environment (this script can run before any bootstrap has
# ever installed calee_regression's dependencies), so it comes BEFORE report
# root resolution below.
SIBLING="../CaleeMobile-Regression"
if [ ! -d "$SIBLING/api" ]; then
    echo "BLOCKED: CaleeMobile-Regression was not found next to this folder."
    echo "Ask your technical owner to check out CaleeMobile-Regression alongside calee-regression."
    exit 3
fi
echo "[OK] CaleeMobile source found"

# The ONE canonical report root (Priority 3) -- inherited already-resolved
# from whichever launcher called this script ("06 Test Full Calee Solution",
# or a standalone "03/04 Test CaleeMobile ..." run, both of which resolve
# and export it themselves via `report-root` before calling here). This
# script deliberately never invokes the calee_regression CLI to resolve it
# itself: none of this script's own gating logic (sibling check, credential
# check, self-prepare decision, flutter-toolchain check) may depend on a
# working Python environment being importable -- see
# calee_regression/report_root.py.
CALEE_REPORT_ROOT="${CALEE_REPORT_ROOT:-$SCRIPT_DIR}"
export CALEE_REPORT_ROOT

RUN_DIR="$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID"
mkdir -p "$RUN_DIR"
API_DIR="$RUN_DIR/mobile-api"
API_REPORT="$API_DIR/results.json"
UI_DIR=""
UI_REPORT=""
if [ "$RUN_API" = true ]; then
    mkdir -p "$API_DIR"
fi
if [ "$RUN_UI" = true ]; then
    UI_DIR="$RUN_DIR/mobile-$PLATFORM"
    UI_REPORT="$UI_DIR/results.json"
    mkdir -p "$UI_DIR"
fi

# --- Release-feature scope (Workstream 1) --------------------------------
# The full-solution launcher ("06 Test Full Calee Solution") already exported
# the CALEE_RELEASE_FEATURE_* profile before invoking us. A standalone
# "03/04 Test CaleeMobile ..." run has not, so populate it here from the SAME
# parsed config/release-platforms.yaml the consolidator uses -- via the
# release-platforms command's exported lines, NEVER a second YAML parse in
# bash. An omitted feature defaults to mandatory=true (see release_platforms.py
# and the "omitted requirement must never silently become optional" rule).
if [ -z "${CALEE_RELEASE_FEATURE_MEALS:-}" ]; then
    eval "$(python -m calee_regression release-platforms)"
fi
export CALEE_RELEASE_FEATURE_MEALS="${CALEE_RELEASE_FEATURE_MEALS:-true}"
export CALEE_RELEASE_FEATURE_ONBOARDING="${CALEE_RELEASE_FEATURE_ONBOARDING:-true}"
export CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR="${CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR:-true}"
export CALEE_RELEASE_FEATURE_KIOSK_ADMIN="${CALEE_RELEASE_FEATURE_KIOSK_ADMIN:-true}"
echo "[OK] Release-feature scope: meals=$CALEE_RELEASE_FEATURE_MEALS onboarding=$CALEE_RELEASE_FEATURE_ONBOARDING google_calendar=$CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR kiosk_admin=$CALEE_RELEASE_FEATURE_KIOSK_ADMIN"

# Default both suites to BLOCKED (exit 3): if the Prepare gate below refuses
# to let them run, that is exactly the state they must be recorded in --
# never a silent pass by never having executed.
API_STATUS=3
UI_STATUS=3

# --- Step 3/4/5: same-run Prepare + environment/fixture/backend gate ------
# A standalone "03/04 Test CaleeMobile ..." run must exercise the app
# against a VERIFIED, same-run environment -- never the old "not checked"
# behaviour. If this run has no environment report yet (i.e. it is not a
# "06 Test Full Calee Solution" run, where Prepare already ran under this
# same run ID), run Prepare now with THIS run ID, then require: Prepare
# passed, it wrote an environment report, that report belongs to THIS run,
# and the regression fixture verified "ok". Any of those failing BLOCKS the
# whole mobile run -- both the API and the UI checks -- see
# docs/RELEASE_POLICY.md and docs/TEST_DATA_RESET_CONTRACT.md.
ENV_REPORT="$RUN_DIR/environment/results.json"
PREPARE_BLOCK=""
ENV_BACKEND=""

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
            PREPARE_BLOCK="Prepare did not pass (exit $PREPARE_STATUS) — refusing to run the mobile checks against an unprepared environment."
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
        PREPARE_BLOCK="the regression fixture was not verified (status: '${ENV_FIXTURE_STATUS:-missing}') — refusing to run the mobile checks against unverified data."
    else
        # The one verified backend flows to BOTH suites. To the API suite as
        # CALEE_API_BASE (the fixture backend run_regression.py talks to), and
        # to run_ui_suite.py both as the backend to build CaleeMobile against
        # (CALEE_MOBILE_BACKEND -> --dart-define=CALEE_API_BASE) and as the
        # fixture backend to confirm against (CALEE_EXPECTED_BACKEND);
        # run_ui_suite then verifies the app actually resolved it. See
        # run_ui_suite.py.
        CALEE_FIXTURE_STATUS="$ENV_FIXTURE_STATUS"
        CALEE_EXPECTED_BACKEND="$ENV_BACKEND"
        CALEE_MOBILE_BACKEND="$ENV_BACKEND"
        export CALEE_FIXTURE_STATUS CALEE_EXPECTED_BACKEND CALEE_MOBILE_BACKEND
        echo "[OK] Environment verified for run $CALEE_RUN_ID (backend: ${ENV_BACKEND:-unknown})"
    fi
fi

# --- Step 6/7: run the suites this mode selects, but only once the gate has passed.
# CALEE_TEST_EMAIL/CALEE_TEST_PASSWORD are read from the environment by
# run_regression.py / run_ui_suite.py themselves (never passed as a bare CLI
# argument here), so they never appear in a process listing (ps) or in any
# echoed command.
if [ -n "$PREPARE_BLOCK" ]; then
    echo ""
    echo "BLOCKED: the CaleeMobile ${PLATFORM:-Client API} checks — $PREPARE_BLOCK"
    if [ "$MODE" = "full" ]; then
        echo "Neither the Client API checks nor the $PLATFORM UI checks ran (Prepare must pass first)."
    fi
    API_STATUS=3
    UI_STATUS=3
else
    # Pass the one verified backend to the Client API suite too, so both
    # suites certify the same backend the fixture was prepared against.
    if [ -n "$ENV_BACKEND" ]; then
        export CALEE_API_BASE="$ENV_BACKEND"
    fi

    # --- Step 6: Client API regression (device-independent) ---
    if [ "$RUN_API" = true ]; then
        echo ""
        echo "Running the CaleeMobile backend API checks..."
        (cd "$SIBLING/api" && python3 run_regression.py --report "$API_REPORT")
        API_STATUS=$?
    fi

    # --- Step 7: mobile UI regression ---
    if [ "$RUN_UI" = true ]; then
        if [ -z "${CALEE_TEST_EMAIL:-}" ] || [ -z "${CALEE_TEST_PASSWORD:-}" ]; then
            echo ""
            echo "BLOCKED: the CaleeMobile $PLATFORM UI checks need CALEE_TEST_EMAIL and CALEE_TEST_PASSWORD to be"
            echo "configured. Ask your technical owner to set these (see docs/SETUP_MAC.md)."
        elif ! command -v flutter >/dev/null 2>&1; then
            echo ""
            if [ "$PLATFORM" = "ios" ]; then
                echo "BLOCKED: the CaleeMobile iPhone UI checks need a Mac with Flutter and Xcode installed."
            else
                echo "BLOCKED: the CaleeMobile Android UI checks need Flutter installed."
            fi
        elif [ ! -d "$SIBLING/ui" ]; then
            echo ""
            echo "BLOCKED: CaleeMobile-Regression/ui was not found next to this folder."
        else
            echo ""
            echo "Preparing the CaleeMobile $PLATFORM UI checks..."
            if ! (cd "$SIBLING/ui" && flutter pub get) >"$UI_DIR/pub-get.log" 2>&1; then
                echo "BLOCKED: \`flutter pub get\` failed in CaleeMobile-Regression/ui — a Flutter toolchain/dependency"
                echo "problem, not a product failure. See $UI_DIR/pub-get.log"
                UI_STATUS=3
            else
                # Priority 4: the CONFIGURED device id controls which device the
                # UI suite drives. A per-platform machine device id
                # (CALEE_IPHONE_DEVICE for iOS, CALEE_ANDROID_DEVICE for Android)
                # becomes CALEE_UI_DEVICE_ID (which run_ui_suite.py reads and also
                # records in the device-identifier metadata), and is passed
                # explicitly as --device-id so the configured iPhone/Android is
                # targeted instead of "whatever single device happens to be
                # attached". An empty value falls back to run_ui_suite.py's own
                # single-device auto-resolution.
                UI_DEVICE_ID="${CALEE_UI_DEVICE_ID:-}"
                if [ -z "$UI_DEVICE_ID" ]; then
                    if [ "$PLATFORM" = "ios" ]; then
                        UI_DEVICE_ID="${CALEE_IPHONE_DEVICE:-}"
                    elif [ "$PLATFORM" = "android" ]; then
                        UI_DEVICE_ID="${CALEE_ANDROID_DEVICE:-}"
                    fi
                fi
                UI_DEVICE_ARGS=()
                if [ -n "$UI_DEVICE_ID" ]; then
                    export CALEE_UI_DEVICE_ID="$UI_DEVICE_ID"
                    UI_DEVICE_ARGS=(--device-id "$UI_DEVICE_ID")
                fi
                # CALEE_MOBILE_BACKEND / CALEE_EXPECTED_BACKEND / CALEE_FIXTURE_STATUS
                # are exported above and read by run_ui_suite.py from the environment.
                # The mobile release features (Workstream 1) are passed explicitly
                # so run_ui_suite.py forwards them to the Dart process as
                # --dart-define=CALEE_RELEASE_FEATURE_* and tags each step with the
                # feature it exercised (kiosk/admin is a tablet feature, handled by
                # the kiosk-admin command, not the mobile UI suite).
                # ${arr[@]+"${arr[@]}"} keeps an empty array safe under `set -u`
                # on the bash 3.2 that ships with macOS.
                (cd "$SIBLING/ui" && python3 run_ui_suite.py \
                    --platform "$PLATFORM" \
                    ${UI_DEVICE_ARGS[@]+"${UI_DEVICE_ARGS[@]}"} \
                    --report "$UI_REPORT" \
                    --log "$UI_DIR/flutter.log" \
                    --feature "meals=$CALEE_RELEASE_FEATURE_MEALS" \
                    --feature "onboarding=$CALEE_RELEASE_FEATURE_ONBOARDING" \
                    --feature "google_calendar=$CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR")
                UI_STATUS=$?
            fi
        fi
    fi
fi

# --- Step 8: record the component(s) this mode produced ------------------
# api-only records ONLY mobile-api; --ui-only records ONLY mobile-<platform>.
# This is what guarantees an Android or iOS UI run can never touch (let alone
# overwrite) the one mobile-api report. record-component itself additionally
# keeps an auditable attempt history and never lets a recorded result improve
# across recordings (see calee_regression/run_context.py), so even a stray
# second recording can't launder an earlier FAIL into a PASS.
if [ "$RUN_API" = true ]; then
    python -m calee_regression record-component --run-id "$CALEE_RUN_ID" --component mobile-api \
        --report-path "$API_REPORT" --exit-code "$API_STATUS" || true
fi
if [ "$RUN_UI" = true ]; then
    python -m calee_regression record-component --run-id "$CALEE_RUN_ID" --component "mobile-$PLATFORM" \
        --report-path "$UI_REPORT" --exit-code "$UI_STATUS" || true
fi

echo ""
case "$MODE" in
    api-only)
        if [ "$API_STATUS" -eq 1 ]; then
            echo "FAIL: CaleeMobile Client API — a real problem was found (see messages above)."
            exit 1
        elif [ "$API_STATUS" -ne 0 ]; then
            echo "BLOCKED: CaleeMobile Client API — see messages above. This is NOT necessarily a product failure."
            exit 3
        else
            echo "PASS: CaleeMobile Client API"
            exit 0
        fi
        ;;
    ui-only)
        if [ "$UI_STATUS" -eq 1 ]; then
            echo "FAIL: CaleeMobile $PLATFORM UI — a real problem was found (see messages above)."
            exit 1
        elif [ "$UI_STATUS" -ne 0 ]; then
            echo "BLOCKED: CaleeMobile $PLATFORM UI — see messages above. This is NOT necessarily a product failure."
            exit 3
        else
            echo "PASS: CaleeMobile $PLATFORM UI"
            exit 0
        fi
        ;;
    *)
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
        ;;
esac
