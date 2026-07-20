"""Unit tests for calee_regression.report_root (Priority 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from calee_regression import report_root


def test_default_is_repo_root_when_nothing_configured(tmp_path):
    # resolve_report_root() returns the repo_root-EQUIVALENT value (what a
    # caller passes to RunWorkspace(...), which appends "reports/runs/<id>"
    # itself) -- not a "reports/" directory. See the module docstring.
    resolved = report_root.resolve_report_root(repo_root=tmp_path, env={})
    assert resolved == tmp_path.resolve()
    assert resolved.is_dir()


def test_env_var_wins_over_machine_report_dir(tmp_path):
    env_root = tmp_path / "from-env"
    machine_root = tmp_path / "from-machine"
    resolved = report_root.resolve_report_root(
        repo_root=tmp_path / "repo",
        machine_report_dir=str(machine_root),
        env={report_root.ENV_VAR: str(env_root)},
    )
    assert resolved == env_root.resolve()
    assert not machine_root.exists()


def test_machine_report_dir_used_when_env_var_absent(tmp_path):
    machine_root = tmp_path / "from-machine"
    resolved = report_root.resolve_report_root(
        repo_root=tmp_path / "repo", machine_report_dir=str(machine_root), env={},
    )
    assert resolved == machine_root.resolve()


def test_resolved_path_is_absolute_and_canonical(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = report_root.resolve_report_root(
        repo_root=tmp_path, env={report_root.ENV_VAR: "relative-reports"},
    )
    assert resolved.is_absolute()
    assert resolved == (tmp_path / "relative-reports").resolve()


def test_relative_configured_value_resolves_against_repo_root_not_cwd(tmp_path, monkeypatch):
    # A technical owner's relative report_dir must behave the same no matter
    # which directory a command happened to be launched from -- never
    # silently depend on the process's CWD.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    resolved = report_root.resolve_report_root(
        repo_root=repo_root, env={report_root.ENV_VAR: "custom-reports"},
    )
    assert resolved == (repo_root / "custom-reports").resolve()
    assert resolved != (elsewhere / "custom-reports").resolve()


def test_expands_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = report_root.resolve_report_root(
        repo_root=tmp_path, env={report_root.ENV_VAR: "~/custom-reports"},
    )
    assert resolved == (tmp_path / "custom-reports").resolve()


def test_directory_created_if_missing(tmp_path):
    target = tmp_path / "does" / "not" / "exist" / "yet"
    assert not target.exists()
    resolved = report_root.resolve_report_root(repo_root=tmp_path, env={report_root.ENV_VAR: str(target)})
    assert resolved.is_dir()


def test_rejects_filesystem_root(tmp_path):
    with pytest.raises(report_root.ReportRootError, match="filesystem root"):
        report_root.resolve_report_root(repo_root=tmp_path, env={report_root.ENV_VAR: "/"})


def test_rejects_unwritable_root(tmp_path, monkeypatch):
    # Real chmod-based unwritability isn't authoritative here: this suite may
    # run as root, under which os.access(..., W_OK) reports writable
    # regardless of the mode bits. Monkeypatch the check itself instead, so
    # this test is meaningful under any uid.
    monkeypatch.setattr(report_root.os, "access", lambda *a, **k: False)
    with pytest.raises(report_root.ReportRootError, match="not writable"):
        report_root.resolve_report_root(repo_root=tmp_path, env={report_root.ENV_VAR: str(tmp_path / "reports")})


def test_whitespace_only_env_var_falls_back_to_default(tmp_path):
    resolved = report_root.resolve_report_root(repo_root=tmp_path, env={report_root.ENV_VAR: "   "})
    assert resolved == tmp_path.resolve()


def test_two_distinct_custom_roots_never_collide(tmp_path):
    root_a = report_root.resolve_report_root(
        repo_root=tmp_path, env={report_root.ENV_VAR: str(tmp_path / "root-a")},
    )
    root_b = report_root.resolve_report_root(
        repo_root=tmp_path, env={report_root.ENV_VAR: str(tmp_path / "root-b")},
    )
    assert root_a != root_b
    assert root_a.is_dir() and root_b.is_dir()


def test_composes_with_run_workspace_without_doubling_the_reports_segment(tmp_path):
    # Regression test: RunWorkspace.root already appends "reports/runs/
    # <run-id>" to whatever repo_root it's given. resolve_report_root() must
    # return the repo_root-EQUIVALENT value, not a "reports/" directory
    # itself, or composing the two doubles up to ".../reports/reports/...".
    from calee_regression import run_context

    custom_root = tmp_path / "custom-calee-reports"
    resolved = report_root.resolve_report_root(
        repo_root=tmp_path / "unused-default", env={report_root.ENV_VAR: str(custom_root)},
    )
    workspace = run_context.RunWorkspace(resolved, "release-test-001")
    assert workspace.root == custom_root.resolve() / "reports" / "runs" / "release-test-001"
    assert workspace.root.parts.count("reports") == 1


def test_default_composes_with_run_workspace_exactly_as_before(tmp_path):
    # Same invariant for the unconfigured (default) case: resolve_report_root()
    # with nothing set must be a drop-in replacement for the bare REPO_ROOT
    # constant every RunWorkspace(...) call site used to pass directly.
    from calee_regression import run_context

    resolved = report_root.resolve_report_root(repo_root=tmp_path, env={})
    workspace = run_context.RunWorkspace(resolved, "release-test-002")
    assert workspace.root == tmp_path.resolve() / "reports" / "runs" / "release-test-002"
