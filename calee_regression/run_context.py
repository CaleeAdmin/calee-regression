"""Single release-run identity: one CALEE_RUN_ID, one workspace directory,
shared by every component of a "06 Test Full Calee Solution" run.

Before this module existed, each component (prepare, the tablet suite, the
mobile UI/API suites, manual checks) wrote its own independently-timestamped
report, and consolidation discovered them with `ls -1dt ... | head -n1` /
fixed "-latest.json" filenames. That construction cannot tell "the report
from *this* run" apart from "whatever happens to be the newest file on
disk" -- a partially-failed run, two overlapping runs, or a leftover file
from yesterday all look identical to a `head -n1`. See docs/RELEASE_POLICY.md
and the "single release run ID" requirement this closes.

Every component report now carries the same run ID (see
extract_report_run_id) and lives at a fixed path inside
`reports/runs/<run_id>/<component>/results.json`. Consolidation
(consolidated_report.build_release_report / cli.py's `consolidate`)
validates every report it's given against validate_component_report before
trusting it -- a missing/mismatched run ID, a path outside the current
workspace, or a report that predates this run's start all raise
RunIdError rather than silently being treated as a normal report.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Fixed component names -- every one of these gets a subdirectory in the
# run workspace whether or not this particular run actually executes it
# (an optional/skipped platform still gets a directory; it just never
# gets a results.json, which component_from_* already reports as
# "not executed").
COMPONENT_NAMES = (
    "environment",
    "tablet",
    "mobile-api",
    "mobile-android",
    "mobile-ios",
    "manual-checks",
    "sync",
)


class RunIdError(RuntimeError):
    """Raised when a component report fails run-ID/workspace validation
    during a run-scoped consolidation. Callers must treat this as BLOCKED,
    never as a silent pass or a product FAIL -- an unverifiable report is
    a process/integrity problem, not evidence either way about the
    product under test.
    """


def generate_run_id(prefix: str = "release") -> str:
    """Example: release-20260716-153012-a1b2c3. The random suffix (not
    just a timestamp) means two runs started in the same second -- e.g. a
    tester double-clicking the launcher twice -- still get distinct
    workspaces instead of silently sharing/racing on one directory.
    """
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def is_valid_run_id(run_id: "str | None") -> bool:
    return bool(run_id) and bool(RUN_ID_RE.match(run_id))


@dataclass(frozen=True)
class RunWorkspace:
    repo_root: Path
    run_id: str

    @property
    def root(self) -> Path:
        return self.repo_root / "reports" / "runs" / self.run_id

    def component_dir(self, component: str) -> Path:
        return self.root / component

    def component_report_path(self, component: str) -> Path:
        return self.component_dir(component) / "results.json"

    @property
    def consolidated_dir(self) -> Path:
        return self.root / "consolidated"

    @property
    def manifest_path(self) -> Path:
        return self.root / "run-manifest.json"

    def ensure_created(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for component in COMPONENT_NAMES:
            self.component_dir(component).mkdir(parents=True, exist_ok=True)
        self.consolidated_dir.mkdir(parents=True, exist_ok=True)

    def is_within(self, path: Path) -> bool:
        """True if `path` resolves to somewhere inside this run's
        workspace -- used to reject a report path pointing outside the
        current run (e.g. a stale --tablet-report left over from a
        previous invocation's shell history)."""
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except (ValueError, OSError):
            return False


@dataclass
class RunManifest:
    """The one authoritative record of what a release run expected, what
    actually happened, and where every artifact landed. See Workstream 3's
    "run manifest" requirement -- every field listed there is here."""

    run_id: str
    started_at: str
    finished_at: str = ""
    expected_components: list = field(default_factory=list)
    release_platform_profile: dict = field(default_factory=dict)
    report_paths: dict = field(default_factory=dict)
    exit_codes: dict = field(default_factory=dict)
    device_ids: dict = field(default_factory=dict)
    build_versions: dict = field(default_factory=dict)
    git_shas: dict = field(default_factory=dict)
    fixture_version: "str | None" = None
    target_backend: "str | None" = None
    tester: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "runId": self.run_id,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "expectedComponents": list(self.expected_components),
            "releasePlatformProfile": dict(self.release_platform_profile),
            "reportPaths": dict(self.report_paths),
            "exitCodes": dict(self.exit_codes),
            "deviceIds": dict(self.device_ids),
            "buildVersions": dict(self.build_versions),
            "gitShas": dict(self.git_shas),
            "fixtureVersion": self.fixture_version,
            "targetBackend": self.target_backend,
            "tester": self.tester,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunManifest":
        return cls(
            run_id=data["runId"],
            started_at=data.get("startedAt", ""),
            finished_at=data.get("finishedAt", ""),
            expected_components=list(data.get("expectedComponents", [])),
            release_platform_profile=dict(data.get("releasePlatformProfile", {})),
            report_paths=dict(data.get("reportPaths", {})),
            exit_codes=dict(data.get("exitCodes", {})),
            device_ids=dict(data.get("deviceIds", {})),
            build_versions=dict(data.get("buildVersions", {})),
            git_shas=dict(data.get("gitShas", {})),
            fixture_version=data.get("fixtureVersion"),
            target_backend=data.get("targetBackend"),
            tester=data.get("tester"),
        )

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "RunManifest":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def record_component(
        self,
        component: str,
        *,
        report_path: "str | None" = None,
        exit_code: "int | None" = None,
        device_id: "str | None" = None,
        build_version: "str | None" = None,
        git_sha: "str | None" = None,
    ) -> None:
        if report_path is not None:
            self.report_paths[component] = report_path
        if exit_code is not None:
            self.exit_codes[component] = exit_code
        if device_id is not None:
            self.device_ids[component] = device_id
        if build_version is not None:
            self.build_versions[component] = build_version
        if git_sha is not None:
            self.git_shas[component] = git_sha


def extract_report_run_id(report: dict) -> "str | None":
    """Reports use either "runId" (calee-regression's own tablet/
    environment/manual-checks components, and CaleeMobile-Regression
    reports run standalone outside a shared release run) or
    "releaseRunId" (CaleeMobile-Regression's api/ui reports when run as
    part of a shared release run -- their own "runId" already means their
    per-invocation backend-object-isolation ID, a different, older
    concept this must not collide with). Prefer releaseRunId when both
    are present.
    """
    if not isinstance(report, dict):
        return None
    return report.get("releaseRunId") or report.get("runId")


def validate_component_report(
    report: dict,
    *,
    report_path: Path,
    run_id: str,
    workspace: RunWorkspace,
    component: str,
    run_started_at_epoch: "float | None" = None,
) -> None:
    """Raises RunIdError if `report` cannot be trusted as belonging to
    this run. Consolidation must call this for every component report
    before it's allowed to contribute to the release decision -- see
    docs/RELEASE_POLICY.md and the "consolidation must reject" list this
    implements: missing run ID, mismatched run ID, report path outside
    the current workspace, report generated before the current run
    started.
    """
    if not workspace.is_within(report_path):
        raise RunIdError(
            f"{component} report {report_path} is outside the current run's workspace "
            f"({workspace.root}) -- refusing to use a report from another location."
        )
    found_run_id = extract_report_run_id(report)
    if not found_run_id:
        raise RunIdError(
            f"{component} report {report_path} has no run ID -- refusing to use a report "
            f"that cannot be tied to this run ({run_id})."
        )
    if found_run_id != run_id:
        raise RunIdError(
            f"{component} report {report_path} has run ID {found_run_id!r}, expected "
            f"{run_id!r} -- refusing to consolidate a report from a different run."
        )
    if run_started_at_epoch is not None:
        try:
            mtime = report_path.stat().st_mtime
        except OSError:
            mtime = None
        # A 30s grace window absorbs clock/filesystem-timestamp skew between
        # "manifest recorded started_at" and "this file's mtime landed"
        # without weakening the actual protection: a report reused from a
        # *previous* run is minutes/hours stale, not seconds.
        if mtime is not None and mtime < (run_started_at_epoch - 30):
            raise RunIdError(
                f"{component} report {report_path} was generated before this run started "
                f"-- refusing to use a stale report left over from an earlier run."
            )
