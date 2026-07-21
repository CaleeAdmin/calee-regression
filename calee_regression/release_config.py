"""Run-scoped EFFECTIVE RELEASE configuration (Priority 3).

Composes ONE authoritative configuration for a release run from two clearly
owned sources, and records the composition -- including every conflict decision
-- as the run's ``release-config`` evidence
(``reports/runs/<run-id>/release-config/results.json``).

Ownership (the ONE documented precedence rule):

  * The MACHINE (``config/machine.local.yaml``) owns HOW/WHERE a run executes:
    which physical devices (tablet serial, iPhone/Android device ids), tool
    paths, the Calee/CaleeShell package + activity wiring, the report root, and
    whether the machine is authorised for kiosk technical tests.
  * The RELEASE CANDIDATE (``config/release-platforms.yaml`` -- the release-scope
    manifest) owns WHAT the release is: required platform scope, feature scope,
    the production/staging profile, the expected application identities, and the
    backend/environment identity.
  * The release candidate is AUTHORITATIVE for release scope. The machine must be
    CONSISTENT WITH and CAPABLE OF that scope. Any machine value that DISAGREES
    with the release candidate on a shared axis (profile, backend), or any
    required capability the machine LACKS (a required platform with no device, a
    kiosk-technical release on a machine not authorised for it), is a CONFLICT
    that BLOCKS -- two sources of truth must never silently diverge. A machine
    that is capable of MORE than the release requires is never a conflict; the
    release scope simply narrows what actually runs.

No secrets appear here (``machine_config`` rejects a secret-bearing file; only
non-secret selections are recorded).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .machine_config import MachineConfig
from .release_installer import (
    CALEE_PACKAGE_ID,
    CALEESHELL_PACKAGE_ID,
    RELEASE_MANIFEST_SCHEMA_V1,
    ReleaseManifest,
)
from .release_platforms import ExpectedBuildIdentity, ReleaseFeatures, ReleasePlatforms

STATUS_OK = "ok"
STATUS_BLOCKED = "blocked"

# Resolution vocabulary for a composed axis.
RES_AGREE = "agree"                    # machine and release candidate agree
RES_RELEASE_ONLY = "release_only"      # only the release candidate constrains this
RES_MACHINE_ONLY = "machine_only"      # only the machine provides this (no release opinion)
RES_NARROWED = "narrowed"              # machine capable of more; release scope narrows it
RES_CONFLICT = "conflict"              # disagreement / machine cannot satisfy -> BLOCKS


@dataclass
class ConfigConflict:
    """One row of the run's configuration/identity comparison matrix
    (Priority 3). ``axis``/``machine_value``/``release_value``/``resolution``/
    ``explanation`` are the original field names (kept so existing callers --
    ``compose_effective_release_config``'s own machine-vs-release axes below,
    and their tests -- are unaffected). ``source_a``/``source_b`` default to
    "machine"/"release-candidate" for those original axes, but a row can name
    any two sources being compared (e.g. "release-bundle-manifest" vs
    "release-platforms.yaml" for the schema-v1 identity cross-check)."""

    axis: str
    machine_value: object = None
    release_value: object = None
    resolution: str = RES_AGREE
    blocking: bool = False
    explanation: str = ""
    source_a: str = "machine"
    source_b: str = "release-candidate"

    def to_dict(self) -> dict:
        return {
            # Priority 3's canonical comparison-matrix field names.
            "field": self.axis,
            "sourceA": self.source_a,
            "valueA": self.machine_value,
            "sourceB": self.source_b,
            "valueB": self.release_value,
            "result": self.resolution,
            "blocking": self.blocking,
            "detail": self.explanation,
            # Original field names, preserved for existing callers/tests.
            "axis": self.axis,
            "machineValue": self.machine_value,
            "releaseValue": self.release_value,
            "resolution": self.resolution,
            "explanation": self.explanation,
        }


def _compare_row(
    field_name: str, source_a: str, value_a, source_b: str, value_b, *, blocking_on_conflict: bool = True
) -> ConfigConflict:
    """One Priority-3 comparison-matrix row between two named, independent
    sources. Both sources silent -> agree (nothing to compare). Only one
    stated -> that source's value stands, non-blocking. Both stated and equal
    -> agree. Both stated and different -> CONFLICT, blocking by default (a
    technical owner must reconcile the disagreement, never have one side
    silently win)."""
    if value_a is None and value_b is None:
        return ConfigConflict(
            axis=field_name, machine_value=None, release_value=None, resolution=RES_AGREE, blocking=False,
            explanation=f"{field_name}: not specified by {source_a} or {source_b}.",
            source_a=source_a, source_b=source_b,
        )
    if value_a is None:
        return ConfigConflict(
            axis=field_name, machine_value=None, release_value=value_b, resolution=RES_RELEASE_ONLY, blocking=False,
            explanation=f"{field_name}: only {source_b} specifies {value_b!r}.",
            source_a=source_a, source_b=source_b,
        )
    if value_b is None:
        return ConfigConflict(
            axis=field_name, machine_value=value_a, release_value=None, resolution=RES_MACHINE_ONLY, blocking=False,
            explanation=f"{field_name}: only {source_a} specifies {value_a!r}.",
            source_a=source_a, source_b=source_b,
        )
    if value_a == value_b:
        return ConfigConflict(
            axis=field_name, machine_value=value_a, release_value=value_b, resolution=RES_AGREE, blocking=False,
            explanation=f"{field_name}: {source_a} and {source_b} agree ({value_a!r}).",
            source_a=source_a, source_b=source_b,
        )
    return ConfigConflict(
        axis=field_name, machine_value=value_a, release_value=value_b, resolution=RES_CONFLICT,
        blocking=blocking_on_conflict,
        explanation=f"{field_name}: {source_a}={value_a!r} disagrees with {source_b}={value_b!r}.",
        source_a=source_a, source_b=source_b,
    )


@dataclass
class EffectiveReleaseConfig:
    """The one effective release configuration for a run: machine selections,
    release-candidate selections, and how every shared axis was resolved."""

    run_id: "str | None" = None
    release_id: "str | None" = None
    status: str = STATUS_OK
    # Priority 2: which release-bundle-manifest schema version drove this
    # composition. 1 when no bundle manifest was available (release-platforms.
    # yaml alone still drives composition, as before schema v2 existed).
    schema_version: int = 1

    # Machine selections (HOW/WHERE).
    tablet_serial: "str | None" = None
    iphone_device: "str | None" = None
    android_device: "str | None" = None
    report_root: "str | None" = None
    calee_package_id: "str | None" = None
    caleeshell_package_id: "str | None" = None
    home_activity: "str | None" = None
    calee_launch_action: "str | None" = None
    allow_caleeshell_technical: bool = False
    machine_backend_url: "str | None" = None

    # Release-candidate selections (WHAT).
    selected_backend: "str | None" = None
    enabled_platforms: "list[str]" = field(default_factory=list)
    enabled_features: "list[str]" = field(default_factory=list)
    profile: str = "staging"
    distributed_build_required: bool = False
    expected_identities: dict = field(default_factory=dict)

    # Composition record.
    conflicts: "list[ConfigConflict]" = field(default_factory=list)
    detail: "list[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def device_id_for(self, platform: str) -> "str | None":
        return {"ios": self.iphone_device, "android": self.android_device, "tablet": self.tablet_serial}.get(platform)

    def to_dict(self) -> dict:
        release_selections = {
            "selectedBackend": self.selected_backend,
            "enabledPlatforms": list(self.enabled_platforms),
            "enabledFeatures": list(self.enabled_features),
            "profile": self.profile,
            "distributedBuildRequired": self.distributed_build_required,
            "expectedIdentities": dict(self.expected_identities),
        }
        return {
            "status": self.status,
            "runId": self.run_id,
            "releaseId": self.release_id,
            "schemaVersion": self.schema_version,
            "detail": list(self.detail),
            "machineSelections": {
                "tabletSerial": self.tablet_serial,
                "iphoneDevice": self.iphone_device,
                "androidDevice": self.android_device,
                "reportRoot": self.report_root,
                "caleePackageId": self.calee_package_id,
                "caleeShellPackageId": self.caleeshell_package_id,
                "homeActivity": self.home_activity,
                "caleeLaunchAction": self.calee_launch_action,
                "allowCaleeShellTechnical": self.allow_caleeshell_technical,
                "machineBackendUrl": self.machine_backend_url,
            },
            "releaseSelections": release_selections,
            # Priority 5: a digest of ONLY releaseSelections (see
            # release_selections_digest's docstring for why machine-derived
            # fields are excluded) -- the installer independently recomputes
            # this same digest and compares it against what the candidate
            # fingerprint recorded, rather than trusting this report's own
            # copy of it.
            "releaseConfigDigest": release_selections_digest(release_selections),
            "deviceIds": {
                "tablet": self.tablet_serial,
                "ios": self.iphone_device,
                "android": self.android_device,
            },
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


def release_selections_digest(release_selections: dict) -> str:
    """A digest (Priority 5) over exactly the RELEASE-derived selections a
    composed ``EffectiveReleaseConfig`` produced -- profile, scope, backend,
    distributed-build requirement, and expected identities -- deliberately
    EXCLUDING ``machineSelections``/``deviceIds``/``conflicts``. Those are
    derived from the machine and would make this digest depend on which
    machine composed it; the release-derived fields are what an installer
    running on a DIFFERENT machine (or the same machine independently
    recomputing this digest from a same-run release-config report, or a
    schema-v2 bundle's own manifest, per Priority 1/5) must be able to
    reproduce byte-for-byte and compare against what's embedded in the
    candidate fingerprint (``release_candidate.py``)."""
    canonical = json.dumps(release_selections, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_selector_evidence_required(
    *, profile: "str | None", enabled_platforms: "list[str] | None", schema_version: "int | None" = None,
    manifest_required: "bool | None" = None,
) -> "bool | None":
    """Priority 2 (this session) -- the ONE documented selector-evidence
    precedence, evaluated with only what is knowable at release-config
    composition time (no explicit per-command CLI flag, no waiver -- those
    only exist at the point a technical owner actually invokes
    ``selector-contract``/``consolidate``, and ``consolidate`` re-derives and
    re-enforces its OWN, fully authoritative decision independently of this
    one -- see its ``selector_contract_gating`` logic):

      * a PRODUCTION release with a mobile platform (android/ios) in scope --
        mandatory, regardless of what the manifest states;
      * a non-production schema-v2 release -- the bundle manifest's own
        ``caleeMobile.selectorEvidenceRequired`` decides, when it states one;
      * a schema-v1 release, or a v2 release whose manifest states no
        opinion -- legacy policy: mandatory whenever a mobile platform is in
        scope;
      * no mobile platform in scope at all, and nothing else says otherwise
        -- selector evidence is not applicable here (returns ``None``).

    A launcher uses this to decide, up front, whether the mobile UI legs are
    even worth attempting; it is never the last word -- ``consolidate``
    always re-validates independently before a release can PASS.
    """
    mobile_in_scope = any(p in (enabled_platforms or ()) for p in ("android", "ios"))
    if (profile or "").strip().lower() == "production" and mobile_in_scope:
        return True
    if schema_version == 2 and manifest_required is not None:
        return bool(manifest_required)
    if mobile_in_scope:
        return True
    return None


def _machine_can(platform: str, machine: MachineConfig) -> bool:
    """Whether the machine is set up to run a given platform."""
    if platform == "tablet":
        return bool(machine.tablet_serial)
    if platform == "ios":
        return "ios" in machine.mobile_platforms and bool(machine.iphone_device)
    if platform == "android":
        return "android" in machine.mobile_platforms
    return False


def _identity_matrix_rows(
    *,
    bundle_manifest: "ReleaseManifest | None",
    expected: ExpectedBuildIdentity,
    is_v2: bool,
    passed_release_id: "str | None",
) -> "list[ConfigConflict]":
    """Priority 3's pre-install identity comparison-matrix rows for the
    fields ``compose_effective_release_config``'s other axes below don't
    already cover: release ID and the full Calee/CaleeShell/CaleeMobile
    expected-identity fields. Empty when no bundle manifest was verified for
    this run (nothing to compare)."""
    if bundle_manifest is None:
        return []
    rows: "list[ConfigConflict]" = []
    bundle_src = "release-bundle-manifest"
    platforms_src = "release-platforms.yaml"

    rows.append(_compare_row(
        "releaseId", bundle_src, bundle_manifest.release_id, "cli/env override", passed_release_id,
    ))

    calee_app = bundle_manifest.calee
    shell_app = bundle_manifest.caleeshell

    if is_v2:
        # Schema v2: the bundle manifest is self-contained/authoritative --
        # parse_manifest already rejected an incomplete/malformed manifest, so
        # every row here is the auditable record of that completeness check
        # (Priority 3: "validate completeness and internal consistency"),
        # never a second, independent cross-check against another source.
        rows.append(_compare_row("profile", bundle_src, bundle_manifest.profile, bundle_src, bundle_manifest.profile))
        rows.append(_compare_row("backend", bundle_src, bundle_manifest.backend, bundle_src, bundle_manifest.backend))
        if bundle_manifest.platforms is not None:
            for label, value in (
                ("tablet", bundle_manifest.platforms.tablet),
                ("mobileAndroid", bundle_manifest.platforms.mobile_android),
                ("mobileIos", bundle_manifest.platforms.mobile_ios),
            ):
                rows.append(_compare_row(f"platforms.{label}", bundle_src, value, bundle_src, value))
        if bundle_manifest.features is not None:
            for label, value in (
                ("synchronization", bundle_manifest.features.synchronization),
                ("meals", bundle_manifest.features.meals),
                ("onboarding", bundle_manifest.features.onboarding),
                ("googleCalendar", bundle_manifest.features.google_calendar),
                ("kioskAdmin", bundle_manifest.features.kiosk_admin),
                ("notifications", bundle_manifest.features.notifications),
            ):
                rows.append(_compare_row(f"features.{label}", bundle_src, value, bundle_src, value))
        for app, prefix in ((calee_app, "calee"), (shell_app, "caleeShell")):
            if app is None:
                continue
            for label, value in (
                ("packageId", app.package_id), ("versionName", app.version_name),
                ("versionCode", app.version_code), ("gitSha", app.git_sha),
                ("signerSha256", app.signer_sha256),
            ):
                rows.append(_compare_row(f"{prefix}.{label}", bundle_src, value, bundle_src, value))
        cm = bundle_manifest.calee_mobile
        if cm is not None:
            for label, value in (
                ("version", cm.version), ("gitSha", cm.git_sha),
                ("selectorEvidenceRequired", cm.selector_evidence_required),
                ("distributedBuildAcceptanceRequired", cm.distributed_build_acceptance_required),
            ):
                rows.append(_compare_row(f"caleeMobile.{label}", bundle_src, value, bundle_src, value))
    else:
        # Schema v1: cross-check every value BOTH the bundle manifest and
        # release-platforms.yaml happen to declare -- disagreement BLOCKS,
        # two sources of truth must never silently diverge. A v1 manifest has
        # no CaleeMobile/profile/backend/platform/feature fields at all, so
        # there is nothing to compare for those here (see the deprecation
        # warning recorded by the caller instead).
        if calee_app is not None:
            rows.append(_compare_row("calee.versionName", bundle_src, calee_app.version_name, platforms_src, expected.calee_build_version))
            rows.append(_compare_row("calee.gitSha", bundle_src, calee_app.git_sha, platforms_src, expected.calee_git_sha))
            expected_version_code = (
                str(expected.calee_version_code).strip() if expected.calee_version_code not in (None, "") else None
            )
            bundle_version_code = str(calee_app.version_code) if calee_app.version_code is not None else None
            rows.append(_compare_row("calee.versionCode", bundle_src, bundle_version_code, platforms_src, expected_version_code))
        if shell_app is not None:
            rows.append(_compare_row("caleeShell.versionName", bundle_src, shell_app.version_name, platforms_src, expected.caleeshell_version))
    return rows


def compose_effective_release_config(
    machine: MachineConfig,
    platforms: ReleasePlatforms,
    features: ReleaseFeatures,
    expected: ExpectedBuildIdentity,
    *,
    run_id: "str | None" = None,
    release_id: "str | None" = None,
    expected_backend: "str | None" = None,
    distributed_build_required: bool = False,
    bundle_manifest: "ReleaseManifest | None" = None,
) -> EffectiveReleaseConfig:
    """Compose the effective release configuration and detect conflicts under
    the one precedence rule (see module docstring). Never raises; an unresolved
    conflict is recorded and makes the whole composition ``blocked``.

    Priority 2 -- ``bundle_manifest`` (the already-verified release bundle
    manifest; see release_installer.verify_release_bundle):

      * schema version 2 -- AUTHORITATIVE for release scope (platforms/
        features/profile/backend) and expected identity (Calee/CaleeShell/
        CaleeMobile). ``platforms``/``features``/``expected_backend`` (which
        would otherwise come from release-platforms.yaml) are NOT consulted;
        release-platforms.yaml is not required. Every manifest-declared value
        is recorded as a self-consistency row in the comparison matrix
        (Priority 3).
      * schema version 1, or no bundle manifest -- release-platforms.yaml
        drives composition exactly as before. When a v1 bundle manifest WAS
        supplied, a deprecation warning is recorded, and every identity value
        it happens to declare is cross-checked against release-platforms.
        yaml's expected_build_identity -- disagreement BLOCKS (Priority 2:
        "cross-check every overlapping value; block on disagreement").
    """
    is_v2 = bundle_manifest is not None and bundle_manifest.is_schema_v2
    passed_release_id = release_id
    release_source_label = "release-platforms.yaml"
    if is_v2:
        # The bundle manifest is authoritative for scope in schema v2 -- swap
        # in its platform/feature scope for the rest of this function (their
        # attribute names deliberately mirror ReleasePlatforms/ReleaseFeatures
        # so every existing conflict-detection axis below is reused as-is).
        platforms = bundle_manifest.platforms or ReleasePlatforms(tablet=False, mobile_android=False, mobile_ios=False)
        features = bundle_manifest.features or ReleaseFeatures(
            synchronization=False, meals=False, onboarding=False, google_calendar=False, kiosk_admin=False,
        )
        expected_backend = bundle_manifest.backend
        release_id = bundle_manifest.release_id
        release_source_label = "release-bundle-manifest"
    elif bundle_manifest is not None and release_id is None:
        # Schema v1 (or unversioned) with no independent --release-id/
        # CALEE_RELEASE_ID/release-platforms.yaml override: adopt the bundle
        # manifest's own releaseId rather than leaving the composed config's
        # release_id null -- there is nothing to disagree with yet, so this is
        # not a conflict (see _identity_matrix_rows' releaseId row below).
        release_id = bundle_manifest.release_id

    cfg = EffectiveReleaseConfig(
        run_id=run_id,
        release_id=release_id,
        schema_version=bundle_manifest.schema_version if bundle_manifest is not None else RELEASE_MANIFEST_SCHEMA_V1,
        tablet_serial=machine.tablet_serial,
        iphone_device=machine.iphone_device,
        android_device=machine.android_device,
        report_root=machine.report_dir,
        calee_package_id=machine.calee_package_id,
        caleeshell_package_id=machine.caleeshell_package_id,
        home_activity=machine.home_activity,
        calee_launch_action=machine.calee_launch_action,
        allow_caleeshell_technical=bool(machine.allow_caleeshell_technical),
        machine_backend_url=machine.backend_url,
        distributed_build_required=bool(
            bundle_manifest.calee_mobile.distributed_build_acceptance_required
            if (is_v2 and bundle_manifest.calee_mobile is not None) else distributed_build_required
        ),
    )
    if bundle_manifest is not None and not is_v2:
        cfg.detail.append(
            "DEPRECATED: the release bundle manifest uses schema version 1 (or declares none). "
            "Migrate to schemaVersion 2 so the bundle manifest is self-contained and authoritative "
            "for release scope and expected identity -- see docs/RELEASE_INSTALLER.md."
        )

    # ── platform scope: release requires; machine must be capable ──────────
    required = [p for p, on in (("tablet", platforms.tablet),
                                ("android", platforms.mobile_android),
                                ("ios", platforms.mobile_ios)) if on]
    enabled: "list[str]" = []
    for platform in ("tablet", "android", "ios"):
        release_requires = platform in required
        machine_capable = _machine_can(platform, machine)
        if release_requires and machine_capable:
            enabled.append(platform)
            cfg.conflicts.append(ConfigConflict(
                axis=f"platform:{platform}", machine_value="capable", release_value="required",
                resolution=RES_AGREE, blocking=False, source_b=release_source_label,
                explanation=f"{platform}: required by the release candidate and the machine is configured for it.",
            ))
        elif release_requires and not machine_capable:
            cfg.conflicts.append(ConfigConflict(
                axis=f"platform:{platform}", machine_value="not-configured", release_value="required",
                resolution=RES_CONFLICT, blocking=True, source_b=release_source_label,
                explanation=(
                    f"{platform}: the release candidate REQUIRES it, but this machine is not configured "
                    f"to run it (no device / platform not enabled). Align the machine or the release scope."
                ),
            ))
        elif (not release_requires) and machine_capable:
            cfg.conflicts.append(ConfigConflict(
                axis=f"platform:{platform}", machine_value="capable", release_value="not-required",
                resolution=RES_NARROWED, blocking=False, source_b=release_source_label,
                explanation=f"{platform}: the machine can run it, but the release does not require it -- narrowed out.",
            ))
    cfg.enabled_platforms = enabled

    # ── feature scope + kiosk authorisation ────────────────────────────────
    feature_flags = {
        "synchronization": features.synchronization, "meals": features.meals,
        "onboarding": features.onboarding, "google_calendar": features.google_calendar,
        "kiosk_admin": features.kiosk_admin,
    }
    if is_v2:
        feature_flags["notifications"] = features.notifications
    cfg.enabled_features = [name for name, on in feature_flags.items() if on]
    if features.kiosk_admin and not machine.allow_caleeshell_technical:
        cfg.conflicts.append(ConfigConflict(
            axis="feature:kiosk_admin", machine_value="not-authorised", release_value="required",
            resolution=RES_CONFLICT, blocking=True, source_b=release_source_label,
            explanation=(
                "kiosk_admin: the release requires the kiosk/admin feature, but this machine is not "
                "authorised for kiosk technical tests (allow_caleeshell_technical: false)."
            ),
        ))

    # ── profile: release candidate authoritative; machine must agree ───────
    release_profile = bundle_manifest.profile if is_v2 else ("production" if expected.production else "staging")
    cfg.profile = release_profile
    machine_profile = (machine.release_profile or "").strip().lower()
    if machine_profile and machine_profile != release_profile:
        cfg.conflicts.append(ConfigConflict(
            axis="profile", machine_value=machine_profile, release_value=release_profile,
            resolution=RES_CONFLICT, blocking=True, source_b=release_source_label,
            explanation=(
                f"profile: the machine is configured for {machine_profile!r} but the release candidate is "
                f"{release_profile!r}. A release must not run against a machine set up for a different profile."
            ),
        ))
    else:
        cfg.conflicts.append(ConfigConflict(
            axis="profile", machine_value=machine_profile or None, release_value=release_profile,
            resolution=RES_AGREE if machine_profile else RES_RELEASE_ONLY, blocking=False, source_b=release_source_label,
            explanation=f"profile: {release_profile} (release candidate authoritative).",
        ))

    # ── backend: machine provides the URL; release may pin an environment ──
    cfg.selected_backend = machine.backend_url
    if expected_backend:
        if machine.backend_url and machine.backend_url != expected_backend:
            cfg.conflicts.append(ConfigConflict(
                axis="backend", machine_value=machine.backend_url, release_value=expected_backend,
                resolution=RES_CONFLICT, blocking=True, source_b=release_source_label,
                explanation=(
                    f"backend: the machine points at {machine.backend_url!r} but the release candidate "
                    f"expects {expected_backend!r}. The tested backend must match the release's environment."
                ),
            ))
            cfg.selected_backend = expected_backend
        else:
            cfg.selected_backend = expected_backend

    # ── canonical package ids must be consistent everywhere ────────────────
    # The installer only ever installs the two canonical packages; the machine
    # config's declared ids must equal them so a single, consistent package id
    # reaches the installer, the signer read and the solution check.
    for label, machine_pkg, canonical in (
        ("calee", machine.calee_package_id, CALEE_PACKAGE_ID),
        ("caleeShell", machine.caleeshell_package_id, CALEESHELL_PACKAGE_ID),
    ):
        if machine_pkg and machine_pkg != canonical:
            cfg.conflicts.append(ConfigConflict(
                axis=f"packageId:{label}", machine_value=machine_pkg, release_value=canonical,
                resolution=RES_CONFLICT, blocking=True, source_b="installer canonical package id",
                explanation=(
                    f"{label} package id: the machine declares {machine_pkg!r} but the installer's canonical "
                    f"package is {canonical!r}. The tablet solution only installs the canonical packages."
                ),
            ))

    # ── expected application identities ─────────────────────────────────────
    if is_v2:
        calee_app, shell_app, cm = bundle_manifest.calee, bundle_manifest.caleeshell, bundle_manifest.calee_mobile
        cfg.expected_identities = {
            "calee": {
                "buildVersion": calee_app.version_name if calee_app else None,
                "gitSha": calee_app.git_sha if calee_app else None,
                "applicationId": calee_app.package_id if calee_app else None,
                "versionCode": calee_app.version_code if calee_app else None,
                "signerSha256": calee_app.signer_sha256 if calee_app else None,
            },
            "caleeShell": {
                "version": shell_app.version_name if shell_app else None,
                "gitSha": shell_app.git_sha if shell_app else None,
                "applicationId": shell_app.package_id if shell_app else None,
                "versionCode": shell_app.version_code if shell_app else None,
                "signerSha256": shell_app.signer_sha256 if shell_app else None,
            },
            "caleeMobile": {
                "buildVersion": cm.version if cm else None,
                "gitSha": cm.git_sha if cm else None,
                "selectorEvidenceRequired": cm.selector_evidence_required if cm else True,
                "distributedBuildAcceptanceRequired": cm.distributed_build_acceptance_required if cm else True,
            },
        }
    else:
        cfg.expected_identities = {
            "calee": {
                "buildVersion": expected.calee_build_version, "gitSha": expected.calee_git_sha,
                "applicationId": expected.calee_application_id, "versionCode": expected.calee_version_code,
            },
            "caleeShell": {"version": expected.caleeshell_version},
            "caleeMobile": {
                "buildVersion": expected.caleemobile_build_version, "gitSha": expected.caleemobile_git_sha,
            },
        }

    # ── Priority 3: the full pre-install identity comparison matrix ────────
    cfg.conflicts.extend(_identity_matrix_rows(
        bundle_manifest=bundle_manifest, expected=expected, is_v2=is_v2, passed_release_id=passed_release_id,
    ))

    blocking = [c for c in cfg.conflicts if c.blocking]
    if blocking:
        cfg.status = STATUS_BLOCKED
        cfg.detail.append(
            f"{len(blocking)} configuration conflict(s) block this run: "
            + "; ".join(c.explanation for c in blocking)
        )
    else:
        cfg.detail.append(
            "Machine and release candidate composed into one effective release configuration with no "
            "blocking conflicts (release candidate authoritative for scope; machine capable of it)."
        )
    return cfg
