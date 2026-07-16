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


def load_release_platforms(path: "Path | str | None" = None) -> ReleasePlatforms:
    config_path = Path(path) if path else Path(os.environ.get("CALEE_RELEASE_PLATFORMS", DEFAULT_CONFIG_PATH))
    if not config_path.is_file():
        return ReleasePlatforms()

    try:
        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ReleasePlatformsError(f"{config_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ReleasePlatformsError(f"{config_path} must contain a YAML mapping at the top level.")

    section = raw.get("release_platforms", raw)
    if not isinstance(section, dict):
        raise ReleasePlatformsError(f"{config_path}'s release_platforms value must be a mapping.")

    return ReleasePlatforms(
        tablet=bool(section.get("tablet", True)),
        mobile_android=bool(section.get("mobile_android", True)),
        mobile_ios=bool(section.get("mobile_ios", True)),
        source=str(config_path),
    )
