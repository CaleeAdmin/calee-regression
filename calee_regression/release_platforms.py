"""Technical-owner-configured release-platform profile.

Determines which mobile UI platforms are release-gating (mandatory) in
the consolidated report. This replaces a previously hard-coded
`mandatory=False` for the Android/iOS UI components in
consolidated_report.py -- see Workstream 9's "the release profile, not
hard-coded mandatory=False, should determine whether Android and iOS UI
results are required."

Absence of config/release-platforms.yaml means every platform defaults to
mandatory=True: an omitted required platform must never silently become
optional just because nobody wrote a config file -- the technical owner
must explicitly opt a platform out.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "release-platforms.yaml"


class ReleasePlatformsError(Exception):
    pass


@dataclass
class ReleasePlatforms:
    tablet: bool = True
    mobile_android: bool = True
    mobile_ios: bool = True
    source: str = "default (no config/release-platforms.yaml found; every platform is mandatory)"


@dataclass
class ReleaseFeatures:
    """Technical-owner-configured release *feature* scope (Workstream 2).

    Which cross-cutting features are in scope for this release, beyond the
    platform (tablet/Android/iOS) selection above. Each maps to a
    mandatory/optional decision the relevant test process honours -- most
    importantly ``synchronization``, which decides whether cross-device sync is
    release-gating in the consolidated report (Workstream 1).

    Absence of config/release-platforms.yaml (or of a ``release_features:``
    section) means every feature defaults to mandatory=True -- the same
    "an omitted requirement must never silently become optional" rule the
    platform selection above follows. A technical owner must explicitly opt a
    feature out.
    """

    synchronization: bool = True
    meals: bool = True
    onboarding: bool = True
    google_calendar: bool = True
    kiosk_admin: bool = True
    source: str = "default (no config/release-platforms.yaml found; every feature is mandatory)"


@dataclass
class ExpectedBuildIdentity:
    """Technical-owner-configured expected build identity for this release
    (Phase 3). Any value left None means "no expectation configured" -- the
    consolidator then only records the detected identity (and still BLOCKS on
    an unknown/dirty build for an in-scope app), rather than checking a match.
    ``allow_dirty`` explicitly approves testing an uncommitted build.

    ``production`` (Workstream 3) marks this as a production release profile:
    the *expected* identity below then becomes REQUIRED, not merely optional --
    a missing expected CaleeMobile SHA/version, tablet applicationId/versionName/
    versionCode/source SHA, or (when CaleeShell is in scope) CaleeShell version
    BLOCKS the release. Consistency of the observed build alone is not evidence
    of release intent; the intended target must be stated up front. In a
    production profile a dirty tree also needs a named waiver (see Waiver),
    ``allow_dirty`` alone is not sufficient.
    """

    calee_build_version: "str | None" = None
    calee_git_sha: "str | None" = None
    calee_application_id: "str | None" = None
    calee_version_code: "str | None" = None
    caleemobile_build_version: "str | None" = None
    caleemobile_git_sha: "str | None" = None
    caleeshell_version: "str | None" = None
    allow_dirty: bool = False
    production: bool = False
    source: str = "default (no config/release-platforms.yaml found)"


@dataclass
class Waiver:
    """A named, auditable approval for a release-identity exception -- today, a
    dirty/uncommitted source tree in a production release (Workstream 3). A
    waiver is only valid when it names WHY (reason), WHO approved it (approver),
    and WHEN (timestamp); an incomplete waiver is treated as no waiver at all
    (the exception then BLOCKS). Recorded verbatim in the consolidated report so
    the approval is auditable after the fact."""

    reason: "str | None" = None
    approver: "str | None" = None
    timestamp: "str | None" = None
    source: str = "none"

    @property
    def is_valid(self) -> bool:
        return bool(
            (self.reason or "").strip()
            and (self.approver or "").strip()
            and (self.timestamp or "").strip()
        )

    def to_dict(self) -> dict:
        return {"reason": self.reason, "approver": self.approver, "timestamp": self.timestamp}


def _load_config(config_path: Path) -> dict:
    try:
        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ReleasePlatformsError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReleasePlatformsError(f"{config_path} must contain a YAML mapping at the top level.")
    return raw


def _resolve_config_path(path: "Path | str | None") -> Path:
    return Path(path) if path else Path(os.environ.get("CALEE_RELEASE_PLATFORMS", DEFAULT_CONFIG_PATH))


def load_release_platforms(path: "Path | str | None" = None) -> ReleasePlatforms:
    config_path = _resolve_config_path(path)
    if not config_path.is_file():
        return ReleasePlatforms()

    raw = _load_config(config_path)
    section = raw.get("release_platforms", raw)
    if not isinstance(section, dict):
        raise ReleasePlatformsError(f"{config_path}'s release_platforms value must be a mapping.")

    return ReleasePlatforms(
        tablet=bool(section.get("tablet", True)),
        mobile_android=bool(section.get("mobile_android", True)),
        mobile_ios=bool(section.get("mobile_ios", True)),
        source=str(config_path),
    )


def load_release_features(path: "Path | str | None" = None) -> ReleaseFeatures:
    """Load the ``release_features:`` section from config/release-platforms.yaml.

    Absent file or section -> every feature mandatory=True (the safe,
    release-gating default). A non-mapping section is a configuration error,
    never silently ignored.
    """
    config_path = _resolve_config_path(path)
    if not config_path.is_file():
        return ReleaseFeatures()

    raw = _load_config(config_path)
    section = raw.get("release_features", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ReleasePlatformsError(f"{config_path}'s release_features value must be a mapping.")

    return ReleaseFeatures(
        synchronization=bool(section.get("synchronization", True)),
        meals=bool(section.get("meals", True)),
        onboarding=bool(section.get("onboarding", True)),
        google_calendar=bool(section.get("google_calendar", True)),
        kiosk_admin=bool(section.get("kiosk_admin", True)),
        source=str(config_path),
    )


def _opt_str(value: "object | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_expected_build_identity(path: "Path | str | None" = None) -> ExpectedBuildIdentity:
    """Load the `expected_build_identity:` section (and top-level
    `allow_dirty:`) from config/release-platforms.yaml. Absent file or section
    means "no expectations configured"."""
    config_path = _resolve_config_path(path)
    if not config_path.is_file():
        return ExpectedBuildIdentity()

    raw = _load_config(config_path)
    section = raw.get("expected_build_identity", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ReleasePlatformsError(f"{config_path}'s expected_build_identity value must be a mapping.")

    # A profile is "production" when either the expected-identity section says so
    # or a top-level `release_profile: production` is set.
    production = bool(section.get("production", str(raw.get("release_profile", "")).strip().lower() == "production"))

    return ExpectedBuildIdentity(
        calee_build_version=_opt_str(section.get("calee_build_version")),
        calee_git_sha=_opt_str(section.get("calee_git_sha")),
        calee_application_id=_opt_str(section.get("calee_application_id")),
        calee_version_code=_opt_str(section.get("calee_version_code")),
        caleemobile_build_version=_opt_str(section.get("caleemobile_build_version")),
        caleemobile_git_sha=_opt_str(section.get("caleemobile_git_sha")),
        caleeshell_version=_opt_str(section.get("caleeshell_version")),
        allow_dirty=bool(section.get("allow_dirty", raw.get("allow_dirty", False))),
        production=production,
        source=str(config_path),
    )


def load_waiver(path: "Path | str | None" = None) -> Waiver:
    """Load the `waiver:` section from config/release-platforms.yaml (a build
    pipeline can also inject one). Absent/incomplete -> an invalid Waiver, which
    the consolidator treats as no waiver (a dirty production build then BLOCKS).
    """
    config_path = _resolve_config_path(path)
    if not config_path.is_file():
        return Waiver()

    raw = _load_config(config_path)
    section = raw.get("waiver", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ReleasePlatformsError(f"{config_path}'s waiver value must be a mapping.")

    return Waiver(
        reason=_opt_str(section.get("reason")),
        approver=_opt_str(section.get("approver")),
        timestamp=_opt_str(section.get("timestamp")),
        source=str(config_path),
    )
