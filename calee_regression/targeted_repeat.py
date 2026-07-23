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

import datetime as _dt
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import yaml

from .consolidated_report import decide_status
from .models import DEVICE_INIT_STANDARD, certification_block

TARGETED_REPORT_SCHEMA_VERSION = 1
TARGETED_REPORT_TYPE = "tablet-targeted-repeat"

PRODUCER = "targeted_repeat.py"

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


def _producer_git_sha() -> "str | None":
    """The calee-regression revision producing this targeted report (Workstream
    6). Best-effort None when git is unavailable."""
    repo_root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def digest_text(text: str) -> str:
    """A sha256 digest of some text (e.g. a profile file's contents), for
    tamper-evident provenance."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_invocation_id() -> str:
    """A unique-per-execution invocation id (UTC timestamp to the microsecond).
    run_targeted additionally refuses to overwrite an existing invocation dir,
    so even an astronomically-unlikely collision fails closed rather than
    clobbering earlier evidence."""
    return "inv-" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S-%f")


def plan_attempts(scenarios, repeat_count: int) -> list:
    """Ordered (scenario, repetition) plan. Repetitions of a scenario are
    contiguous; each gets a distinct directory key.

    The directory key is prefixed with the scenario's POSITION index so two
    scenarios that share a filename stem in different directories
    (``a/home.yaml`` and ``b/home.yaml``, both stem ``home``) can never collide
    on the same evidence directory (Workstream 6)."""
    if repeat_count < 1:
        raise ValueError(f"repeat_count must be >= 1, got {repeat_count}")
    planned = []
    for index, scenario in enumerate(scenarios, start=1):
        stem = _stem(scenario)
        for repetition in range(1, repeat_count + 1):
            planned.append(
                {
                    "scenario": scenario,
                    "stem": stem,
                    "scenarioIndex": index,
                    "repetition": repetition,
                    "dirName": _sanitize(f"s{index:02d}-{stem}-r{repetition}"),
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
    interrupted=False,
    invocation_id=None,
    release_id=None,
    profile_path=None,
    profile_digest=None,
    started_at=None,
    finished_at=None,
    provenance=None,
    producer_git_sha=None,
):
    """Assemble the aggregate targeted-run report with full provenance
    (Workstream 6). ``provenance`` is an optional identity bag carrying whatever
    the caller could capture (deviceId, backend, fixtureVersion, tablet
    build/package identity, apkSha256); absent values are recorded as None,
    never omitted, so the report is self-describing about what could and could
    not be proven."""
    statuses = [a["status"] for a in attempt_records]
    counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_BLOCKED: 0}
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1
    provenance = provenance or {}
    report = {
        "reportSchemaVersion": TARGETED_REPORT_SCHEMA_VERSION,
        "reportType": TARGETED_REPORT_TYPE,
        "producer": PRODUCER,
        "producerGitSha": producer_git_sha,
        "runId": run_id or "",
        "releaseId": release_id,
        "invocationId": invocation_id,
        "profilePath": profile_path,
        "profileDigest": profile_digest,
        "scenarios": list(scenarios),
        "repeatCount": repeat_count,
        "deviceId": provenance.get("deviceId"),
        "tabletBuildIdentity": provenance.get("tabletBuildIdentity"),
        "apkSha256": provenance.get("apkSha256"),
        "backend": provenance.get("backend"),
        "fixtureVersion": provenance.get("fixtureVersion"),
        "startedAt": started_at,
        "finishedAt": finished_at,
        "stopOnFailure": bool(stop_on_failure),
        "stoppedEarly": bool(stopped_early),
        # An unexpected exception interrupted execution -- the aggregate is still
        # produced, with the interrupted attempt recorded BLOCKED (never a
        # missing report).
        "interrupted": bool(interrupted),
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
    invocation_id=None,
    release_id=None,
    profile_path=None,
    profile_digest=None,
    provenance=None,
    started_at=None,
):
    """Run every scenario ``repeat_count`` times, preserving each attempt under
    an IMMUTABLE per-invocation directory, and write the aggregate targeted
    report. Returns (report_dict, status).

    Immutability (Workstream 6): every execution gets a unique ``invocation_id``
    and writes its attempts + aggregate under
    ``out_dir/invocations/<invocation_id>/`` -- a later invocation NEVER
    overwrites an earlier one (an existing invocation dir is refused). The
    canonical ``out_dir/results.json`` is a thin index/selected-result document
    that points at the invocations but never destroys an earlier invocation's
    evidence. If an unexpected exception interrupts execution, the interrupted
    attempt is recorded BLOCKED (never a missing report) and the aggregate is
    still written.

    ``run_once(scenario_path: str, attempt_dir: Path) -> dict`` runs ONE
    scenario, writes its own evidence into ``attempt_dir``, and returns its
    SuiteResult.to_dict(). Injected so this is testable without a device.
    """
    out_dir = Path(out_dir)
    invocation_id = invocation_id or new_invocation_id()
    invocations_root = out_dir / "invocations"
    invocation_dir = invocations_root / _sanitize(invocation_id)
    if invocation_dir.exists():
        raise FileExistsError(
            f"targeted-repeat invocation directory already exists ({invocation_dir}); "
            "refusing to overwrite earlier immutable evidence -- use a fresh invocation id."
        )
    attempts_root = invocation_dir / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=False)

    started_at = started_at or _dt.datetime.now(_dt.timezone.utc).isoformat()
    producer_git_sha = _producer_git_sha()
    planned = plan_attempts(scenarios, repeat_count)
    attempt_records = []
    stopped_early = False
    interrupted = False

    def _write_reports(finished_at):
        report = build_targeted_report(
            attempt_records,
            scenarios=scenarios,
            repeat_count=repeat_count,
            stop_on_failure=stop_on_failure,
            device_initialization_mode=device_initialization_mode,
            run_id=run_id,
            stopped_early=stopped_early,
            interrupted=interrupted,
            invocation_id=invocation_id,
            release_id=release_id,
            profile_path=profile_path,
            profile_digest=profile_digest,
            started_at=started_at,
            finished_at=finished_at,
            provenance=provenance,
            producer_git_sha=producer_git_sha,
        )
        # The immutable per-invocation aggregate.
        with (invocation_dir / "results.json").open("w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        # The canonical index/selected-result document -- overwriting this never
        # destroys an earlier invocation's own aggregate under invocations/.
        _write_canonical_index(out_dir, invocation_id, report)
        return report

    try:
        for plan in planned:
            attempt_dir = attempts_root / plan["dirName"]
            attempt_dir.mkdir(parents=True, exist_ok=False)
            base_record = {
                "scenario": plan["scenario"],
                "scenarioIndex": plan["scenarioIndex"],
                "repetition": plan["repetition"],
                "attemptDir": str(attempt_dir),
                "reportPath": str(attempt_dir / "results.json"),
            }
            try:
                suite_dict = run_once(plan["scenario"], attempt_dir)
            except Exception as exc:  # noqa: BLE001 -- an interrupted attempt is BLOCKED, not a crash
                interrupted = True
                attempt_records.append({
                    **base_record,
                    "status": STATUS_BLOCKED,
                    "interrupted": True,
                    "error": str(exc),
                    "passed_count": 0, "failed_count": 0, "blocked_count": 1, "skipped_count": 0,
                })
                break
            status = attempt_status(suite_dict)
            attempt_records.append({
                **base_record,
                "status": status,
                "interrupted": False,
                "passed_count": (suite_dict or {}).get("passed_count", 0),
                "failed_count": (suite_dict or {}).get("failed_count", 0),
                "blocked_count": (suite_dict or {}).get("blocked_count", 0),
                "skipped_count": (suite_dict or {}).get("skipped_count", 0),
            })
            if stop_on_failure and status == STATUS_FAIL:
                stopped_early = True
                break
    finally:
        finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        report = _write_reports(finished_at)

    return report, report["status"]


def _write_canonical_index(out_dir: Path, invocation_id: str, report: dict) -> None:
    """Write/refresh the canonical ``out_dir/results.json`` -- the selected
    invocation's aggregate, plus an append-only index of every invocation seen
    (Workstream 6). Overwriting this index never destroys an earlier
    invocation's own immutable aggregate under ``invocations/``."""
    index_path = out_dir / "results.json"
    existing_invocations = []
    if index_path.is_file():
        try:
            prior = json.loads(index_path.read_text(encoding="utf-8"))
            existing_invocations = list(prior.get("invocations", []))
        except (OSError, json.JSONDecodeError):
            existing_invocations = []
    entry = {
        "invocationId": invocation_id,
        "status": report["status"],
        "reportPath": str(out_dir / "invocations" / _sanitize(invocation_id) / "results.json"),
        "startedAt": report.get("startedAt"),
        "finishedAt": report.get("finishedAt"),
        "interrupted": report.get("interrupted", False),
    }
    # Append-only: never drop a previously recorded invocation.
    existing_invocations = [e for e in existing_invocations if e.get("invocationId") != invocation_id]
    existing_invocations.append(entry)
    canonical = dict(report)
    canonical["selectedInvocationId"] = invocation_id
    canonical["invocations"] = existing_invocations
    with index_path.open("w", encoding="utf-8") as f:
        json.dump(canonical, f, indent=2)
        f.write("\n")
