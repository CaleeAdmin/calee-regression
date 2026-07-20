"""Priority 8/9 -- the framework-tests CI workflow proves the merged content.

Asserts (from the workflow YAML itself) that:
  * it runs on pushes (main included), pull requests, AND the merge_group
    merge commit -- so what actually lands is tested;
  * a push to main runs the SAME required framework gates as a PR (pytest,
    coverage/promotion consistency, scenario validation, shellcheck);
  * the exact commit SHA is embedded in retained framework-test evidence;
  * a merge-commit / main smoke check verifies that embedding;
  * no job carries an `if:` that could make a required check go grey/skipped
    (a skipped check must never be mistaken for a passing one).
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "framework-tests.yml"


def _load():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as boolean True.
    on = data.get("on")
    if on is None:
        on = data.get(True)
    return data, (on or {})


def test_workflow_runs_on_push_pr_and_merge_group():
    _, on = _load()
    assert "push" in on, "framework-tests must run on push (main included)"
    assert "pull_request" in on, "framework-tests must run on pull requests"
    assert "merge_group" in on, "framework-tests must run on the merge-queue merge commit (Priority 8)"


def test_push_to_main_runs_the_same_required_gates():
    data, _ = _load()
    steps = data["jobs"]["test"]["steps"]
    run_cmds = " \n".join(s.get("run", "") for s in steps if isinstance(s, dict))
    # The core release gates a PR runs are the same ones a main push runs
    # (single unconditional job -> identical on every event).
    assert "python -m pytest" in run_cmds
    assert "coverage-report --check" in run_cmds
    assert "load_scenario" in run_cmds  # scenario files validated
    assert "shellcheck" in run_cmds


def test_no_job_or_step_can_go_grey_skipped():
    data, _ = _load()
    for job_name, job in data["jobs"].items():
        assert "if" not in job, f"job {job_name!r} has an if: that could skip a required check"
        # Steps may carry if: always()/event guards for evidence, but no step
        # guards a required *gate* behind a condition that would skip it silently.
        for step in job.get("steps", []):
            cond = str(step.get("if", ""))
            if cond and "always()" not in cond and "merge_group" not in cond and "refs/heads/main" not in cond:
                raise AssertionError(f"step {step.get('name')!r} has a non-evidence if: {cond!r}")


def test_exact_commit_sha_is_embedded_and_retained():
    data, _ = _load()
    steps = data["jobs"]["test"]["steps"]
    names = [s.get("name", "") for s in steps]
    # SHA-embedding evidence step.
    assert any("commit SHA" in n or "exact commit" in n.lower() for n in names)
    evidence = next(s for s in steps if "evidence" in s.get("name", "").lower())
    assert "GITHUB_SHA" in evidence.get("run", "")
    # Retained as an artifact with an explicit retention window.
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert int(upload["with"]["retention-days"]) >= 1
    assert upload["with"]["path"].endswith(".json")


def test_merge_commit_smoke_check_present():
    data, _ = _load()
    steps = data["jobs"]["test"]["steps"]
    smoke = [s for s in steps if "smoke" in s.get("name", "").lower()]
    assert smoke, "a merge-commit / main smoke check must be present"
    cond = str(smoke[0].get("if", ""))
    assert "merge_group" in cond and "refs/heads/main" in cond
    assert "GITHUB_SHA" in smoke[0].get("run", "")
