"""Actually execute the Flutter toolchain to back locally-generated selector
evidence (Priority 1 Problem A; local-evidence hardening, Priority 4).

A caller-supplied Flutter version string must never become proof of the
installed toolchain. The release gate previously handed a hard-coded ``3.44.1``
to the selector-contract emitter, which recorded it verbatim -- so a machine
with no Flutter at all could still emit "evidence" claiming Flutter 3.44.1.

When selector evidence is generated locally (the development fallback; a
production release accepts only a GitHub-authenticated CI artifact), this module
runs the real commands against the *exact* CaleeMobile checkout and records what
actually happened. Priority 4 additionally requires that local evidence can only
come from a real, clean, fully-identified pair of checkouts:

  * both sources are Git repositories (a resolvable HEAD);
  * both HEAD SHAs are full 40-character SHAs (no ambiguous abbreviations);
  * both worktrees are clean, unless a NAMED development waiver is supplied
    (a dirty tree is otherwise unreproducible evidence);
  * the actual Flutter framework version equals the pinned ``3.44.1``;
  * the actual Dart SDK version is recorded;
  * ``flutter pub get`` resolves;
  * ``flutter analyze --fatal-infos`` is clean (infos are fatal, matching
    CaleeMobile product CI);
  * the selector-contract tests pass;
  * the generated evidence records the exact verified source SHAs.

The complete verification record is then integrity-protected with its own
``recordDigest`` (Priority 4), so the SHAs/versions/commands it attests to
cannot be altered after the fact without detection.

Everything is injectable (``which``/``runner``/``git_sha``/``is_clean``) so the
policy is unit-tested without a real Flutter install or Git repo.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .identity_format import is_full_git_sha

# The Flutter framework version a local release build must actually be built
# with, mirroring selector_evidence.EXPECTED_FLUTTER_VERSION.
DEFAULT_EXPECTED_FLUTTER_VERSION = "3.44.1"

CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]
# (clean?, dirty file lines). A non-repo path is reported clean here -- the
# missing-repo / non-full-SHA condition is flagged by the SHA check instead, so
# it isn't double-reported as "dirty".
CleanCheck = Callable[[Path], "tuple[bool, list[str]]"]


@dataclass
class CommandRecord:
    """One executed command: exactly what ran and what it returned."""

    argv: "list[str]"
    cwd: str
    exit_code: "int | None"
    label: str
    error: "str | None" = None  # populated when the command could not be spawned

    def to_dict(self) -> dict:
        data = {
            "label": self.label,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exitCode": self.exit_code,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


@dataclass
class ToolchainVerification:
    """The result of actually exercising the Flutter toolchain for a local
    selector-evidence generation."""

    ok: bool
    flutter_path: "str | None" = None
    flutter_version: "str | None" = None
    dart_version: "str | None" = None
    caleemobile_sha: "str | None" = None
    regression_sha: "str | None" = None
    caleemobile_clean: "bool | None" = None
    regression_clean: "bool | None" = None
    dirty_waiver: "str | None" = None
    dirty_sources: "list[str]" = field(default_factory=list)
    commands: "list[CommandRecord]" = field(default_factory=list)
    problems: "list[str]" = field(default_factory=list)

    def _payload(self) -> dict:
        return {
            "ok": self.ok,
            "flutterPath": self.flutter_path,
            "flutterVersion": self.flutter_version,
            "dartVersion": self.dart_version,
            "caleemobileSha": self.caleemobile_sha,
            "regressionSha": self.regression_sha,
            "caleemobileClean": self.caleemobile_clean,
            "regressionClean": self.regression_clean,
            "dirtyWaiver": self.dirty_waiver,
            "dirtySources": list(self.dirty_sources),
            "commands": [c.to_dict() for c in self.commands],
            "problems": list(self.problems),
        }

    def to_dict(self) -> dict:
        """Serialise the record and protect it with a ``recordDigest`` (Priority 4).

        The digest covers every attested field (versions, SHAs, cleanliness,
        commands, problems), so the local-verification record cannot be altered
        without detection -- independently of the provenance envelope that also
        embeds it.
        """
        data = self._payload()
        data["recordDigest"] = _record_digest(data)
        return data


def _record_digest(payload: "dict[str, Any]") -> str:
    body = {k: v for k, v in payload.items() if k != "recordDigest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_toolchain_record(record: "dict[str, Any]") -> "list[str]":
    """Re-verify a serialised toolchain record's ``recordDigest`` (Priority 4).

    Returns problems (empty == intact). Used at consolidation / provenance
    re-verification so a tampered local-verification block BLOCKS.
    """
    problems: "list[str]" = []
    recorded = record.get("recordDigest")
    if not recorded:
        problems.append("local-verification record has no recordDigest -- it is not integrity-protected.")
        return problems
    actual = _record_digest({k: v for k, v in record.items() if k != "recordDigest"})
    if actual != recorded:
        problems.append(
            f"local-verification record digest mismatch: recorded {recorded}, actual {actual} "
            f"-- the local toolchain record was modified after it was produced."
        )
    return problems


def _git_head_sha(repo_path: Path) -> "str | None":
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def _git_worktree_clean(repo_path: Path) -> "tuple[bool, list[str]]":
    """(clean?, porcelain lines). A path that is not a Git repo returns clean --
    the non-repo condition is flagged by the HEAD-SHA check, not here."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return True, []
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return (not lines), lines


def parse_flutter_version_machine(stdout: str) -> "tuple[str | None, str | None]":
    """Parse ``flutter --version --machine`` JSON output.

    Returns ``(frameworkVersion, dartSdkVersion)`` -- either may be None if the
    output is not the expected JSON shape.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    fw = data.get("frameworkVersion")
    dart = data.get("dartSdkVersion")
    fw = str(fw).strip() if fw not in (None, "") else None
    dart = str(dart).strip() if dart not in (None, "") else None
    return fw, dart


def _norm(value: "Any | None") -> "str | None":
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def verify_local_toolchain(
    caleemobile_path: "Path | str",
    regression_path: "Path | str",
    *,
    expected_flutter_version: "str | None" = DEFAULT_EXPECTED_FLUTTER_VERSION,
    dirty_waiver: "str | None" = None,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    runner: "CommandRunner | None" = None,
    git_sha: "Callable[[Path], Optional[str]]" = _git_head_sha,
    is_clean: "CleanCheck" = _git_worktree_clean,
    timeout: int = 600,
) -> ToolchainVerification:
    """Run the real Flutter toolchain against the exact CaleeMobile checkout and
    record what actually happened, enforcing the Priority-4 source requirements.

    ``which``/``runner``/``git_sha``/``is_clean`` are injectable so the policy is
    unit-tested without a real Flutter install or Git repo. ``runner`` defaults to
    ``subprocess.run``. ``dirty_waiver`` (a non-empty name) permits a dirty
    worktree, recorded in the result; without it, a dirty tree is a problem.
    """
    if runner is None:
        runner = subprocess.run

    cm_path = Path(caleemobile_path)
    reg_path = Path(regression_path)
    problems: "list[str]" = []
    commands: "list[CommandRecord]" = []

    caleemobile_sha = git_sha(cm_path)
    regression_sha = git_sha(reg_path)
    waiver = _norm(dirty_waiver)

    # --- P4: both sources are Git repos with full 40-char HEAD SHAs ---------
    for label, path, sha in (
        ("CaleeMobile", cm_path, caleemobile_sha),
        ("CaleeMobile-Regression", reg_path, regression_sha),
    ):
        if sha is None:
            problems.append(
                f"{label} source at {path} is not a Git repository (no resolvable HEAD) -- "
                f"local release evidence requires a real, identifiable checkout."
            )
        elif not is_full_git_sha(sha):
            problems.append(
                f"{label} HEAD {sha!r} is not a full 40-character Git SHA -- an abbreviated/"
                f"ambiguous identity cannot anchor release evidence."
            )

    # --- P4: both worktrees clean unless a NAMED development waiver exists ---
    cm_clean, cm_dirty = is_clean(cm_path)
    reg_clean, reg_dirty = is_clean(reg_path)
    dirty_sources: "list[str]" = []
    if not cm_clean:
        dirty_sources.append("CaleeMobile")
    if not reg_clean:
        dirty_sources.append("CaleeMobile-Regression")
    if dirty_sources and not waiver:
        problems.append(
            f"{', '.join(dirty_sources)} worktree(s) are dirty (uncommitted changes) -- local "
            f"release evidence requires clean checkouts, or an explicit named --dirty-waiver "
            f"recording why an exception is acceptable."
        )

    if not cm_path.is_dir():
        problems.append(f"CaleeMobile checkout not found at {cm_path} -- cannot verify the toolchain against it.")
        return _result(False, None, None, None, caleemobile_sha, regression_sha,
                       cm_clean, reg_clean, waiver, dirty_sources, commands, problems)

    flutter_path = which("flutter")
    if not flutter_path:
        problems.append(
            "flutter is not on PATH -- a local release build cannot record an actual toolchain. "
            "Provide a GitHub-authenticated CI selector artifact, or install Flutter "
            f"{expected_flutter_version or ''}".strip() + "."
        )
        return _result(False, None, None, None, caleemobile_sha, regression_sha,
                       cm_clean, reg_clean, waiver, dirty_sources, commands, problems)

    def _run(label: str, argv: "list[str]", cwd: Path) -> "subprocess.CompletedProcess[str] | None":
        try:
            proc = runner(argv, capture_output=True, text=True, timeout=timeout, cwd=str(cwd))
        except (OSError, subprocess.SubprocessError) as exc:
            commands.append(CommandRecord(argv=argv, cwd=str(cwd), exit_code=None, label=label, error=str(exc)))
            problems.append(f"{label} could not be executed: {exc}")
            return None
        commands.append(CommandRecord(argv=argv, cwd=str(cwd), exit_code=proc.returncode, label=label))
        return proc

    # 1. flutter --version --machine -- the ACTUAL versions + resolved path.
    version_proc = _run("flutter --version", [flutter_path, "--version", "--machine"], cm_path)
    flutter_version: "str | None" = None
    dart_version: "str | None" = None
    if version_proc is not None:
        if version_proc.returncode != 0:
            problems.append(f"`flutter --version` exited {version_proc.returncode}.")
        flutter_version, dart_version = parse_flutter_version_machine(version_proc.stdout or "")
        if flutter_version is None:
            problems.append("`flutter --version --machine` did not report a frameworkVersion -- cannot record the actual Flutter version.")
        elif expected_flutter_version is not None and flutter_version != expected_flutter_version:
            problems.append(
                f"actual Flutter version {flutter_version!r} != required {expected_flutter_version!r} "
                f"-- the installed toolchain is not the pinned release toolchain."
            )
        if dart_version is None:
            problems.append("`flutter --version --machine` did not report a dartSdkVersion.")

    # 2. flutter pub get -- dependencies resolve against THIS checkout.
    pub_get = _run("flutter pub get", [flutter_path, "pub", "get"], cm_path)
    if pub_get is not None and pub_get.returncode != 0:
        problems.append(f"`flutter pub get` exited {pub_get.returncode} against {cm_path}.")

    # 3. flutter analyze --fatal-infos -- the checkout is statically clean, infos
    #    fatal (matching CaleeMobile product CI, .github/workflows/flutter-ci.yml).
    analyze = _run("flutter analyze", [flutter_path, "analyze", "--fatal-infos"], cm_path)
    if analyze is not None and analyze.returncode != 0:
        problems.append(f"`flutter analyze --fatal-infos` exited {analyze.returncode} against {cm_path}.")

    # 4. the selector-contract tests (CaleeMobile-Regression ui/).
    reg_ui = reg_path / "ui"
    if not (reg_ui / "test_selector_contract.py").is_file():
        problems.append(f"selector-contract tests not found at {reg_ui}/test_selector_contract.py.")
    else:
        tests = _run(
            "selector-contract tests",
            ["python3", "-m", "unittest", "test_selector_contract"],
            reg_ui,
        )
        if tests is not None and tests.returncode != 0:
            problems.append(f"selector-contract tests exited {tests.returncode}.")

    ok = not problems
    return _result(ok, flutter_path, flutter_version, dart_version, caleemobile_sha,
                   regression_sha, cm_clean, reg_clean, waiver, dirty_sources, commands, problems)


def _result(ok, flutter_path, flutter_version, dart_version, caleemobile_sha,
            regression_sha, cm_clean, reg_clean, waiver, dirty_sources, commands, problems):
    return ToolchainVerification(
        ok=ok,
        flutter_path=flutter_path,
        flutter_version=flutter_version,
        dart_version=dart_version,
        caleemobile_sha=caleemobile_sha,
        regression_sha=regression_sha,
        caleemobile_clean=cm_clean,
        regression_clean=reg_clean,
        dirty_waiver=waiver,
        dirty_sources=list(dirty_sources),
        commands=commands,
        problems=problems,
    )
