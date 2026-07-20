#!/bin/bash
# The MACHINE_* variables below are populated at run time by
# `eval "$(python3 -m calee_regression machine-config)"`, so shellcheck cannot
# see their assignment; SC2154 (referenced but not assigned) is expected here.
# shellcheck disable=SC2154
# ============================================================================
# 00 Run Calee Release Regression
# ----------------------------------------------------------------------------
# The ONE thing a nontechnical tester double-clicks. It:
#   1. reads the technical owner's machine config,
#   2. verifies the release bundle you dropped in the release folder,
#   3. installs it on the connected tablet (data-preserving),
#   4. runs the full Calee regression (delegates to "06 Test Full Calee
#      Solution", which drives Prepare -> tablet -> CaleeMobile -> sync ->
#      manual checks -> consolidation),
#   5. opens the final report.
#
# You never edit YAML/JSON/env. Every outcome is shown in plain language:
#   Ready / Installing / Testing / Passed / Failed / Blocked / Needs technical owner
#
# When a device is missing, this reports "Needs technical owner" and stops --
# it never pretends a device was present. Raw logs for the technical owner are
# under reports/<run>/ and the advanced diagnostics folder.
# ============================================================================
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

# ── plain-language status helpers ───────────────────────────────────────────
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
    printf "  Send this report to your technical owner : %s\n" "${4:-reports/latest-run/}"
}

say "Calee Release Regression"
echo "Reading your machine configuration…"

# ── 1. machine config ───────────────────────────────────────────────────────
if ! MACHINE_VARS="$(python3 -m calee_regression machine-config 2>machine_config_error.txt)"; then
    needs_owner "The machine configuration is missing or invalid." \
                "No — this is a setup problem, not a product failure." \
                "Ask your technical owner to create config/machine.local.yaml (see config/machine.local.example.yaml)." \
                "machine_config_error.txt"
    cat machine_config_error.txt
    read -r -p "Press Enter to close..." _
    exit 3
fi
eval "$MACHINE_VARS"
rm -f machine_config_error.txt
state_ready "Machine configuration loaded. Backend: ${MACHINE_BACKEND_URL}"

# ── 2. verify the release bundle ────────────────────────────────────────────
state_doing "Checking the release bundle you placed in: ${MACHINE_RELEASE_BUNDLE_DIR}" "READY"
mkdir -p reports
if ! python3 -m calee_regression verify-release-bundle \
        --bundle "$MACHINE_RELEASE_BUNDLE_DIR" \
        --report "reports/bundle-verification.json"; then
    needs_owner "The release bundle did not pass verification (see the problems listed above)." \
                "No — the bundle the technical owner supplied is malformed; nothing was installed or tested." \
                "Ask your technical owner to rebuild/re-sign the release bundle and drop it back in the folder." \
                "reports/bundle-verification.json"
    read -r -p "Press Enter to close..." _
    exit 2
fi
state_pass "Release bundle verified."

# ── 3. install on the tablet ────────────────────────────────────────────────
state_doing "Installing the release on the tablet (your data is preserved)…" "INSTALLING"
SERIAL_ARG=()
[ -n "$MACHINE_TABLET_SERIAL" ] && SERIAL_ARG=(--serial "$MACHINE_TABLET_SERIAL")
python3 -m calee_regression install-tablet-release \
    --bundle "$MACHINE_RELEASE_BUNDLE_DIR" \
    "${SERIAL_ARG[@]}" \
    --report "reports/tablet-install.json"
INSTALL_STATUS=$?
if [ $INSTALL_STATUS -eq 3 ]; then
    needs_owner "The release could not be installed on the tablet (no device, a signature mismatch, or a version/HOME mismatch)." \
                "No — this is a device/installation problem, not a Calee product failure." \
                "Connect and unlock the prepared Calee tablet, then run this again. If it still can't install, send the report." \
                "reports/tablet-install.json"
    read -r -p "Press Enter to close..." _
    exit 3
elif [ $INSTALL_STATUS -ne 0 ]; then
    needs_owner "The installer reported an unexpected problem." \
                "No — installation problem, not a product failure." \
                "Send the install report to your technical owner." \
                "reports/tablet-install.json"
    read -r -p "Press Enter to close..." _
    exit $INSTALL_STATUS
fi
state_pass "Release installed on the tablet."

# ── 4. run the full regression (delegate to 06) ─────────────────────────────
state_doing "Running the full Calee regression. This takes a while — leave it running…" "TESTING"
# "06 Test Full Calee Solution" owns the Prepare -> tablet -> mobile -> sync ->
# manual -> consolidate orchestration and already produces one consolidated
# PASS/FAIL/BLOCKED report. We delegate to it rather than duplicate it, then
# translate its exit code into plain language below.
bash "$DIR/06 Test Full Calee Solution.command" </dev/null
REGRESSION_STATUS=$?

# ── 5. plain-language final state ───────────────────────────────────────────
case $REGRESSION_STATUS in
    0)
        state_pass "The Calee release passed regression. It is good to ship."
        ;;
    1)
        state_fail "A real product problem was found. Do NOT ship. Send the report to your technical owner."
        needs_owner "One or more product checks failed." \
                    "YES — this is a genuine product failure." \
                    "Do not ship this release. Send the report." \
                    "reports/latest-run/"
        ;;
    *)
        needs_owner "Some required checks could not run (a missing device, credential, or fixture)." \
                    "No — these are environment/setup blockers, not proven product failures." \
                    "Make sure the tablet AND iPhone are connected and prepared, then run this again." \
                    "reports/latest-run/"
        ;;
esac

# ── 6. open the report ──────────────────────────────────────────────────────
echo ""
echo "Opening the final report…"
bash "$DIR/07 Open Latest Report.command" </dev/null || true

read -r -p "Press Enter to close..." _
exit $REGRESSION_STATUS
