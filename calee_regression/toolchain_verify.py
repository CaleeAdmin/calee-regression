"""Actually execute the Flutter toolchain to back locally-generated selector
evidence (Priority 1, Problem A).

A caller-supplied Flutter version string must never become proof of the
installed toolchain. The release gate previously handed a hard-coded
``3.44.1`` to the selector-contract emitter, which recorded it verbatim as
``flutterVersion`` -- so a machine with no Flutter at all could still emit
"evidence" claiming it was produced on Flutter 3.44.1. That is fabricated
toolchain metadata.

When selector evidence is generated locally (the development fallback; a
production release accepts only a CI-produced artifact), this module runs the
real commands against the *exact* CaleeMobile checkout and records what
actually happened:

  * ``flutter --version --machine`` -> the actual Flutter framework version,
    the actual Dart SDK version, and the resolved ``flutter`` executable path;
  * ``flutter pub get``  -> dependencies resolve against this checkout;
  * ``flutter analyze``  -> the checkout is statically clean;
  * the CaleeMobile-Regression selector-contract tests
    (``ui/ python3 -m unittest test_selector_contract``).

Every command's argv, working directory and exit code is recorded, along with
the CaleeMobile and CaleeMobile-Regression source SHAs the verification ran
against. If ``flutter`` is not on PATH, or any command fails, verification is
NOT ok -- and the gate BLOCKS rather than trusting an unverified toolchain
string. The *recorded* ``flutterVersion`` is then the one this module parsed
from ``flutter --version`` output, never the caller's string.

Everything is injectable (``which``/``runner``/``git_sha``) so the policy is
unit-tested without a real Flutter install.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# The Flutter framework version a local release build must actually be built
# with, mirroring selector_evidence.EXPECTED_FLUTTER_VERSION. Kept here as a
# default so a caller can tighten/loosen it, but note: this is the version we
# REQUIRE the real toolchain to report, not a string we record on its behalf.
DEFAULT_EXPECTED_FLUTTER_VERSION = "3.44.1"

CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


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
    commands: "list[CommandRecord]" = field(default_factory=list)
    problems: "list[str]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "flutterPath": self.flutter_path,
            "flutterVersion": self.flutter_version,
            "dartVersion": self.dart_version,
            "caleemobileSha": self.caleemobile_sha,
            "regressionSha": self.regression_sha,
            "commands": [c.to_dict() for c in self.commands],
            "problems": list(self.problems),
        }


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


def parse_flutter_version_machine(stdout: str) -> "tuple[str | None, str | None]":
    """Parse ``flutter --version --machine`` JSON output.

    Returns ``(frameworkVersion, dartSdkVersion)`` -- either may be None if the
    output is not the expected JSON shape. ``--machine`` is preferred over the
    human text because it is stable and unambiguous.
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


def verify_local_toolchain(
    caleemobile_path: "Path | str",
    regression_path: "Path | str",
    *,
    expected_flutter_version: "str | None" = DEFAULT_EXPECTED_FLUTTER_VERSION,
    which: "Callable[[str], Optional[str]]" = shutil.which,
    runner: "CommandRunner | None" = None,
    git_sha: "Callable[[Path], Optional[str]]" = _git_head_sha,
    timeout: int = 600,
) -> ToolchainVerification:
    """Run the real Flutter toolchain against the exact CaleeMobile checkout and
    record what actually happened.

    ``which``/``runner``/``git_sha`` are injectable so the policy is unit-tested
    without a real Flutter install. ``runner`` defaults to ``subprocess.run``.

    The verification is OK only when: ``flutter`` resolves on PATH; every command
    (``flutter --version --machine``, ``flutter pub get``, ``flutter analyze``,
    the selector-contract tests) exits 0; and the parsed Flutter version matches
    ``expected_flutter_version`` (when one is required). The recorded
    ``flutter_version`` is the parsed one -- never a caller-supplied string.
    """
    if runner is None:
        runner = subprocess.run

    cm_path = Path(caleemobile_path)
    reg_path = Path(regression_path)
    problems: "list[str]" = []
    commands: "list[CommandRecord]" = []

    caleemobile_sha = git_sha(cm_path)
    regression_sha = git_sha(reg_path)

    if not cm_path.is_dir():
        problems.append(f"CaleeMobile checkout not found at {cm_path} -- cannot verify the toolchain against it.")
        return ToolchainVerification(
            ok=False, caleemobile_sha=caleemobile_sha, regression_sha=regression_sha,
            commands=commands, problems=problems,
        )

    flutter_path = which("flutter")
    if not flutter_path:
        problems.append(
            "flutter is not on PATH -- a local release build cannot record an actual toolchain. "
            "Provide a CI-produced selector artifact via --source, or install Flutter "
            f"{expected_flutter_version or ''}".strip() + "."
        )
        return ToolchainVerification(
            ok=False, flutter_path=None, caleemobile_sha=caleemobile_sha,
            regression_sha=regression_sha, commands=commands, problems=problems,
        )

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

    # 3. flutter analyze -- the checkout is statically clean.
    analyze = _run("flutter analyze", [flutter_path, "analyze"], cm_path)
    if analyze is not None and analyze.returncode != 0:
        problems.append(f"`flutter analyze` exited {analyze.returncode} against {cm_path}.")

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
    return ToolchainVerification(
        ok=ok,
        flutter_path=flutter_path,
        flutter_version=flutter_version,
        dart_version=dart_version,
        caleemobile_sha=caleemobile_sha,
        regression_sha=regression_sha,
        commands=commands,
        problems=problems,
    )
