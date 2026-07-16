from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_WARNING = "warning"
STATUS_BLOCKED = "blocked"

# Process exit codes. These are the framework's outward contract with CI and
# the tester-facing launchers: a caller must be able to tell "the product is
# broken" (EXIT_REGRESSION) apart from "the test environment/tooling/config
# is broken" (EXIT_BLOCKED / EXIT_INVALID_CONFIG) without parsing output.
EXIT_SUCCESS = 0
EXIT_REGRESSION = 1
EXIT_INVALID_CONFIG = 2
EXIT_BLOCKED = 3

REQUIRES_STATE_FRESH = "fresh"
REQUIRES_STATE_LOGGED_IN_TABLET = "logged_in_tablet"
REQUIRES_STATE_PHYSICAL_TABLET = "physical_tablet"
REQUIRES_STATE_ANY = "any"

VALID_REQUIRES_STATES = {
    REQUIRES_STATE_FRESH,
    REQUIRES_STATE_LOGGED_IN_TABLET,
    REQUIRES_STATE_PHYSICAL_TABLET,
    REQUIRES_STATE_ANY,
}

LAUNCH_STRATEGIES = {"direct_activity", "start_action", "calee_shell", "normal_launcher"}

STATE_MISMATCH_HINT = (
    "Calee launched, but the screen is not the logged-in home screen. "
    "This scenario requires a prepared tablet or test account."
)


@dataclass
class StepResult:
    name: str
    action: str
    status: str
    message: str = ""
    duration_seconds: float = 0.0
    screenshot_path: "str | None" = None
    diff_path: "str | None" = None
    hint: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "action": self.action,
            "status": self.status,
            "message": self.message,
            "duration_seconds": self.duration_seconds,
            "screenshot_path": self.screenshot_path,
            "diff_path": self.diff_path,
            "hint": self.hint,
        }


@dataclass
class Scenario:
    name: str
    file: Path
    tags: list
    requires_state: str
    default_timeout_seconds: int
    steps: list


@dataclass
class ScenarioResult:
    name: str
    file: str
    status: str
    steps: list = field(default_factory=list)
    duration_seconds: float = 0.0
    skip_reason: "str | None" = None
    blocked_reason: "str | None" = None
    tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "file": self.file,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "skip_reason": self.skip_reason,
            "blocked_reason": self.blocked_reason,
            "tags": self.tags,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass
class SuiteResult:
    name: str
    scenarios: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def passed_count(self) -> int:
        return sum(1 for s in self.scenarios if s.status == STATUS_PASSED)

    @property
    def failed_count(self) -> int:
        return sum(1 for s in self.scenarios if s.status == STATUS_FAILED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for s in self.scenarios if s.status == STATUS_SKIPPED)

    @property
    def blocked_count(self) -> int:
        return sum(1 for s in self.scenarios if s.status == STATUS_BLOCKED)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "blocked_count": self.blocked_count,
            "scenarios": [s.to_dict() for s in self.scenarios],
        }


@dataclass
class DoctorCheck:
    name: str
    status: str
    message: str
    hint: "str | None" = None


@dataclass
class VisualDiffResult:
    match: bool
    diff_ratio: float
    baseline_path: "str | None"
    actual_path: str
    diff_path: "str | None"
    message: str
