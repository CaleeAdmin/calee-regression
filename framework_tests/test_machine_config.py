"""Offline tests for the centralised machine config loader (Phase 4),
including its refusal to hold secrets and its consistency with the shipped
example file.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from calee_regression import machine_config as mc
from calee_regression.machine_config import (
    MachineConfigError,
    load_machine_config,
    validate_machine_config,
)
from calee_regression.suites import REPO_ROOT

_VALID = {
    "tablet_serial": "TAB123",
    "expected_tablet_state": "logged_in_tablet",
    "calee_package_id": "com.viso.calee",
    "caleeshell_package_id": "com.viso.caleeshell",
    "home_activity": "com.viso.caleeshell/.ui.LauncherActivity",
    "calee_launch_action": "com.viso.calee.action.START",
    "release_bundle_dir": "~/Calee-Releases/current",
    "backend_url": "https://hub-dev.calee.com.au",
    "release_profile": "production",
    "report_dir": "reports",
    "mobile_platforms": ["android", "ios"],
    "iphone_device": "",
    "allow_caleeshell_technical": False,
}


def _write(tmp_path, data):
    path = tmp_path / "machine.local.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_valid_machine_config_loads(tmp_path):
    cfg = load_machine_config(_write(tmp_path, _VALID))
    assert cfg.tablet_serial == "TAB123"
    assert cfg.mobile_platforms == ["android", "ios"]
    assert cfg.allow_caleeshell_technical is False
    assert cfg.iphone_device is None  # empty string normalised to None


def test_resolved_bundle_dir_expands_home_and_pins_symlink_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    release_root = home / "Calee-Releases"
    target = release_root / ".current.versions" / "bundle-123"
    target.mkdir(parents=True)
    (release_root / "current").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))

    cfg = load_machine_config(_write(tmp_path, _VALID))
    resolved = cfg.resolved_bundle_dir()

    assert "~" not in str(resolved)
    assert resolved == target.resolve()


def test_missing_required_field_is_rejected(tmp_path):
    data = dict(_VALID)
    del data["backend_url"]
    with pytest.raises(MachineConfigError) as exc:
        load_machine_config(_write(tmp_path, data))
    assert "backend_url" in str(exc.value)


def test_invalid_tablet_state_is_rejected(tmp_path):
    data = dict(_VALID, expected_tablet_state="banana")
    with pytest.raises(MachineConfigError) as exc:
        load_machine_config(_write(tmp_path, data))
    assert "expected_tablet_state" in str(exc.value)


def test_invalid_mobile_platform_is_rejected(tmp_path):
    data = dict(_VALID, mobile_platforms=["android", "blackberry"])
    with pytest.raises(MachineConfigError) as exc:
        load_machine_config(_write(tmp_path, data))
    assert "blackberry" in str(exc.value)


@pytest.mark.parametrize("bad_url", [
    "http://hub-dev.calee.com.au",
    "hub-dev.calee.com.au",
    "https://user:pass@hub-dev.calee.com.au",
    "https://real.calee.com.au@evil.example/",
    "https://hub-dev.calee.com.au#frag",
    "https://hub-dev.calee.com.au:99999",
    " https://hub-dev.calee.com.au",
])
def test_backend_url_structural_problems_are_rejected(tmp_path, bad_url):
    data = dict(_VALID, backend_url=bad_url)
    with pytest.raises(MachineConfigError) as exc:
        load_machine_config(_write(tmp_path, data))
    assert "backend_url" in str(exc.value)


def test_inline_password_is_rejected():
    errors = validate_machine_config(dict(_VALID, regression_password="hunter2"))
    assert any("must not contain secrets" in e for e in errors)
    assert any("regression_password" in e for e in errors)


def test_nested_token_is_rejected():
    errors = validate_machine_config(dict(_VALID, ai=({"api_token": "abc"})))
    assert any("must not contain secrets" in e for e in errors)
    assert any("api_token" in e for e in errors)


def test_secret_key_variants_are_caught():
    for key in ("password", "PASSWORD", "apiKey", "some_secret", "auth_credential"):
        errors = validate_machine_config(dict(_VALID, **{key: "x"})) if key != "PASSWORD" else validate_machine_config({**_VALID, "PASSWORD": "x"})
        assert any("must not contain secrets" in e for e in errors), key


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(MachineConfigError) as exc:
        load_machine_config(tmp_path / "nope.yaml")
    assert "not found" in str(exc.value)


def test_shipped_example_is_valid_and_secret_free():
    example = REPO_ROOT / "config" / "machine.local.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    errors = validate_machine_config(raw)
    assert errors == [], errors


# --- Priority 4 (this session): pinned signed-export trust-key fingerprint -


def test_valid_trusted_signed_export_fingerprint_loads(tmp_path):
    cfg = load_machine_config(_write(tmp_path, dict(_VALID, trusted_signed_export_public_key_sha256="a" * 64)))
    assert cfg.trusted_signed_export_public_key_sha256 == "a" * 64


def test_trusted_signed_export_fingerprint_absent_by_default(tmp_path):
    cfg = load_machine_config(_write(tmp_path, _VALID))
    assert cfg.trusted_signed_export_public_key_sha256 is None


def test_trusted_signed_export_fingerprint_wrong_length_rejected():
    errors = validate_machine_config(dict(_VALID, trusted_signed_export_public_key_sha256="abc123"))
    assert any("trusted_signed_export_public_key_sha256" in e for e in errors)


def test_trusted_signed_export_fingerprint_non_hex_rejected():
    errors = validate_machine_config(
        dict(_VALID, trusted_signed_export_public_key_sha256="g" * 64)
    )
    assert any("trusted_signed_export_public_key_sha256" in e for e in errors)


def test_trusted_signed_export_fingerprint_is_lowercased_on_load(tmp_path):
    cfg = load_machine_config(_write(tmp_path, dict(_VALID, trusted_signed_export_public_key_sha256="A" * 64)))
    assert cfg.trusted_signed_export_public_key_sha256 == "a" * 64
