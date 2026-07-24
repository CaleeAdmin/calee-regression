#!/bin/bash
# The MACHINE_* variables below are populated at run time by
# `eval "$("$CALEE_PYTHON" -m calee_regression machine-config-snapshot ...)"`, whose
# assignment the linter cannot see; SC2154 (referenced but not assigned) is
# expected here and disabled on the next line.
# shellcheck disable=SC2154
# ============================================================================
# 00 Run Calee Release Regression
# ----------------------------------------------------------------------------
# The ONE thing a nontechnical tester double-clicks. It:
#   1. creates ONE release run ID up front (before any verification),
#   2. loads the technical owner's machine config as the single authoritative
#      source and records a secrets-excluded snapshot into the run,
#   3. verifies the release bundle (manifest schema, checksums, Git SHAs) --
#      WITHOUT touching any device,
#   4. composes the ONE effective release configuration (folding the verified
#      bundle manifest into it -- schema v2 is authoritative for scope; schema
#      v1 is cross-checked against config/release-platforms.yaml) -- also
#      WITHOUT touching any device. NO `adb install`, reboot, HOME mutation or
#      product test may occur before this gate succeeds.
#   5. installs the release bundle on the connected tablet (verifying the actual
#      APK contents + signer first), recording all installer evidence INTO the
#      same run,
#   6. runs the full Calee regression under the SAME run (delegates to "06 Test
#      Full Calee Solution", inheriting the run ID and CONSUMING -- never
#      recomposing -- this run's release-config evidence),
#   7. opens the final consolidated report.
#
# You never edit YAML/JSON/env. Every outcome is shown in plain language:
#   Ready / Verifying / Configuring / Installing / Testing / Passed / Failed /
#   Blocked / Needs technical owner
#
# When a device is missing, this reports "Needs technical owner" and stops --
# it never pretends a device was present. All evidence for the technical owner
# is under reports/runs/<run-id>/.
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
    printf "  Send this report to your technical owner : %s\n" "${4:-${CALEE_REPORT_ROOT:-.}/reports/latest-run/}"
}

# Priority 7: even when an early gate (an invalid bundle, a machine-config
# problem) stops the run before the full regression ("06"), STILL produce ONE
# consolidated report. `consolidate` auto-discovers the components recorded so
# far, marks every downstream component as not-run because of the gate, refreshes
# reports/latest-run, and we open it. Echoes the consolidated exit code (the
# consolidated PASS/FAIL/BLOCKED status) so the caller exits with it.
consolidate_gate() {
    # Safe, read-only identity evidence where possible -- never a product test.
    "$CALEE_PYTHON" -m calee_regression build-identity --run-id "$CALEE_RUN_ID" --phase pre >/dev/null 2>&1 || true
    "$CALEE_PYTHON" -m calee_regression build-identity --run-id "$CALEE_RUN_ID" --phase post >/dev/null 2>&1 || true
    # Mirror 06's mandatory-component flags for whatever evidence exists, and
    # allow-unknown build identity (an early gate cannot collect the full build
    # identity). The consolidated status is driven by the recorded component(s):
    # a BLOCKED/INVALID installation or machine-config yields a BLOCKED report.
    local args=(--run-id "$CALEE_RUN_ID" --allow-unknown-build-identity)
    [ -f "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/machine-config/results.json" ] && args+=(--machine-config-mandatory)
    [ -f "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config/results.json" ] && args+=(--release-config-mandatory)
    [ -f "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/installation/results.json" ] && args+=(--installation-mandatory)
    "$CALEE_PYTHON" -m calee_regression consolidate "${args[@]}"
    local status=$?
    echo ""
    echo "Opening the final report…"
    bash "$DIR/07 Open Latest Report.command" </dev/null || true
    return $status
}

say "Calee Release Regression"

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

# ── 0.5. report root (Priority 3) ────────────────────────────────────────────
# Resolve the ONE canonical report root for this ENTIRE run -- the
# CALEE_REPORT_ROOT environment variable if already set, else
# config/machine.local.yaml's report_dir, else this repo's own reports/
# directory -- BEFORE a single file is written, and export it so every
# downstream process (every delegated calee_regression subcommand, "06",
# scripts/test_caleemobile.sh, "07 Open Latest Report") agrees on exactly
# where evidence lives. An unsafe/unwritable configured root BLOCKS here
# rather than silently falling back. See calee_regression/report_root.py.
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

# ── 1. ONE run ID, created BEFORE any release verification (Priority 6) ──────
# Everything below -- the machine-config snapshot, bundle verification, APK
# inspection, installation, and the full regression delegated to "06" -- writes
# into $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/. There is no second run
# ID created later.
CALEE_RUN_ID="release-$(date +%Y%m%d-%H%M%S)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
export CALEE_RUN_ID
mkdir -p "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID"
echo "Run ID: $CALEE_RUN_ID"
echo "Report root: $CALEE_REPORT_ROOT"
echo "Workspace: $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/"

# ── 2. machine config = the single authoritative source (Priority 4) ─────────
echo ""
echo "Reading your machine configuration…"
if ! MACHINE_VARS="$("$CALEE_PYTHON" -m calee_regression machine-config-snapshot --run-id "$CALEE_RUN_ID" 2>machine_config_error.txt)"; then
    needs_owner "The machine configuration is missing or invalid (or contains a secret it must not)." \
                "No — this is a setup problem, not a product failure." \
                "Ask your technical owner to fix config/machine.local.yaml (see config/machine.local.example.yaml)." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/machine-config/results.json"
    cat machine_config_error.txt
    rm -f machine_config_error.txt
    # Priority 7: the machine-config gate blocked, but still consolidate the
    # BLOCKED machine-config evidence (downstream marked not-run) into ONE report.
    consolidate_gate
    CONSOLIDATED_STATUS=$?
    read -r -p "Press Enter to close..." _
    exit $CONSOLIDATED_STATUS
fi
eval "$MACHINE_VARS"
rm -f machine_config_error.txt
# The reconciled effective config drives EVERY downstream command, so machine
# config actually controls execution with no second, conflicting source.
export CALEE_TEST_CONFIG="$MACHINE_EFFECTIVE_CONFIG"
export CALEE_EXPECTED_BACKEND="$MACHINE_BACKEND_URL"
export CALEE_API_BASE="$MACHINE_BACKEND_URL"
if [ "$MACHINE_ALLOW_CALEESHELL_TECHNICAL" = "true" ]; then
    export CALEE_CONFIRM_TECHNICAL=1
fi
# Priority 4: the configured mobile device ids control which iPhone/Android the
# UI suite drives (scripts/test_caleemobile.sh -> run_ui_suite.py --device-id),
# so a machine with more than one device targets the CONFIGURED one instead of
# guessing. Empty values fall back to run_ui_suite.py's single-device resolution.
[ -n "${MACHINE_IPHONE_DEVICE:-}" ] && export CALEE_IPHONE_DEVICE="$MACHINE_IPHONE_DEVICE"
[ -n "${MACHINE_ANDROID_DEVICE:-}" ] && export CALEE_ANDROID_DEVICE="$MACHINE_ANDROID_DEVICE"
state_ready "Machine configuration loaded (authoritative). Backend: ${MACHINE_BACKEND_URL}"

# ── 3. verify the release bundle — NO device touch (Priority 1) ──────────────
# Manifest schema (v1 or v2), full Git SHAs, package ids, version formats, APK
# existence, SHA-256 checksums, no unexpected/traversal files -- entirely
# offline. This runs BEFORE release configuration is composed and BEFORE any
# `adb` command, so a malformed bundle is caught with a clear, separate
# diagnosis instead of surfacing later as an installer or config failure.
state_doing "Verifying the release bundle (no device touched yet)…" "VERIFYING"
mkdir -p "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config"
"$CALEE_PYTHON" -m calee_regression verify-release-bundle \
    --bundle "$MACHINE_RELEASE_BUNDLE_DIR" \
    --report "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config/bundle-verification.json"
BUNDLE_VERIFY_STATUS=$?

if [ $BUNDLE_VERIFY_STATUS -ne 0 ]; then
    # Nothing was installed and no release configuration was composed -- STILL
    # produce ONE consolidated BLOCKED report (Priority 7).
    needs_owner "The release bundle failed verification (manifest schema, checksums, or Git SHAs)." \
                "No — the bundle the technical owner supplied is malformed; nothing was installed or tested." \
                "Ask your technical owner to rebuild/re-sign the release bundle and drop it back in the folder." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config/bundle-verification.json"
    consolidate_gate
    CONSOLIDATED_STATUS=$?
    read -r -p "Press Enter to close..." _
    exit $CONSOLIDATED_STATUS
fi
state_pass "Release bundle verified."

# ── 4. compose the effective release configuration — NO device touch ────────
# Folds the (already-verified) release bundle manifest into the ONE effective
# release configuration for this run: schema v2 makes the bundle itself
# authoritative for platform/feature scope, profile, backend and expected
# identity; schema v1 cross-checks the bundle against config/release-
# platforms.yaml and BLOCKS on disagreement. Writes the full pre-install
# identity comparison matrix to reports/runs/$CALEE_RUN_ID/release-config/
# results.json. Launcher "06" below CONSUMES this same evidence -- it never
# recomposes a second, possibly-different configuration for this run.
state_doing "Composing the effective release configuration…" "CONFIGURING"
if RELEASE_CFG_OUT="$("$CALEE_PYTHON" -m calee_regression release-config --bundle "$MACHINE_RELEASE_BUNDLE_DIR" --run-id "$CALEE_RUN_ID" 2>release_config_error.txt)"; then
    RELEASE_CFG_STATUS=0
else
    RELEASE_CFG_STATUS=$?
fi
eval "$RELEASE_CFG_OUT" 2>/dev/null || true

if [ $RELEASE_CFG_STATUS -ne 0 ]; then
    # Priority 1, requirement 6: release configuration blocked -- execute NO
    # `adb install`, NO reboot, NO HOME mutation, NO product tests. Installation
    # is simply never attempted (never recorded), so consolidate's auto-
    # discovery correctly shows it as not-run because this gate blocked. STILL
    # produce ONE consolidated BLOCKED report (Priority 7), update
    # reports/latest-run, and open the final report.
    cat release_config_error.txt
    rm -f release_config_error.txt
    needs_owner "The machine and release configurations conflict (or the schema-1 bundle disagrees with config/release-platforms.yaml)." \
                "No — this is a setup/configuration problem, not a proven product failure." \
                "Ask your technical owner to check config/machine.local.yaml and config/release-platforms.yaml against the release bundle -- see the release-config report." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config/results.json"
    consolidate_gate
    CONSOLIDATED_STATUS=$?
    read -r -p "Press Enter to close..." _
    exit $CONSOLIDATED_STATUS
fi
rm -f release_config_error.txt
state_pass "Effective release configuration composed."

# ── 4.5. acquire exact-identity release evidence — NO device touch ──────────
# Derives every expected identity from the just-verified bundle + effective
# configuration, finds the EXACT matching GitHub Actions evidence (never "the
# latest successful run"), authenticates each artifact against its workflow
# run and GitHub-recorded digest, and caches it under
# reports/runs/$CALEE_RUN_ID/evidence/. Fail-closed: missing credentials or
# missing/ambiguous evidence BLOCKS here with a plain-language remediation --
# no token or API detail is ever shown or logged.
state_doing "Finding exact CI evidence…" "VERIFYING"
echo "  Authenticating selector evidence…"
echo "  Authenticating Android build evidence…"
echo "  Authenticating iOS build evidence…"
"$CALEE_PYTHON" -m calee_regression acquire-release-evidence \
    --bundle "$MACHINE_RELEASE_BUNDLE_DIR" \
    --run-id "$CALEE_RUN_ID"
EVIDENCE_STATUS=$?

if [ $EVIDENCE_STATUS -ne 0 ]; then
    state_block "The release evidence could not be authenticated (see the acquisition manifest)."
    needs_owner "The exact CI/selector/build evidence for this release could not be found or authenticated." \
                "No — evidence is missing, ambiguous, or GitHub authentication is not set up; nothing was installed or tested." \
                "Ask your technical owner to check the acquisition manifest: GitHub authentication may be missing, the merged-main CI run for this exact version may not exist yet, selector certification may not have been run for this release, or App Store Connect / Play Console evidence may be unavailable." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/evidence/acquisition-manifest.json"
    consolidate_gate
    CONSOLIDATED_STATUS=$?
    read -r -p "Press Enter to close..." _
    exit $CONSOLIDATED_STATUS
fi
state_pass "Release evidence found and authenticated."

# ── 5. install the release into the SAME run (Priority 1/5/6) ────────────────
# Only reached once BOTH the bundle verification AND release configuration
# gates above have passed. inspect actual APK contents + signer -> read-only
# tablet inspection -> ordered data-preserving install. Every piece of
# installer evidence is written to reports/runs/$CALEE_RUN_ID/installation/.
state_doing "Installing the release on the tablet (your data is preserved)…" "INSTALLING"
SERIAL_ARG=()
[ -n "$MACHINE_TABLET_SERIAL" ] && SERIAL_ARG=(--serial "$MACHINE_TABLET_SERIAL")
"$CALEE_PYTHON" -m calee_regression install-tablet-release \
    --bundle "$MACHINE_RELEASE_BUNDLE_DIR" \
    "${SERIAL_ARG[@]}" \
    --run-id "$CALEE_RUN_ID"
INSTALL_STATUS=$?

if [ $INSTALL_STATUS -eq 2 ]; then
    # A malformed/mislabelled bundle: nothing was installed, and running the
    # regression against whatever is on the tablet would be misleading. Do NOT
    # run product tests -- but STILL produce ONE consolidated report (Priority 7):
    # the INVALID/BLOCKED installation evidence is already recorded, and
    # consolidate marks every downstream component not-run because of this gate.
    needs_owner "The release bundle failed verification or its actual APK contents/signer did not match the manifest." \
                "No — the bundle the technical owner supplied is malformed; nothing was installed or tested." \
                "Ask your technical owner to rebuild/re-sign the release bundle and drop it back in the folder." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/installation/results.json"
    consolidate_gate
    CONSOLIDATED_STATUS=$?
    read -r -p "Press Enter to close..." _
    exit $CONSOLIDATED_STATUS
fi
if [ $INSTALL_STATUS -eq 0 ]; then
    state_pass "Release installed on the tablet and verified."
else
    # BLOCKED (no device, missing SDK tools, signer trust could not be
    # established, version/HOME mismatch, or any other installation-time
    # problem): the installation evidence is already recorded into the run.
    # Priority 1: installation is a PRE-PRODUCT gate -- do NOT delegate to "06"
    # (never run the tablet/mobile/sync/kiosk/manual product checks) against a
    # release that never finished installing. Still produce ONE consolidated
    # report (Priority 7): the BLOCKED installation evidence is already
    # recorded, and consolidate marks every downstream component not-run
    # because of this gate.
    state_block "The release could not be installed (see the installation report)."
    needs_owner "The release could not be installed on the tablet (see the installation report)." \
                "No — this is an installation/environment blocker (missing device, missing SDK tool, signer trust, or a version/HOME mismatch), not a proven product failure." \
                "Check the tablet is connected, unlocked, and awake, then run this again. If it keeps happening, ask your technical owner to check the installation report." \
                "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/installation/results.json"
    consolidate_gate
    CONSOLIDATED_STATUS=$?
    read -r -p "Press Enter to close..." _
    exit $CONSOLIDATED_STATUS
fi

# ── 6. run the full regression under the SAME run (delegate to 06) ───────────
state_doing "Running the full Calee regression. This takes a while — leave it running…" "TESTING"
# "06 Test Full Calee Solution" owns Prepare -> tablet -> mobile -> sync ->
# kiosk -> manual -> consolidate, INHERITS $CALEE_RUN_ID (so the release-config,
# installation and machine-config evidence recorded above are consolidated in
# the SAME run), CONSUMES (never recomposes) this run's release-config
# evidence, and produces one consolidated PASS/FAIL/BLOCKED report.
#
# Priority 2: NO `</dev/null` here. The delegated launcher contains mandatory
# interactive manual checks; the tester's terminal input must reach them. An
# EOF'd stdin would let those checks pass without being answered (a false PASS),
# so this launcher inherits the real terminal stdin instead.
bash "$DIR/06 Test Full Calee Solution.command"
REGRESSION_STATUS=$?

# ── 7. plain-language final state ───────────────────────────────────────────
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

# ── 8. open the report ──────────────────────────────────────────────────────
echo ""
echo "Opening the final report…"
bash "$DIR/07 Open Latest Report.command" </dev/null || true

read -r -p "Press Enter to close..." _
exit $REGRESSION_STATUS
