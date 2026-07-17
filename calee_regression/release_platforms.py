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
class ExpectedBuildIdentity:
    """Technical-owner-configured expected build identity for this release
    (Phase 3). Any value left None means "no expectation configured" -- the
    consolidator then only records the detected identity (and still BLOCKS on
    an unknown/dirty build for an in-scope app), rather than checking a match.
    ``allow_dirty`` explicitly approves testing an uncommitted build."""

    calee_build_version: "str | None" = None
    calee_git_sha: "str | None" = None
    caleemobile_build_version: "str | None" = None
    caleemobile_git_sha: "str | None" = None
    allow_dirty: bool = False
    source: str = "default (no config/release-platforms.yaml found)"


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

    return ExpectedBuildIdentity(
        calee_build_version=_opt_str(section.get("calee_build_version")),
        calee_git_sha=_opt_str(section.get("calee_git_sha")),
        caleemobile_build_version=_opt_str(section.get("caleemobile_build_version")),
        caleemobile_git_sha=_opt_str(section.get("caleemobile_git_sha")),
        allow_dirty=bool(section.get("allow_dirty", raw.get("allow_dirty", False))),
        source=str(config_path),
    )
