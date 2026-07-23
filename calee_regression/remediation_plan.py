"""Release remediation planning from a focused post-fix verification run
(Phase 6).

After a blocked release run and a subsequent `focused-verify` diagnostic run,
`release-remediation-plan` compares the two runs' identities, classifies every
expected release component (framework-fixed and resumable, still untested,
must be rerun, or requiring a fresh release run), and writes one immutable,
typed plan under the RELEASE run's workspace.

Two invariants this module enforces structurally:

  * NO_RELEASE_PROMOTION_ALLOWED is ALWAYS present: a focused run never
    promotes anything to a release PASS and never rewrites an existing
    release result -- the plan is diagnostic planning evidence only;
  * an Android platform that is in the release scope but was never qualified
    (this Mac ran iOS-only focused checks) is listed as unqualified with
    ANDROID_DEVICE_REQUIRED -- never silently excluded.

Everything here is pure (dict in, dict out) so every branch is unit-testable;
file loading/validation/immutable writing lives in cli.py's
`release-remediation-plan` command.
"""

from __future__ import annotations

from .models import EXIT_BLOCKED, EXIT_REGRESSION, EXIT_SUCCESS

REPORT_TYPE = "release-remediation-plan"
SCHEMA_VERSION = 1

# The complete decision vocabulary (a plan may contain several).
DECISION_START_FRESH_RELEASE_RUN = "START_FRESH_RELEASE_RUN"
DECISION_RESUME_BLOCKED_COMPONENTS = "RESUME_BLOCKED_COMPONENTS"
DECISION_RERUN_TABLET_STANDARD = "RERUN_TABLET_STANDARD"
DECISION_RERUN_IOS = "RERUN_IOS"
DECISION_ANDROID_DEVICE_REQUIRED = "ANDROID_DEVICE_REQUIRED"
DECISION_KIOSK_AUTHORIZATION_REQUIRED = "KIOSK_AUTHORIZATION_REQUIRED"
DECISION_RELEASE_INPUT_MISMATCH = "RELEASE_INPUT_MISMATCH"
DECISION_NO_RELEASE_PROMOTION_ALLOWED = "NO_RELEASE_PROMOTION_ALLOWED"

# Stable presentation order for the decisions list.
_DECISION_ORDER = (
    DECISION_RELEASE_INPUT_MISMATCH,
    DECISION_START_FRESH_RELEASE_RUN,
    DECISION_RESUME_BLOCKED_COMPONENTS,
    DECISION_RERUN_TABLET_STANDARD,
    DECISION_RERUN_IOS,
    DECISION_ANDROID_DEVICE_REQUIRED,
    DECISION_KIOSK_AUTHORIZATION_REQUIRED,
    DECISION_NO_RELEASE_PROMOTION_ALLOWED,
)

# Per-component classifications.
CLASS_PASSED = "passed"
CLASS_FRAMEWORK_FIXED_RESUMABLE = "framework-fixed-resumable"
CLASS_BLOCKED_UNRESOLVED = "blocked-unresolved"
CLASS_FAILED_RERUN_REQUIRED = "failed-rerun-required"
CLASS_UNTESTED = "untested"
CLASS_ANDROID_UNQUALIFIED = "android-unqualified"
CLASS_KIOSK_AUTHORIZATION_REQUIRED = "kiosk-authorization-required"

# Which focused step(s) stand in for a release component: ALL listed steps
# must have PASSed in the focused run for the component to count as
# framework-fixed. Components without a focused counterpart can never be
# framework-fixed by a focused run.
FOCUSED_COUNTERPARTS = {
    "environment": ("fixture",),
    "tablet": ("tablet-standard",),
    "mobile-api": ("api-1", "api-2"),
    "mobile-ios": ("ios",),
}

# The one unmissable statement stamped into every plan.
DIAGNOSTIC_ONLY_STATEMENT = (
    "diagnostic planning evidence only -- not a release component result; a "
    "focused run never promotes to a release PASS and never rewrites an "
    "existing release result"
)

# Identity fields whose mismatch invalidates the whole plan-to-resume idea
# (the focused evidence was gathered against different inputs).
_HARD_MISMATCH_FIELDS = ("backend", "productSha", "fixtureVersion")


def compare_identities(focused_summary: dict, release_manifest: dict) -> "list[dict]":
    """Field-by-field identity comparison between the focused summary and the
    release run manifest. Each entry records both sides and whether they
    match (None when one side is unknown -- unknown is never silently
    treated as a match OR a hard mismatch). ``hard`` marks the fields whose
    proven mismatch forces START_FRESH_RELEASE_RUN."""
    profile = release_manifest.get("releasePlatformProfile") or {}
    focused_devices = focused_summary.get("deviceIds") or {}
    release_devices = release_manifest.get("deviceIds") or {}
    product_build = focused_summary.get("productBuild") or {}
    regression_shas = focused_summary.get("regressionShas") or {}
    release_git_shas = release_manifest.get("gitShas") or {}
    fields = [
        ("releaseId", focused_summary.get("releaseId"),
         release_manifest.get("releaseId"), False),
        ("backend", focused_summary.get("verifiedBackend"),
         release_manifest.get("targetBackend"), True),
        ("productSha", product_build.get("caleeMobileSha"),
         release_git_shas.get("caleeMobile") or release_git_shas.get("mobile-ios"), True),
        ("fixtureVersion", focused_summary.get("fixtureVersion"),
         release_manifest.get("fixtureVersion"), True),
        ("regressionSha", regression_shas.get("calee-regression"),
         release_git_shas.get("calee-regression") or release_git_shas.get("tablet"), False),
        ("caleeMobileRegressionSha", regression_shas.get("caleemobile-regression"),
         release_git_shas.get("caleemobile-regression"), False),
        ("platformScope", None,
         sorted(k for k, v in profile.items() if v) or None, False),
        ("tabletDeviceId", focused_devices.get("tablet"),
         release_devices.get("tablet") or release_devices.get("installation"), False),
        ("iosDeviceId", focused_devices.get("ios"),
         release_devices.get("mobile-ios"), False),
        ("installedBuildIdentity",
         (focused_summary.get("installedArtifactIdentity") or {}).get("status"),
         (release_manifest.get("buildVersions") or {}).get("installation"), False),
        ("featureScope", None,
         sorted(k for k, v in profile.items() if v) or None, False),
    ]
    comparison = []
    for name, focused_value, release_value, hard in fields:
        if focused_value is None or release_value is None:
            match = None
        else:
            match = focused_value == release_value
        comparison.append({
            "field": name,
            "focused": focused_value,
            "release": release_value,
            "match": match,
            "hard": hard,
        })
    return comparison


def hard_mismatches(comparison: "list[dict]") -> "list[dict]":
    """Only PROVEN mismatches (both sides known, unequal) on hard fields."""
    return [
        entry for entry in comparison
        if entry["field"] in _HARD_MISMATCH_FIELDS and entry["match"] is False
    ]


def _focused_step_statuses(focused_summary: dict) -> "dict[str, str]":
    return {
        step.get("id"): step.get("status")
        for step in focused_summary.get("steps") or []
        if isinstance(step, dict)
    }


def _android_in_scope(release_manifest: dict) -> bool:
    profile = release_manifest.get("releasePlatformProfile") or {}
    return bool(profile.get("mobile_android"))


def _android_qualified(release_manifest: dict) -> bool:
    """Android counts as qualified only when the release run recorded a clean
    mobile-android PASS. A focused run on this (iOS-only) Mac can never
    qualify Android."""
    return release_manifest.get("exitCodes", {}).get("mobile-android") == EXIT_SUCCESS


def classify_components(focused_summary: dict, release_manifest: dict) -> "list[dict]":
    """Classify every expected release component from the release manifest's
    worst-wins effective exit codes and the focused run's step statuses.
    Never mutates anything -- classification is evidence about what the
    RELEASE run still needs, not a change to it."""
    step_statuses = _focused_step_statuses(focused_summary)
    exit_codes = release_manifest.get("exitCodes") or {}
    expected = release_manifest.get("expectedComponents") or sorted(exit_codes)
    classifications = []
    for component in expected:
        code = exit_codes.get(component)
        counterparts = FOCUSED_COUNTERPARTS.get(component, ())
        counterparts_pass = bool(counterparts) and all(
            step_statuses.get(step) == "pass" for step in counterparts
        )
        if component == "mobile-android" and _android_in_scope(release_manifest) \
                and not _android_qualified(release_manifest):
            classification = CLASS_ANDROID_UNQUALIFIED
            detail = (
                "Android is in the release platform scope but was never qualified -- "
                "this focused run (iOS-only Mac) cannot qualify it; an Android device "
                "is required."
            )
        elif code is None:
            classification = CLASS_UNTESTED
            detail = "never recorded in the release run -- still untested."
        elif code == EXIT_SUCCESS:
            classification = CLASS_PASSED
            detail = "release run recorded PASS -- left untouched by this plan."
        elif code == EXIT_REGRESSION:
            classification = CLASS_FAILED_RERUN_REQUIRED
            detail = (
                "release run recorded a product FAIL -- a focused run never rewrites "
                "it; the component must be rerun (and the failure resolved) in a "
                "release-certifying run."
            )
        elif component == "kiosk-admin":
            classification = CLASS_KIOSK_AUTHORIZATION_REQUIRED
            detail = (
                "release run recorded BLOCKED for kiosk/admin evidence -- physical "
                "kiosk authorization is required before it can be certified."
            )
        elif counterparts_pass:
            classification = CLASS_FRAMEWORK_FIXED_RESUMABLE
            detail = (
                f"release run recorded BLOCKED (exit {code}) and the focused "
                f"counterpart step(s) {', '.join(counterparts)} now PASS -- the "
                "original blocker is framework-fixed; the component can be resumed/"
                "rerun in the release run."
            )
        else:
            classification = CLASS_BLOCKED_UNRESOLVED
            detail = (
                f"release run recorded BLOCKED (exit {code}) and the focused run "
                "did not demonstrate a passing counterpart -- still blocked."
            )
        classifications.append({
            "component": component,
            "releaseExitCode": code,
            "focusedCounterparts": list(counterparts),
            "classification": classification,
            "detail": detail,
        })
    return classifications


def decide(comparison: "list[dict]", classifications: "list[dict]") -> "list[str]":
    """Derive the decision set. NO_RELEASE_PROMOTION_ALLOWED is
    unconditionally present -- there is no code path that omits it."""
    decisions = {DECISION_NO_RELEASE_PROMOTION_ALLOWED}
    if hard_mismatches(comparison):
        decisions.add(DECISION_RELEASE_INPUT_MISMATCH)
        decisions.add(DECISION_START_FRESH_RELEASE_RUN)
    by_class = {c["classification"] for c in classifications}
    if CLASS_FRAMEWORK_FIXED_RESUMABLE in by_class:
        decisions.add(DECISION_RESUME_BLOCKED_COMPONENTS)
        for entry in classifications:
            if entry["classification"] != CLASS_FRAMEWORK_FIXED_RESUMABLE:
                continue
            if entry["component"] == "tablet":
                decisions.add(DECISION_RERUN_TABLET_STANDARD)
            if entry["component"] == "mobile-ios":
                decisions.add(DECISION_RERUN_IOS)
    if CLASS_ANDROID_UNQUALIFIED in by_class:
        decisions.add(DECISION_ANDROID_DEVICE_REQUIRED)
    if CLASS_KIOSK_AUTHORIZATION_REQUIRED in by_class:
        decisions.add(DECISION_KIOSK_AUTHORIZATION_REQUIRED)
    return [d for d in _DECISION_ORDER if d in decisions]


def build_plan(
    *,
    focused_summary: dict,
    release_manifest: dict,
    focused_run_id: str,
    release_run_id: str,
    planned_from_invocation: str,
    all_invocations: "list[str]",
    supporting_reports: "list[dict]",
    consolidated_status: "str | None" = None,
    generated_at: "str | None" = None,
    producer_git_sha: "str | None" = None,
) -> dict:
    """Assemble the full immutable remediation plan report (pure).

    ``supporting_reports`` is a list of {"path": ..., "sha256": ...} entries
    for EVERY focused report the plan relies on (the summary itself plus each
    focused child report it references), so the plan is evidence-bound the
    same way the focused summary is."""
    comparison = compare_identities(focused_summary, release_manifest)
    classifications = classify_components(focused_summary, release_manifest)
    decisions = decide(comparison, classifications)
    return {
        "reportType": REPORT_TYPE,
        "reportSchemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "producer": "calee_regression.release-remediation-plan",
        "producerGitSha": producer_git_sha,
        "evidenceRole": DIAGNOSTIC_ONLY_STATEMENT,
        "focusedRun": {
            "runId": focused_run_id,
            "releaseId": focused_summary.get("releaseId"),
            "invocationPlannedFrom": planned_from_invocation,
            "allInvocations": list(all_invocations),
            "status": focused_summary.get("status"),
        },
        "releaseRun": {
            "runId": release_run_id,
            "targetBackend": release_manifest.get("targetBackend"),
            "consolidatedStatus": consolidated_status,
        },
        "identityComparison": comparison,
        "hardMismatches": hard_mismatches(comparison),
        "componentClassifications": classifications,
        "decisions": decisions,
        "supportingFocusedReports": list(supporting_reports),
        "certificationEligible": False,
        "certification": (
            "not-a-release-certification (remediation planning from focused "
            "diagnostic evidence only)"
        ),
    }


def plan_exit_code(_plan: dict) -> int:
    """A produced plan is always exit 0 -- the plan's content, not the exit
    code, tells the tester what to do next. Input-validation failures never
    reach build_plan (the CLI exits 2/3 first)."""
    return EXIT_SUCCESS


# Kept importable for CLI/test symmetry with the other status maps.
EXIT_INPUTS_INVALID = EXIT_BLOCKED
