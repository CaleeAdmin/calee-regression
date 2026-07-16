"""Bridges to CaleeMobile-Regression's fixture CLI (a sibling repo) so the
tablet framework can trigger a REG-* fixture reset/verify before running
scenarios that depend on it, without duplicating any of that logic here.

See docs/TEST_DATA_RESET_CONTRACT.md for what gets created and
CaleeMobile-Regression/api/caleemobile_regression/fixture.py for the
implementation this shells out to.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .models import EXIT_SUCCESS

DEFAULT_SIBLING_NAME = "CaleeMobile-Regression"


class FixtureBridgeError(Exception):
    """Raised when the fixture bridge itself can't run or reports failure.

    Always treat this as BLOCKED, never as a product FAIL: it means the
    fixture couldn't be prepared/verified, not that a Calee feature was
    exercised and produced a wrong result.
    """


def find_sibling_repo(repo_root: Path, name: str = DEFAULT_SIBLING_NAME) -> "Path | None":
    candidate = repo_root.parent / name
    if (candidate / "api" / "manage_fixture.py").is_file():
        return candidate
    return None


def run_fixture_action(
    action: str,
    *,
    repo_root: Path,
    base_url: str,
    email: str,
    password: str,
    timeout_seconds: int = 120,
) -> str:
    """Runs ``manage_fixture.py {action}`` in the sibling repo. Returns stdout.

    Never lets a missing-repo, timeout, or subprocess-launch problem escape
    as some other exception type -- everything funnels through
    FixtureBridgeError so callers have exactly one thing to catch and map to
    BLOCKED.
    """
    if action not in ("reset", "verify"):
        raise FixtureBridgeError(f"Unknown fixture action {action!r}; must be 'reset' or 'verify'.")

    sibling = find_sibling_repo(repo_root)
    if sibling is None:
        raise FixtureBridgeError(
            f"{DEFAULT_SIBLING_NAME} was not found as a sibling directory of this repo "
            f"(expected ../{DEFAULT_SIBLING_NAME}/api/manage_fixture.py). Fixture reset/verify "
            f"requires it to be checked out alongside calee-regression."
        )

    try:
        result = subprocess.run(
            [
                sys.executable, "manage_fixture.py", action,
                "--base-url", base_url, "--email", email, "--password", password,
            ],
            cwd=str(sibling / "api"),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise FixtureBridgeError(f"Fixture {action} timed out after {timeout_seconds}s.") from exc
    except OSError as exc:
        raise FixtureBridgeError(f"Could not run manage_fixture.py: {exc}") from exc

    if result.returncode != EXIT_SUCCESS:
        raise FixtureBridgeError(
            f"Fixture {action} did not succeed (exit code {result.returncode}).\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout
