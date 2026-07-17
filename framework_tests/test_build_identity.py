"""Tests for automatic build-identity collection (Phase 3).

The parsers are pure and covered directly. The collectors shell out to git;
they're exercised against a real throwaway git repo in tmp_path, plus the
degrade-cleanly paths (missing pubspec, non-git directory, no device).
"""

from __future__ import annotations

import subprocess

import pytest
from click.testing import CliRunner

from calee_regression import build_identity
from calee_regression.cli import main
from calee_regression.models import EXIT_SUCCESS


# --- pure parsers --------------------------------------------------------


def test_parse_pubspec_version_reads_top_level_version_only():
    text = "name: calee_mobile\nversion: 0.0.22+22\ndependencies:\n  foo:\n    version: 9.9.9\n"
    assert build_identity.parse_pubspec_version(text) == "0.0.22+22"


def test_parse_pubspec_version_absent_is_none():
    assert build_identity.parse_pubspec_version("name: x\n") is None
    assert build_identity.parse_pubspec_version("") is None


def test_parse_git_sha_strips_and_handles_empty():
    assert build_identity.parse_git_sha("abc123\n") == "abc123"
    assert build_identity.parse_git_sha("  \n") is None
    assert build_identity.parse_git_sha(None) is None


def test_parse_git_dirty():
    assert build_identity.parse_git_dirty(" M lib/main.dart\n") is True
    assert build_identity.parse_git_dirty("") is False
    assert build_identity.parse_git_dirty("\n\n") is False
    assert build_identity.parse_git_dirty(None) is False


def test_parse_dumpsys_version_fields():
    sample = "  Package [com.calee.app] (abc):\n    versionName=0.3.22\n    versionCode=322 minSdk=24\n"
    assert build_identity.parse_dumpsys_version_name(sample) == "0.3.22"
    assert build_identity.parse_dumpsys_version_code(sample) == "322"
    assert build_identity.parse_dumpsys_version_name("nothing here") is None
    assert build_identity.parse_dumpsys_version_code("nothing here") is None


# --- BuildIdentity.to_shell ----------------------------------------------


def test_to_shell_emits_available_and_dirty_booleans_and_quoted_values():
    identity = build_identity.BuildIdentity(
        available=True, build_version="0.0.22+22", git_sha="deadbeef", dirty=False,
    )
    shell = identity.to_shell("CALEEMOBILE")
    assert "AUTO_CALEEMOBILE_IDENTITY_AVAILABLE=true" in shell
    assert "AUTO_CALEEMOBILE_DIRTY=false" in shell
    assert "AUTO_CALEEMOBILE_BUILD_VERSION=0.0.22+22" in shell
    assert "AUTO_CALEEMOBILE_GIT_SHA=deadbeef" in shell


def test_to_shell_omits_unknown_values_but_always_emits_flags():
    identity = build_identity.BuildIdentity(available=False)
    shell = identity.to_shell("CALEE")
    assert "AUTO_CALEE_IDENTITY_AVAILABLE=false" in shell
    assert "AUTO_CALEE_DIRTY=false" in shell
    assert "AUTO_CALEE_BUILD_VERSION" not in shell
    assert "AUTO_CALEE_GIT_SHA" not in shell


# --- collectors ----------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Tester")


def test_collect_caleemobile_identity_from_clean_repo(tmp_path):
    src = tmp_path / "CaleeMobile"
    _make_git_repo(src)
    (src / "pubspec.yaml").write_text("name: calee_mobile\nversion: 0.0.22+22\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")

    identity = build_identity.collect_caleemobile_identity(src)
    assert identity.available is True
    assert identity.build_version == "0.0.22+22"
    assert identity.git_sha and len(identity.git_sha) >= 7
    assert identity.dirty is False


def test_collect_caleemobile_identity_detects_dirty(tmp_path):
    src = tmp_path / "CaleeMobile"
    _make_git_repo(src)
    (src / "pubspec.yaml").write_text("name: calee_mobile\nversion: 0.0.22+22\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "init")
    # Uncommitted local change -> dirty.
    (src / "pubspec.yaml").write_text("name: calee_mobile\nversion: 0.0.23+23\n")

    identity = build_identity.collect_caleemobile_identity(src)
    assert identity.available is True
    assert identity.build_version == "0.0.23+23"
    assert identity.dirty is True


def test_collect_caleemobile_identity_missing_pubspec_is_unavailable(tmp_path):
    src = tmp_path / "CaleeMobile"
    src.mkdir()
    identity = build_identity.collect_caleemobile_identity(src)
    assert identity.available is False
    assert identity.build_version is None


def test_collect_caleemobile_identity_non_git_dir_still_reads_version(tmp_path):
    src = tmp_path / "CaleeMobile"
    src.mkdir()
    (src / "pubspec.yaml").write_text("name: calee_mobile\nversion: 1.2.3+4\n")
    identity = build_identity.collect_caleemobile_identity(src)
    assert identity.available is True
    assert identity.build_version == "1.2.3+4"
    # Not a git repo -> no SHA, not dirty, but still "available" by version.
    assert identity.git_sha is None
    assert identity.dirty is False


def test_collect_calee_tablet_identity_without_device_is_unavailable(tmp_path):
    # No android_package and no source -> nothing detectable -> unavailable.
    identity = build_identity.collect_calee_tablet_identity(caleeshell_version="1.4.0")
    assert identity.available is False
    assert identity.build_version is None
    # A CaleeShell version passed in is still recorded.
    assert identity.caleeshell_version == "1.4.0"


def test_collect_calee_tablet_identity_unreachable_adb_is_unavailable(tmp_path):
    # A bogus adb path can't produce a version -> unavailable, never a crash.
    identity = build_identity.collect_calee_tablet_identity(
        android_package="com.calee.app", adb_path="/nonexistent/adb-binary",
    )
    assert identity.available is False
    assert identity.application_id == "com.calee.app"


# --- build-identity CLI command ------------------------------------------


@pytest.fixture
def _repo_root(tmp_path, monkeypatch):
    import calee_regression.cli as cli_mod
    monkeypatch.setattr(cli_mod, "REPO_ROOT", tmp_path / "calee-regression")
    (tmp_path / "calee-regression").mkdir()
    return tmp_path


def test_build_identity_command_emits_auto_assignments(_repo_root):
    # A CaleeMobile checkout sitting next to the (mocked) repo root.
    cm = _repo_root / "CaleeMobile"
    _make_git_repo(cm)
    (cm / "pubspec.yaml").write_text("name: calee_mobile\nversion: 0.0.22+22\n")
    _git(cm, "add", "-A")
    _git(cm, "commit", "-q", "-m", "init")

    result = CliRunner().invoke(main, ["build-identity"])
    assert result.exit_code == EXIT_SUCCESS
    assert "AUTO_CALEEMOBILE_IDENTITY_AVAILABLE=true" in result.output
    assert "AUTO_CALEEMOBILE_BUILD_VERSION=0.0.22+22" in result.output
    assert "AUTO_CALEEMOBILE_DIRTY=false" in result.output
    # No tablet source/device -> tablet identity unavailable, but still emitted.
    assert "AUTO_CALEE_IDENTITY_AVAILABLE=false" in result.output


def test_build_identity_command_reports_missing_caleemobile_as_unavailable(_repo_root):
    # No CaleeMobile checkout next to the repo -> unavailable, still exits 0.
    result = CliRunner().invoke(main, ["build-identity"])
    assert result.exit_code == EXIT_SUCCESS
    assert "AUTO_CALEEMOBILE_IDENTITY_AVAILABLE=false" in result.output
