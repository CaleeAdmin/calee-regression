"""Resume a blocked release qualification without repeating already-passed
destructive or disruptive steps.

See docs/RELEASE_POLICY.md's "Resuming a blocked run" section for the policy
this implements, and the ``resume-release``/``inspect-resume`` CLI commands
in ``cli.py`` for the operator-facing entry points.

Core safety model
------------------

A release run may be resumed only when its immutable inputs still match the
ORIGINAL attempt (see :data:`IMMUTABLE_FIELDS` and :func:`collect_immutable_inputs`/
:func:`diff_immutable_inputs`). There is no bypass flag: a mismatch always
refuses the resume (``EXIT_BLOCKED``), never merely warns.

Per-component reuse is a SEPARATE, narrower decision (see
:func:`evaluate_component_reuse`): a prior PASS may be reused only when its
report still validates (same run, same release, same recorded input digest,
every referenced evidence file still present and byte-identical). FAIL,
BLOCKED, NOT_RUN and mandatory SKIP are never reusable -- they always need
(re-)execution.

Installation gets one further, narrower, ADDITIONAL live check on top of the
generic reuse decision (:func:`evaluate_installation_reuse`): even a
structurally valid prior PASS may only be reused once a bounded, read-only
ADB probe confirms the CURRENTLY connected tablet is the same physical
device and its installed package identity is unchanged -- no APK is
reinstalled and the tablet is never rebooted merely to decide this.

Attempts
--------

Every resume call is its own immutable "attempt", recorded under
``reports/runs/<run-id>/attempts/<n>/`` -- never overwriting a previous
attempt's record (see :class:`AttemptRecord`). Attempt 1 is the run's
original state, snapshotted the first time anyone asks to resume it; its
``immutable-inputs.json`` is the permanent baseline every later attempt is
compared against, not merely the most recent one.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import consolidated_report as cr
from . import fixture_bridge
from . import release_candidate as release_candidate_mod
from . import release_installer
from . import run_context
from .models import EXIT_BLOCKED, EXIT_INVALID_CONFIG, EXIT_REGRESSION, EXIT_SUCCESS

ATTEMPTS_DIRNAME = "attempts"

# ---------------------------------------------------------------------------
# Digest helpers
# ---------------------------------------------------------------------------


def _sha256_json(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> "str | None":
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_json(path: Path) -> "dict | None":
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _git_sha(repo_dir: "Path | None", *, runner: "Callable[[list], Any] | None" = None) -> "str | None":
    """``git rev-parse HEAD`` in ``repo_dir``, or None when it can't be read
    (not a git checkout, git missing, or the directory doesn't exist). Never
    raises -- an unreadable git SHA is simply an unavailable immutable input,
    not a crash."""
    if repo_dir is None or not Path(repo_dir).is_dir():
        return None
    run = runner or (lambda argv: subprocess.run(argv, cwd=str(repo_dir), capture_output=True, text=True, timeout=10))
    try:
        result = run(["git", "rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    return (getattr(result, "stdout", "") or "").strip() or None


# ---------------------------------------------------------------------------
# Immutable input snapshot
# ---------------------------------------------------------------------------

# The exact immutable-input vocabulary this feature validates (see module
# docstring / docs/RELEASE_POLICY.md). Kept as an explicit tuple (rather than
# just "whatever ImmutableInputs.to_dict() happens to contain") so the set of
# validated fields is a deliberate, reviewable list, not an accident of
# dataclass field order.
IMMUTABLE_FIELDS = (
    "releaseId",
    "releaseManifestSchemaVersion",
    "candidateFingerprintDigest",
    "apkSha256",
    "expectedPackageIds",
    "expectedVersionNames",
    "expectedVersionCodes",
    "expectedSignerFingerprints",
    "expectedGitShas",
    "releaseConfigDigest",
    "targetBackend",
    "releaseProfile",
    "platformScope",
    "featureScope",
    "regressionSha",
    "caleeMobileRegressionSha",
    "caleeMobileExpectedSha",
    "caleeMobileExpectedVersion",
    "manualCheckDefinitionVersion",
    "selectorEvidenceRequired",
    "distributedBuildEvidenceRequired",
)
# tabletStableIdentity is validated separately (see diff_immutable_inputs) --
# an unreachable tablet is "unknown", not "changed", so it cannot be folded
# into the same equality check as everything else here.
_TABLET_IDENTITY_FIELD = "tabletStableIdentity"


@dataclass
class ImmutableInputs:
    release_id: "str | None" = None
    release_manifest_schema_version: "int | None" = None
    candidate_fingerprint_digest: "str | None" = None
    apk_sha256: dict = field(default_factory=dict)
    expected_package_ids: dict = field(default_factory=dict)
    expected_version_names: dict = field(default_factory=dict)
    expected_version_codes: dict = field(default_factory=dict)
    expected_signer_fingerprints: dict = field(default_factory=dict)
    expected_git_shas: dict = field(default_factory=dict)
    release_config_digest: "str | None" = None
    target_backend: "str | None" = None
    release_profile: "str | None" = None
    platform_scope: list = field(default_factory=list)
    feature_scope: list = field(default_factory=list)
    regression_sha: "str | None" = None
    caleemobile_regression_sha: "str | None" = None
    caleemobile_expected_sha: "str | None" = None
    caleemobile_expected_version: "str | None" = None
    tablet_stable_identity: "dict | None" = None
    manual_check_definition_version: "str | None" = None
    selector_evidence_required: "bool | None" = None
    distributed_build_evidence_required: "bool | None" = None
    # Fields this collection pass could not determine at all (e.g. no adb
    # runner supplied, no release-config evidence present yet). Recorded so a
    # diff can tell "unavailable" apart from "actually absent/None".
    unavailable_fields: "list[str]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "releaseId": self.release_id,
            "releaseManifestSchemaVersion": self.release_manifest_schema_version,
            "candidateFingerprintDigest": self.candidate_fingerprint_digest,
            "apkSha256": dict(self.apk_sha256),
            "expectedPackageIds": dict(self.expected_package_ids),
            "expectedVersionNames": dict(self.expected_version_names),
            "expectedVersionCodes": dict(self.expected_version_codes),
            "expectedSignerFingerprints": dict(self.expected_signer_fingerprints),
            "expectedGitShas": dict(self.expected_git_shas),
            "releaseConfigDigest": self.release_config_digest,
            "targetBackend": self.target_backend,
            "releaseProfile": self.release_profile,
            "platformScope": list(self.platform_scope),
            "featureScope": list(self.feature_scope),
            "regressionSha": self.regression_sha,
            "caleeMobileRegressionSha": self.caleemobile_regression_sha,
            "caleeMobileExpectedSha": self.caleemobile_expected_sha,
            "caleeMobileExpectedVersion": self.caleemobile_expected_version,
            "tabletStableIdentity": self.tablet_stable_identity,
            "manualCheckDefinitionVersion": self.manual_check_definition_version,
            "selectorEvidenceRequired": self.selector_evidence_required,
            "distributedBuildEvidenceRequired": self.distributed_build_evidence_required,
            "unavailableFields": list(self.unavailable_fields),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ImmutableInputs":
        return cls(
            release_id=data.get("releaseId"),
            release_manifest_schema_version=data.get("releaseManifestSchemaVersion"),
            candidate_fingerprint_digest=data.get("candidateFingerprintDigest"),
            apk_sha256=dict(data.get("apkSha256") or {}),
            expected_package_ids=dict(data.get("expectedPackageIds") or {}),
            expected_version_names=dict(data.get("expectedVersionNames") or {}),
            expected_version_codes=dict(data.get("expectedVersionCodes") or {}),
            expected_signer_fingerprints=dict(data.get("expectedSignerFingerprints") or {}),
            expected_git_shas=dict(data.get("expectedGitShas") or {}),
            release_config_digest=data.get("releaseConfigDigest"),
            target_backend=data.get("targetBackend"),
            release_profile=data.get("releaseProfile"),
            platform_scope=list(data.get("platformScope") or []),
            feature_scope=list(data.get("featureScope") or []),
            regression_sha=data.get("regressionSha"),
            caleemobile_regression_sha=data.get("caleeMobileRegressionSha"),
            caleemobile_expected_sha=data.get("caleeMobileExpectedSha"),
            caleemobile_expected_version=data.get("caleeMobileExpectedVersion"),
            tablet_stable_identity=data.get("tabletStableIdentity"),
            manual_check_definition_version=data.get("manualCheckDefinitionVersion"),
            selector_evidence_required=data.get("selectorEvidenceRequired"),
            distributed_build_evidence_required=data.get("distributedBuildEvidenceRequired"),
            unavailable_fields=list(data.get("unavailableFields") or []),
        )

    def digest(self) -> str:
        payload = self.to_dict()
        payload.pop("unavailableFields", None)
        return _sha256_json(payload)


def _manual_check_definition_version(repo_root: Path) -> "str | None":
    candidate = repo_root / "config" / "manual-checks.json"
    if not candidate.is_file():
        candidate = repo_root / "config" / "manual-checks.example.json"
    if not candidate.is_file():
        return None
    digest = _sha256_file(candidate)
    return f"sha256:{digest}" if digest else None


def collect_immutable_inputs(
    workspace: run_context.RunWorkspace,
    *,
    repo_root: Path,
    sibling_regression_root: "Path | None" = None,
    adb_runner: "release_installer.AdbRunner | None" = None,
    tablet_serial: "str | None" = None,
    git_sha_runner: "Callable[[list], Any] | None" = None,
) -> ImmutableInputs:
    """Collect this run's immutable inputs from its ALREADY-RECORDED evidence
    (release-config + the frozen release-candidate fingerprint), plus the
    CURRENT calee-regression/CaleeMobile-Regression git SHAs and (when an adb
    runner is supplied) a live, bounded, read-only tablet-identity probe.

    Never re-derives anything from a live --bundle path: once a release
    candidate has been frozen for a run, release-config's own report (and the
    fingerprint it embeds) is the sole source of truth here -- see
    release_candidate.verify_candidate_fingerprint for the independent
    byte-level tamper check performed separately in check_candidate_unchanged.
    """
    inputs = ImmutableInputs()
    unavailable = inputs.unavailable_fields

    release_config_report = _read_json(workspace.component_report_path("release-config"))
    if release_config_report:
        inputs.release_id = release_config_report.get("releaseId")
        inputs.release_manifest_schema_version = release_config_report.get("schemaVersion")
        inputs.release_config_digest = release_config_report.get("releaseConfigDigest")
        selections = release_config_report.get("releaseSelections") or {}
        inputs.target_backend = selections.get("selectedBackend")
        inputs.release_profile = selections.get("profile")
        inputs.platform_scope = sorted(selections.get("enabledPlatforms") or [])
        inputs.feature_scope = sorted(selections.get("enabledFeatures") or [])
        identities = selections.get("expectedIdentities") or {}
        for app_key in ("calee", "caleeShell"):
            app = identities.get(app_key) or {}
            if app.get("applicationId"):
                inputs.expected_package_ids[app_key] = app["applicationId"]
            version_name = app.get("versionName") or app.get("buildVersion") or app.get("version")
            if version_name:
                inputs.expected_version_names[app_key] = version_name
            if app.get("versionCode") is not None:
                inputs.expected_version_codes[app_key] = str(app["versionCode"])
            if app.get("signerSha256"):
                inputs.expected_signer_fingerprints[app_key] = app["signerSha256"]
            if app.get("gitSha"):
                inputs.expected_git_shas[app_key] = app["gitSha"]
        caleemobile = identities.get("caleeMobile") or {}
        inputs.caleemobile_expected_sha = caleemobile.get("gitSha")
        inputs.caleemobile_expected_version = caleemobile.get("buildVersion")
        if caleemobile.get("gitSha"):
            inputs.expected_git_shas["caleeMobile"] = caleemobile["gitSha"]
        inputs.selector_evidence_required = caleemobile.get("selectorEvidenceRequired")
        inputs.distributed_build_evidence_required = caleemobile.get("distributedBuildAcceptanceRequired")
        fingerprint_dict = release_config_report.get("releaseCandidateFingerprint")
        if fingerprint_dict:
            inputs.candidate_fingerprint_digest = fingerprint_dict.get("envelopeDigest")
            inputs.apk_sha256 = {
                key: (value.get("sha256") if isinstance(value, dict) else None)
                for key, value in (fingerprint_dict.get("apkSha256") or {}).items()
            }
    else:
        unavailable.append("releaseConfig")

    inputs.manual_check_definition_version = _manual_check_definition_version(repo_root)
    if inputs.manual_check_definition_version is None:
        unavailable.append("manualCheckDefinitionVersion")

    inputs.regression_sha = _git_sha(repo_root, runner=git_sha_runner)
    if inputs.regression_sha is None:
        unavailable.append("regressionSha")

    sibling = sibling_regression_root or fixture_bridge.find_sibling_repo(repo_root)
    inputs.caleemobile_regression_sha = _git_sha(sibling, runner=git_sha_runner)
    if inputs.caleemobile_regression_sha is None:
        unavailable.append("caleeMobileRegressionSha")

    if adb_runner is not None:
        identity, _detail = release_installer.capture_device_identity(adb_runner, tablet_serial)
        if identity is not None:
            inputs.tablet_stable_identity = identity.to_dict()
        else:
            unavailable.append(_TABLET_IDENTITY_FIELD)
    else:
        installation_report = _read_json(workspace.component_report_path("installation"))
        recorded = (installation_report or {}).get("tabletStableIdentity")
        if recorded:
            inputs.tablet_stable_identity = recorded
        else:
            unavailable.append(_TABLET_IDENTITY_FIELD)

    return inputs


def diff_immutable_inputs(baseline: ImmutableInputs, current: ImmutableInputs) -> "list[str]":
    """Compare `current` against the frozen `baseline`. Returns a list of
    human-readable mismatches; empty means every immutable input still
    matches. A field neither side could determine is skipped (there is
    nothing to compare); a field only one side determined is still compared
    (None vs. a real value is itself a mismatch worth surfacing)."""
    problems: "list[str]" = []
    b = baseline.to_dict()
    c = current.to_dict()
    for key in IMMUTABLE_FIELDS:
        bv, cv = b.get(key), c.get(key)
        if bv in (None, {}, []) and cv in (None, {}, []):
            continue
        if bv != cv:
            problems.append(f"{key}: original attempt recorded {bv!r}, current is {cv!r}.")
    bt, ct = b.get(_TABLET_IDENTITY_FIELD), c.get(_TABLET_IDENTITY_FIELD)
    if bt and ct and bt != ct:
        problems.append(
            f"{_TABLET_IDENTITY_FIELD}: original attempt's tablet was {bt!r}, currently connected "
            f"tablet is {ct!r} -- this is not the same physical device."
        )
    return problems


# ---------------------------------------------------------------------------
# Per-component reuse policy
# ---------------------------------------------------------------------------

DECISION_REUSE = "reuse"
DECISION_EXECUTE = "execute"
DECISION_REFUSED = "refused"

# Components resume_release itself knows how to (re-)execute in-process.
# Everything else in run_context.COMPONENT_NAMES is either a live-tablet or
# Appium/sibling-repo-driven suite this module deliberately does not attempt
# to reimplement -- it only DECIDES whether such a component may be reused,
# and leaves actually running it to the same commands the tester launchers
# already use for a fresh run (see docs/RELEASE_POLICY.md).
PREPARE_COMPONENT = "environment"
INSTALLATION_COMPONENT = "installation"


@dataclass
class ComponentDecision:
    component: str
    decision: str  # DECISION_REUSE | DECISION_EXECUTE | DECISION_REFUSED
    reason: str
    input_digest: "str | None" = None
    evidence_path: "str | None" = None
    status: "str | None" = None
    # True only for a refusal so severe that the WHOLE resume must be
    # refused (a new run required), not merely "re-execute this component" --
    # currently only installation's live tablet/package identity mismatch.
    blocks_resume: bool = False

    def to_dict(self) -> dict:
        return {
            "component": self.component,
            "decision": self.decision,
            "reason": self.reason,
            "inputDigest": self.input_digest,
            "evidencePath": self.evidence_path,
            "status": self.status,
            "blocksResume": self.blocks_resume,
        }


def component_input_digest(component: str, baseline_digest: str) -> str:
    """One digest per component, covering every input relevant to it: since
    a component's correctness depends on the run's WHOLE immutable-input
    baseline (release/build identity, config, scope), folding the component
    name into the same baseline digest gives every component its own stable,
    comparable identity without needing a bespoke per-component input list."""
    return _sha256_json({"baselineDigest": baseline_digest, "component": component})


def _report_effective_status(report: "dict | None") -> str:
    """Best-effort PASS/FAIL/BLOCKED/NOT_RUN classification of an arbitrary
    component report, robust to this framework's report shapes: a simple
    {"status": ...} envelope (installation/release-config/machine-config/
    environment/selector-contract/subscribed-fixture/distributed-build-
    acceptance), a scored suite/API report with passed/failed/blocked counts
    (tablet/mobile-api/mobile-android/mobile-ios), or a manual-checks list
    (see consolidated_report.component_from_manual_checks, mirrored here)."""
    if not report:
        return cr.STATUS_NOT_RUN
    raw_status = report.get("status")
    if isinstance(raw_status, str):
        normalized = raw_status.strip().lower()
        if normalized in ("ok", "pass", "passed"):
            return cr.STATUS_PASS
        if normalized in ("fail", "failed"):
            return cr.STATUS_FAIL
        if normalized in ("blocked", "invalid"):
            return cr.STATUS_BLOCKED
        if normalized in ("not_run", "skipped"):
            return cr.STATUS_NOT_RUN
    checks = report.get("checks")
    if isinstance(checks, list) and checks:
        failed = sum(1 for c in checks if isinstance(c, dict) and c.get("mandatory", True) and c.get("status") == cr.STATUS_FAIL)
        blocked = sum(1 for c in checks if isinstance(c, dict) and c.get("mandatory", True) and c.get("status") in (cr.STATUS_BLOCKED, None))
        passed = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == cr.STATUS_PASS)
        return cr.decide_status(passed=passed, failed=failed, blocked=blocked, total=len(checks))
    passed = report.get("passed_count", report.get("passed"))
    failed = report.get("failed_count", report.get("failed"))
    blocked = report.get("blocked_count", report.get("blocked"))
    mandatory_skipped = report.get("mandatory_skipped_count", 0) or 0
    if passed is None and failed is None and blocked is None:
        return cr.STATUS_NOT_RUN
    return cr.decide_status(passed=passed or 0, failed=failed or 0, blocked=(blocked or 0) + mandatory_skipped)


def _check_referenced_evidence(report: dict) -> "str | None":
    """When a report declares an `evidenceFiles` list of {"path", "sha256"}
    entries, every one must still exist and still hash to the recorded
    digest. A report with no such declaration has nothing to check here --
    this is additive, not a requirement every producer must adopt."""
    entries = report.get("evidenceFiles")
    if not entries:
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path_str = entry.get("path")
        expected_sha = entry.get("sha256")
        if not path_str:
            continue
        path = Path(path_str)
        if not path.is_file():
            return f"referenced evidence file {path_str} no longer exists"
        if expected_sha:
            actual = _sha256_file(path)
            if actual != expected_sha:
                return (
                    f"referenced evidence file {path_str} digest no longer matches "
                    f"(expected {expected_sha}, got {actual})"
                )
    return None


def evaluate_component_reuse(
    component: str,
    *,
    workspace: run_context.RunWorkspace,
    run_id: str,
    release_id: "str | None",
    baseline_digest: str,
    run_started_at_epoch: "float | None",
    current_fixture_version: "str | None" = None,
) -> ComponentDecision:
    """Decide whether `component`'s existing report can be reused as-is.

    Reuse PASS only. FAIL, BLOCKED, NOT_RUN and mandatory SKIP are never
    reused -- they always need (re-)execution, though a report that exists
    but fails any integrity check is REFUSED (with a reason), not silently
    folded into the same "never executed" bucket as a genuinely absent one.

    A new fixture version is not itself an immutable-input mismatch (Prepare
    is allowed to establish one on resume) -- but a component whose OWN
    report recorded which fixture version it ran against is never reused
    once that version has moved on; fixture-dependent functional results
    must be bound to the CURRENT verified fixture, never a stale one.
    """
    input_digest = component_input_digest(component, baseline_digest)
    report_path = workspace.component_report_path(component)
    if not report_path.is_file():
        return ComponentDecision(component, DECISION_EXECUTE, "no prior report -- not yet executed", input_digest)

    try:
        raw = report_path.read_text(encoding="utf-8")
        report = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return ComponentDecision(
            component, DECISION_REFUSED, f"stale or malformed report ({exc}) -- must re-execute",
            input_digest, str(report_path),
        )
    if not isinstance(report, dict):
        return ComponentDecision(
            component, DECISION_REFUSED, "malformed report (not a JSON object) -- must re-execute",
            input_digest, str(report_path),
        )

    try:
        run_context.validate_component_report(
            report, report_path=report_path, run_id=run_id, workspace=workspace,
            component=component, run_started_at_epoch=run_started_at_epoch,
        )
    except run_context.RunIdError as exc:
        return ComponentDecision(
            component, DECISION_REFUSED, f"report failed run/workspace validation: {exc}",
            input_digest, str(report_path),
        )

    found_release_id = report.get("releaseId")
    if release_id and found_release_id and found_release_id != release_id:
        return ComponentDecision(
            component, DECISION_REFUSED,
            f"report is for release {found_release_id!r}, expected {release_id!r}",
            input_digest, str(report_path),
        )

    status = _report_effective_status(report)
    if status == cr.STATUS_NOT_RUN:
        return ComponentDecision(component, DECISION_EXECUTE, "not yet executed", input_digest, str(report_path), status=status)
    if status == cr.STATUS_FAIL:
        return ComponentDecision(component, DECISION_REFUSED, "prior result was FAIL -- never reused", input_digest, str(report_path), status=status)
    if status == cr.STATUS_BLOCKED:
        return ComponentDecision(component, DECISION_REFUSED, "prior result was BLOCKED -- never reused", input_digest, str(report_path), status=status)

    recorded_fixture_version = report.get("fixtureVersion")
    if current_fixture_version and recorded_fixture_version and recorded_fixture_version != current_fixture_version:
        return ComponentDecision(
            component, DECISION_REFUSED,
            f"fixture version changed since this component passed (was {recorded_fixture_version!r}, now "
            f"{current_fixture_version!r}) -- fixture-dependent results are never reused across a fixture "
            f"version change",
            input_digest, str(report_path), status=status,
        )

    recorded_digest = report.get("resumeInputDigest")
    if recorded_digest is not None and recorded_digest != input_digest:
        return ComponentDecision(
            component, DECISION_REFUSED,
            "component input digest no longer matches this attempt's immutable inputs -- must re-execute",
            input_digest, str(report_path), status=status,
        )

    evidence_problem = _check_referenced_evidence(report)
    if evidence_problem:
        return ComponentDecision(component, DECISION_REFUSED, evidence_problem, input_digest, str(report_path), status=status)

    return ComponentDecision(
        component, DECISION_REUSE, "prior PASS re-validated (same run, same release, same inputs)",
        input_digest, str(report_path), status=status,
    )


def evaluate_installation_reuse(
    decision: ComponentDecision,
    *,
    adb_runner: "release_installer.AdbRunner | None",
    tablet_serial: "str | None",
) -> ComponentDecision:
    """Installation's one EXTRA, live, bounded, READ-ONLY check on top of the
    generic reuse decision above: even a structurally valid prior PASS may
    only be reused when the CURRENTLY connected tablet's stable identity and
    its installed package identity both still match what that PASS recorded.
    No APK is reinstalled and the tablet is never rebooted to decide this."""
    if decision.decision != DECISION_REUSE:
        return decision
    if adb_runner is None:
        return ComponentDecision(
            decision.component, DECISION_EXECUTE,
            "installation previously passed, but no tablet was available to re-verify this invocation -- "
            "installation must be (re-)verified before it can be reused",
            decision.input_digest, decision.evidence_path, status=decision.status,
        )
    report = _read_json(Path(decision.evidence_path)) or {}
    recorded_identity_raw = report.get("tabletStableIdentity")
    if not recorded_identity_raw:
        return ComponentDecision(
            decision.component, DECISION_EXECUTE,
            "the passed installation has no recorded tablet stable identity to re-verify against -- "
            "installation must be (re-)verified before it can be reused",
            decision.input_digest, decision.evidence_path, status=decision.status,
        )
    recorded_identity = release_installer.DeviceIdentity(
        configured_transport=recorded_identity_raw.get("configuredTransport"),
        serialno=recorded_identity_raw.get("serialno"),
        manufacturer=recorded_identity_raw.get("manufacturer"),
        model=recorded_identity_raw.get("model"),
        product=recorded_identity_raw.get("product"),
        transport_type=recorded_identity_raw.get("transportType", "usb"),
        wireless_host=recorded_identity_raw.get("wirelessHost"),
        wireless_port=recorded_identity_raw.get("wirelessPort"),
    )
    current_identity, identity_error = release_installer.capture_device_identity(adb_runner, tablet_serial)
    if current_identity is None:
        return ComponentDecision(
            decision.component, DECISION_REFUSED,
            f"could not read the currently connected tablet's stable identity ({identity_error}) -- "
            "refusing to reuse installation; connect the same tablet and retry, or start a new release run",
            decision.input_digest, decision.evidence_path, status=decision.status,
        )
    if not release_installer.stable_identity_matches(recorded_identity, current_identity):
        return ComponentDecision(
            decision.component, DECISION_REFUSED,
            "the currently connected tablet's stable identity does not match the tablet this "
            "installation was recorded against -- this is not the same physical tablet; a new release "
            "run is required",
            decision.input_digest, decision.evidence_path, status=decision.status, blocks_resume=True,
        )
    inspection = release_installer.inspect_tablet(adb_runner, serial=tablet_serial)
    if inspection.status != release_installer.STATUS_OK:
        return ComponentDecision(
            decision.component, DECISION_REFUSED,
            f"could not re-verify installed package identity ({inspection.detail}) -- refusing to reuse "
            "installation",
            decision.input_digest, decision.evidence_path, status=decision.status,
        )
    installed_by_pkg = {i.package_id: i for i in inspection.installed}
    execution = report.get("execution") or {}
    for recorded in execution.get("installed", []):
        pkg = recorded.get("packageId")
        current = installed_by_pkg.get(pkg)
        if current is None or not current.present:
            return ComponentDecision(
                decision.component, DECISION_REFUSED,
                f"{pkg} is no longer installed on the tablet -- installed package identity changed since "
                "this installation passed; a new release run is required",
                decision.input_digest, decision.evidence_path, status=decision.status, blocks_resume=True,
            )
        if current.version_name != recorded.get("versionName") or str(current.version_code) != str(recorded.get("versionCode")):
            return ComponentDecision(
                decision.component, DECISION_REFUSED,
                f"{pkg} installed identity changed (was {recorded.get('versionName')}/{recorded.get('versionCode')}, "
                f"now {current.version_name}/{current.version_code}) -- a new release run is required",
                decision.input_digest, decision.evidence_path, status=decision.status, blocks_resume=True,
            )
    return decision


def check_candidate_unchanged(workspace: run_context.RunWorkspace, release_config_report: "dict | None") -> "list[str]":
    """Independently re-verify the frozen release-candidate snapshot's
    CURRENT bytes against its recorded fingerprint -- catches tampering
    (a manifest/checksums/APK swapped in-place) that a mere digest-string
    comparison inside collect_immutable_inputs would not, since that digest
    is itself read from the (potentially tampered) fingerprint file."""
    snapshot_dir = workspace.component_dir("release-candidate")
    fingerprint_path = snapshot_dir / release_candidate_mod.FINGERPRINT_FILENAME
    if not fingerprint_path.is_file():
        return []
    try:
        fingerprint = release_candidate_mod.load_candidate_fingerprint(fingerprint_path)
    except release_candidate_mod.CandidateFingerprintError as exc:
        return [f"release-candidate fingerprint is unreadable: {exc}"]
    kwargs = {}
    if release_config_report is not None:
        kwargs = dict(
            expected_run_id=workspace.run_id,
            expected_release_id=release_config_report.get("releaseId"),
            expected_schema_version=release_config_report.get("schemaVersion"),
            expected_release_config_digest=release_config_report.get("releaseConfigDigest"),
        )
    return release_candidate_mod.verify_candidate_fingerprint(snapshot_dir, fingerprint, **kwargs)


# ---------------------------------------------------------------------------
# Attempt ledger
# ---------------------------------------------------------------------------


def attempts_root(workspace: run_context.RunWorkspace) -> Path:
    return workspace.root / ATTEMPTS_DIRNAME


def attempt_dir(workspace: run_context.RunWorkspace, attempt_number: int) -> Path:
    return attempts_root(workspace) / str(attempt_number)


def existing_attempt_numbers(workspace: run_context.RunWorkspace) -> "list[int]":
    root = attempts_root(workspace)
    if not root.is_dir():
        return []
    numbers = [int(child.name) for child in root.iterdir() if child.is_dir() and child.name.isdigit()]
    return sorted(numbers)


@dataclass
class AttemptRecord:
    attempt_number: int
    run_id: str
    started_at: str
    completed_at: "str | None" = None
    command: str = "resume-release"
    mode: str = "resume"
    operator: "str | None" = None
    original_run_id: "str | None" = None
    immutable_validation: dict = field(default_factory=dict)
    components_reused: "list[str]" = field(default_factory=list)
    components_executed: "list[dict]" = field(default_factory=list)
    components_refused: "list[dict]" = field(default_factory=list)
    exit_code: "int | None" = None
    final_result: "str | None" = None
    # Evidence acquisition performed FOR THIS ATTEMPT (this session): a
    # secret-free summary of what acquire-release-evidence did before blocked
    # components were re-decided. Newly acquired evidence is bound to this
    # attempt via this record; earlier attempts' snapshots are never touched.
    evidence_acquisition: "dict | None" = None

    def to_dict(self) -> dict:
        return {
            "attemptNumber": self.attempt_number,
            "runId": self.run_id,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "command": self.command,
            "mode": self.mode,
            "operator": self.operator,
            "originalRunId": self.original_run_id,
            "immutableValidation": self.immutable_validation,
            "componentsReused": list(self.components_reused),
            "componentsExecuted": list(self.components_executed),
            "componentsRefused": list(self.components_refused),
            "exitCode": self.exit_code,
            "finalResult": self.final_result,
            **({"evidenceAcquisition": self.evidence_acquisition}
               if self.evidence_acquisition is not None else {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AttemptRecord":
        return cls(
            attempt_number=data["attemptNumber"],
            run_id=data["runId"],
            started_at=data.get("startedAt", ""),
            completed_at=data.get("completedAt"),
            command=data.get("command", "resume-release"),
            mode=data.get("mode", "resume"),
            operator=data.get("operator"),
            original_run_id=data.get("originalRunId"),
            immutable_validation=dict(data.get("immutableValidation") or {}),
            components_reused=list(data.get("componentsReused") or []),
            components_executed=list(data.get("componentsExecuted") or []),
            components_refused=list(data.get("componentsRefused") or []),
            exit_code=data.get("exitCode"),
            final_result=data.get("finalResult"),
            evidence_acquisition=data.get("evidenceAcquisition"),
        )

    def write(self, workspace: run_context.RunWorkspace) -> Path:
        path = attempt_dir(workspace, self.attempt_number) / "attempt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path


def load_attempt(workspace: run_context.RunWorkspace, attempt_number: int) -> "AttemptRecord | None":
    path = attempt_dir(workspace, attempt_number) / "attempt.json"
    data = _read_json(path)
    return AttemptRecord.from_dict(data) if data is not None else None


def latest_attempt(workspace: run_context.RunWorkspace) -> "AttemptRecord | None":
    numbers = existing_attempt_numbers(workspace)
    if not numbers:
        return None
    return load_attempt(workspace, numbers[-1])


def _snapshot_components(workspace: run_context.RunWorkspace, attempt_number: int) -> None:
    """Copy every component's CURRENT canonical report (and the run manifest)
    into this attempt's own directory. This is a copy, not a move -- the
    canonical `reports/runs/<run-id>/<component>/results.json` path is left
    untouched so every existing reader (consolidate, record-component) keeps
    working unmodified; the attempt directory is purely an additional,
    immutable audit trail. Never overwrites a previous attempt's snapshot."""
    attempt_dir(workspace, attempt_number).mkdir(parents=True, exist_ok=True)
    dest_root = attempt_dir(workspace, attempt_number) / "components"
    for component in run_context.COMPONENT_NAMES:
        src = workspace.component_report_path(component)
        if not src.is_file():
            continue
        dest = dest_root / component / "results.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    if workspace.manifest_path.is_file():
        manifest_dest = attempt_dir(workspace, attempt_number) / "run-manifest.json"
        manifest_dest.write_text(workspace.manifest_path.read_text(encoding="utf-8"), encoding="utf-8")


def _provisional_overall_status(workspace: run_context.RunWorkspace) -> "tuple[str, str | None]":
    """A best-effort, offline overall status for THIS attempt's record --
    NOT a replacement for `consolidate`'s own authoritative, fail-closed
    decision (which also applies mandatory/optional gating this module does
    not have access to). Used only to populate AttemptRecord.final_result
    and the tester-facing run listing."""
    worst = cr.STATUS_PASS
    blocking = None
    for component in run_context.COMPONENT_NAMES:
        report = _read_json(workspace.component_report_path(component))
        status = _report_effective_status(report)
        if status == cr.STATUS_FAIL:
            return status, component
        if status in (cr.STATUS_BLOCKED, cr.STATUS_NOT_RUN) and blocking is None:
            worst, blocking = cr.STATUS_BLOCKED, component
    return worst, blocking


def _bootstrap_attempt_one(workspace: run_context.RunWorkspace, immutable_inputs: ImmutableInputs) -> AttemptRecord:
    """The first time anyone asks to resume a run, retroactively snapshot its
    CURRENT state as "Attempt 1" -- the run's original state and the
    permanent immutable-input baseline every later attempt is compared
    against. Never called again for the same run once attempt 1 exists."""
    baseline_path = attempt_dir(workspace, 1) / "immutable-inputs.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(immutable_inputs.to_dict(), indent=2) + "\n", encoding="utf-8")
    _snapshot_components(workspace, 1)
    manifest = run_context.RunManifest.load(workspace.manifest_path) if workspace.manifest_path.is_file() else None
    exit_code = run_context.worst_exit_code(list(manifest.exit_codes.values())) if manifest and manifest.exit_codes else None
    overall, _blocking = _provisional_overall_status(workspace)
    record = AttemptRecord(
        attempt_number=1,
        run_id=workspace.run_id,
        started_at=(manifest.started_at if manifest else ""),
        completed_at=(manifest.started_at if manifest else ""),
        command="original",
        mode="original",
        operator=(manifest.tester if manifest else None),
        original_run_id=workspace.run_id,
        immutable_validation={"matched": True, "mismatches": [], "baselineDigest": immutable_inputs.digest()},
        exit_code=exit_code,
        final_result=overall,
    )
    record.write(workspace)
    return record


def load_baseline_immutable_inputs(workspace: run_context.RunWorkspace) -> "ImmutableInputs | None":
    path = attempt_dir(workspace, 1) / "immutable-inputs.json"
    data = _read_json(path)
    return ImmutableInputs.from_dict(data) if data is not None else None


# ---------------------------------------------------------------------------
# Prepare re-execution (the one component resume-release runs in-process)
# ---------------------------------------------------------------------------


@dataclass
class PrepareOutcome:
    status: str  # "pass" | "blocked"
    exit_code: int
    detail: "list[str]" = field(default_factory=list)


PrepareRunner = Callable[[], PrepareOutcome]

# Acquires missing release evidence for a resume attempt (see
# evidence_acquisition.py + the resume-release CLI wiring). Takes the run
# workspace, returns a secret-free summary dict recorded on the attempt.
EvidenceAcquirer = Callable[[run_context.RunWorkspace], dict]


def default_prepare_runner(
    *, run_id: str, repo_root: Path, config_path: "str | None" = None, suite_name: "str | None" = None,
) -> PrepareOutcome:
    """Rerun Prepare (environment readiness + fixture diagnose/reset/verify)
    the same way the tester launchers do: `python -m calee_regression
    prepare --run-id <run-id> ...`, inheriting this process's environment so
    an operator never has to manually reconstruct fixture credentials or
    other configuration to continue a release run."""
    argv = [sys.executable, "-m", "calee_regression", "prepare", "--run-id", run_id]
    if config_path:
        argv += ["--config", config_path]
    if suite_name:
        argv += ["--suite", suite_name]
    try:
        result = subprocess.run(argv, cwd=str(repo_root), capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return PrepareOutcome(status="blocked", exit_code=EXIT_BLOCKED, detail=[f"could not run prepare: {exc}"])
    status = "pass" if result.returncode == EXIT_SUCCESS else "blocked"
    detail = []
    if result.stdout.strip():
        detail.append(result.stdout.strip()[-2000:])
    if result.stderr.strip():
        detail.append(result.stderr.strip()[-2000:])
    return PrepareOutcome(status=status, exit_code=result.returncode, detail=detail)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


@dataclass
class ResumeOutcome:
    run_id: str
    attempt_number: int
    resumable: bool
    immutable_mismatches: "list[str]"
    decisions: "list[ComponentDecision]"
    exit_code: int
    attempt: "AttemptRecord | None" = None


def _immutable_gate(
    workspace: run_context.RunWorkspace,
    *,
    repo_root: Path,
    sibling_regression_root: "Path | None",
    adb_runner: "release_installer.AdbRunner | None",
    tablet_serial: "str | None",
) -> "tuple[ImmutableInputs, ImmutableInputs, list[str], int]":
    """Bootstrap attempt 1 if needed, then diff the CURRENT immutable inputs
    against attempt 1's permanent baseline. Returns
    (baseline, current, mismatches, next_attempt_number)."""
    bootstrapped = False
    if not existing_attempt_numbers(workspace):
        bootstrap_inputs = collect_immutable_inputs(
            workspace, repo_root=repo_root, sibling_regression_root=sibling_regression_root,
        )
        _bootstrap_attempt_one(workspace, bootstrap_inputs)
        bootstrapped = True
    baseline = load_baseline_immutable_inputs(workspace)
    current = collect_immutable_inputs(
        workspace, repo_root=repo_root, sibling_regression_root=sibling_regression_root,
        adb_runner=adb_runner, tablet_serial=tablet_serial,
    )
    if bootstrapped:
        # Nothing has changed yet by construction -- attempt 1 IS this
        # collection.
        mismatches: "list[str]" = []
    else:
        mismatches = diff_immutable_inputs(baseline, current)
    next_attempt_number = (existing_attempt_numbers(workspace) or [1])[-1] + 1
    return baseline, current, mismatches, next_attempt_number


def inspect_resume(
    run_id: str,
    *,
    repo_root: Path,
    report_root: "Path | None" = None,
    sibling_regression_root: "Path | None" = None,
    adb_runner: "release_installer.AdbRunner | None" = None,
    tablet_serial: "str | None" = None,
) -> ResumeOutcome:
    """Read-only: report whether `run_id` is resumable, without mutating
    anything -- no attempt is bootstrapped/written, Prepare is never rerun,
    the tablet is never touched beyond the same bounded read-only probe a
    real resume would perform."""
    workspace = run_context.RunWorkspace(report_root or repo_root, run_id)
    release_config_report = _read_json(workspace.component_report_path("release-config"))

    if not existing_attempt_numbers(workspace):
        current = collect_immutable_inputs(
            workspace, repo_root=repo_root, sibling_regression_root=sibling_regression_root,
            adb_runner=adb_runner, tablet_serial=tablet_serial,
        )
        mismatches: "list[str]" = []
        next_attempt_number = 1
    else:
        baseline = load_baseline_immutable_inputs(workspace)
        current = collect_immutable_inputs(
            workspace, repo_root=repo_root, sibling_regression_root=sibling_regression_root,
            adb_runner=adb_runner, tablet_serial=tablet_serial,
        )
        mismatches = diff_immutable_inputs(baseline, current) if baseline is not None else []
        next_attempt_number = existing_attempt_numbers(workspace)[-1] + 1

    mismatches = mismatches + check_candidate_unchanged(workspace, release_config_report)

    manifest = run_context.RunManifest.load(workspace.manifest_path) if workspace.manifest_path.is_file() else None
    run_started_at_epoch = _epoch(manifest.started_at) if manifest else None
    current_fixture_version = manifest.fixture_version if manifest else None

    decisions = [
        evaluate_component_reuse(
            component, workspace=workspace, run_id=run_id, release_id=current.release_id,
            baseline_digest=current.digest(), run_started_at_epoch=run_started_at_epoch,
            current_fixture_version=current_fixture_version,
        )
        for component in run_context.COMPONENT_NAMES
    ]
    for i, decision in enumerate(decisions):
        if decision.component == INSTALLATION_COMPONENT:
            decisions[i] = evaluate_installation_reuse(decision, adb_runner=adb_runner, tablet_serial=tablet_serial)

    resumable = not mismatches and not any(d.blocks_resume for d in decisions)
    exit_code = EXIT_BLOCKED if not resumable else EXIT_SUCCESS
    return ResumeOutcome(
        run_id=run_id, attempt_number=next_attempt_number, resumable=resumable,
        immutable_mismatches=mismatches, decisions=decisions, exit_code=exit_code, attempt=None,
    )


def _epoch(started_at: "str | None") -> "float | None":
    if not started_at:
        return None
    try:
        return time.mktime(time.strptime(started_at, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def perform_resume(
    run_id: str,
    *,
    repo_root: Path,
    report_root: "Path | None" = None,
    sibling_regression_root: "Path | None" = None,
    adb_runner: "release_installer.AdbRunner | None" = None,
    tablet_serial: "str | None" = None,
    prepare_runner: "PrepareRunner | None" = None,
    config_path: "str | None" = None,
    suite_name: "str | None" = None,
    operator: "str | None" = None,
    evidence_acquirer: "EvidenceAcquirer | None" = None,
) -> ResumeOutcome:
    """Resume `run_id`: validate immutable inputs (refusing outright on any
    mismatch, no bypass), decide per-component reuse, rerun Prepare in-process
    when it isn't reusable, and record this as a new, immutable attempt.

    Never reinstalls APKs or reboots the tablet itself -- installation is
    only ever marked reused or "requires execution"; actually re-installing
    is left to the same install-tablet-release invocation a fresh run would
    use (see the tester launcher integration).
    """
    workspace = run_context.RunWorkspace(report_root or repo_root, run_id)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    baseline, current, mismatches, attempt_number = _immutable_gate(
        workspace, repo_root=repo_root, sibling_regression_root=sibling_regression_root,
        adb_runner=adb_runner, tablet_serial=tablet_serial,
    )
    release_config_report = _read_json(workspace.component_report_path("release-config"))
    mismatches = mismatches + check_candidate_unchanged(workspace, release_config_report)

    if mismatches:
        record = AttemptRecord(
            attempt_number=attempt_number, run_id=run_id, started_at=started_at,
            completed_at=time.strftime("%Y-%m-%d %H:%M:%S"), command="resume-release", mode="resume",
            operator=operator, original_run_id=run_id,
            immutable_validation={"matched": False, "mismatches": mismatches, "baselineDigest": baseline.digest() if baseline else None},
            exit_code=EXIT_BLOCKED, final_result="blocked",
        )
        record.write(workspace)
        return ResumeOutcome(
            run_id=run_id, attempt_number=attempt_number, resumable=False, immutable_mismatches=mismatches,
            decisions=[], exit_code=EXIT_BLOCKED, attempt=record,
        )

    # Evidence acquisition (this session): acquire evidence that was missing
    # when the run blocked, BEFORE component reuse is decided, so blocked
    # evidence-dependent components can be rerun against it in THIS attempt.
    # The acquirer is fail-closed and run-scoped; it never mutates a prior
    # attempt's snapshots, and reused-PASS components are untouched -- a
    # PASS's evidence is never silently replaced (evaluate_component_reuse
    # still re-verifies every referenced evidence file's digest).
    evidence_acquisition_summary: "dict | None" = None
    if evidence_acquirer is not None:
        try:
            evidence_acquisition_summary = evidence_acquirer(workspace)
        except Exception as exc:  # noqa: BLE001 - an acquirer fault must never abort the resume gate
            evidence_acquisition_summary = {"status": "blocked", "detail": str(exc)}

    manifest = run_context.RunManifest.load(workspace.manifest_path) if workspace.manifest_path.is_file() else None
    run_started_at_epoch = _epoch(manifest.started_at) if manifest else None

    # Prepare is decided (and, when not reusable, rerun) FIRST -- it may
    # establish a NEW verified fixture version, and every fixture-dependent
    # component's reuse decision below must be bound to whatever fixture
    # version is actually current by the time it's evaluated, never a stale
    # one (see evaluate_component_reuse's fixture-version check).
    prepare_decision = evaluate_component_reuse(
        PREPARE_COMPONENT, workspace=workspace, run_id=run_id, release_id=current.release_id,
        baseline_digest=current.digest(), run_started_at_epoch=run_started_at_epoch,
        current_fixture_version=(manifest.fixture_version if manifest else None),
    )
    prepare_executed = False
    prepare_blocked = False
    if prepare_decision.decision != DECISION_REUSE:
        prepare_executed = True
        runner = prepare_runner or (
            lambda: default_prepare_runner(run_id=run_id, repo_root=repo_root, config_path=config_path, suite_name=suite_name)
        )
        outcome = runner()
        prepare_blocked = outcome.status != "pass"
        prepare_decision = ComponentDecision(
            PREPARE_COMPONENT,
            DECISION_REFUSED if prepare_blocked else DECISION_EXECUTE,
            f"{prepare_decision.reason} -- rerun this attempt: "
            f"{'PASS' if not prepare_blocked else 'BLOCKED'} (exit {outcome.exit_code})",
            prepare_decision.input_digest, prepare_decision.evidence_path,
            status=("blocked" if prepare_blocked else "pass"),
        )

    # Reload the manifest -- Prepare may just have updated fixture_version.
    manifest = run_context.RunManifest.load(workspace.manifest_path) if workspace.manifest_path.is_file() else manifest
    current_fixture_version = manifest.fixture_version if manifest else None

    decisions = [prepare_decision]
    for component in run_context.COMPONENT_NAMES:
        if component == PREPARE_COMPONENT:
            continue
        decisions.append(evaluate_component_reuse(
            component, workspace=workspace, run_id=run_id, release_id=current.release_id,
            baseline_digest=current.digest(), run_started_at_epoch=run_started_at_epoch,
            current_fixture_version=current_fixture_version,
        ))
    order = {component: i for i, component in enumerate(run_context.COMPONENT_NAMES)}
    decisions.sort(key=lambda d: order[d.component])
    by_component = {d.component: i for i, d in enumerate(decisions)}
    decisions[by_component[INSTALLATION_COMPONENT]] = evaluate_installation_reuse(
        decisions[by_component[INSTALLATION_COMPONENT]], adb_runner=adb_runner, tablet_serial=tablet_serial,
    )

    if any(d.blocks_resume for d in decisions):
        record = AttemptRecord(
            attempt_number=attempt_number, run_id=run_id, started_at=started_at,
            completed_at=time.strftime("%Y-%m-%d %H:%M:%S"), command="resume-release", mode="resume",
            operator=operator, original_run_id=run_id,
            immutable_validation={"matched": True, "mismatches": [], "baselineDigest": baseline.digest() if baseline else None},
            components_refused=[d.to_dict() for d in decisions if d.decision == DECISION_REFUSED],
            exit_code=EXIT_BLOCKED, final_result="blocked",
            evidence_acquisition=evidence_acquisition_summary,
        )
        record.write(workspace)
        return ResumeOutcome(
            run_id=run_id, attempt_number=attempt_number, resumable=False, immutable_mismatches=[],
            decisions=decisions, exit_code=EXIT_BLOCKED, attempt=record,
        )

    _snapshot_components(workspace, attempt_number)

    overall, _blocking = _provisional_overall_status(workspace)
    reused = [d.component for d in decisions if d.decision == DECISION_REUSE]
    executed = [{"component": d.component, "reason": d.reason} for d in decisions if d.decision == DECISION_EXECUTE]
    if prepare_executed and not any(e["component"] == PREPARE_COMPONENT for e in executed):
        executed.append({"component": PREPARE_COMPONENT, "reason": prepare_decision.reason})
    refused = [d.to_dict() for d in decisions if d.decision == DECISION_REFUSED]

    any_fail = any(_report_effective_status(_read_json(workspace.component_report_path(c))) == cr.STATUS_FAIL for c in run_context.COMPONENT_NAMES)
    if prepare_blocked:
        exit_code = EXIT_BLOCKED
    elif any_fail:
        exit_code = EXIT_REGRESSION
    else:
        exit_code = EXIT_SUCCESS

    record = AttemptRecord(
        attempt_number=attempt_number, run_id=run_id, started_at=started_at,
        completed_at=time.strftime("%Y-%m-%d %H:%M:%S"), command="resume-release", mode="resume",
        operator=operator, original_run_id=run_id,
        immutable_validation={"matched": True, "mismatches": [], "baselineDigest": baseline.digest() if baseline else None},
        components_reused=reused, components_executed=executed, components_refused=refused,
        exit_code=exit_code, final_result=overall,
        evidence_acquisition=evidence_acquisition_summary,
    )
    record.write(workspace)
    return ResumeOutcome(
        run_id=run_id, attempt_number=attempt_number, resumable=True, immutable_mismatches=[],
        decisions=decisions, exit_code=exit_code, attempt=record,
    )


# ---------------------------------------------------------------------------
# Tester-facing run listing / selection (see the resume launcher)
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    run_id: str
    release_id: "str | None"
    started_at: "str | None"
    overall_result: str
    last_blocking_component: "str | None"
    installation_reusable: "bool | None"


def list_runs(repo_root: Path, *, report_root: "Path | None" = None) -> "list[RunSummary]":
    """Read-only enumeration of every run workspace under reports/runs/, for
    the tester-facing resume launcher's explicit run-selection menu. Never
    picks a "newest" run automatically -- it only ever returns the full list
    for a human (or a test) to choose from."""
    root_base = report_root or repo_root
    runs_dir = root_base / "reports" / "runs"
    if not runs_dir.is_dir():
        return []
    summaries = []
    for run_dir in sorted((p for p in runs_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
        run_id = run_dir.name
        if not run_context.is_valid_run_id(run_id):
            continue
        workspace = run_context.RunWorkspace(root_base, run_id)
        manifest = run_context.RunManifest.load(workspace.manifest_path) if workspace.manifest_path.is_file() else None
        release_config_report = _read_json(workspace.component_report_path("release-config"))
        release_id = release_config_report.get("releaseId") if release_config_report else None
        overall, blocking = _provisional_overall_status(workspace)
        installation_report = _read_json(workspace.component_report_path("installation"))
        installation_reusable = (
            _report_effective_status(installation_report) == cr.STATUS_PASS if installation_report is not None else None
        )
        summaries.append(RunSummary(
            run_id=run_id, release_id=release_id, started_at=(manifest.started_at if manifest else None),
            overall_result=overall, last_blocking_component=blocking, installation_reusable=installation_reusable,
        ))
    return summaries


def render_run_menu(runs: "list[RunSummary]") -> str:
    lines = []
    for idx, run in enumerate(runs, start=1):
        install_note = (
            "yes" if run.installation_reusable else ("no" if run.installation_reusable is False else "unknown")
        )
        lines.append(
            f"{idx}. release={run.release_id or 'unknown'} run={run.run_id} "
            f"started={run.started_at or 'unknown'} result={run.overall_result.upper()} "
            f"blocked_at={run.last_blocking_component or '-'} installation_reusable={install_note}"
        )
    return "\n".join(lines)


def _source_attempt_for(workspace: run_context.RunWorkspace, component: str, numbers: "list[int]") -> "int | None":
    """The earliest attempt whose snapshot shows `component` as an already-
    valid PASS -- i.e. the attempt that actually produced the effective
    evidence a later attempt is reusing, not merely the most recent one."""
    for n in numbers:
        report = _read_json(attempt_dir(workspace, n) / "components" / component / "results.json")
        if _report_effective_status(report) == cr.STATUS_PASS:
            return n
    return numbers[0] if numbers else None


def component_resume_info(workspace: run_context.RunWorkspace) -> "dict[str, dict]":
    """Per-component resume provenance for the LATEST attempt, keyed by
    component slug -- consumed by cli.py's `consolidate` to populate
    ComponentResult.resume so the HTML/JSON/JUnit/ZIP reports make a
    resumed run's reuse/execution history obvious. Returns {} for a run
    that was never resumed (no attempts/ directory at all)."""
    numbers = existing_attempt_numbers(workspace)
    if not numbers:
        return {}

    history: "dict[str, list[str]]" = {component: [] for component in run_context.COMPONENT_NAMES}
    for n in numbers:
        record = load_attempt(workspace, n)
        if record is None:
            continue
        for component in record.components_reused:
            history.setdefault(component, []).append(f"attempt {n}: REUSED PASS")
        for entry in record.components_executed:
            component = entry.get("component") if isinstance(entry, dict) else entry
            reason = entry.get("reason", "") if isinstance(entry, dict) else ""
            history.setdefault(component, []).append(f"attempt {n}: executed ({reason})" if reason else f"attempt {n}: executed")
        for entry in record.components_refused:
            component = entry.get("component") if isinstance(entry, dict) else entry
            reason = entry.get("reason", "") if isinstance(entry, dict) else ""
            history.setdefault(component, []).append(
                f"attempt {n}: refused reuse ({reason})" if reason else f"attempt {n}: refused reuse"
            )

    latest = load_attempt(workspace, numbers[-1])
    if latest is None:
        return {}

    info: "dict[str, dict]" = {}
    for component in latest.components_reused:
        source_attempt = _source_attempt_for(workspace, component, numbers)
        info[component] = {
            "executionMode": "reused",
            "sourceAttempt": source_attempt,
            "evidencePath": str(workspace.component_report_path(component)),
            "reuseValidation": "PASS",
            "previousAttempts": [h for h in history.get(component, []) if not h.startswith(f"attempt {numbers[-1]}:")],
        }
    for entry in latest.components_executed:
        component = entry.get("component") if isinstance(entry, dict) else entry
        # "componentsExecuted" records what THIS attempt identified as
        # needing execution -- for everything except Prepare (which
        # resume-release actually reruns in-process), that's still just a
        # plan until the launcher gets around to it. Only claim
        # "executionMode: executed" once a real report backs it up; a
        # component still NOT_RUN by consolidation time is honestly "still
        # required", not falsely "already executed".
        current_status = _report_effective_status(_read_json(workspace.component_report_path(component)))
        info[component] = {
            "executionMode": "executed" if current_status != cr.STATUS_NOT_RUN else "required",
            "sourceAttempt": numbers[-1],
            "evidencePath": str(workspace.component_report_path(component)),
            "reuseValidation": "NOT_APPLICABLE",
            "previousAttempts": [h for h in history.get(component, []) if not h.startswith(f"attempt {numbers[-1]}:")],
        }
    for entry in latest.components_refused:
        component = entry.get("component") if isinstance(entry, dict) else entry
        input_digest = entry.get("inputDigest") if isinstance(entry, dict) else None
        evidence_path = entry.get("evidencePath") if isinstance(entry, dict) else None
        info.setdefault(component, {
            "executionMode": "required",
            "sourceAttempt": numbers[-1],
            "evidencePath": evidence_path,
            "reuseValidation": "REFUSED",
            "inputDigest": input_digest,
            "previousAttempts": [h for h in history.get(component, []) if not h.startswith(f"attempt {numbers[-1]}:")],
        })
    return info


def choose_run(runs: "list[RunSummary]", *, input_fn=input, print_fn=print) -> "RunSummary | None":
    """Require explicit selection -- never auto-picks the newest run."""
    if not runs:
        print_fn("No runs found under reports/runs/.")
        return None
    print_fn(render_run_menu(runs))
    print_fn("0. Cancel")
    while True:
        choice = (input_fn("Select a run to resume (number): ") or "").strip()
        if choice == "0":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(runs):
            return runs[int(choice) - 1]
        print_fn(f"'{choice}' is not one of the options above -- please choose a number 0-{len(runs)}.")
