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

from dataclasses import dataclass, field

from .machine_config import MachineConfig
from .release_installer import CALEE_PACKAGE_ID, CALEESHELL_PACKAGE_ID
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
    axis: str
    machine_value: object = None
    release_value: object = None
    resolution: str = RES_AGREE
    blocking: bool = False
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "axis": self.axis,
            "machineValue": self.machine_value,
            "releaseValue": self.release_value,
            "resolution": self.resolution,
            "blocking": self.blocking,
            "explanation": self.explanation,
        }


@dataclass
class EffectiveReleaseConfig:
    """The one effective release configuration for a run: machine selections,
    release-candidate selections, and how every shared axis was resolved."""

    run_id: "str | None" = None
    release_id: "str | None" = None
    status: str = STATUS_OK

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
        return {
            "status": self.status,
            "runId": self.run_id,
            "releaseId": self.release_id,
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
            "releaseSelections": {
                "selectedBackend": self.selected_backend,
                "enabledPlatforms": list(self.enabled_platforms),
                "enabledFeatures": list(self.enabled_features),
                "profile": self.profile,
                "distributedBuildRequired": self.distributed_build_required,
                "expectedIdentities": dict(self.expected_identities),
            },
            "deviceIds": {
                "tablet": self.tablet_serial,
                "ios": self.iphone_device,
                "android": self.android_device,
            },
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


def _machine_can(platform: str, machine: MachineConfig) -> bool:
    """Whether the machine is set up to run a given platform."""
    if platform == "tablet":
        return bool(machine.tablet_serial)
    if platform == "ios":
        return "ios" in machine.mobile_platforms and bool(machine.iphone_device)
    if platform == "android":
        return "android" in machine.mobile_platforms
    return False


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
) -> EffectiveReleaseConfig:
    """Compose the effective release configuration and detect conflicts under
    the one precedence rule (see module docstring). Never raises; an unresolved
    conflict is recorded and makes the whole composition ``blocked``."""
    cfg = EffectiveReleaseConfig(
        run_id=run_id,
        release_id=release_id,
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
        distributed_build_required=bool(distributed_build_required),
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
                resolution=RES_AGREE, blocking=False,
                explanation=f"{platform}: required by the release candidate and the machine is configured for it.",
            ))
        elif release_requires and not machine_capable:
            cfg.conflicts.append(ConfigConflict(
                axis=f"platform:{platform}", machine_value="not-configured", release_value="required",
                resolution=RES_CONFLICT, blocking=True,
                explanation=(
                    f"{platform}: the release candidate REQUIRES it, but this machine is not configured "
                    f"to run it (no device / platform not enabled). Align the machine or the release scope."
                ),
            ))
        elif (not release_requires) and machine_capable:
            cfg.conflicts.append(ConfigConflict(
                axis=f"platform:{platform}", machine_value="capable", release_value="not-required",
                resolution=RES_NARROWED, blocking=False,
                explanation=f"{platform}: the machine can run it, but the release does not require it -- narrowed out.",
            ))
    cfg.enabled_platforms = enabled

    # ── feature scope + kiosk authorisation ────────────────────────────────
    feature_flags = {
        "synchronization": features.synchronization, "meals": features.meals,
        "onboarding": features.onboarding, "google_calendar": features.google_calendar,
        "kiosk_admin": features.kiosk_admin,
    }
    cfg.enabled_features = [name for name, on in feature_flags.items() if on]
    if features.kiosk_admin and not machine.allow_caleeshell_technical:
        cfg.conflicts.append(ConfigConflict(
            axis="feature:kiosk_admin", machine_value="not-authorised", release_value="required",
            resolution=RES_CONFLICT, blocking=True,
            explanation=(
                "kiosk_admin: the release requires the kiosk/admin feature, but this machine is not "
                "authorised for kiosk technical tests (allow_caleeshell_technical: false)."
            ),
        ))

    # ── profile: release candidate authoritative; machine must agree ───────
    release_profile = "production" if expected.production else "staging"
    cfg.profile = release_profile
    machine_profile = (machine.release_profile or "").strip().lower()
    if machine_profile and machine_profile != release_profile:
        cfg.conflicts.append(ConfigConflict(
            axis="profile", machine_value=machine_profile, release_value=release_profile,
            resolution=RES_CONFLICT, blocking=True,
            explanation=(
                f"profile: the machine is configured for {machine_profile!r} but the release candidate is "
                f"{release_profile!r}. A release must not run against a machine set up for a different profile."
            ),
        ))
    else:
        cfg.conflicts.append(ConfigConflict(
            axis="profile", machine_value=machine_profile or None, release_value=release_profile,
            resolution=RES_AGREE if machine_profile else RES_RELEASE_ONLY, blocking=False,
            explanation=f"profile: {release_profile} (release candidate authoritative).",
        ))

    # ── backend: machine provides the URL; release may pin an environment ──
    cfg.selected_backend = machine.backend_url
    if expected_backend:
        if machine.backend_url and machine.backend_url != expected_backend:
            cfg.conflicts.append(ConfigConflict(
                axis="backend", machine_value=machine.backend_url, release_value=expected_backend,
                resolution=RES_CONFLICT, blocking=True,
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
                resolution=RES_CONFLICT, blocking=True,
                explanation=(
                    f"{label} package id: the machine declares {machine_pkg!r} but the installer's canonical "
                    f"package is {canonical!r}. The tablet solution only installs the canonical packages."
                ),
            ))

    # ── expected application identities (release candidate owns these) ─────
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
