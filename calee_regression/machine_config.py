"""Centralised technical-owner machine configuration (Phase 4).

Loads and validates ``config/machine.local.yaml`` -- the single file a
technical owner fills in per MacBook (device serial, release-bundle folder,
backend URL, active profile, enabled mobile platforms, HOME/launch wiring,
CaleeShell technical-test permission). See ``config/machine.local.example.yaml``.

The one hard security rule enforced here: **no secrets in this file.** If it
contains any key that looks like a password/token/secret/key, loading fails
with a clear pointer to the credential provider
(``calee_regression/credentials.py``). Secrets are resolved at run time from
the environment or the macOS Keychain, never committed or dropped in a config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import url_validation

VALID_TABLET_STATES = {"fresh", "logged_in_tablet"}
VALID_MOBILE_PLATFORMS = {"android", "ios"}

# Keys that would indicate a secret was pasted into the machine config. Matched
# case-insensitively as a substring of the key name.
_SECRET_KEY_MARKERS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential")

_REQUIRED_STRING_FIELDS = (
    "expected_tablet_state",
    "calee_package_id",
    "caleeshell_package_id",
    "home_activity",
    "calee_launch_action",
    "release_bundle_dir",
    "backend_url",
    "release_profile",
    "report_dir",
)


class MachineConfigError(Exception):
    """The machine config is missing, malformed, or contains a secret. Callers
    treat this as a configuration problem (BLOCKED/invalid-config), never a
    product failure."""


@dataclass
class MachineConfig:
    tablet_serial: "str | None"
    expected_tablet_state: str
    calee_package_id: str
    caleeshell_package_id: str
    home_activity: str
    calee_launch_action: str
    release_bundle_dir: str
    backend_url: str
    release_profile: str
    report_dir: str
    mobile_platforms: "list[str]" = field(default_factory=list)
    iphone_device: "str | None" = None
    android_device: "str | None" = None
    allow_caleeshell_technical: bool = False
    config_path: "Path | None" = None

    def resolved_bundle_dir(self) -> Path:
        """The release-bundle folder as an absolute path, expanding ``~``."""
        return Path(os.path.expanduser(self.release_bundle_dir)).resolve()


def _find_secret_keys(obj, prefix="") -> "list[str]":
    """Recursively collect any key path that looks like a secret."""
    found: "list[str]" = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_l = str(key).lower()
            path = f"{prefix}.{key}" if prefix else str(key)
            if any(marker in key_l for marker in _SECRET_KEY_MARKERS):
                found.append(path)
            found.extend(_find_secret_keys(value, path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_find_secret_keys(item, f"{prefix}[{i}]"))
    return found


def validate_machine_config(raw: dict) -> "list[str]":
    """Validate a raw machine-config mapping, returning a list of problems
    (empty when valid). Pure -- no filesystem access."""
    errors: "list[str]" = []
    if not isinstance(raw, dict):
        return ["machine config must be a YAML mapping at the top level."]

    secret_keys = _find_secret_keys(raw)
    if secret_keys:
        errors.append(
            "machine config must not contain secrets. Remove these key(s): "
            + ", ".join(sorted(secret_keys))
            + ". Provide the regression username/password/token via environment variables or the "
            "macOS Keychain instead (see calee_regression/credentials.py)."
        )

    for name in _REQUIRED_STRING_FIELDS:
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"machine config field {name!r} is required and must be a non-empty string.")

    # Priority 7: structured URL validation for backend_url (scheme/host/
    # userinfo/fragment/port/whitespace) -- checked only when it's otherwise a
    # non-empty string (the required-field check above already reports a
    # missing/blank value; this adds format checking on top of that).
    backend_url = raw.get("backend_url")
    if isinstance(backend_url, str) and backend_url.strip():
        for problem in url_validation.validate_backend_url(backend_url):
            errors.append(f"machine config field 'backend_url' is invalid: {problem}")

    state = raw.get("expected_tablet_state")
    if state is not None and state not in VALID_TABLET_STATES:
        errors.append(
            f"expected_tablet_state {state!r} is invalid; must be one of {sorted(VALID_TABLET_STATES)}."
        )

    platforms = raw.get("mobile_platforms", [])
    if not isinstance(platforms, list):
        errors.append("mobile_platforms must be a list.")
    else:
        for p in platforms:
            if p not in VALID_MOBILE_PLATFORMS:
                errors.append(f"mobile_platforms entry {p!r} is invalid; must be one of {sorted(VALID_MOBILE_PLATFORMS)}.")

    if "allow_caleeshell_technical" in raw and not isinstance(raw["allow_caleeshell_technical"], bool):
        errors.append("allow_caleeshell_technical must be a boolean.")

    return errors


def load_machine_config(path) -> MachineConfig:
    """Load and validate a machine config, raising MachineConfigError with
    every problem listed on failure."""
    path = Path(path)
    if not path.is_file():
        raise MachineConfigError(f"Machine config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MachineConfigError(f"Machine config at {path} is not valid YAML: {exc}") from exc

    errors = validate_machine_config(raw)
    if errors:
        raise MachineConfigError(
            f"Machine config at {path} has {len(errors)} problem(s):\n" + "\n".join(f"  - {e}" for e in errors)
        )

    return MachineConfig(
        tablet_serial=raw.get("tablet_serial") or None,
        expected_tablet_state=raw["expected_tablet_state"],
        calee_package_id=raw["calee_package_id"],
        caleeshell_package_id=raw["caleeshell_package_id"],
        home_activity=raw["home_activity"],
        calee_launch_action=raw["calee_launch_action"],
        release_bundle_dir=raw["release_bundle_dir"],
        backend_url=raw["backend_url"],
        release_profile=raw["release_profile"],
        report_dir=raw["report_dir"],
        mobile_platforms=list(raw.get("mobile_platforms", [])),
        iphone_device=raw.get("iphone_device") or None,
        android_device=raw.get("android_device") or None,
        allow_caleeshell_technical=bool(raw.get("allow_caleeshell_technical", False)),
        config_path=path.resolve(),
    )
