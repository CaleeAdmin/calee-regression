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

# Hermetic interpreter (Workstream 1). This script is invoked directly (by the
# launcher tests and by "run-with-credentials"), not only after
# ensure_environment.sh has run, so it resolves the repository-owned
# interpreter itself: every `-m calee_regression` call below runs through
# "$CALEE_PYTHON", never a bare python from a stripped PATH or a foreign venv.
_CALEE_REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
# shellcheck source=scripts/lib/hermetic_python.sh
. "$_CALEE_REPO_ROOT/scripts/lib/hermetic_python.sh"
_calee_resolve_python "$_CALEE_REPO_ROOT"

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

# --- Release-feature scope (Workstream 1 + 5) ----------------------------
# The mobile checks must consume the EXACT feature scope this release run
# already composed. "06 Test Full Calee Solution" exports CALEE_RELEASE_FEATURE_*
# from THIS run's schema-v2 release-config result before invoking this script
# (see scripts/run_release_technical.sh / the 06 launcher), so those values are
# already in the environment here and are used as-is. When they are NOT set --
# a standalone "03/04 Test CaleeMobile ..." run, or any run with no schema-v2
# release-config -- this resolves them via the `release-feature-scope` command,
# which prefers THIS run's schema-v2 release-config feature scope and falls back
# to the legacy config/release-platforms.yaml profile ONLY when there is
# genuinely no schema-v2 release bundle for the run. The scope is NEVER
# re-parsed from YAML in bash: it comes from that one Python resolver, the same
# one `consolidate` gates on (Workstream 5 -- this closes the former KNOWN GAP
# where this fallback always read the legacy file even for a schema-v2 release).
# An omitted/malformed feature defaults to mandatory=true (never silently
# optional); a malformed scope makes `release-feature-scope` exit non-zero.
#
# Workstream 8: consume the scope from the environment ONLY when the COMPLETE
# set of feature variables is already present (a "06 Test Full Calee Solution"
# run exports all of them together). Never infer that because one variable
# (e.g. MEALS) is set, the others are too -- a partial environment must
# re-resolve, not silently default. And capture the resolver's OUTPUT and EXIT
# STATUS separately BEFORE eval, so a resolver crash/BLOCK is never swallowed by
# command substitution followed by :-true defaults.
_scope_complete=true
for _feat in SYNCHRONIZATION MEALS ONBOARDING GOOGLE_CALENDAR KIOSK_ADMIN; do
    eval "_feat_val=\${CALEE_RELEASE_FEATURE_${_feat}:-}"
    if [ -z "$_feat_val" ]; then
        _scope_complete=false
    fi
done
if [ "$_scope_complete" != true ]; then
    _scope_output="$("$CALEE_PYTHON" -m calee_regression release-feature-scope --run-id "$CALEE_RUN_ID")"
    _scope_status=$?
    if [ "$_scope_status" -ne 0 ]; then
        echo ""
        echo "BLOCKED: could not resolve the release-feature scope (exit $_scope_status) — refusing to run the mobile checks with an unknown or malformed scope."
        echo "$_scope_output" >&2
        exit "$_scope_status"
    fi
    eval "$_scope_output"
fi
export CALEE_RELEASE_FEATURE_SYNCHRONIZATION="${CALEE_RELEASE_FEATURE_SYNCHRONIZATION:-true}"
export CALEE_RELEASE_FEATURE_MEALS="${CALEE_RELEASE_FEATURE_MEALS:-true}"
export CALEE_RELEASE_FEATURE_ONBOARDING="${CALEE_RELEASE_FEATURE_ONBOARDING:-true}"
export CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR="${CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR:-true}"
export CALEE_RELEASE_FEATURE_KIOSK_ADMIN="${CALEE_RELEASE_FEATURE_KIOSK_ADMIN:-true}"
echo "[OK] Release-feature scope: sync=$CALEE_RELEASE_FEATURE_SYNCHRONIZATION meals=$CALEE_RELEASE_FEATURE_MEALS onboarding=$CALEE_RELEASE_FEATURE_ONBOARDING google_calendar=$CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR kiosk_admin=$CALEE_RELEASE_FEATURE_KIOSK_ADMIN (source: ${CALEE_RELEASE_FEATURE_SOURCE:-default})"

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
        "$CALEE_PYTHON" -m calee_regression prepare --config "$CALEE_TEST_CONFIG" --run-id "$CALEE_RUN_ID"
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
                # Guided-handoff evidence (Workstream 5): discover ONLY THIS
                # run's canonical same-run evidence paths (never a "latest"
                # file), and pass them to the serial orchestrator so the
                # aggregate binds and gates on them. A missing mandatory
                # feature's evidence is blocked downstream, never inferred.
                # ${arr[@]+"${arr[@]}"} keeps an empty array safe under `set -u`
                # on macOS bash 3.2.
                HANDOFF_ARGS=()
                ONBOARDING_EVIDENCE="$RUN_DIR/handoff/onboarding/evidence.json"
                GOOGLE_EVIDENCE="$RUN_DIR/handoff/google-calendar/evidence.json"
                if [ -f "$ONBOARDING_EVIDENCE" ]; then
                    HANDOFF_ARGS+=(--onboarding-handoff-evidence "$ONBOARDING_EVIDENCE")
                fi
                if [ -f "$GOOGLE_EVIDENCE" ]; then
                    HANDOFF_ARGS+=(--google-handoff-evidence "$GOOGLE_EVIDENCE")
                fi
                # Serial per-file orchestration (Workstream 3/4): run_ui_manifest.py
                # runs each integration-test FILE in its OWN Flutter process (a
                # physical iPhone stalls when the whole integration_test directory
                # is launched in one process), retries ONLY a confirmed
                # launch/tooling failure once, preserves EVERY attempt under
                # "$UI_DIR/files/", and writes ONE canonical aggregate platform
                # report to "$UI_REPORT" -- shaped so the consolidator consumes it
                # exactly like a single-suite report (per-file reports are
                # subordinate evidence). The ordered manifest lives in
                # CaleeMobile-Regression (run_ui_manifest.DEFAULT_MANIFEST); this
                # launcher NEVER enumerates test files itself. Both platforms use
                # the same orchestrator, so a standalone "03/04" run and "06" share
                # one implementation and iOS/Android reports can never collide.
                #
                # CALEE_MOBILE_BACKEND / CALEE_EXPECTED_BACKEND / CALEE_FIXTURE_STATUS
                # / CALEE_RELEASE_ID / CALEE_RUN_ID / CALEE_FIXTURE_VERSION are
                # exported above/by the launcher and read from the environment;
                # run_ui_manifest forwards the SAME identity + feature scope to every
                # per-file run_ui_suite.py process, which reads credentials from the
                # environment (never argv) -- the secure-credential boundary is
                # unchanged. The mobile release features (Workstream 1/5) are passed
                # explicitly (kiosk/admin is a tablet feature, handled by the
                # kiosk-admin command, not the mobile UI suite).
                # ${arr[@]+"${arr[@]}"} keeps an empty array safe under `set -u`
                # on the bash 3.2 that ships with macOS.
                (cd "$SIBLING/ui" && python3 run_ui_manifest.py \
                    --platform "$PLATFORM" \
                    ${UI_DEVICE_ARGS[@]+"${UI_DEVICE_ARGS[@]}"} \
                    ${HANDOFF_ARGS[@]+"${HANDOFF_ARGS[@]}"} \
                    --report "$UI_REPORT" \
                    --work-dir "$UI_DIR" \
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
# Each recording is snapshotted into an IMMUTABLE per-invocation directory
# (Workstream 4): re-invoking the same platform in the same run preserves every
# invocation's evidence -- a later PASS can never erase an earlier FAIL, on disk
# or in the manifest (worst-wins). The invocation id is unique per invocation
# (timestamp + this process id).
_INVOCATION_STAMP="$(date +%Y%m%dT%H%M%S)-$$"
if [ "$RUN_API" = true ]; then
    "$CALEE_PYTHON" -m calee_regression record-component --run-id "$CALEE_RUN_ID" --component mobile-api \
        --report-path "$API_REPORT" --exit-code "$API_STATUS" \
        --invocation-id "api-$_INVOCATION_STAMP" || true
fi
if [ "$RUN_UI" = true ]; then
    "$CALEE_PYTHON" -m calee_regression record-component --run-id "$CALEE_RUN_ID" --component "mobile-$PLATFORM" \
        --report-path "$UI_REPORT" --exit-code "$UI_STATUS" \
        --invocation-id "$PLATFORM-$_INVOCATION_STAMP" || true
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
