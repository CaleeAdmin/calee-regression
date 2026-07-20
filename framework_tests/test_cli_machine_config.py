"""CLI tests for the machine-config command (Phase 4) that the one-button
launcher sources."""

from __future__ import annotations

import yaml
from click.testing import CliRunner

from calee_regression import cli
from calee_regression.models import EXIT_INVALID_CONFIG, EXIT_SUCCESS

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
    "mobile_platforms": ["android"],
}


def test_machine_config_emits_shell_vars(tmp_path):
    path = tmp_path / "machine.local.yaml"
    path.write_text(yaml.safe_dump(_VALID))
    result = CliRunner().invoke(cli.main, ["machine-config", "--config", str(path)])
    assert result.exit_code == EXIT_SUCCESS, result.output
    assert "MACHINE_BACKEND_URL=https://hub-dev.calee.com.au" in result.output
    assert "MACHINE_MOBILE_PLATFORMS=android" in result.output
    assert "MACHINE_ALLOW_CALEESHELL_TECHNICAL=false" in result.output
    # the bundle dir is expanded (no ~)
    assert "~" not in [line for line in result.output.splitlines() if "RELEASE_BUNDLE_DIR" in line][0]


def test_machine_config_rejects_inline_secret(tmp_path):
    path = tmp_path / "machine.local.yaml"
    path.write_text(yaml.safe_dump(dict(_VALID, regression_password="hunter2")))
    result = CliRunner().invoke(cli.main, ["machine-config", "--config", str(path)])
    assert result.exit_code == EXIT_INVALID_CONFIG
    assert "must not contain secrets" in result.output
    # the launcher must never see the secret echoed back as a shell var
    assert "hunter2" not in result.output


def test_machine_config_missing_file_is_invalid_config(tmp_path):
    result = CliRunner().invoke(cli.main, ["machine-config", "--config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == EXIT_INVALID_CONFIG
