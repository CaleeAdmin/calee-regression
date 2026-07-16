"""Guided, tester-facing manual-check recorder (Workstream 6).

Replaces hand-editing config/manual-checks.example.json (which
docs/NON_TECH_TESTER_GUIDE.md explicitly says a non-technical tester must
never do) with a numbered terminal menu: the tester only ever types a
single digit, never JSON, YAML, or a shell command. Launched by
double-click via "05 Record Manual Checks.command".

Output is written in exactly the ManualCheck JSON shape `consolidate
--manual-checks` already expects (see consolidated_report.py), so the
recorded file can be passed straight through with no conversion step.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_BLOCKED = "blocked"

_MENU = (
    "Choose:\n"
    "1. Pass\n"
    "2. Fail\n"
    "3. Blocked\n"
    "4. Add note\n"
    "5. Add screenshot path\n"
    "6. Go back\n"
)


class ManualChecksDefinitionError(Exception):
    pass


def load_check_definitions(path: Path) -> list:
    """Loads the check *definitions* (title/instruction/expectedResult/
    mandatory -- no status yet) from config/manual-checks.json, or
    config/manual-checks.example.json if the real one hasn't been set up.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManualChecksDefinitionError(f"Could not read manual check definitions from {path}: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise ManualChecksDefinitionError(f"{path} must contain a non-empty JSON list of manual checks.")

    for item in raw:
        if not isinstance(item, dict) or not item.get("title") or not item.get("instruction"):
            raise ManualChecksDefinitionError(f"{path} has an entry missing a 'title' or 'instruction'.")

    return raw


def _new_result(definition: dict) -> dict:
    return {
        "title": definition["title"],
        "instruction": definition["instruction"],
        "expectedResult": definition.get("expectedResult", ""),
        "status": None,
        "note": "",
        "screenshotRef": None,
        "mandatory": bool(definition.get("mandatory", True)),
    }


def _render_check(index: int, total: int, result: dict) -> str:
    lines = [
        "",
        f"Manual check {index + 1} of {total}: {result['title']}",
        "",
        "Instruction:",
        result["instruction"],
        "",
        "Expected:",
        result["expectedResult"] or "(no expected result recorded)",
    ]
    if not result["mandatory"]:
        lines.append("")
        lines.append("(This check is OPTIONAL for this release.)")
    if result["status"] is not None:
        lines.append("")
        lines.append(f"Currently recorded: {result['status'].upper()}")
        if result["note"]:
            lines.append(f"Note: {result['note']}")
        if result["screenshotRef"]:
            lines.append(f"Screenshot: {result['screenshotRef']}")
    lines.append("")
    lines.append(_MENU)
    return "\n".join(lines)


def run_recorder(definitions: list, *, input_fn=None, print_fn=None) -> list:
    """Drives the guided menu over `definitions` (as returned by
    load_check_definitions). Returns a list of ManualCheck-shaped dicts.

    `input_fn`/`print_fn` are injectable so this can be unit-tested with a
    scripted sequence of answers instead of a real terminal -- see
    framework_tests/test_manual_checks.py. Resolved lazily (not as a
    default-argument value) so patching builtins.input still works: a
    default of `input_fn=input` would bind the builtin at function-
    definition time, before any test could monkeypatch it.
    """
    input_fn = input_fn or input
    print_fn = print_fn or print
    results = [_new_result(d) for d in definitions]
    index = 0
    total = len(results)

    while index < total:
        result = results[index]
        print_fn(_render_check(index, total, result))
        choice = (input_fn("Choose: ") or "").strip()

        if choice == "1":
            result["status"] = STATUS_PASS
            index += 1
        elif choice == "2":
            result["status"] = STATUS_FAIL
            index += 1
        elif choice == "3":
            result["status"] = STATUS_BLOCKED
            index += 1
        elif choice == "4":
            result["note"] = (input_fn("Note: ") or "").strip()
        elif choice == "5":
            result["screenshotRef"] = (input_fn("Screenshot path: ") or "").strip() or None
        elif choice == "6":
            index = max(0, index - 1)
        else:
            print_fn(f"'{choice}' is not one of the options above -- please choose 1-6.")

    return results


def write_results(results: list, path: Path, *, run_id: "str | None" = None) -> Path:
    """Writes `results` as a bare JSON list (the shape `consolidate
    --manual-checks` has always accepted) unless `run_id` is given, in
    which case it's wrapped as `{"runId": ..., "checks": [...]}` so
    consolidation can verify this report belongs to the current release
    run (see run_context.py). Bare-list output stays the default for a
    standalone "05 Record Manual Checks" run outside a shared run
    workspace -- there's no run ID to validate against in that case.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"runId": run_id, "checks": results} if run_id else results
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def summarize(results: list) -> str:
    lines = ["", "=== Manual check summary ==="]
    unanswered_mandatory = 0
    for result in results:
        status = (result["status"] or "NOT RECORDED").upper()
        marker = "" if result["mandatory"] else " (optional)"
        lines.append(f"  {result['title']}{marker}: {status}")
        if result["status"] is None and result["mandatory"]:
            unanswered_mandatory += 1
    if unanswered_mandatory:
        lines.append("")
        lines.append(
            f"{unanswered_mandatory} mandatory check(s) were not recorded -- these will BLOCK the "
            f"release until you run this again and answer them."
        )
    return "\n".join(lines)


def default_output_path(reports_dir: Path) -> Path:
    return Path(reports_dir) / f"manual-checks-{time.strftime('%Y%m%d-%H%M%S')}.json"
