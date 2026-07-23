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

# The ONE canonical report root (Priority 3) -- env override, else
# config/machine.local.yaml's report_dir, else this repo's own reports/
# directory. Run standalone, we resolve it here; delegated from "00", we
# INHERIT the already-resolved, already-exported value so this run never
# disagrees with itself about where evidence lives. An unsafe/unwritable
# configured root BLOCKS rather than silently falling back. See
# calee_regression/report_root.py.
if [ -z "${CALEE_REPORT_ROOT:-}" ]; then
    if ! CALEE_REPORT_ROOT="$(python3 -m calee_regression report-root)"; then
        echo "$CALEE_REPORT_ROOT" >&2
        echo "BLOCKED: the configured report root could not be resolved."
        read -p "Press Enter to close..."
        exit 3
    fi
fi
export CALEE_REPORT_ROOT

# One run ID for this entire release run, shared by every component (Prepare,
# tablet, CaleeMobile API/UI, manual checks, consolidation). Every component
# writes to a fixed path inside $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/
# -- never a timestamped directory a later step has to rediscover by listing
# and sorting, and never a shared always-overwritten file another run could
# be racing against. See calee_regression/run_context.py.
#
# Priority 6: when the one-button launcher ("00 Run Calee Release Regression")
# already created the run ID and recorded the machine-config snapshot +
# installation evidence under it, we INHERIT that run ID here (never mint a
# second one) so the whole release -- installation included -- lives in ONE
# workspace. Run standalone, we generate one.
CALEE_RUN_ID="${CALEE_RUN_ID:-release-$(date +%Y%m%d-%H%M%S)-$(python3 -c 'import secrets; print(secrets.token_hex(3))')}"
export CALEE_RUN_ID
echo "Run ID: $CALEE_RUN_ID"
echo "Report root: $CALEE_REPORT_ROOT"
echo "Workspace: $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/"
echo ""

# Priority 3/4: when a machine config exists, the ONE effective release
# configuration (machine + release candidate/bundle manifest) fully controls
# this launcher -- platform scope, per-feature mandatoriness, profile, and
# expected identities all come from its emitted RELEASE_* variables (see
# calee_regression/cli.py's _emit_release_config_vars), configured iPhone/
# Android device ids are exported for the UI suite, and a machine/release
# conflict BLOCKS (recorded in the run's release-config evidence).
#
# Priority 2: a schema-v2 release bundle is self-contained and authoritative
# for platforms/features/profile/expected identity -- config/release-
# platforms.yaml is NOT consulted for it at all, so a problem with that
# legacy file can never block a valid v2 bundle. A schema-v1 (or bundle-less)
# run still loads and cross-checks the legacy file, but only INSIDE release-
# config's own gate below -- never via a separate, unguarded eval that could
# abort this launcher before that gate has a chance to run.
#
# Priority 1: when launcher "00" already composed this run's release-config
# (before installing the release), this command CONSUMES that same-run
# evidence and does NOT recompute a second, possibly-different composition --
# see calee_regression/cli.py's release_config_cmd, which detects the
# already-written reports/runs/$CALEE_RUN_ID/release-config/results.json and
# re-validates + re-emits it instead of composing again. Run standalone (no
# "00" delegation), no such evidence exists yet, so this composes it fresh,
# exactly as before Priority 1.
if [ -f config/machine.local.yaml ]; then
    if RELEASE_CFG_OUT="$(python -m calee_regression release-config --run-id "$CALEE_RUN_ID" 2>/dev/null)"; then
        RELEASE_CFG_STATUS=0
    else
        RELEASE_CFG_STATUS=$?
    fi
    # Apply the composed platform/feature/profile/expected-identity scope
    # regardless (the emitted values are the safe machine∩release
    # intersection, or -- on a BLOCKED composition -- still emitted so the
    # rest of this script has a consistent, if unusable, view).
    eval "$RELEASE_CFG_OUT" 2>/dev/null || true
    [ -n "${RELEASE_IPHONE_DEVICE:-}" ] && export CALEE_IPHONE_DEVICE="$RELEASE_IPHONE_DEVICE"
    [ -n "${RELEASE_ANDROID_DEVICE:-}" ] && export CALEE_ANDROID_DEVICE="$RELEASE_ANDROID_DEVICE"
else
    # No machine config at all: there is no bundle/schema-v2 path in play
    # (a bundle is always resolved via machine.local.yaml's
    # release_bundle_dir), so fall back to the legacy release-platforms
    # profile alone -- a malformed file here still aborts the launcher, which
    # is correct: schema-v1 (the only possibility with no machine config)
    # must keep using and cross-checking it (Priority 2 requirement 8).
    eval "$(python -m calee_regression release-platforms)"
    RELEASE_CFG_STATUS=0
fi

echo ""
if [ "$RELEASE_CFG_STATUS" -ne 0 ]; then
    # Priority 1: release-config is a PRE-PRODUCT gate, exactly like
    # machine-config and installation. A machine/release-candidate conflict
    # (profile disagreement, backend pin mismatch, a required platform the
    # machine can't provide, ...) BLOCKS BEFORE Prepare ever runs -- Prepare is
    # never attempted, and NONE of the downstream product checks (tablet,
    # mobile, sync, kiosk, manual) run. This is a setup/configuration blocker,
    # never a product FAIL.
    echo "BLOCKED: the machine and release-candidate configurations conflict —"
    echo "see $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config/results.json."
    echo ""
    echo "--- Step 1: Prepare Test Environment — SKIPPED (release-config gate blocked) ---"
    PREPARE_STATUS=$RELEASE_CFG_STATUS
else
    echo "--- Step 1: Prepare Test Environment (incl. Appium) ---"
    python -m calee_regression prepare --config "$CALEE_TEST_CONFIG" --suite tablet-full --run-id "$CALEE_RUN_ID"
    PREPARE_STATUS=$?
fi

echo ""
echo "--- Collecting pre-run build identity ---"
# Phase 4: capture which builds are about to be tested BEFORE any test runs and
# save it to $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/identity/pre.json. A matching post.json
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
# environment report Prepare already wrote ($CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/
# environment/results.json -- nothing below overwrites it), collect only the
# safe build identity above/below, and still produce ONE consolidated BLOCKED
# bundle so the release has an auditable record of exactly why it stopped.
if [ "$PREPARE_STATUS" -eq 0 ]; then
    echo ""
    echo "--- Step 1.5: Prepare the today-relative subscribed calendar fixture ---"
    # Priority 5/6/7: resolve ONE date for the run, generate the today-relative
    # subscribed ICS, and run it through exactly ONE explicit mode -- never a
    # silent fallback between them (config/machine.local.yaml's
    # subscribed_fixture.mode; defaults to "offline-only", which never claims
    # provisioning). "published" mode publishes the ICS to a stable external
    # URL already subscribed by the regression account (WebDAV/presigned-PUT/
    # S3-CLI/local adapter -- no calee-hub-core endpoint involved) and polls
    # until the run-specific event is visible; "fixed-date" uses the existing
    # static fixture at its own known date. Records first-class subscribed-
    # fixture evidence under this run. Scenario variables are consumable ONLY
    # via subscribed_publisher.safe_scenario_variables_from_report (Priority 6)
    # -- a blocked/partial published attempt can never supply them.
    #
    # Priority 6 (this session): --gate/--non-gating is this launcher's own
    # explicit execution policy for THIS STEP's exit code, derived (in order)
    # from: (1) an explicit technical-owner override
    # (CALEE_SUBSCRIBED_FIXTURE_GATE=true/false), (2) this release's OWN
    # feature scope ($RELEASE_FEATURE_GOOGLE_CALENDAR -- not in scope means
    # non-gating, there is nothing subscribed-calendar-related to verify),
    # else (3) omitted entirely so `prepare-subscribed-fixture` derives its
    # own default from the scenario's promotion state -- the SAME derivation
    # `consolidate` below uses for whether this component is mandatory, so
    # the two can never disagree. A gated BLOCKED result does not abort this
    # launcher (the tablet suite continues; the scenario itself stays
    # draft-unverified/excluded from the general suite while unpromoted) --
    # it is `consolidate`'s independent re-derivation that is the final,
    # authoritative release gate.
    SUBSCRIBED_GATE_ARG=""
    if [ "${CALEE_SUBSCRIBED_FIXTURE_GATE:-}" = "true" ] || [ "${CALEE_SUBSCRIBED_FIXTURE_GATE:-}" = "1" ]; then
        SUBSCRIBED_GATE_ARG="--gate"
    elif [ "${CALEE_SUBSCRIBED_FIXTURE_GATE:-}" = "false" ] || [ "${CALEE_SUBSCRIBED_FIXTURE_GATE:-}" = "0" ]; then
        SUBSCRIBED_GATE_ARG="--non-gating"
    elif [ "${RELEASE_FEATURE_GOOGLE_CALENDAR:-true}" = "false" ]; then
        SUBSCRIBED_GATE_ARG="--non-gating"
    fi

    if [ -n "$SUBSCRIBED_GATE_ARG" ]; then
        python -m calee_regression prepare-subscribed-fixture \
            --run-id "$CALEE_RUN_ID" \
            "$SUBSCRIBED_GATE_ARG"
    else
        python -m calee_regression prepare-subscribed-fixture \
            --run-id "$CALEE_RUN_ID"
    fi
    SUBSCRIBED_FIXTURE_STATUS=$?
    if [ "$SUBSCRIBED_FIXTURE_STATUS" -ne 0 ]; then
        # Priority 6, requirement 7: a technical owner must never be able to
        # overlook a blocked, GATING published fixture -- an unmissable banner,
        # not just a line buried in the step's own output above.
        echo ""
        echo "=== WARNING: the subscribed-calendar fixture is BLOCKED and GATING (status $SUBSCRIBED_FIXTURE_STATUS) ==="
        echo "See $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/subscribed-fixture/results.json for the exact"
        echo "publication/public-read/ingestion failure. No scenario variables from this run are safe to use."
        echo "This WILL block the release once/if this scenario is promoted -- see docs/SUBSCRIBED_CALENDAR_REGRESSION.md."
    fi

    echo ""
    echo "--- Step 2: Calee Tablet ---"
    python -m calee_regression suite --config "$CALEE_TEST_CONFIG" --suite full-tester --run-id "$CALEE_RUN_ID"

    echo ""
    echo "--- Step 2.5: CaleeMobile selector-contract gate (BEFORE any mobile functional test) ---"
    # Priority 1: a release must never ship CaleeMobile while its selector proof
    # is for a DIFFERENT build. Before the CaleeMobile Client API, the Android/iOS
    # UI, or cross-device sync run, obtain (or generate) machine-readable selector
    # evidence for the EXACT release SHA+version, validate it against the hardened
    # schema, and record it at $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/selector-contract/
    # results.json.
    #
    # Priority 2 (this session) -- documented mandatory/optional policy, ONE
    # already-resolved decision this launcher reads and acts on rather than
    # re-deriving itself: $RELEASE_SELECTOR_EVIDENCE_REQUIRED (emitted above by
    # "release-config", or by "release-platforms" when no machine config exists --
    # see calee_regression/release_config.py's resolve_selector_evidence_required)
    # is true when:
    #   * this is a PRODUCTION release with a mobile platform in scope
    #     (UNCONDITIONALLY, regardless of the manifest's own opinion), or
    #   * a non-production schema-v2 release's own bundle manifest states
    #     caleeMobile.selectorEvidenceRequired: true, or
    #   * a schema-v1 release (or a v2 manifest with no stated opinion) has a
    #     mobile platform in scope (the legacy default).
    # When mandatory, evidence missing/unreadable/malformed/stale/for another
    # SHA-version/wrong-Flutter/not-PASS/missing-any-selector BLOCKS (exit 3) and
    # EVERY mobile functional leg below is skipped -- the consolidated bundle can
    # never read as a PASS without valid selector evidence for the build being
    # released. When optional, the gate still runs (never silently skipped) and
    # is recorded as an explicit optional component either way, but a failure
    # there only skips the selector-DEPENDENT UI/sync legs (see below) -- the
    # device-independent Client API check may still proceed. Either way,
    # "consolidate" further down re-validates the SAME evidence independently and
    # is passed the matching --selector-contract-mandatory/-optional flag, so the
    # two gates can never disagree. A production release never permits LOCAL
    # selector generation regardless of this flag -- enforced inside the
    # selector-contract command itself.
    #
    # The expected CaleeMobile identity is read from THIS RUN'S OWN release-config
    # composition ($RELEASE_EXPECTED_CALEEMOBILE_GIT_SHA / $RELEASE_EXPECTED_
    # CALEEMOBILE_VERSION -- the schema-v2 bundle manifest's caleeMobile identity
    # block, or config/release-platforms.yaml for schema v1/a bare run), which a
    # technical owner may still override per-run via CALEEMOBILE_EXPECTED_GIT_SHA/
    # CALEEMOBILE_EXPECTED_BUILD_VERSION; a downloaded CI artifact may be supplied
    # via CALEEMOBILE_SELECTOR_EVIDENCE (else it generates locally from the
    # sibling CaleeMobile-Regression + CaleeMobile checkouts -- a development-only
    # fallback the command itself refuses in production).
    CALEEMOBILE_EXPECTED_GIT_SHA="${CALEEMOBILE_EXPECTED_GIT_SHA:-${RELEASE_EXPECTED_CALEEMOBILE_GIT_SHA:-}}"
    CALEEMOBILE_EXPECTED_BUILD_VERSION="${CALEEMOBILE_EXPECTED_BUILD_VERSION:-${RELEASE_EXPECTED_CALEEMOBILE_VERSION:-}}"

    SELECTOR_ARGS=(--run-id "$CALEE_RUN_ID")
    if [ "${RELEASE_SELECTOR_EVIDENCE_REQUIRED:-true}" = "false" ]; then
        SELECTOR_ARGS+=(--optional)
        SELECTOR_MANDATORY=false
    else
        SELECTOR_ARGS+=(--mandatory)
        SELECTOR_MANDATORY=true
    fi
    [ -n "${CALEEMOBILE_EXPECTED_GIT_SHA:-}" ] && SELECTOR_ARGS+=(--expected-sha "$CALEEMOBILE_EXPECTED_GIT_SHA")
    [ -n "${CALEEMOBILE_EXPECTED_BUILD_VERSION:-}" ] && SELECTOR_ARGS+=(--expected-version "$CALEEMOBILE_EXPECTED_BUILD_VERSION")
    # CALEE_LOCAL_SELECTOR_ARTIFACT_SUPPORT
    [ -n "${CALEEMOBILE_SELECTOR_GITHUB_RUN_ID:-}" ] && SELECTOR_ARGS+=(--github-run-id "$CALEEMOBILE_SELECTOR_GITHUB_RUN_ID")
    [ -n "${CALEEMOBILE_SELECTOR_GITHUB_ARTIFACT_ID:-}" ] && SELECTOR_ARGS+=(--github-artifact-id "$CALEEMOBILE_SELECTOR_GITHUB_ARTIFACT_ID")
    [ -n "${CALEEMOBILE_SELECTOR_GITHUB_ARTIFACT_ZIP:-}" ] && SELECTOR_ARGS+=(--github-artifact-zip "$CALEEMOBILE_SELECTOR_GITHUB_ARTIFACT_ZIP")
    [ -n "${CALEEMOBILE_SELECTOR_EVIDENCE:-}" ] && SELECTOR_ARGS+=(--source "$CALEEMOBILE_SELECTOR_EVIDENCE")
    python -m calee_regression selector-contract "${SELECTOR_ARGS[@]}"
    SELECTOR_GATE_STATUS=$?

    if [ "$SELECTOR_GATE_STATUS" -eq 0 ]; then
    # Release-feature scope propagation (Workstream 5): export THIS run's
    # authoritative feature scope from its already-composed schema-v2
    # release-config result (never the legacy file, once composed) BEFORE the
    # mobile checks, so every mobile leg below (api-only, android/ios --ui-only)
    # is told the exact scope the release composed -- not a legacy re-parse. The
    # resolver prefers schema-v2 and falls back to config/release-platforms.yaml
    # only when there is genuinely no schema-v2 bundle. A malformed scope exits
    # non-zero (fail-closed); the mobile legs then inherit the exported vars.
    if ! eval "$(python3 -m calee_regression release-feature-scope --run-id "$CALEE_RUN_ID")"; then
        echo "BLOCKED: could not resolve this run's release-feature scope — refusing to run the mobile checks with an unknown scope." >&2
    fi
    echo ""
    echo "--- Step 3: CaleeMobile Client API (device-independent — run once) ---"
    # The Client API suite is device-independent, so it runs EXACTLY ONCE for the
    # whole release, never once per platform. The Android and iOS steps below run
    # the UI ONLY (--ui-only), so neither can re-run or overwrite this run's one
    # $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/mobile-api/results.json. An initial API result
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
        echo "--- Step 4: CaleeMobile Android UI — SKIPPED (not part of this release; see this run's release-config, or config/release-platforms.yaml for a schema-v1/bare run) ---"
    fi

    if [ "$RELEASE_PLATFORM_IOS" = "true" ]; then
        echo ""
        echo "--- Step 5: CaleeMobile iPhone UI ---"
        python3 -m calee_regression run-with-credentials -- bash scripts/test_caleemobile.sh ios --ui-only
    else
        echo ""
        echo "--- Step 5: CaleeMobile iPhone UI — SKIPPED (not part of this release; see this run's release-config, or config/release-platforms.yaml for a schema-v1/bare run) ---"
    fi

    echo ""
    echo "--- Step 6: Cross-device synchronization ---"
    # Sync runs AFTER the mobile UI legs and BEFORE manual checks. It reuses
    # this run's verified backend + regression fixture + credentials and the
    # same CALEE_RUN_ID (the sync-smoke command reads the prepared-and-verified
    # backend from this run's environment report), driving the mobile legs on
    # ONE in-scope CaleeMobile platform -- Android preferred, else iOS. It writes
    # $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/sync/results.json, which consolidate
    # auto-discovers and, for a full Calee solution release, gates on: sync
    # defaults to MANDATORY (this run's schema-v2 release-config feature scope
    # when composed, else config/release-platforms.yaml's
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
    elif [ "$SELECTOR_MANDATORY" = "false" ]; then
        # Priority 2: selector evidence was OPTIONAL for this release and did
        # NOT pass. Never silently omitted -- the selector-contract report
        # above is preserved and consolidated as an explicit optional
        # BLOCKED component. Because the selectors were not verified for
        # this exact build, every selector-DEPENDENT leg (the mobile UI, and
        # cross-device sync, which drives that same UI) is still skipped --
        # an unverified selector must never back a real UI test, mandatory
        # or not. The device-independent Client API check does not depend
        # on selectors at all, so it may still proceed.
        echo ""
        echo "=== CaleeMobile selector contract is OPTIONAL for this release and did not pass (status $SELECTOR_GATE_STATUS) ==="
        echo "  - CaleeMobile Client API:       proceeding (device-independent, does not use selectors)"
        echo "  - CaleeMobile Android UI:       SKIPPED (selector evidence optional and not verified)"
        echo "  - CaleeMobile iPhone UI:        SKIPPED (selector evidence optional and not verified)"
        echo "  - Cross-device synchronization: SKIPPED (drives mobile UI; selector evidence optional and not verified)"
        echo "The selector-contract report Step 2.5 wrote is preserved and consolidated as an"
        echo "explicit optional component -- it does not block this release by itself."

        echo ""
        echo "--- Step 3: CaleeMobile Client API (device-independent — run once) ---"
        python3 -m calee_regression run-with-credentials -- bash scripts/test_caleemobile.sh api-only
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
    if [ "$RELEASE_CFG_STATUS" -ne 0 ]; then
        echo "=== release-config gate blocked (status $RELEASE_CFG_STATUS) — FAIL FAST ==="
        echo "The machine and release-candidate configurations conflict (see"
        echo "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config/results.json), so NONE of the"
        echo "downstream steps ran for this release:"
        echo "  - Prepare Test Environment:    SKIPPED (release-config gate blocked)"
        echo "  - Calee Tablet suite:          SKIPPED (release-config gate blocked)"
        echo "  - CaleeMobile Client API:      SKIPPED (release-config gate blocked)"
        echo "  - CaleeMobile Android UI:      SKIPPED (release-config gate blocked)"
        echo "  - CaleeMobile iPhone UI:       SKIPPED (release-config gate blocked)"
        echo "  - Cross-device synchronization: SKIPPED (release-config gate blocked)"
        echo "  - CaleeShell kiosk/admin:      SKIPPED (release-config gate blocked)"
        echo "  - Manual functional checks:    SKIPPED (release-config gate blocked)"
        echo "The release-config report is preserved and consolidated below into one"
        echo "BLOCKED bundle."
    else
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
# --phase post also writes $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/identity/post.json; the
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
# ($CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/<component>/results.json) and rejects
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
# Calee solution release unless it was opted out -- via this run's own
# schema-v2 release-config feature scope when composed, else the technical
# owner's config/release-platforms.yaml (release_features.synchronization:
# false) -- in which case it is still shown in the report as an explicit
# optional component. $RELEASE_FEATURE_SYNCHRONIZATION itself comes from
# whichever of those two the "Apply the composed ... scope" block above
# exported.
if [ "$RELEASE_FEATURE_SYNCHRONIZATION" = "false" ]; then
    CONSOLIDATE_ARGS+=(--sync-optional)
else
    CONSOLIDATE_ARGS+=(--sync-mandatory)
fi
# CaleeMobile selector contract: release-gating exactly per the SAME resolved
# $RELEASE_SELECTOR_EVIDENCE_REQUIRED decision Step 2.5 already acted on above
# (Priority 2, this session) -- never unconditional, so the launcher-level gate
# and consolidate's own independent re-validation can never disagree. Step 2.5
# recorded the evidence under this run; consolidate re-validates it independently
# and, when mandatory, BLOCKS on any problem (missing/malformed/wrong-build/
# not-PASS/stale) exactly like the gate. consolidate itself is the final,
# authoritative word (it re-derives this same policy from the release-config
# composition, a named waiver, and its own explicit flags), so this flag is a
# consistent DEFAULT for this launcher, never the only enforcement point.
if [ "${RELEASE_SELECTOR_EVIDENCE_REQUIRED:-true}" = "false" ]; then
    CONSOLIDATE_ARGS+=(--selector-contract-optional)
else
    CONSOLIDATE_ARGS+=(--selector-contract-mandatory)
fi
# Machine-config snapshot (Priority 4) and tablet release installation
# (Priority 5/6). When the one-button launcher ("00") created this run and
# recorded them under it, they are release-gating consolidated components: a
# missing/invalid machine-config snapshot, or a BLOCKED/FAILED installation,
# can never read as a release PASS. (Auto-included as mandatory by consolidate
# when the reports exist; passed explicitly here so the intent is on the record.)
if [ -f "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/machine-config/results.json" ]; then
    CONSOLIDATE_ARGS+=(--machine-config-mandatory)
fi
if [ -f "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/installation/results.json" ]; then
    CONSOLIDATE_ARGS+=(--installation-mandatory)
fi
# Release-config composition (Priority 1/3) is release-gating exactly like
# machine-config and installation: a BLOCKED/missing composition can never
# read as a release PASS.
if [ -f "$CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/release-config/results.json" ]; then
    CONSOLIDATE_ARGS+=(--release-config-mandatory)
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
    # Fail-fast run: surface the EXACT problem from the report of whichever
    # pre-product gate actually stopped the run (Priority 1) -- release-config
    # if IT blocked before Prepare ever ran, else Prepare's own environment
    # report -- so the tester sees why the whole release stopped without
    # opening the bundle. Read-only, best-effort.
    echo ""
    if [ "$RELEASE_CFG_STATUS" -ne 0 ]; then
        echo "The release-config gate blocked (status $RELEASE_CFG_STATUS) before Prepare ran. Exact problem(s) from the release-config report:"
        python - "$CALEE_RUN_ID" <<'PY'
import json
import os
import pathlib
import sys

run_id = sys.argv[1]
report_root = pathlib.Path(os.environ.get("CALEE_REPORT_ROOT") or ".")
report = report_root / "reports" / "runs" / run_id / "release-config" / "results.json"
try:
    data = json.loads(report.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001 - best-effort, never crash the launcher
    print(f"  (release-config report could not be read: {exc})")
    sys.exit(0)
detail = data.get("detail") or []
if isinstance(detail, str):
    detail = [detail]
print(f"  Release-config status: {data.get('status', 'unknown')}")
for line in detail:
    print(f"  - {line}")
conflicts = data.get("conflicts") or []
blocking_conflicts = [c for c in conflicts if isinstance(c, dict) and c.get("blocking")]
for c in blocking_conflicts:
    print(f"  - CONFLICT [{c.get('axis')}]: {c.get('explanation')}")
if not detail and not blocking_conflicts:
    print("  - (no further detail recorded)")
PY
    else
        echo "Prepare did not succeed (status $PREPARE_STATUS). Exact problem(s) from the environment report:"
        python - "$CALEE_RUN_ID" <<'PY'
import json
import os
import pathlib
import sys

run_id = sys.argv[1]
report_root = pathlib.Path(os.environ.get("CALEE_REPORT_ROOT") or ".")
report = report_root / "reports" / "runs" / run_id / "environment" / "results.json"
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
    fi
    if [ "$STATUS" -eq 0 ]; then
        # Not reachable in practice any more: Prepare/release-config are now
        # mandatory consolidated components (see consolidated_report.py), so a
        # blocked gate always makes $STATUS non-zero too. Kept as a hard
        # backstop -- a passing consolidate must never mask a blocked gate.
        echo "NOTE: an early pre-product gate reported a problem earlier in this run — see above."
        STATUS=3
    fi
fi
echo "Run ID: $CALEE_RUN_ID"
echo "Report workspace: $CALEE_REPORT_ROOT/reports/runs/$CALEE_RUN_ID/"

read -p "Press Enter to close..."
exit $STATUS
