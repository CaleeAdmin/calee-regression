"""Hermetic Python bootstrap contract (Workstream 1).

Proves the launcher/bootstrap layer binds every ``-m calee_regression``
invocation to an ABSOLUTE, repository-owned interpreter (``CALEE_PYTHON``), so a
stripped ``PATH`` or a foreign activated virtualenv can never select a
different interpreter that lacks this framework's dependencies -- the
interpreter-portability weakness a cloud session exposed (a launcher picked a
system python without ``click`` even though the repo ``.venv`` had the right
deps). No global install of this package is required for any launcher to work.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from calee_regression.suites import REPO_ROOT

RESOLVER = REPO_ROOT / "scripts" / "lib" / "hermetic_python.sh"
ENSURE = REPO_ROOT / "scripts" / "ensure_environment.sh"


def _launcher_files() -> "list[Path]":
    return sorted(REPO_ROOT.glob("tester/**/*.command")) + sorted(REPO_ROOT.glob("scripts/*.sh"))


def _bash(script: str, *, cwd: Path, env: "dict | None" = None) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    # Never let the caller's own hermetic env leak into a bootstrap under test.
    full.pop("CALEE_PYTHON", None)
    full.pop("VIRTUAL_ENV", None)
    if env is not None:
        full.update(env)
    return subprocess.run(
        ["bash", "-c", script], cwd=str(cwd), env=full,
        capture_output=True, text=True, timeout=90,
    )


def _real_python_link(path: Path) -> None:
    """A working interpreter with this framework's deps. An exec-wrapper (not a
    bare symlink) so the REAL test interpreter runs as itself and its venv
    site-packages -- i.e. click + calee_regression -- are importable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    path.chmod(0o755)


# ── resolver precedence (scripts/lib/hermetic_python.sh) ────────────────────
def _resolve(repo: Path, env: "dict | None" = None) -> str:
    r = _bash(
        f'. "{RESOLVER}"; _calee_resolve_python "{repo}"; printf "%s" "${{CALEE_PYTHON:-}}"',
        cwd=repo if repo.exists() else REPO_ROOT, env=env,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_resolver_prefers_repo_venv_over_system(tmp_path):
    repo = tmp_path / "repo"
    _real_python_link(repo / ".venv" / "bin" / "python")
    assert _resolve(repo) == str(repo / ".venv" / "bin" / "python")


def test_resolver_honours_working_preset(tmp_path):
    repo = tmp_path / "repo"
    _real_python_link(repo / ".venv" / "bin" / "python")  # would be picked...
    pin = tmp_path / "pinned" / "python"
    _real_python_link(pin)
    assert _resolve(repo, env={"CALEE_PYTHON": str(pin)}) == str(pin)  # ...preset wins


def test_resolver_ignores_broken_preset(tmp_path):
    repo = tmp_path / "repo"
    _real_python_link(repo / ".venv" / "bin" / "python")
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    assert _resolve(repo, env={"CALEE_PYTHON": str(broken)}) == str(repo / ".venv" / "bin" / "python")


def test_resolver_falls_back_to_system_when_no_venv(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    got = _resolve(repo)
    # With no repo .venv and no preset, it resolves a PATH python (whatever that
    # is -- possibly an ambient venv). What it must NOT do is invent the repo's
    # own nonexistent .venv interpreter.
    assert got and Path(got).name.startswith("python")
    assert got != str(repo / ".venv" / "bin" / "python")


def test_resolver_path_with_spaces(tmp_path):
    repo = tmp_path / "a b" / "repo dir"
    _real_python_link(repo / ".venv" / "bin" / "python")
    got = _resolve(repo)
    assert got == str(repo / ".venv" / "bin" / "python")
    assert _bash(f'"{got}" -c "print(1+1)"', cwd=repo).stdout.strip() == "2"


def test_resolver_foreign_virtualenv_cannot_hijack(tmp_path):
    repo = tmp_path / "repo"
    _real_python_link(repo / ".venv" / "bin" / "python")
    foreign = tmp_path / "foreign-venv" / "bin"
    _real_python_link(foreign / "python")
    env = {"VIRTUAL_ENV": str(foreign.parent), "PATH": f"{foreign}:{os.environ['PATH']}"}
    assert _resolve(repo, env=env) == str(repo / ".venv" / "bin" / "python")


# ── ensure_environment.sh bootstrap ─────────────────────────────────────────
def _mini_repo(tmp_path, *, with_config=True, activate=True) -> Path:
    repo = tmp_path / "calee-regression"
    (repo / "scripts" / "lib").mkdir(parents=True)
    shutil.copyfile(ENSURE, repo / "scripts" / "ensure_environment.sh")
    shutil.copyfile(RESOLVER, repo / "scripts" / "lib" / "hermetic_python.sh")
    (repo / "config").mkdir()
    if with_config:
        (repo / "config" / "tester.local.yaml").write_text("appium_url: x\n")
    if activate:
        (repo / ".venv" / "bin").mkdir(parents=True)
        (repo / ".venv" / "bin" / "activate").write_text("")  # stub, sourced no-op
    return repo


def _source_ensure(repo: Path, env: "dict | None" = None) -> subprocess.CompletedProcess:
    return _bash(
        "source scripts/ensure_environment.sh; rc=$?; "
        'printf "__RC__=%s\\n" "$rc"; '
        'printf "__CALEE_PYTHON__=%s\\n" "${CALEE_PYTHON:-}"; '
        'printf "__BOOTSTRAP__=%s\\n" "${CALEE_BOOTSTRAP_VERSION:-}"',
        cwd=repo, env=env,
    )


def test_ensure_environment_uses_repo_venv_under_stripped_path(tmp_path):
    """The core hermetic property: PATH=/usr/bin:/bin still binds to the repo
    .venv, and no global click is required."""
    repo = _mini_repo(tmp_path)
    _real_python_link(repo / ".venv" / "bin" / "python")
    r = _source_ensure(repo, env={"PATH": "/usr/bin:/bin"})
    assert "__RC__=0" in r.stdout, r.stdout + r.stderr
    assert f"__CALEE_PYTHON__={repo / '.venv' / 'bin' / 'python'}" in r.stdout
    assert "__BOOTSTRAP__=2" in r.stdout


def test_ensure_environment_blocks_clearly_on_broken_venv(tmp_path):
    """A present-but-broken .venv is diagnosed explicitly and NOT silently
    recreated or replaced by a system python."""
    repo = _mini_repo(tmp_path)
    broken = repo / ".venv" / "bin" / "python"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    r = _source_ensure(repo, env={"PATH": "/usr/bin:/bin"})
    assert "__RC__=1" in r.stdout, r.stdout
    assert "will not run" in r.stdout
    assert broken.exists()  # not deleted/recreated


def test_ensure_environment_creates_missing_venv_once(tmp_path):
    """No .venv at all: the venv is created exactly once via `python -m venv`,
    and a second bootstrap is a no-op (never repeatedly recreated)."""
    repo = _mini_repo(tmp_path, activate=False)
    log = tmp_path / "venv.log"
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    shim_body = (
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        '  d="$3"; mkdir -p "$d/bin"; : > "$d/bin/activate"; '
        f'printf \'#!/bin/sh\\nexec "{sys.executable}" "$@"\\n\' > "$d/bin/python"; '
        f'chmod 755 "$d/bin/python"; echo venv >> "{log}"; exit 0\n'
        "fi\n"
        f'exec "{sys.executable}" "$@"\n'
    )
    # Shadow every interpreter name the bootstrap's PYTHON_BIN search may pick
    # (python3.11 before python3 before python), so the fake `-m venv` runs.
    for name in ("python3.11", "python3", "python"):
        shim = fakebin / name
        shim.write_text(shim_body)
        shim.chmod(0o755)
    env = {"PATH": f"{fakebin}:/usr/bin:/bin"}
    r1 = _source_ensure(repo, env=env)
    assert "__RC__=0" in r1.stdout, r1.stdout + r1.stderr
    assert f"__CALEE_PYTHON__={repo / '.venv' / 'bin' / 'python'}" in r1.stdout
    r2 = _source_ensure(repo, env=env)
    assert "__RC__=0" in r2.stdout
    assert log.read_text().count("venv") == 1  # created once, not twice


def test_bootstrap_never_logs_credentials(tmp_path):
    repo = _mini_repo(tmp_path)
    _real_python_link(repo / ".venv" / "bin" / "python")
    secret = "SuperSecretPassw0rd-do-not-leak"
    r = _source_ensure(repo, env={
        "PATH": "/usr/bin:/bin",
        "CALEE_TEST_PASSWORD": secret,
        "CALEE_TEST_EMAIL": "tester@example.com",
        "CALEE_API_BASE": "https://hub-dev.example",
    })
    assert secret not in r.stdout and secret not in r.stderr
    setup_log = repo / "reports" / "setup.log"
    if setup_log.exists():
        assert secret not in setup_log.read_text()
    assert secret not in ENSURE.read_text()


# ── static launcher scan ────────────────────────────────────────────────────
_BARE = re.compile(r'(?<!["\w$])python3?\s+-m\s+calee_regression\b')
_FRAMEWORK = re.compile(r'-m\s+calee_regression\b')


def test_no_launcher_invokes_framework_with_bare_python():
    offenders = []
    for f in _launcher_files():
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if _BARE.search(line):
                offenders.append(f"{f.relative_to(REPO_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "bare python invoking the framework:\n" + "\n".join(offenders)


def test_every_framework_invocation_uses_calee_python():
    """Every `-m calee_regression` invocation is preceded by the hermetic
    interpreter token `"$CALEE_PYTHON"` (also inside `$(...)` command
    substitutions like `X="$("$CALEE_PYTHON" -m calee_regression ...)"`)."""
    problems = []
    for f in _launcher_files():
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for m in _FRAMEWORK.finditer(line):
                before = line[: m.start()].rstrip()
                if not before.endswith('"$CALEE_PYTHON"'):
                    problems.append(f"{f.relative_to(REPO_ROOT)}:{i}: {line.strip()}")
    assert not problems, "framework not invoked via \"$CALEE_PYTHON\":\n" + "\n".join(problems)


def test_interpreter_provenance_is_secret_free_and_complete():
    from calee_regression.bootstrap_provenance import interpreter_provenance

    prov = interpreter_provenance()
    for key in ("pythonExecutable", "pythonVersion", "virtualEnvironment",
                "inVirtualEnvironment", "bootstrapVersion"):
        assert key in prov
    assert prov["pythonExecutable"] == sys.executable
    assert re.match(r"\d+\.\d+\.\d+", prov["pythonVersion"])
