"""Adapter that makes config/machine.local.yaml the single authoritative
per-MacBook configuration source for a full release run (Priority 4).

Before this, a release run drew from several independent configuration paths:
the machine config (config/machine.local.yaml -> machine_config.MachineConfig),
the lower-level tester config (config/tester.local.yaml -> config.Config), and
config/release-platforms.yaml. Values that overlap (the tablet serial vs udid,
the expected tablet state, the package ids, the CaleeShell HOME activity, the
Calee launch action, the report dir, the technical-test permission) could
silently disagree, so it was ambiguous which one actually controlled a run.

This module reconciles them into ONE effective configuration:

  * machine.local.yaml is AUTHORITATIVE for every value it owns;
  * the lower-level tester config still supplies the values machine config does
    not model (appium_url, launch_strategy, screenshot thresholds, ...), so the
    existing lower-level commands keep working unchanged;
  * a conflicting legacy value is explicitly OVERRIDDEN with a recorded
    explanation (never two silently-disagreeing sources of truth);
  * the reconciled result is written back as an effective tester config the
    runner loads, and a secrets-excluded snapshot records the selected backend,
    devices, package ids and release profile in the run evidence.

No secrets ever appear here -- machine_config.load_machine_config already
rejects a secret-bearing file, and the snapshot only records non-secret
selections.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .machine_config import MachineConfig

# The machine-config field <-> tester-config field overlaps this adapter
# reconciles. Each entry: (machine attribute, tester-config key, human label).
# home_activity is handled separately (it splits into shell_package + activity).
_OVERLAP = (
    ("tablet_serial", "udid", "tablet serial / device udid"),
    ("expected_tablet_state", "expected_state", "expected tablet state"),
    ("calee_package_id", "app_package", "Calee package id"),
    ("caleeshell_package_id", "shell_package", "CaleeShell package id"),
    ("calee_launch_action", "start_action", "Calee launch action"),
    ("report_dir", "report_dir", "report directory"),
)


@dataclass
class Reconciliation:
    """One reconciled field: what machine config says, what the legacy tester
    config said, and how the conflict was resolved."""

    field: str
    machine_value: "str | None"
    legacy_value: "str | None"
    resolution: str  # "machine_only" | "agree" | "overridden"
    explanation: str

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "machineValue": self.machine_value,
            "legacyValue": self.legacy_value,
            "resolution": self.resolution,
            "explanation": self.explanation,
        }


@dataclass
class EffectiveConfig:
    """The reconciled, authoritative configuration for a run.

    ``tester_config`` is a plain dict suitable to write as the effective
    config/tester.local.yaml the runner loads (config.load_config). The
    remaining fields are the machine-only values that drive the installer,
    fixture reset, sync and platform selection directly."""

    tester_config: dict = field(default_factory=dict)
    reconciliations: "list[Reconciliation]" = field(default_factory=list)
    # Machine-only authoritative values (no lower-level-config equivalent).
    tablet_serial: "str | None" = None
    release_bundle_dir: "str | None" = None
    backend_url: "str | None" = None
    release_profile: "str | None" = None
    mobile_platforms: "list[str]" = field(default_factory=list)
    iphone_device: "str | None" = None
    android_device: "str | None" = None
    calee_package_id: "str | None" = None
    caleeshell_package_id: "str | None" = None
    home_activity: "str | None" = None
    calee_launch_action: "str | None" = None
    report_dir: "str | None" = None
    allow_caleeshell_technical: bool = False


def _split_home_activity(home_activity: str) -> "tuple[str | None, str | None]":
    """Split ``com.viso.caleeshell/.ui.LauncherActivity`` into
    ``(package, activity)``. A value with no ``/`` yields (None, value)."""
    if "/" in home_activity:
        pkg, _, activity = home_activity.partition("/")
        return pkg or None, activity or None
    return None, home_activity or None


def reconcile(machine: MachineConfig, legacy_raw: "dict | None") -> EffectiveConfig:
    """Reconcile the authoritative machine config with an optional legacy tester
    config (its raw YAML mapping). Machine config wins every overlap; a
    differing legacy value is overridden and recorded.

    ``legacy_raw`` is the raw dict from config/tester.local.yaml (or None). Its
    non-overlapping keys (appium_url, launch_strategy, screenshot thresholds,
    ...) are preserved so the lower-level commands keep working."""
    legacy = dict(legacy_raw or {})
    effective_tester = dict(legacy)
    reconciliations: "list[Reconciliation]" = []

    def _apply(machine_value, key, label):
        legacy_value = legacy.get(key)
        if machine_value is None:
            return  # machine doesn't own this value; leave legacy as-is
        if legacy_value is None:
            resolution, explanation = "machine_only", f"{label}: taken from machine config ({machine_value!r})."
        elif str(legacy_value) == str(machine_value):
            resolution, explanation = "agree", f"{label}: machine config and tester config agree ({machine_value!r})."
        else:
            resolution, explanation = (
                "overridden",
                f"{label}: machine config value {machine_value!r} OVERRODE the tester config value "
                f"{legacy_value!r} -- machine.local.yaml is the single authoritative source.",
            )
        effective_tester[key] = machine_value
        reconciliations.append(
            Reconciliation(field=key, machine_value=str(machine_value) if machine_value is not None else None,
                           legacy_value=str(legacy_value) if legacy_value is not None else None,
                           resolution=resolution, explanation=explanation)
        )

    for machine_attr, key, label in _OVERLAP:
        _apply(getattr(machine, machine_attr), key, label)

    # HOME activity: machine config owns the whole CaleeShell HOME component,
    # which maps onto the tester config's shell_package + shell_activity.
    home_pkg, home_activity = _split_home_activity(machine.home_activity)
    if home_activity is not None:
        _apply(home_activity, "shell_activity", "CaleeShell HOME activity")
    if home_pkg is not None:
        # Keep shell_package consistent with home_activity's package (it must
        # equal caleeshell_package_id -- already applied above; re-assert here).
        effective_tester["shell_package"] = home_pkg

    # CaleeShell technical-test permission maps onto allow_release_technical.
    _apply(
        bool(machine.allow_caleeshell_technical), "allow_release_technical",
        "CaleeShell technical-test permission",
    )

    return EffectiveConfig(
        tester_config=effective_tester,
        reconciliations=reconciliations,
        tablet_serial=machine.tablet_serial,
        release_bundle_dir=str(machine.resolved_bundle_dir()),
        backend_url=machine.backend_url,
        release_profile=machine.release_profile,
        mobile_platforms=list(machine.mobile_platforms),
        iphone_device=machine.iphone_device,
        android_device=machine.android_device,
        calee_package_id=machine.calee_package_id,
        caleeshell_package_id=machine.caleeshell_package_id,
        home_activity=machine.home_activity,
        calee_launch_action=machine.calee_launch_action,
        report_dir=machine.report_dir,
        allow_caleeshell_technical=bool(machine.allow_caleeshell_technical),
    )


def snapshot(effective: EffectiveConfig, *, machine_config_path: "str | None" = None,
             effective_tester_config_path: "str | None" = None) -> dict:
    """A secrets-excluded snapshot of the authoritative selections for the run
    evidence (Priority 4). Records the selected backend, devices, package ids
    and release profile so they appear in the final consolidated report + ZIP.

    ``status`` is always ``ok`` here -- a failed load is recorded by the CLI as
    a BLOCKED snapshot before this is ever reached."""
    return {
        "status": "ok",
        "detail": [
            f"Machine configuration is the single authoritative source for this run "
            f"({len([r for r in effective.reconciliations if r.resolution == 'overridden'])} legacy "
            f"value(s) overridden, recorded below)."
        ],
        "machineConfigPath": machine_config_path,
        "effectiveTesterConfigPath": effective_tester_config_path,
        "selected": {
            "backendUrl": effective.backend_url,
            "releaseProfile": effective.release_profile,
            "releaseBundleDir": effective.release_bundle_dir,
            "tabletSerial": effective.tablet_serial,
            "expectedTabletState": effective.tester_config.get("expected_state"),
            "mobilePlatforms": list(effective.mobile_platforms),
            "iphoneDevice": effective.iphone_device,
            "androidDevice": effective.android_device,
            "caleePackageId": effective.calee_package_id,
            "caleeShellPackageId": effective.caleeshell_package_id,
            "homeActivity": effective.home_activity,
            "caleeLaunchAction": effective.calee_launch_action,
            "reportDir": effective.report_dir,
            "allowCaleeShellTechnical": effective.allow_caleeshell_technical,
        },
        "reconciliations": [r.to_dict() for r in effective.reconciliations],
    }
