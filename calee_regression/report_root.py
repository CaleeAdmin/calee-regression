"""The ONE canonical report root for a release run (Priority 3).

Before this module existed, "where do reports go" had three independent,
disconnected answers that could silently disagree: `RunWorkspace` always
hardcoded the repo's own installed location (`REPO_ROOT`); `Config.report_dir`
(a CWD-relative tester-config default) only mattered for a standalone
`run`/`suite` invocation with no `--run-id`, which never happens inside an
orchestrated launcher run; and `MachineConfig.report_dir` /
`EffectiveReleaseConfig.report_root` were faithfully recorded as evidence but
never actually consumed by anything that writes a file. A technical owner
who pointed `machine.local.yaml`'s `report_dir` at an external drive would see
it validated, reconciled, and echoed back -- while every real artifact still
landed under `<repo>/reports/`.

`resolve_report_root()` is the single function every path-constructing call
site (every `RunWorkspace(...)` in `cli.py`, the `latest-run` symlink, the
manual-checks default path, both tester launchers, and every script under
`scripts/`) now goes through. Precedence, highest first:

  1. the `CALEE_REPORT_ROOT` environment variable -- exported once by the
     tester launchers (`tester/00`/`tester/06`, via `report-root`, this
     module's CLI command) before anything writes a file, so every child
     process (every `calee_regression` subcommand, `scripts/test_caleemobile.sh`,
     `07 Open Latest Report.command`) inherits the same resolved value. A
     technical owner or CI job may also export it directly.
  2. an explicit `machine_report_dir` (a caller that already loaded
     `MachineConfig` passes its `report_dir` field), for a standalone CLI
     invocation with no shell-exported `CALEE_REPORT_ROOT`. Like
     `CALEE_REPORT_ROOT`, this is the repo-root-equivalent value (see below) --
     `config/machine.local.example.yaml` documents `report_dir: "."` as the
     default (no-op) value precisely so copying the example verbatim changes
     nothing. This field previously had no live functional consumer (only the
     unrelated, CWD-relative `Config.report_dir` / standalone `ReportBuilder`
     fallback did, and only for a bare `run`/`suite` call with no `--run-id`,
     which never happens inside an orchestrated launcher run) so redefining
     its meaning here changes no existing behavior.
  3. `repo_root` itself -- the existing, unchanged default.

A relative configured value (either source) is resolved against `repo_root`,
never the process's current working directory -- so the result doesn't
depend on which directory a command happened to be launched from.

The resolved value always becomes an absolute, canonicalized path; the
directory is created if missing and confirmed writable. An unsafe root
(a filesystem root itself, or one that can't be created/written to) raises
`ReportRootError` -- callers must treat this as BLOCKED, never silently fall
back to the default (a silent fallback is exactly the "one component uses the
custom root, another silently uses `<repo>/reports`" bug this module exists
to close).

IMPORTANT -- this function returns the `repo_root`-equivalent value, NOT a
"reports/" directory. `RunWorkspace.root` itself is unchanged (`repo_root /
"reports" / "runs" / run_id`, it still appends "reports" itself): callers
pass `resolve_report_root(...)` in the `repo_root` parameter instead of the
bare `REPO_ROOT` constant, so `CALEE_REPORT_ROOT=/tmp/custom-calee-reports`
places a run's evidence at `/tmp/custom-calee-reports/reports/runs/<run-id>/
...` -- the exact same `reports/runs/<run-id>` shape every existing test and
script already expects, just rooted wherever the technical owner configured
instead of always at the repo's own install location. This keeps every one
of `RunWorkspace`'s existing unit tests (which construct it directly with an
arbitrary `repo_root`) valid unmodified. Appending an extra `"reports"`
segment to this function's return value (as opposed to letting
`RunWorkspace`/the shell launchers append it) would double it up.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "CALEE_REPORT_ROOT"


class ReportRootError(RuntimeError):
    """Raised when the configured report root cannot be made into a safe,
    writable directory. Callers must treat this as BLOCKED -- never silently
    substitute the default, which would silently defeat the whole point of a
    canonical, configurable root."""


def _is_filesystem_root(path: Path) -> bool:
    """True for `/` (POSIX) or a bare drive root like `C:\\` (Windows) --
    the one class of resolved path that can never be a safe report root
    (every component would be writing directly into the OS root)."""
    return path == path.parent


def resolve_report_root(
    *,
    repo_root: Path,
    machine_report_dir: "str | None" = None,
    env: "dict[str, str] | None" = None,
) -> Path:
    """Resolve, validate, and return the one canonical report root -- the
    ``repo_root``-equivalent value a caller should pass to
    ``RunWorkspace(...)`` (which appends ``reports/runs/<run-id>`` itself;
    see the module docstring for why this function must NOT also append
    ``"reports"``).

    ``repo_root`` is the fallback base (``REPO_ROOT`` in practice) used only
    when neither the environment nor ``machine_report_dir`` supplies a value
    -- the existing, unchanged default location.
    """
    environ = env if env is not None else os.environ
    raw = (environ.get(ENV_VAR) or "").strip() or (machine_report_dir or "").strip()

    if raw:
        expanded = Path(raw).expanduser()
        # Resolve a relative configured value against repo_root (never the
        # process's CWD) so the result is predictable regardless of which
        # directory a command was launched from.
        candidate = expanded if expanded.is_absolute() else (Path(repo_root) / expanded)
    else:
        candidate = Path(repo_root)

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ReportRootError(f"Report root {candidate} could not be resolved: {exc}") from exc

    if _is_filesystem_root(resolved):
        raise ReportRootError(
            f"Report root {resolved} is a filesystem root -- refusing to write reports directly into it."
        )

    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportRootError(f"Report root {resolved} could not be created: {exc}") from exc

    if not os.access(resolved, os.W_OK):
        raise ReportRootError(f"Report root {resolved} is not writable.")

    return resolved
