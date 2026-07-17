from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path


def default_run_name(kind: str, name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


_STATUS_MARKERS = {
    "passed": "[PASS]",
    "failed": "[FAIL]",
    "skipped": "[SKIP]",
    "warning": "[WARN]",
    "blocked": "[BLOCKED]",
}

_STATUS_COLORS = {
    "passed": "#1a7f37",
    "failed": "#cf222e",
    "skipped": "#6e7781",
    "warning": "#9a6700",
    "blocked": "#8250df",
}


class ReportBuilder:
    def __init__(self, config, run_name: str, repo_root=None, out_dir=None):
        """`out_dir`, when given, is used verbatim instead of the default
        auto-timestamped `<report_dir>/<run_name>-<timestamp>/` directory --
        used by the CLI to write directly into a shared release run's fixed
        workspace path (reports/runs/<run_id>/tablet/) instead of a
        directory whose name a caller would otherwise have to rediscover
        with something like `ls -1dt` (see run_context.py)."""
        self.config = config
        self.run_name = run_name
        if out_dir is not None:
            self.dir = Path(out_dir)
        else:
            sanitized = re.sub(r"[^A-Za-z0-9_-]", "-", run_name)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            self.dir = Path(config.report_dir) / f"{sanitized}-{timestamp}"
        self.screenshots_dir = self.dir / "screenshots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def screenshot_path(self, filename: str) -> Path:
        candidate = self.screenshots_dir / f"{filename}.png"
        counter = 1
        while candidate.exists():
            candidate = self.screenshots_dir / f"{filename}_{counter}.png"
            counter += 1
        return candidate

    def diff_dir(self) -> Path:
        return self.screenshots_dir

    def write(self, suite_result) -> Path:
        self._write_summary_txt(suite_result)
        self._write_summary_html(suite_result)
        self._write_results_json(suite_result)
        self._write_junit_xml(suite_result)
        return self.dir

    def _write_summary_txt(self, suite_result) -> None:
        lines = []
        lines.append(f"Suite: {suite_result.name}")
        lines.append(f"Started:  {suite_result.started_at}")
        lines.append(f"Finished: {suite_result.finished_at}")
        lines.append(
            f"Passed: {suite_result.passed_count}  Failed: {suite_result.failed_count}  "
            f"Skipped: {suite_result.skipped_count}  Blocked: {suite_result.blocked_count}"
        )
        lines.append("")
        for scenario in suite_result.scenarios:
            lines.append(f"== {scenario.name} [{scenario.status.upper()}] ({scenario.file}) ==")
            if scenario.skip_reason:
                lines.append(f"  skip reason: {scenario.skip_reason}")
            if scenario.blocked_reason:
                lines.append(f"  blocked reason: {scenario.blocked_reason}")
            for step in scenario.steps:
                marker = _STATUS_MARKERS.get(step.status, f"[{step.status.upper()}]")
                lines.append(f"  {marker} {step.name} ({step.action}) - {step.message}")
                if step.hint:
                    lines.append(f"         hint: {step.hint}")
            lines.append("")
        (self.dir / "summary.txt").write_text("\n".join(lines), encoding="utf-8")

    def _write_summary_html(self, suite_result) -> None:
        parts = []
        parts.append("<!doctype html><html><head><meta charset='utf-8'>")
        parts.append(f"<title>Calee regression report: {_escape(suite_result.name)}</title>")
        parts.append(
            "<style>"
            "body{font-family:-apple-system,Helvetica,Arial,sans-serif;background:#fff;color:#1f2328;"
            "margin:0;padding:24px;}"
            "h1{margin-top:0;}"
            ".summary{padding:12px 16px;border:1px solid #d0d7de;border-radius:6px;margin-bottom:24px;"
            "background:#f6f8fa;}"
            ".scenario{border:1px solid #d0d7de;border-radius:6px;margin-bottom:16px;padding:12px 16px;}"
            ".step{padding:6px 0;border-top:1px solid #eaeef2;}"
            ".step:first-child{border-top:none;}"
            ".hint{background:#fff8c5;border:1px solid #d4a72c;border-radius:4px;padding:8px;margin-top:4px;}"
            ".shots img{max-width:320px;margin:8px 8px 0 0;border:1px solid #d0d7de;border-radius:4px;}"
            "</style></head><body>"
        )
        parts.append(f"<h1>Calee regression report: {_escape(suite_result.name)}</h1>")
        parts.append(
            f"<div class='summary'>Started: {_escape(suite_result.started_at)}<br>"
            f"Finished: {_escape(suite_result.finished_at)}<br>"
            f"<b>Passed: {suite_result.passed_count} &nbsp; Failed: {suite_result.failed_count} "
            f"&nbsp; Skipped: {suite_result.skipped_count} &nbsp; "
            f"Blocked: {suite_result.blocked_count}</b></div>"
        )
        for scenario in suite_result.scenarios:
            color = _STATUS_COLORS.get(scenario.status, "#1f2328")
            parts.append(f"<div class='scenario'><h2 style='color:{color}'>{_escape(scenario.name)} "
                         f"[{_escape(scenario.status.upper())}]</h2>")
            parts.append(f"<div>file: {_escape(scenario.file)}</div>")
            if scenario.skip_reason:
                parts.append(f"<div class='hint'>{_escape(scenario.skip_reason)}</div>")
            if scenario.blocked_reason:
                parts.append(f"<div class='hint'>{_escape(scenario.blocked_reason)}</div>")
            for step in scenario.steps:
                step_color = _STATUS_COLORS.get(step.status, "#1f2328")
                parts.append(
                    f"<div class='step'><span style='color:{step_color};font-weight:bold'>"
                    f"{_escape(step.status.upper())}</span> — {_escape(step.name)} "
                    f"({_escape(step.action)}): {_escape(step.message)}</div>"
                )
                if step.hint:
                    parts.append(f"<div class='hint'>{_escape(step.hint)}</div>")
                shots = []
                if step.screenshot_path:
                    shots.append(step.screenshot_path)
                if step.diff_path:
                    shots.append(step.diff_path)
                if shots:
                    parts.append("<div class='shots'>")
                    for shot in shots:
                        rel = _relative_to_report(self.dir, shot)
                        parts.append(f"<img src='{_escape(rel)}' alt='{_escape(step.name)}'>")
                    parts.append("</div>")
            parts.append("</div>")
        parts.append("</body></html>")
        (self.dir / "summary.html").write_text("".join(parts), encoding="utf-8")

    def _write_results_json(self, suite_result) -> None:
        with (self.dir / "results.json").open("w", encoding="utf-8") as f:
            json.dump(suite_result.to_dict(), f, indent=2)

    def _write_junit_xml(self, suite_result) -> None:
        total_time = sum(s.duration_seconds for s in suite_result.scenarios)
        testsuite = ET.Element(
            "testsuite",
            {
                "name": suite_result.name,
                "tests": str(len(suite_result.scenarios)),
                "failures": str(suite_result.failed_count),
                "skipped": str(suite_result.skipped_count),
                # JUnit's "errors" bucket is the standard place to report a test
                # that could not be executed due to an environment/tooling
                # problem, as distinct from "failures" (a real assertion
                # failure) — that's exactly what BLOCKED means here.
                "errors": str(suite_result.blocked_count),
                "time": f"{total_time:.3f}",
            },
        )
        for scenario in suite_result.scenarios:
            testcase = ET.SubElement(
                testsuite,
                "testcase",
                {
                    "classname": suite_result.name,
                    "name": scenario.name,
                    "time": f"{scenario.duration_seconds:.3f}",
                },
            )
            if scenario.status == "failed":
                messages = "; ".join(s.message for s in scenario.steps if s.status == "failed")
                failure = ET.SubElement(testcase, "failure", {"message": messages or "scenario failed"})
                failure.text = messages
            elif scenario.status == "blocked":
                error = ET.SubElement(
                    testcase, "error", {"message": scenario.blocked_reason or "blocked"}
                )
                error.text = scenario.blocked_reason or ""
            elif scenario.status == "skipped":
                ET.SubElement(testcase, "skipped", {"message": scenario.skip_reason or "skipped"})
        tree = ET.ElementTree(testsuite)
        ET.indent(tree, space="  ")
        tree.write(self.dir / "junit.xml", encoding="utf-8", xml_declaration=True)


def _escape(value) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _relative_to_report(report_dir: Path, path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(report_dir.resolve()))
    except ValueError:
        return path
