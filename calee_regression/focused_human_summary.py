"""Plain-language rendering of a focused-verify summary (Phase 8).

`render` turns the validated machine summary (summary.json) into a
tester-readable summary.txt written alongside it -- same immutability rules
(refuse overwrite, read-only on disk). Derived EXCLUSIVELY from explicit
whitelisted fields of the summary dict (never a blind dump, so an unexpected
key -- credential-adjacent or otherwise -- can never leak into the text), no
re-reading of child state, no secrets, and it NEVER claims release
readiness.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import focused_workflow

NON_CERTIFICATION_STATEMENT = (
    "All evidence in this run is DIAGNOSTIC ONLY. This is NOT a release "
    "certification, and nothing here promotes any release component to PASS."
)


def _step_line(step: dict) -> str:
    """One line per step from explicit fields only."""
    title = step.get("title") or step.get("id") or "unknown step"
    mode = step.get("mode")
    line = f"  - {title}" + (f" [{mode}]" if mode else "")
    evidence = step.get("evidence")
    if evidence == "reused":
        line += " (evidence reused from a prior invocation)"
    report_path = step.get("reportPath")
    if report_path:
        line += f"\n      report: {report_path}"
    return line


def _blocker_line(step: dict) -> str:
    line = _step_line(step)
    blocked_by = step.get("blockedBy")
    detail = step.get("detail")
    status = step.get("status")
    reason_code = blocked_by or status or "unknown"
    kind = (
        "framework/tooling blocker" if status in (
            focused_workflow.STATUS_BLOCKED, focused_workflow.STATUS_BLOCKED_NOT_RUN)
        else "invalid invocation/configuration"
    )
    line += f"\n      reason code: {reason_code} ({kind})"
    if detail:
        line += f"\n      detail: {detail}"
    return line


def _section(title: str, lines: "list[str]", empty: str) -> "list[str]":
    out = [title, "-" * len(title)]
    out.extend(lines if lines else [f"  {empty}"])
    out.append("")
    return out


def next_command(summary: dict) -> "tuple[str, str]":
    """(command, explanation). The command is always real and pastable --
    never an angle-bracket placeholder. The one genuinely unknown value (the
    release run id) is the literal token RELEASE_RUN_ID with an explicit
    substitution instruction."""
    run_id = summary.get("runId") or "unknown-run"
    if summary.get("status") == focused_workflow.STATUS_PASS:
        command = (
            "python3 -m calee_regression release-remediation-plan "
            f"--focused-run {run_id} --release-run RELEASE_RUN_ID"
        )
        explanation = (
            "Every focused check passed. To plan how the blocked release run can "
            "proceed, run the command below, replacing the literal token "
            "RELEASE_RUN_ID with your real release run id (find it under "
            "reports/runs/)."
        )
    else:
        command = (
            "python3 -m calee_regression focused-verify "
            "--config config/tester.local.yaml --preflight-only"
        )
        explanation = (
            "Not every focused check passed. Re-run the preflight below to check "
            "environment/tooling readiness before trying again."
        )
    return command, explanation


def render(summary: dict) -> str:
    """Render the plain-text summary from the validated summary dict only."""
    steps = [s for s in summary.get("steps") or [] if isinstance(s, dict)]
    passed = [s for s in steps if s.get("status") == focused_workflow.STATUS_PASS]
    failed = [s for s in steps if s.get("status") == focused_workflow.STATUS_FAIL]
    blocked = [s for s in steps if s.get("status") in (
        focused_workflow.STATUS_BLOCKED, focused_workflow.STATUS_INVALID_CONFIG)]
    not_run = [s for s in steps if s.get("status") == focused_workflow.STATUS_BLOCKED_NOT_RUN]

    regression_shas = summary.get("regressionShas") or {}
    product_build = summary.get("productBuild") or {}
    device_ids = summary.get("deviceIds") or {}
    artifact = summary.get("installedArtifactIdentity") or {}

    lines: "list[str]" = []
    lines.append("FOCUSED POST-FIX VERIFICATION -- PLAIN-LANGUAGE SUMMARY")
    lines.append("=" * 55)
    lines.append(f"Run: {summary.get('runId')}  (invocation {summary.get('invocationId')})")
    lines.append(f"Overall result: {str(summary.get('status') or 'unknown').upper()}")
    lines.append("")

    lines += _section("What passed?", [_step_line(s) for s in passed], "Nothing passed.")
    lines += _section(
        "What failed? (product failures)",
        [_step_line(s) + (f"\n      detail: {s['detail']}" if s.get("detail") else "") for s in failed],
        "Nothing failed.",
    )
    lines += _section(
        "What was blocked? (framework/tooling problems, NOT product failures)",
        [_blocker_line(s) for s in blocked], "Nothing was blocked.",
    )
    lines += _section(
        "What did not run?",
        [_blocker_line(s) for s in not_run], "Every planned step ran.",
    )

    identity = [
        f"  Backend tested: {summary.get('verifiedBackend')}",
        f"  Fixture version: {summary.get('fixtureVersion')}",
        f"  Tablet device: {device_ids.get('tablet')}",
        f"  iPhone device: {device_ids.get('ios')}",
        f"  calee-regression SHA: {regression_shas.get('calee-regression')}",
        f"  CaleeMobile-Regression SHA: {regression_shas.get('caleemobile-regression')}",
        f"  CaleeMobile (product) SHA: {product_build.get('caleeMobileSha')}",
        f"  Installed artifact identity: {artifact.get('status')}"
        + (f" ({artifact.get('reason')})" if artifact.get("reason") else ""),
    ]
    lines += _section("What identity was tested?", identity, "unknown")

    lines += _section(
        "Which evidence is diagnostic only?",
        ["  ALL of it. " + NON_CERTIFICATION_STATEMENT],
        "",
    )
    lines += _section(
        "Which release component still needs certification?",
        ["  Every release component. A focused run certifies nothing: the tablet,"
         " mobile API, Android, iPhone, sync, manual-check and kiosk components"
         " must all still be certified by a real release run."],
        "",
    )

    command, explanation = next_command(summary)
    lines += _section(
        "What should you run next?",
        [f"  {explanation}", "", f"  {command}"],
        "",
    )
    return "\n".join(lines).rstrip() + "\n"


def write(summary: dict, path: Path) -> Path:
    """Write the rendered summary immutably: refuse to overwrite an existing
    file (raises FileExistsError) and mark it read-only on disk
    (best-effort), exactly like summary.json."""
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable summary {path}")
    path.write_text(render(summary), encoding="utf-8")
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass
    return path
