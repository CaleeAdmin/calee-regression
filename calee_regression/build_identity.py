"""Automatic build-identity collection (Phase 3).

Records exactly which builds a release run tested, so the consolidated
report can prove the intended Calee tablet build and the intended CaleeMobile
commit were the ones under test -- and BLOCK when that can't be established
(never certify an unknown or dirty build for release).

The parsers here (Flutter pubspec version, git output, adb `dumpsys package`
output) are pure and unit-tested. The collectors shell out to git/adb and
read files; they never raise -- a missing tool, device, or checkout degrades
to ``available=False`` (which the consolidator turns into BLOCKED when that
app's identity is in scope), never a crash or a silently-invented value.

The `build-identity` CLI command emits every collected value as
``AUTO_<NAME>=<shell-quoted>`` assignments, so a launcher can
``eval "$(python -m calee_regression build-identity)"`` and then prefer any
value a technical owner set manually, falling back to the auto-detected one.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

# `version: 0.0.22+22` at column 0 in pubspec.yaml (never an indented
# `version:` under some dependency).
_PUBSPEC_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)
_DUMPSYS_VERSION_NAME_RE = re.compile(r"versionName=(\S+)")
_DUMPSYS_VERSION_CODE_RE = re.compile(r"versionCode=(\d+)")


def parse_pubspec_version(pubspec_text: str) -> "str | None":
    """Extract the `version:` value (e.g. ``0.0.22+22``) from pubspec.yaml."""
    match = _PUBSPEC_VERSION_RE.search(pubspec_text or "")
    return match.group(1) if match else None


def parse_git_sha(rev_parse_output: "str | None") -> "str | None":
    """Normalize `git rev-parse HEAD` output to a bare SHA, or None."""
    if not rev_parse_output:
        return None
    sha = rev_parse_output.strip()
    return sha or None


def parse_git_dirty(porcelain_output: "str | None") -> bool:
    """True when `git status --porcelain` reported any uncommitted change."""
    if not porcelain_output:
        return False
    return any(line.strip() for line in porcelain_output.splitlines())


def parse_dumpsys_version_name(dumpsys_output: "str | None") -> "str | None":
    if not dumpsys_output:
        return None
    match = _DUMPSYS_VERSION_NAME_RE.search(dumpsys_output)
    return match.group(1) if match else None


def parse_dumpsys_version_code(dumpsys_output: "str | None") -> "str | None":
    if not dumpsys_output:
        return None
    match = _DUMPSYS_VERSION_CODE_RE.search(dumpsys_output)
    return match.group(1) if match else None


@dataclass
class BuildIdentity:
    """One app's detected build identity. ``available`` is the gate the
    consolidator reads: False means "we could not determine which build this
    is", which BLOCKS a release when the app is in scope."""

    available: bool = False
    build_version: "str | None" = None
    git_sha: "str | None" = None
    dirty: bool = False
    version_code: "str | None" = None
    application_id: "str | None" = None
    caleeshell_version: "str | None" = None
    source: "str | None" = None

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "buildVersion": self.build_version,
            "gitSha": self.git_sha,
            "dirty": self.dirty,
            "versionCode": self.version_code,
            "applicationId": self.application_id,
            "caleeShellVersion": self.caleeshell_version,
            "source": self.source,
        }

    def to_shell(self, prefix: str) -> str:
        """Emit ``AUTO_<PREFIX>_*`` shell assignments (quoted). Only non-None
        string values are emitted; booleans are always emitted as
        ``true``/``false`` so a launcher can branch on them unambiguously."""
        lines = [
            f"AUTO_{prefix}_IDENTITY_AVAILABLE={'true' if self.available else 'false'}",
            f"AUTO_{prefix}_DIRTY={'true' if self.dirty else 'false'}",
        ]
        for suffix, value in (
            ("BUILD_VERSION", self.build_version),
            ("GIT_SHA", self.git_sha),
            ("VERSION_CODE", self.version_code),
            ("APPLICATION_ID", self.application_id),
            ("CALEESHELL_VERSION", self.caleeshell_version),
        ):
            if value is not None:
                lines.append(f"AUTO_{prefix}_{suffix}={shlex.quote(str(value))}")
        return "\n".join(lines)


def _run(cmd: "list[str]", cwd: "Path | None" = None, timeout: float = 20) -> "str | None":
    """Run a command, returning stdout on success or None on any failure
    (non-zero exit, missing binary, timeout) -- collectors must degrade, never
    raise."""
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _git_identity(source: Path) -> "tuple[str | None, bool]":
    """(git_sha, dirty) for a checkout, best-effort. Returns (None, False)
    when `source` is not a git working tree or git is unavailable."""
    inside = _run(["git", "-C", str(source), "rev-parse", "--is-inside-work-tree"])
    if not inside or inside.strip() != "true":
        return None, False
    sha = parse_git_sha(_run(["git", "-C", str(source), "rev-parse", "HEAD"]))
    dirty = parse_git_dirty(_run(["git", "-C", str(source), "status", "--porcelain"]))
    return sha, dirty


def collect_caleemobile_identity(source_dir: "Path | str") -> BuildIdentity:
    """CaleeMobile identity from its source checkout: pubspec version+build,
    Git SHA, and dirty flag. ``available`` is True once the pubspec version is
    read -- that is the minimum needed to say which build this is; the Git SHA
    and dirty flag are recorded best-effort on top."""
    source = Path(source_dir)
    pubspec = source / "pubspec.yaml"
    if not pubspec.is_file():
        return BuildIdentity(available=False, source=str(source))
    try:
        version = parse_pubspec_version(pubspec.read_text(encoding="utf-8"))
    except OSError:
        version = None
    sha, dirty = _git_identity(source)
    return BuildIdentity(
        available=bool(version),
        build_version=version,
        git_sha=sha,
        dirty=dirty,
        source=str(source),
    )


def collect_calee_tablet_identity(
    *,
    source_dir: "Path | str | None" = None,
    android_package: "str | None" = None,
    adb_path: str = "adb",
    caleeshell_version: "str | None" = None,
) -> BuildIdentity:
    """Calee tablet identity, best-effort. Installed package version/
    versionCode come from `adb shell dumpsys package <android_package>` when a
    device is connected; the tablet source Git SHA/dirty from `source_dir`
    when a checkout is available. ``available`` is True once an installed
    package version is read.

    Everything degrades cleanly: no device, no adb, or no source checkout just
    leaves those fields None and ``available=False`` -- which the consolidator
    turns into BLOCKED when the tablet is in scope (mandatory identity
    unavailable), never a fabricated pass."""
    version_name = None
    version_code = None
    if android_package:
        dumpsys = _run([adb_path, "shell", "dumpsys", "package", android_package], timeout=30)
        version_name = parse_dumpsys_version_name(dumpsys)
        version_code = parse_dumpsys_version_code(dumpsys)

    sha, dirty = (None, False)
    if source_dir is not None:
        sha, dirty = _git_identity(Path(source_dir))

    return BuildIdentity(
        available=bool(version_name),
        build_version=version_name,
        git_sha=sha,
        dirty=dirty,
        version_code=version_code,
        application_id=android_package,
        caleeshell_version=caleeshell_version,
        source=str(source_dir) if source_dir is not None else None,
    )
