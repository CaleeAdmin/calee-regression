"""Cross-repository focused-execution contract, release side (this session's
Workstream 12).

CaleeMobile-Regression is the single source of truth for what its focused
CLIs accept and produce (``python3 -m caleemobile_regression
--describe-contract`` there). This repository vendors a generated copy of
that JSON (``schemas/focused-execution-contract.json``) so its offline tests
and the focused-verify orchestrator can validate -- without a network or a
sibling checkout -- that every option and suite name they construct is one
the mobile side actually supports, and BLOCK on an unsupported contract
version instead of drifting silently.

To refresh the vendored copy after a mobile-side contract change:

    cd ../CaleeMobile-Regression/api
    python3 -m caleemobile_regression --describe-contract \
        > ../../calee-regression/schemas/focused-execution-contract.json

When a sibling CaleeMobile-Regression checkout is present, the framework
tests ALSO diff the vendored copy against the sibling's live contract, so a
stale vendored file is caught in development before it can drift.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDORED_CONTRACT_PATH = REPO_ROOT / "schemas" / "focused-execution-contract.json"

# The contract versions this orchestrator explicitly supports. An unsupported
# version BLOCKS the focused run (Workstream 12).
SUPPORTED_CONTRACT_VERSIONS = {1}

EXECUTION_PURPOSE_FOCUSED_ENV_CHECK = "focused-environment-check"
EXECUTION_PURPOSE_FOCUSED_POST_FIX = "focused-post-fix-verification"


class FocusedContractError(Exception):
    """The focused-execution contract is missing, malformed, or unsupported --
    the focused run must BLOCK (environment/config), never guess."""


def load_contract(path: "Path | None" = None) -> dict:
    """Load and validate the vendored focused-execution contract."""
    path = path or VENDORED_CONTRACT_PATH
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FocusedContractError(f"focused-execution contract at {path} is unreadable: {exc}")
    if not isinstance(contract, dict) or contract.get("contractType") != "focused-execution-contract":
        raise FocusedContractError(f"{path} is not a focused-execution contract document")
    version = contract.get("focusedContractVersion")
    if version not in SUPPORTED_CONTRACT_VERSIONS:
        raise FocusedContractError(
            f"focused-execution contract version {version!r} is not supported "
            f"(supported: {sorted(SUPPORTED_CONTRACT_VERSIONS)}); refresh the vendored contract "
            f"and/or update this orchestrator deliberately -- never run against an unknown contract"
        )
    return contract


def validate_focused_invocation(
    contract: dict, *, api_suite: str, execution_purposes: "list[str]", ui_target: str
) -> "list[str]":
    """The drift problems for a planned focused invocation (empty = supported).

    Checks the API suite name, every execution purpose the orchestrator will
    pass, and the UI target against what the mobile side declares.
    """
    problems = []
    if api_suite not in contract.get("apiSuites", []):
        problems.append(
            f"API suite {api_suite!r} is not in the mobile contract's supported suites "
            f"{contract.get('apiSuites')}"
        )
    for purpose in execution_purposes:
        if purpose not in contract.get("executionPurposes", []):
            problems.append(f"execution purpose {purpose!r} is not in the mobile contract")
    if ui_target not in contract.get("uiTargets", {}).values():
        problems.append(
            f"UI target {ui_target!r} is not a target the mobile contract declares "
            f"({sorted(contract.get('uiTargets', {}).values())})"
        )
    return problems
