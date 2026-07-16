"""Tests for the subprocess bridge to CaleeMobile-Regression's fixture CLI.

Uses a fake sibling repo (a tiny stub manage_fixture.py) written to tmp_path
-- no real network, credentials, or CaleeMobile-Regression checkout needed.
"""

from __future__ import annotations

import stat

import pytest

from calee_regression.fixture_bridge import FixtureBridgeError, find_sibling_repo, run_fixture_action


def _make_sibling(tmp_path, script_body: str):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    sibling_api = tmp_path / "CaleeMobile-Regression" / "api"
    sibling_api.mkdir(parents=True)
    script = sibling_api / "manage_fixture.py"
    script.write_text(script_body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return repo_root


def test_find_sibling_repo_returns_none_when_absent(tmp_path):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    assert find_sibling_repo(repo_root) is None


def test_find_sibling_repo_finds_it(tmp_path):
    repo_root = _make_sibling(tmp_path, "import sys\nsys.exit(0)\n")
    found = find_sibling_repo(repo_root)
    assert found == tmp_path / "CaleeMobile-Regression"


def test_run_fixture_action_raises_when_sibling_missing(tmp_path):
    repo_root = tmp_path / "calee-regression"
    repo_root.mkdir()
    with pytest.raises(FixtureBridgeError, match="was not found as a sibling"):
        run_fixture_action("reset", repo_root=repo_root, base_url="https://x", email="a@x", password="p")


def test_run_fixture_action_rejects_unknown_action(tmp_path):
    repo_root = _make_sibling(tmp_path, "import sys\nsys.exit(0)\n")
    with pytest.raises(FixtureBridgeError, match="Unknown fixture action"):
        run_fixture_action("wipe", repo_root=repo_root, base_url="https://x", email="a@x", password="p")


def test_run_fixture_action_succeeds(tmp_path):
    repo_root = _make_sibling(
        tmp_path,
        "import sys\nprint('Fixture reset OK (version=v1)')\nsys.exit(0)\n",
    )
    output = run_fixture_action("reset", repo_root=repo_root, base_url="https://x", email="a@x", password="p")
    assert "Fixture reset OK" in output


def test_run_fixture_action_raises_on_nonzero_exit(tmp_path):
    repo_root = _make_sibling(
        tmp_path,
        "import sys\nprint('=== Blocked: bad credentials ===', file=sys.stderr)\nsys.exit(3)\n",
    )
    with pytest.raises(FixtureBridgeError, match="did not succeed"):
        run_fixture_action("reset", repo_root=repo_root, base_url="https://x", email="a@x", password="p")
