#!/bin/bash
# ============================================================================
# 08 Resume Blocked Release
# ----------------------------------------------------------------------------
# Resumes an EXISTING, previously-blocked release run instead of starting a
# new one from scratch. It never repeats an already-passed destructive or
# disruptive step (reinstalling the tablet, rebooting it) unless the resume
# validator has independently decided that step can no longer be trusted.
#
# It:
#   1. lists every existing run under reports/runs/ and requires an EXPLICIT
#      choice -- it never guesses or auto-picks the newest run;
#   2. checks (read-only) whether the chosen run can be resumed at all --
#      immutable release/build inputs (release ID, candidate fingerprint,
#      APK digests, expected identities, backend, profile, scope, git SHAs,
#      tablet stable identity) must still match the ORIGINAL attempt;
#   3. if resumable, resumes it: a previously-passed installation is reused
#      (no reinstall, no reboot) only after a bounded, read-only tablet
#      recheck; a blocked Prepare (environment + fixture) is rerun; every
#      other component is decided (reused / needs execution) and recorded;
#   4. installs the release ONLY if the resume decided installation can no
#      longer be reused;
#   5. continues the SAME run through "06 Test Full Calee Solution" for
#      whatever still needs to run, and opens the final consolidated report.
#
# If the run cannot be resumed (an immutable input changed, or the tablet/
# installed package identity no longer matches), this stops and tells you a
# new release run is required -- there is no way to force it anyway.
# ============================================================================
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

say()          { printf "\n%s\n" "····································································"; printf "  %s\n" "$1"; printf "%s\n" "····································································"; }
state_ready()  { printf "\n[ READY ]  %s\n" "$1"; }
state_doing()  { printf "\n[ %s ]  %s\n" "$2" "$1"; }
state_pass()   { printf "\n[ PASSED ]  %s\n" "$1"; }
state_fail()   { printf "\n[ FAILED ]  %s\n" "$1"; }
state_block()  { printf "\n[ BLOCKED ]  %s\n" "$1"; }
needs_owner()  {
    printf "\n[ NEEDS TECHNICAL OWNER ]\n"
    printf "  What could not run : %s\n" "$1"
    printf "  Is this a product failure? : %s\n" "$2"
    printf "  What you can do now : %s\n" "$3"
    printf "  Send this report to your technical owner : %s\n" "${4:-${CALEE_REPORT_ROOT:-.}/reports/latest-run/}"
}

say "Resume a Blocked Release Qualification"
echo "This does NOT start a new release run -- it continues one you select"
echo "below, reusing whatever already passed and only re-running what is"
echo "blocked, not yet run, or no longer trustworthy."

# ── 0. environment bootstrap (venv + dependencies) ──────────────────────────
# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    needs_owner "The test environment could not be set up on this Mac." \
                "No — this is a one-time setup problem, not a product failure." \
                "Ask your technical owner to complete the one-time setup (see docs/SETUP_MAC.md)." \
                "reports/setup.log"
    read -r -p "Press Enter to close..." _
    exit $BOOTSTRAP_STATUS
fi

# ── 0.5. report root (must agree with every other launcher) ─────────────────
if [ -z "${CALEE_REPORT_ROOT:-}" ]; then
    if ! CALEE_REPORT_ROOT="$("$CALEE_PYTHON" -m calee_regression report-root)"; then
        needs_owner "The configured report root could not be resolved." \
                    "No — this is a setup/configuration problem, not a product failure." \
                    "Ask your technical owner to check config/machine.local.yaml's report_dir (or the CALEE_REPORT_ROOT environment variable)." \
                    "reports/setup.log"
        read -r -p "Press Enter to close..." _
        exit 3
    fi
fi
export CALEE_REPORT_ROOT

# ── 1. list every existing run and require an explicit choice ───────────────
say "Choose a run to resume"
SELECTION_FILE="$(mktemp)"
"$CALEE_PYTHON" -m calee_regression select-run-to-resume --config "$CALEE_TEST_CONFIG" --out-file "$SELECTION_FILE"
SELECTED_RUN_ID=""
[ -s "$SELECTION_FILE" ] && SELECTED_RUN_ID="$(cat "$SELECTION_FILE")"
rm -f "$SELECTION_FILE"

if [ -z "$SELECTED_RUN_ID" ]; then
    echo ""
    echo "Cancelled -- nothing was resumed."
    read -r -p "Press Enter to close..." _
    exit 0
fi
export CALEE_RUN_ID="$SELECTED_RUN_ID"
echo ""
echo "Selected run: $CALEE_RUN_ID"

# ── 2. read-only resumability check BEFORE touching anything ────────────────
state_doing "Checking whether this run can be resumed…" "CHECKING"
"$CALEE_PYTHON" -m calee_regression inspect-resume --run-id "$CALEE_RUN_ID" --config "$CALEE_TEST_CONFIG"
INSPECT_STATUS=$?
if [ $INSPECT_STATUS -ne 0 ]; then
    state_block "This run cannot be resumed."
    needs_owner "One or more of the release's immutable inputs (release ID, candidate fingerprint, APK digests, expected identities, backend, profile, scope, git SHAs, or the tablet's own identity) no longer match the original attempt." \
                "No — this is a safety refusal, not a product failure." \
                "Start a NEW release run instead ('00 Run Calee Release Regression') -- this one cannot be safely continued." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/"
    read -r -p "Press Enter to close..." _
    exit $INSPECT_STATUS
fi
state_ready "This run can be resumed."

# ── 3. resume: reuse what's still valid, rerun Prepare if it was blocked ────
state_doing "Resuming the run (reusing already-passed evidence where safe)…" "RESUMING"
RESUME_REPORT="$(mktemp)"
"$CALEE_PYTHON" -m calee_regression resume-release --run-id "$CALEE_RUN_ID" --config "$CALEE_TEST_CONFIG" --report "$RESUME_REPORT"
RESUME_STATUS=$?

if [ $RESUME_STATUS -eq 3 ]; then
    state_block "Resume was refused."
    needs_owner "The resume validator refused to continue this run (see the detail above)." \
                "No — this is a safety refusal, not a product failure." \
                "Start a NEW release run instead ('00 Run Calee Release Regression')." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/"
    rm -f "$RESUME_REPORT"
    read -r -p "Press Enter to close..." _
    exit $RESUME_STATUS
fi
state_pass "Resume decision recorded (see components above: REUSED PASS / REQUIRES EXECUTION / REFUSED)."

# ── 4. install ONLY if the resume decided installation cannot be reused ─────
# Never reinstalls or reboots the tablet until this decision says so.
INSTALL_NEEDED="$(python3 - "$RESUME_REPORT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
components_needing_install = {"installation"}
executed = {e.get("component") for e in data.get("componentsExecuted", [])}
refused = {e.get("component") for e in data.get("componentsRefused", [])}
print("yes" if components_needing_install & (executed | refused) else "no")
PY
)"
rm -f "$RESUME_REPORT"

if [ "$INSTALL_NEEDED" = "yes" ]; then
    state_doing "Installation must be (re-)verified — this may reinstall/reboot the tablet…" "INSTALLING"
    if ! MACHINE_VARS="$("$CALEE_PYTHON" -m calee_regression machine-config-snapshot --run-id "$CALEE_RUN_ID" 2>machine_config_error.txt)"; then
        needs_owner "The machine configuration is missing or invalid." \
                    "No — this is a setup problem, not a product failure." \
                    "Ask your technical owner to fix config/machine.local.yaml." \
                    "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/machine-config/results.json"
        cat machine_config_error.txt
        rm -f machine_config_error.txt
        read -r -p "Press Enter to close..." _
        exit 3
    fi
    eval "$MACHINE_VARS"
    rm -f machine_config_error.txt
    SERIAL_ARG=""
    [ -n "$MACHINE_TABLET_SERIAL" ] && SERIAL_ARG="--serial $MACHINE_TABLET_SERIAL"
    # shellcheck disable=SC2086
    "$CALEE_PYTHON" -m calee_regression install-tablet-release \
        --bundle "$MACHINE_RELEASE_BUNDLE_DIR" \
        $SERIAL_ARG \
        --run-id "$CALEE_RUN_ID"
    INSTALL_STATUS=$?
    if [ $INSTALL_STATUS -ne 0 ]; then
        state_block "The release could not be installed."
        needs_owner "The release could not be (re-)installed on the tablet (see the installation report)." \
                    "No — this is an installation/environment blocker, not a proven product failure." \
                    "Check the tablet is connected, unlocked, and awake, then run this again." \
                    "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/installation/results.json"
        read -r -p "Press Enter to close..." _
        exit $INSTALL_STATUS
    fi
    state_pass "Release installed on the tablet and verified."
else
    state_ready "Installation: REUSED PASS (no reinstall, no reboot)."
fi

# ── 5. continue the SAME run through "06" for whatever still needs to run ───
state_doing "Continuing the release qualification. This takes a while — leave it running…" "TESTING"
# No `</dev/null` -- the delegated launcher contains mandatory interactive
# manual checks; the tester's terminal input must reach them.
bash "$DIR/06 Test Full Calee Solution.command"
REGRESSION_STATUS=$?

case $REGRESSION_STATUS in
    0)
        state_pass "The Calee release passed regression. It is good to ship."
        ;;
    1)
        state_fail "A real product problem was found. Do NOT ship. Send the report to your technical owner."
        needs_owner "One or more product checks failed." \
                    "YES — this is a genuine product failure." \
                    "Do not ship this release. Send the report." \
                    "$CALEE_REPORT_ROOT/reports/latest-run/"
        ;;
    *)
        needs_owner "Some required checks could not run (a missing device, credential, fixture, or installation)." \
                    "No — these are environment/setup blockers, not proven product failures." \
                    "Make sure the tablet AND iPhone are connected and prepared, then run this again." \
                    "$CALEE_REPORT_ROOT/reports/latest-run/"
        ;;
esac

echo ""
echo "Opening the final report…"
bash "$DIR/07 Open Latest Report.command" </dev/null || true

read -r -p "Press Enter to close..." _
exit $REGRESSION_STATUS
