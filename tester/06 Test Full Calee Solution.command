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

# One run ID for this entire release run, shared by every component (Prepare,
# tablet, CaleeMobile API/UI, manual checks, consolidation). Every component
# writes to a fixed path inside reports/runs/$CALEE_RUN_ID/ -- never a
# timestamped directory a later step has to rediscover by listing and sorting,
# and never a shared always-overwritten file another run could be racing
# against. See calee_regression/run_context.py.
#
# Priority 6: when the one-button launcher ("00 Run Calee Release Regression")
# already created the run ID and recorded the machine-config snapshot +
# installation evidence under it, we INHERIT that run ID here (never mint a
# second one) so the whole release -- installation included -- lives in ONE
# workspace. Run standalone, we generate one.
CALEE_RUN_ID="${CALEE_RUN_ID:-release-$(date +%Y%m%d-%H%M%S)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')}"
export CALEE_RUN_ID
echo "Run ID: $CALEE_RUN_ID"
echo "Workspace: reports/runs/$CALEE_RUN_ID/"
echo ""

# Determine which platforms this release actually includes (technical-owner
# config/release-platforms.yaml; defaults to "every platform" if absent --
# see release_platforms.py). Never silently narrowed by what happens to be
# convenient to run right now.
eval "$(python -m calee_regression release-platforms)"

# Priority 3/4: when a machine config exists, compose the ONE effective release
# configuration (machine + release candidate) and let it drive this launcher --
# so the MACHINE's platform scope + device ids reach 06, not just the release
# candidate's. The composed RELEASE_PLATFORM_* (machine capability ∩ release
# scope) overrides the release-platforms-only values above, the configured
# iPhone/Android device ids are exported for the UI suite, and a machine/release
# conflict BLOCKS (recorded in the run's release-config evidence). Absent a
# machine config (CI/example), the release-platforms defaults above stand.
if [ -f config/machine.local.yaml ]; then
    if RELEASE_CFG_OUT="$(python -m calee_regression release-config --run-id "$CALEE_RUN_ID" 2>/dev/null)"; then
        RELEASE_CFG_STATUS=0
    else
        RELEASE_CFG_STATUS=$?
    fi
    # Apply the composed platform scope + device ids regardless (the emitted
    # values are the safe machine∩release intersection).
    eval "$RELEASE_CFG_OUT" 2>/dev/null || true
    [ -n "${RELEASE_IPHONE_DEVICE:-}" ] && export CALEE_IPHONE_DEVICE="$RELEASE_IPHONE_DEVICE"
    [ -n "${RELEASE_ANDROID_DEVICE:-}" ] && export CALEE_ANDROID_DEVICE="$RELEASE_ANDROID_DEVICE"
    if [ "$RELEASE_CFG_STATUS" -ne 0 ]; then
        echo ""
        echo "BLOCKED: the machine and release-candidate configurations conflict —"
        echo "see reports/runs/$CALEE_RUN_ID/release-config/results.json. Continuing to"
        echo "produce ONE consolidated BLOCKED report."
    fi
fi

echo ""
echo "--- Step 1: Prepare Test Environment (incl. Appium) ---"
python -m calee_regression prepare --config "$CALEE_TEST_CONFIG" --suite tablet-full --run-id "$CALEE_RUN_ID"
PREPARE_STATUS=$?

echo ""
echo "--- Collecting pre-run build identity ---"
# Phase 4: capture which builds are about to be tested BEFORE any test runs and
# save it to reports/runs/$CALEE_RUN_ID/identity/pre.json. A matching post.json
# is captured after testing (below); consolidate BLOCKS when an in-scope app's
# identity changed during the run (e.g. the CaleeMobile SHA or the installed
# tablet package changed mid-run). Never collected only at consolidation time.
#
# This step reads local git/adb state only -- it runs no product test -- so it
# is safe to collect even on a fail-fast BLOCKED run, and the consolidated
# bundle needs BOTH the pre and post snapshot (exactly one is "incomplete
# capture", which BLOCKS); see consolidated_report.component_from_identity_stability.
python -m calee_regression build-identity --run-id "$CALEE_RUN_ID" --phase pre >/dev/null

# Phase 1: fail fast when Prepare did not succeed.
#
# Prepare has no concept of a product FAIL -- it exits 0 (READY) or non-zero
# (BLOCKED: Appium unavailable, preflight error, missing fixture credentials,
# fixture reset/verify failure, ...). When it is NOT ready, the environment is
# not in a known-good state, so running the tablet, Client API, Android UI,
# iOS UI, synchronization or manual functional checks now would either fail for
# environmental reasons (pure noise) or -- worse -- assert against an
# unprepared/unverified fixture and be mistaken for a product result. We
# therefore skip EVERY downstream functional test command, preserve the
# environment report Prepare already wrote (reports/runs/$CALEE_RUN_ID/
# environment/results.json -- nothing below overwrites it), collect only the
# safe build identity above/below, and still produce ONE consolidated BLOCKED
# bundle so the release has an auditable record of exactly why it stopped.
if [ "$PREPARE_STATUS" -eq 0 ]; then
    echo ""
    echo "--- Step 1.5: Provision the today-relative subscribed calendar fixture ---"
    # Priority 6: resolve ONE date for the run, generate the today-relative
    # subscribed ICS, provision it through the AUTHENTICATED regression endpoint
    # (never an unauthenticated reset), record evidence under this run, and make
    # the generated event titles available to the tablet scenario as
    # ${REG_SUB_*} variables. Without a hub backend this records BLOCKED and is
    # never faked; it never blocks the run on its own (the subscribed scenario
    # is draft-unverified). CALEE_HUB_BASE selects the endpoint when present.
    python -m calee_regression prepare-subscribed-fixture --run-id "$CALEE_RUN_ID"

    echo ""
    echo "--- Step 2: Calee Tablet ---"
    python -m calee_regression suite --config "$CALEE_TEST_CONFIG" --suite full-tester --run-id "$CALEE_RUN_ID"

    echo ""
    echo "--- Step 2.5: CaleeMobile selector-contract gate (BEFORE any mobile functional test) ---"
    # Priority 1: a release must never ship CaleeMobile while its selector proof
    # is for a DIFFERENT build. Before the CaleeMobile Client API, the Android/iOS
    # UI, or cross-device sync run, obtain (or generate) machine-readable selector
    # evidence for the EXACT release SHA+version, validate it against the hardened
    # schema, and record it at reports/runs/$CALEE_RUN_ID/selector-contract/
    # results.json. The gate BLOCKS (exit 3) when evidence is missing, unreadable,
    # malformed, stale, for another SHA/version, produced with the wrong Flutter
    # version, not PASS, or reporting any missing selector -- and then the mobile
    # functional legs below are SKIPPED, so the consolidated bundle can never read
    # as a PASS without valid selector evidence for the build being released.
    #
    # The expected identity falls back to config/release-platforms.yaml and then
    # the detected CaleeMobile checkout; a technical owner can pin it via
    # CALEEMOBILE_EXPECTED_GIT_SHA/CALEEMOBILE_EXPECTED_BUILD_VERSION, or supply a
    # downloaded CI artifact via CALEEMOBILE_SELECTOR_EVIDENCE (else it generates
    # locally from the sibling CaleeMobile-Regression + CaleeMobile checkouts).
    SELECTOR_ARGS=(--run-id "$CALEE_RUN_ID" --mandatory)
    [ -n "${CALEEMOBILE_EXPECTED_GIT_SHA:-}" ] && SELECTOR_ARGS+=(--expected-sha "$CALEEMOBILE_EXPECTED_GIT_SHA")
    [ -n "${CALEEMOBILE_EXPECTED_BUILD_VERSION:-}" ] && SELECTOR_ARGS+=(--expected-version "$CALEEMOBILE_EXPECTED_BUILD_VERSION")
    [ -n "${CALEEMOBILE_SELECTOR_EVIDENCE:-}" ] && SELECTOR_ARGS+=(--source "$CALEEMOBILE_SELECTOR_EVIDENCE")
    python -m calee_regression selector-contract "${SELECTOR_ARGS[@]}"
    SELECTOR_GATE_STATUS=$?

    if [ "$SELECTOR_GATE_STATUS" -eq 0 ]; then
    echo ""
    echo "--- Step 3: CaleeMobile Client API (device-independent — run once) ---"
    # The Client API suite is device-independent, so it runs EXACTLY ONCE for the
    # whole release, never once per platform. The Android and iOS steps below run
    # the UI ONLY (--ui-only), so neither can re-run or overwrite this run's one
    # reports/runs/$CALEE_RUN_ID/mobile-api/results.json. An initial API result
    # therefore stands for the whole release; see scripts/test_caleemobile.sh and
    # Phase 3.
    # Priority 5: the Bash mobile orchestration (and run_regression.py /
    # run_ui_suite.py underneath it) receives CALEE_TEST_EMAIL / CALEE_TEST_
    # PASSWORD through the single secure credential boundary -- resolved once
    # from the environment OR the macOS Keychain and placed only in the child
    # environment, never on a command line. A Keychain-only technical owner does
    # not have to export the credentials for the mobile suites to run.
    python3 -m calee_regression run-with-credentials -- bash scripts/test_caleemobile.sh api-only

    if [ "$RELEASE_PLATFORM_ANDROID" = "true" ]; then
        echo ""
        echo "--- Step 4: CaleeMobile Android UI ---"
        python3 -m calee_regression run-with-credentials -- bash scripts/test_caleemobile.sh android --ui-only
    else
        echo ""
        echo "--- Step 4: CaleeMobile Android UI — SKIPPED (not part of this release; see config/release-platforms.yaml) ---"
    fi

    if [ "$RELEASE_PLATFORM_IOS" = "true" ]; then
        echo ""
        echo "--- Step 5: CaleeMobile iPhone UI ---"
        python3 -m calee_regression run-with-credentials -- bash scripts/test_caleemobile.sh ios --ui-only
    else
        echo ""
        echo "--- Step 5: CaleeMobile iPhone UI — SKIPPED (not part of this release; see config/release-platforms.yaml) ---"
    fi

    echo ""
    echo "--- Step 6: Cross-device synchronization ---"
    # Sync runs AFTER the mobile UI legs and BEFORE manual checks. It reuses
    # this run's verified backend + regression fixture + credentials and the
    # same CALEE_RUN_ID (the sync-smoke command reads the prepared-and-verified
    # backend from this run's environment report), driving the mobile legs on
    # ONE in-scope CaleeMobile platform -- Android preferred, else iOS. It writes
    # reports/runs/$CALEE_RUN_ID/sync/results.json, which consolidate
    # auto-discovers and, for a full Calee solution release, gates on: sync
    # defaults to MANDATORY (config/release-platforms.yaml
    # release_features.synchronization). A missing, stale, run-ID-mismatched,
    # BLOCKED or FAILED mandatory sync can never read as a release PASS.
    if [ "$RELEASE_PLATFORM_ANDROID" = "true" ]; then
        SYNC_PLATFORM="android"
    elif [ "$RELEASE_PLATFORM_IOS" = "true" ]; then
        SYNC_PLATFORM="ios"
    else
        # No in-scope CaleeMobile platform to drive the sync mobile legs. A
        # mandatory sync then BLOCKS (it has no mobile surface to verify
        # against); the sync-smoke command records that explicitly.
        SYNC_PLATFORM="none"
    fi
    SYNC_ARGS=(--config "$CALEE_TEST_CONFIG" --run-id "$CALEE_RUN_ID" --platform "$SYNC_PLATFORM")
    if [ "$RELEASE_FEATURE_SYNCHRONIZATION" = "false" ]; then
        SYNC_ARGS+=(--optional)
    else
        SYNC_ARGS+=(--mandatory)
    fi
    python -m calee_regression sync-smoke "${SYNC_ARGS[@]}"
    else
        echo ""
        echo "=== CaleeMobile selector contract did not pass (status $SELECTOR_GATE_STATUS) — mobile functional tests SKIPPED ==="
        echo "The CaleeMobile selector proof is missing or not for the exact build being"
        echo "released, so the mobile functional legs would assert against unverified"
        echo "selectors (pure noise) or be mistaken for a product result:"
        echo "  - CaleeMobile Client API:       SKIPPED (selector contract not satisfied)"
        echo "  - CaleeMobile Android UI:       SKIPPED (selector contract not satisfied)"
        echo "  - CaleeMobile iPhone UI:        SKIPPED (selector contract not satisfied)"
        echo "  - Cross-device synchronization: SKIPPED (selector contract not satisfied)"
        echo "The selector-contract report Step 2.5 wrote is preserved and consolidated"
        echo "below into a BLOCKED bundle. Selectors passing for another CaleeMobile"
        echo "build are not evidence about the one being released."
    fi

    echo ""
    echo "--- Step 7: CaleeShell kiosk/admin ---"
    # Kiosk/admin (Workstream 4) gets its own release-gating component, exactly
    # like sync. When mandatory it runs the physical kiosk suite on a disposable,
    # device-owner tablet -- NOT just full-tester -- and records a clear BLOCKED
    # marker (never a false PASS from the optional find.text("Admin") probe) when
    # the confirmation or a suitable tablet is missing. When excluded it is
    # recorded as an explicit optional/not-run component.
    KIOSK_ARGS=(--config "$CALEE_TEST_CONFIG" --run-id "$CALEE_RUN_ID")
    # Default to mandatory when the profile var is absent (an omitted feature is
    # release-gating, never silently optional).
    if [ "${RELEASE_FEATURE_KIOSK_ADMIN:-true}" = "false" ]; then
        KIOSK_ARGS+=(--optional)
    else
        KIOSK_ARGS+=(--mandatory)
    fi
    [ -n "${CALEESHELL_VERSION:-}" ] && KIOSK_ARGS+=(--caleeshell-version "$CALEESHELL_VERSION")
    # The technical owner opts into driving the disposable tablet with
    # CALEE_CONFIRM_TECHNICAL=1 (or allow_release_technical in the config).
    if [ "${CALEE_CONFIRM_TECHNICAL:-false}" = "true" ] || [ "${CALEE_CONFIRM_TECHNICAL:-0}" = "1" ]; then
        KIOSK_ARGS+=(--confirm-technical)
    fi
    python -m calee_regression kiosk-admin "${KIOSK_ARGS[@]}"

    echo ""
    echo "--- Step 8: Manual Checks ---"
    python -m calee_regression record-manual-checks --run-id "$CALEE_RUN_ID"
else
    echo ""
    echo "=== Prepare did not succeed (status $PREPARE_STATUS) — FAIL FAST ==="
    echo "The test environment is not in a known-good state, so NONE of the"
    echo "downstream functional tests will run for this release:"
    echo "  - Calee Tablet suite:          SKIPPED (Prepare not ready)"
    echo "  - CaleeMobile Client API:      SKIPPED (Prepare not ready)"
    echo "  - CaleeMobile Android UI:      SKIPPED (Prepare not ready)"
    echo "  - CaleeMobile iPhone UI:       SKIPPED (Prepare not ready)"
    echo "  - Cross-device synchronization: SKIPPED (Prepare not ready)"
    echo "  - CaleeShell kiosk/admin:      SKIPPED (Prepare not ready)"
    echo "  - Manual functional checks:    SKIPPED (Prepare not ready)"
    echo "The environment report Prepare wrote is preserved and consolidated"
    echo "below into one BLOCKED bundle."
fi

echo ""
echo "--- Collecting post-run build identity ---"
# Automatic build-identity collection (Phase 3/4). Detect the CaleeMobile
# version/commit/dirty state from its checkout, and the Calee tablet
# identity from adb/source where available, so a release PASS can prove
# exactly which builds were tested. A technical owner can still override any
# value by exporting the matching env var; the AUTO_* values only fill the
# gaps. Never fabricated: an undetectable identity stays unavailable, which
# the consolidator turns into BLOCKED for an in-scope app.
#
# --phase post also writes reports/runs/$CALEE_RUN_ID/identity/post.json; the
# consolidator compares it against pre.json (captured before testing) and
# BLOCKS when an in-scope app's identity changed during the run (Phase 4).
# Like the pre snapshot, this reads local git/adb only and runs no product
# test, so it is collected on the fail-fast BLOCKED path too.
eval "$(python -m calee_regression build-identity --run-id "$CALEE_RUN_ID" --phase post)"
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
# Cross-device synchronization gating (Workstream 1): mandatory for a full
# Calee solution release unless the technical owner opted it out via
# config/release-platforms.yaml (release_features.synchronization: false), in
# which case it is still shown in the report as an explicit optional component.
if [ "$RELEASE_FEATURE_SYNCHRONIZATION" = "false" ]; then
    CONSOLIDATE_ARGS+=(--sync-optional)
else
    CONSOLIDATE_ARGS+=(--sync-mandatory)
fi
# CaleeMobile selector contract (Priority 1) is UNCONDITIONALLY release-gating
# for a full Calee solution: a release can never PASS without valid selector
# evidence for the exact CaleeMobile build. Step 2.5 recorded it under this run;
# consolidate re-validates the embedded evidence independently and BLOCKS on any
# problem (missing/malformed/wrong-build/not-PASS/stale), exactly like the gate.
CONSOLIDATE_ARGS+=(--selector-contract-mandatory)
# Machine-config snapshot (Priority 4) and tablet release installation
# (Priority 5/6). When the one-button launcher ("00") created this run and
# recorded them under it, they are release-gating consolidated components: a
# missing/invalid machine-config snapshot, or a BLOCKED/FAILED installation,
# can never read as a release PASS. (Auto-included as mandatory by consolidate
# when the reports exist; passed explicitly here so the intent is on the record.)
if [ -f "reports/runs/$CALEE_RUN_ID/machine-config/results.json" ]; then
    CONSOLIDATE_ARGS+=(--machine-config-mandatory)
fi
if [ -f "reports/runs/$CALEE_RUN_ID/installation/results.json" ]; then
    CONSOLIDATE_ARGS+=(--installation-mandatory)
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
if [ "$PREPARE_STATUS" -ne 0 ]; then
    # Fail-fast run: surface the EXACT Prepare problem from the environment
    # report (Prepare's own words) so the tester sees why the whole release
    # stopped without opening the bundle. Read-only, best-effort.
    echo ""
    echo "Prepare did not succeed (status $PREPARE_STATUS). Exact problem(s) from the environment report:"
    python - "$CALEE_RUN_ID" <<'PY'
import json
import pathlib
import sys

run_id = sys.argv[1]
report = pathlib.Path("reports") / "runs" / run_id / "environment" / "results.json"
try:
    data = json.loads(report.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001 - best-effort, never crash the launcher
    print(f"  (environment report could not be read: {exc})")
    sys.exit(0)
detail = data.get("detail") or []
if isinstance(detail, str):
    detail = [detail]
print(f"  Prepare status: {data.get('status', 'unknown')}")
for line in detail:
    print(f"  - {line}")
if not detail:
    print("  - (no further detail recorded)")
PY
    if [ "$STATUS" -eq 0 ]; then
        # Not reachable in practice any more: Prepare is now a mandatory
        # consolidated component (see consolidated_report.py), so a failed
        # Prepare always makes $STATUS non-zero too. Kept as a hard backstop --
        # a passing consolidate must never mask a failed Prepare step.
        echo "NOTE: Prepare Test Environment reported a problem earlier in this run — see Step 1 above."
        STATUS=3
    fi
fi
echo "Run ID: $CALEE_RUN_ID"
echo "Report workspace: reports/runs/$CALEE_RUN_ID/"

read -p "Press Enter to close..."
exit $STATUS
