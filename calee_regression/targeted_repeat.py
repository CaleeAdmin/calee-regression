"""Permanent targeted scenario-repeat runner (Workstream 7).

Runs one or more scenarios repeatedly, preserving EVERY attempt's evidence
(screenshots, page source, activity, package, locator, elapsed time, scenario,
step -- all already captured per attempt by ScenarioRunner/ReportBuilder), and
aggregates them into a dedicated targeted-run report that NEVER overwrites the
normal full-suite report.

Used for determinism checks on the recently-corrected scenarios (see the
checked-in profile scenarios/profiles/corrected_scenarios.yaml). The four
scenarios are DATA in that profile, never hardcoded into this core logic --
this runner accepts any scenarios.

Design contract:
  * accept one or more scenario files (directly and/or via a profile);
  * accept a repeat count; each attempt gets a DISTINCT report directory;
  * stop-on-first-failure is configurable and defaults OFF, so later failures
    are never hidden;
  * produce an aggregate targeted report carrying standard/diagnostic
    certification metadata (Workstream 6);
  * never overwrite the normal full-suite report (write to a separate dir).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from .consolidated_report import decide_status
from .models import DEVICE_INIT_STANDARD, certification_block

TARGETED_REPORT_SCHEMA_VERSION = 1
TARGETED_REPORT_TYPE = "tablet-targeted-repeat"

# Points at the CHECKED-IN profile, not an in-code scenario list -- the four
# corrected scenarios are data a tester can edit without touching core logic.
DEFAULT_TARGETED_PROFILE = "scenarios/profiles/corrected_scenarios.yaml"

# decide_status returns these; keep local names so callers don't import them
# from two places.
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_BLOCKED = "blocked"


def load_profile(path) -> list:
    """Load an ordered scenario list from a checked-in profile file: either a
    YAML mapping with a ``scenarios:`` list, or a bare YAML list. Returns the
    scenario path strings in order."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        scenarios = data.get("scenarios", [])
    else:
        scenarios = data
    if not isinstance(scenarios, list) or not all(isinstance(s, str) for s in scenarios):
        raise ValueError(f"Profile {path} must contain a list of scenario path strings.")
    return [s for s in scenarios if s.strip()]


def _stem(scenario_path) -> str:
    return Path(str(scenario_path)).stem


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def plan_attempts(scenarios, repeat_count: int) -> list:
    """Ordered (scenario, repetition) plan. Repetitions of a scenario are
    contiguous; each gets a distinct directory key."""
    if repeat_count < 1:
        raise ValueError(f"repeat_count must be >= 1, got {repeat_count}")
    planned = []
    for scenario in scenarios:
        stem = _stem(scenario)
        for repetition in range(1, repeat_count + 1):
            planned.append(
                {
                    "scenario": scenario,
                    "stem": stem,
                    "repetition": repetition,
                    "dirName": _sanitize(f"{stem}-r{repetition}"),
                }
            )
    return planned


def attempt_status(suite_dict) -> str:
    """A single attempt's status from its SuiteResult.to_dict() shape, using the
    same decide_status the tablet CLI/consolidator use, so a targeted attempt
    and a normal run can never disagree about the same counts. A mandatory
    skipped scenario folds into the blocked bucket."""
    if not isinstance(suite_dict, dict):
        return STATUS_BLOCKED
    passed = suite_dict.get("passed_count", 0)
    failed = suite_dict.get("failed_count", 0)
    blocked = suite_dict.get("blocked_count", 0)
    skipped = suite_dict.get("skipped_count", 0)
    mandatory_skipped = suite_dict.get(
        "mandatory_skipped_count",
        sum(
            1
            for s in suite_dict.get("scenarios", [])
            if s.get("status") == "skipped" and s.get("mandatory", True)
        ),
    )
    total = len(suite_dict.get("scenarios", [])) or (passed + failed + blocked + skipped)
    return decide_status(passed=passed, failed=failed, blocked=blocked + mandatory_skipped, total=total)


def aggregate_status(statuses) -> str:
    """Overall targeted-run status: FAIL if any attempt failed; else BLOCKED if
    any attempt blocked; else PASS. Empty -> BLOCKED (nothing ran)."""
    statuses = list(statuses)
    if not statuses:
        return STATUS_BLOCKED
    if any(s == STATUS_FAIL for s in statuses):
        return STATUS_FAIL
    if any(s == STATUS_BLOCKED for s in statuses):
        return STATUS_BLOCKED
    return STATUS_PASS


def build_targeted_report(
    attempt_records,
    *,
    scenarios,
    repeat_count,
    stop_on_failure,
    device_initialization_mode=DEVICE_INIT_STANDARD,
    run_id=None,
    stopped_early=False,
):
    """Assemble the aggregate targeted-run report."""
    statuses = [a["status"] for a in attempt_records]
    counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_BLOCKED: 0}
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1
    report = {
        "reportSchemaVersion": TARGETED_REPORT_SCHEMA_VERSION,
        "reportType": TARGETED_REPORT_TYPE,
        "runId": run_id or "",
        "scenarios": list(scenarios),
        "repeatCount": repeat_count,
        "stopOnFailure": bool(stop_on_failure),
        "stoppedEarly": bool(stopped_early),
        "attempts": attempt_records,
        "attemptCounts": counts,
        "status": aggregate_status(statuses),
    }
    report.update(certification_block(device_initialization_mode))
    return report


def run_targeted(
    *,
    scenarios,
    repeat_count,
    out_dir,
    run_once,
    stop_on_failure=False,
    device_initialization_mode=DEVICE_INIT_STANDARD,
    run_id=None,
):
    """Run every scenario ``repeat_count`` times, preserving each attempt, and
    write the aggregate targeted report to ``out_dir/results.json``. Returns
    (report_dict, status).

    ``run_once(scenario_path: str, attempt_dir: Path) -> dict`` runs ONE
    scenario, writes its own evidence into ``attempt_dir`` (results.json +
    screenshots/page source), and returns its SuiteResult.to_dict(). Injected
    so this is testable without a device.
    """
    out_dir = Path(out_dir)
    attempts_root = out_dir / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)

    planned = plan_attempts(scenarios, repeat_count)
    attempt_records = []
    stopped_early = False
    for plan in planned:
        attempt_dir = attempts_root / plan["dirName"]
        attempt_dir.mkdir(parents=True, exist_ok=True)
        suite_dict = run_once(plan["scenario"], attempt_dir)
        status = attempt_status(suite_dict)
        attempt_records.append(
            {
                "scenario": plan["scenario"],
                "repetition": plan["repetition"],
                "status": status,
                "reportPath": str(attempt_dir / "results.json"),
                "passed_count": (suite_dict or {}).get("passed_count", 0),
                "failed_count": (suite_dict or {}).get("failed_count", 0),
                "blocked_count": (suite_dict or {}).get("blocked_count", 0),
                "skipped_count": (suite_dict or {}).get("skipped_count", 0),
            }
        )
        if stop_on_failure and status == STATUS_FAIL:
            stopped_early = True
            break

    report = build_targeted_report(
        attempt_records,
        scenarios=scenarios,
        repeat_count=repeat_count,
        stop_on_failure=stop_on_failure,
        device_initialization_mode=device_initialization_mode,
        run_id=run_id,
        stopped_early=stopped_early,
    )
    with (out_dir / "results.json").open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    return report, report["status"]
