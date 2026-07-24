"""Mac qualification-handoff plan (Workstream 6).

Turns the current completeness report, release scope and host capabilities into
a concrete, secret-free, ordered plan a technical owner can run on Yiwen's Mac
to move the *qualification* measure -- making the cloud->Mac handoff a
first-class, reproducible framework workflow instead of tribal knowledge.

Guarantees:
  * no secret value, ever (only credential SOURCE categories by name);
  * no literal ``<RUN_ID>`` in a pasteable command -- a generated run id is
    referenced through the ``$CALEE_RUN_ID`` shell variable;
  * every framework command runs through the hermetic ``$CALEE_PYTHON``;
  * each step states whether it MUTATES the regression fixture or is read-only,
    which dimensions it can advance, whether it needs manual guided evidence,
    kiosk authorisation, or an Android device;
  * focused DIAGNOSTIC verification is clearly distinguished from full release
    CERTIFICATION;
  * the release scope is never silently narrowed -- a mandatory platform with
    no device is surfaced as a required action, not dropped.

Everything is derived + injectable, so the same plan is exercised offline for a
cloud host and for a fully-equipped Mac.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import framework_completeness as completeness_mod
from . import host_capabilities as hc
from . import release_platforms as release_platforms_mod
from .build_identity import parse_git_sha
from .credentials import REGRESSION_PASSWORD, REGRESSION_USERNAME

SCHEMA_VERSION = 1

PHASE_READONLY = "read-only"
PHASE_DIAGNOSTIC = "focused-diagnostic"
PHASE_CERTIFICATION = "release-certification"

PY = '"$CALEE_PYTHON"'  # hermetic interpreter token (Workstream 1)


def _git_sha(repo: Path, runner) -> "str | None":
    if not (repo / ".git").exists():
        return None
    try:
        result = runner(["git", "-C", str(repo), "rev-parse", "HEAD"])
    except Exception:  # noqa: BLE001
        return None
    return parse_git_sha(getattr(result, "stdout", "") or "")


def _sibling(repo_root: Path, name: str) -> Path:
    return repo_root.parent / name


def _step(**kw) -> dict:
    base = {
        "phase": PHASE_READONLY,
        "mutatesFixture": False,
        "readOnly": True,
        "advancesDimensions": [],
        "requiresManualEvidence": False,
        "requiresKioskAuthorization": False,
        "requiresAndroidDevice": False,
        "expectedReportPath": None,
        "expectedResultType": None,
        "note": "",
    }
    base.update(kw)
    return base


def build_plan(
    *,
    repo_root: "Path | None" = None,
    config_path: "str | None" = None,
    host: "dict | None" = None,
    completeness: "completeness_mod.CompletenessReport | None" = None,
    platforms: "release_platforms_mod.ReleasePlatforms | None" = None,
    features: "release_platforms_mod.ReleaseFeatures | None" = None,
    git_runner=None,
) -> dict:
    repo_root = Path(repo_root) if repo_root else completeness_mod.REPO_ROOT
    host = host or hc.gather_host_capabilities(repo_root=repo_root)
    report = completeness or completeness_mod.build_report()
    platforms = platforms or release_platforms_mod.load_release_platforms()
    features = features or release_platforms_mod.load_release_features()
    runner = git_runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=15))

    config_ref = config_path or "config/tester.local.yaml"

    # ── required identities (SHAs recorded, never assumed) ───────────────────
    regression_shas = {
        "calee-regression": _git_sha(repo_root, runner),
        "CaleeMobile-Regression": _git_sha(_sibling(repo_root, "CaleeMobile-Regression"), runner),
    }
    product_shas = {
        "Calee": _git_sha(_sibling(repo_root, "Calee"), runner),
        "CaleeMobile": _git_sha(_sibling(repo_root, "CaleeMobile"), runner),
    }

    # ── release scope (never silently narrowed) ──────────────────────────────
    platform_scope = {
        "tablet": platforms.tablet,
        "android": platforms.mobile_android,
        "ios": platforms.mobile_ios,
        "source": platforms.source,
    }
    kiosk_mandatory = bool(getattr(features, "kiosk_admin", False))

    # ── required devices, derived from the mandatory scope ───────────────────
    required_devices = []
    if platform_scope["tablet"]:
        required_devices.append({"device": "Calee tablet", "requiredBy": "tablet", "visible": host["devices"]["android"]["status"] == hc.AVAILABLE})
    if platform_scope["android"]:
        required_devices.append({"device": "Android phone or approved emulator", "requiredBy": "android", "visible": host["devices"]["android"]["status"] == hc.AVAILABLE})
    if platform_scope["ios"]:
        required_devices.append({"device": "physical iPhone", "requiredBy": "ios", "visible": host["devices"]["ios"]["status"] == hc.AVAILABLE})

    # ── credential SOURCES (names/categories only, never values) ─────────────
    cred_sources = host["credentialSources"]
    required_credentials = [
        {"name": c["name"], "envVar": c["envVar"], "required": c["required"],
         "availableVia": c["source"] if c["status"] == hc.AVAILABLE else "none"}
        for c in cred_sources["credentials"]
    ] + [{"name": "backend", "envVar": "CALEE_API_BASE", "required": True,
          "availableVia": "environment" if host["backend"]["status"] == hc.AVAILABLE else "none"}]

    # ── host prerequisites (from host-capabilities) ──────────────────────────
    prerequisites = [
        {"requirement": "macOS host", "status": "ok" if host["hostCategory"] == "macos" else "MISSING",
         "detail": f"host is {host['hostCategory']}"},
        {"requirement": "ADB", "status": _ok(host["toolchains"]["adb"]["status"])},
        {"requirement": "Appium", "status": _ok(host["toolchains"]["appium"]["status"])},
        {"requirement": "Flutter", "status": _ok(host["toolchains"]["flutter"]["status"])},
        {"requirement": "Xcode (xcrun)", "status": _ok(host["toolchains"]["xcode"]["status"])},
        {"requirement": "backend configured", "status": "ok" if host["backend"]["status"] == hc.AVAILABLE else "MISSING"},
        {"requirement": "tester config", "status": "ok" if host["testerConfig"]["status"] == hc.PRESENT else "MISSING"},
    ]

    android_capable = host["physicalQualification"]["android"]["capable"]
    kiosk_device_authorized = False  # never assumed; kiosk auth is explicit + out of band

    # ── ordered steps ────────────────────────────────────────────────────────
    steps = []
    steps.append(_step(
        id="host-capabilities", title="Confirm this host can qualify (read-only)",
        command=f'{PY} -m calee_regression host-capabilities',
        expectedResultType="host-capabilities",
        note="If executionCapability is OFFLINE_FRAMEWORK_ONLY, stop: this host cannot qualify anything.",
    ))
    steps.append(_step(
        id="preflight", title="Focused preflight (read-only; no fixture reset, no mutation)",
        command=f'{PY} -m calee_regression focused-verify --config {config_ref} --preflight-only',
        expectedResultType="focused-preflight",
        note="Resolve any BLOCKED gate (credentials/backend/appium/devices) before proceeding.",
    ))
    steps.append(_step(
        id="focused-verify", title="Focused post-fix verification (DIAGNOSTIC, mutates fixture)",
        command=f'{PY} -m calee_regression focused-verify --config {config_ref}',
        phase=PHASE_DIAGNOSTIC, mutatesFixture=True, readOnly=False,
        advancesDimensions=["mobileApiCoverage", "tabletReadCoverage"],
        expectedReportPath="reports/runs/$CALEE_RUN_ID/focused-verify/<invocation>/summary.json",
        expectedResultType="focused-verify-summary",
        note="Makes NO release-certification claim; it produces diagnostic (non-certifying) evidence only.",
    ))
    steps.append(_step(
        id="release-run", title="Full solution release run (CERTIFICATION, mutates fixture)",
        command=('export CALEE_RUN_ID="release-$(date +%Y%m%d-%H%M%S)"; '
                 'bash "tester/06 Test Full Calee Solution.command"'),
        phase=PHASE_CERTIFICATION, mutatesFixture=True, readOnly=False,
        advancesDimensions=[
            "tabletStandardQualification", "tabletReadCoverage", "tabletMutationCoverage",
            "mobileUiCoverage", "mobileApiCoverage", "crossDeviceSyncCoverage",
        ],
        requiresManualEvidence=True,
        expectedReportPath="reports/runs/$CALEE_RUN_ID/consolidated/consolidated-report.json",
        expectedResultType="consolidated-release-report",
        requiresAndroidDevice=platform_scope["android"],
        note="Certification-eligible evidence; the command generates a fresh run id into $CALEE_RUN_ID and reuses it, so nothing must be hand-substituted.",
    ))
    steps.append(_step(
        id="guided-handoff", title="Guided handoffs: new-family onboarding + Google Calendar OAuth",
        command=f'{PY} -m calee_regression record-manual-checks --run-id "$CALEE_RUN_ID"',
        phase=PHASE_CERTIFICATION, mutatesFixture=False, readOnly=False,
        advancesDimensions=["guidedHandoffCoverage"], requiresManualEvidence=True,
        expectedResultType="manual-checks",
        note="Guided in-app evidence recorded through the permanent recorder; never records a secret.",
    ))
    if kiosk_mandatory:
        steps.append(_step(
            id="kiosk-admin", title="Kiosk/admin qualification (device-owner-authorised tablet ONLY)",
            command=f'bash "tester/technical/Run Release Technical.command"',
            phase=PHASE_CERTIFICATION, mutatesFixture=True, readOnly=False,
            advancesDimensions=["kioskAdminQualification"], requiresKioskAuthorization=True,
            expectedResultType="kiosk-admin",
            note="Run ONLY on a tablet explicitly approved as a disposable device-owner test device.",
        ))
    steps.append(_step(
        id="export-evidence", title="Export a sanitized audit evidence bundle (read-only)",
        command=f'{PY} -m calee_regression evidence-bundle export --run-id "$CALEE_RUN_ID" --profile audit --output "$HOME/calee-audit-$CALEE_RUN_ID.zip"',
        expectedResultType="evidence-bundle",
        note="Produces a redacted, non-certifying bundle safe to hand to a cloud analysis session.",
    ))

    # ── blocking actions (scope not silently narrowed) ───────────────────────
    blocking_actions = []
    if platform_scope["android"] and not android_capable:
        blocking_actions.append("ANDROID_DEVICE_REQUIRED: Android is in the mandatory release scope but no Android device/emulator is available; do NOT drop Android from scope.")
    if kiosk_mandatory and not kiosk_device_authorized:
        blocking_actions.append("KIOSK_AUTHORIZATION_REQUIRED: kiosk/admin is mandatory but no tablet is authorised as a disposable device owner; authorise one out of band before the kiosk step.")
    if host["executionCapability"] == hc.OFFLINE_FRAMEWORK_ONLY:
        blocking_actions.append("OFFLINE_FRAMEWORK_ONLY: this host has no device tooling; run this plan on the qualification Mac.")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "report": "qualification-plan",
        "executionCapability": host["executionCapability"],
        "hostPrerequisites": prerequisites,
        "requiredRepositories": regression_shas,
        "requiredProducts": product_shas,
        "platformScope": platform_scope,
        "requiredDevices": required_devices,
        "requiredReleaseBundle": host["releaseBundle"],
        "requiredCredentials": required_credentials,
        "diagnosticVsCertification": {
            "focusedDiagnostic": "focused-verify makes NO certification claim; it produces diagnostic evidence to unblock development.",
            "releaseCertification": "the full solution run produces certification-eligible evidence that can advance qualification and release readiness.",
        },
        "steps": steps,
        "blockingActions": blocking_actions,
        "releaseReadiness": report.release_readiness()["status"],
        "cleanupGuidance": (
            "Every fixture-mutating step resets + verifies the REG-* fixture before it runs; if a run is interrupted, "
            "re-run 'focused-verify --preflight-only' to confirm the fixture is clean before retrying."
        ),
        "interruptionGuidance": (
            "A partial run leaves an immutable reports/runs/$CALEE_RUN_ID workspace. Resume a blocked release run with "
            "'inspect-resume'/'resume-release' rather than starting fresh, to avoid repeating destructive steps."
        ),
    }


def _ok(status: str) -> str:
    return "ok" if status == hc.AVAILABLE else status


def render_markdown(plan: dict) -> str:
    lines = ["# Calee qualification plan (Mac handoff)", ""]
    lines.append(f"- Execution capability: **{plan['executionCapability']}**")
    lines.append(f"- Release readiness (current): **{plan['releaseReadiness']}**")
    lines.append(f"- Platform scope: `{plan['platformScope']['source']}` "
                 f"(tablet={plan['platformScope']['tablet']}, android={plan['platformScope']['android']}, ios={plan['platformScope']['ios']})")
    lines.append("")
    if plan["blockingActions"]:
        lines.append("## Blocking actions (resolve before/around the affected steps)")
        for b in plan["blockingActions"]:
            lines.append(f"- ⚠️ {b}")
        lines.append("")
    lines.append("## Required identities")
    for label, shas in (("Regression repos", plan["requiredRepositories"]), ("Product checkouts", plan["requiredProducts"])):
        lines.append(f"- {label}: " + ", ".join(f"{k}=`{v or 'UNKNOWN'}`" for k, v in shas.items()))
    lines.append("")
    lines.append("## Host prerequisites")
    for p in plan["hostPrerequisites"]:
        lines.append(f"- {p['requirement']}: **{p['status']}**")
    lines.append("")
    lines.append("## Required credentials (source category only — never a value)")
    for c in plan["requiredCredentials"]:
        lines.append(f"- {c['name']} (`{c['envVar']}`): {'required' if c['required'] else 'optional'}, available via **{c['availableVia']}**")
    lines.append("")
    lines.append("## Ordered steps")
    lines.append("")
    lines.append("| # | Step | Phase | Fixture | Advances | Command |")
    lines.append("|---|---|---|---|---|---|")
    for i, s in enumerate(plan["steps"], 1):
        fixture = "mutates" if s["mutatesFixture"] else ("read-only" if s["readOnly"] else "—")
        adv = ", ".join(s["advancesDimensions"]) or "—"
        cmd = s["command"].replace("|", "\\|")
        lines.append(f"| {i} | {s['title']} | {s['phase']} | {fixture} | {adv} | `{cmd}` |")
    lines.append("")
    for s in plan["steps"]:
        flags = []
        if s["requiresManualEvidence"]:
            flags.append("manual guided evidence")
        if s["requiresKioskAuthorization"]:
            flags.append("kiosk authorisation")
        if s["requiresAndroidDevice"]:
            flags.append("Android device")
        if s["note"] or flags:
            lines.append(f"- **{s['id']}**: {s['note']}" + (f" _(needs: {', '.join(flags)})_" if flags else ""))
    lines.append("")
    lines.append("## Cleanup & interruption")
    lines.append(f"- {plan['cleanupGuidance']}")
    lines.append(f"- {plan['interruptionGuidance']}")
    lines.append("")
    return "\n".join(lines)
