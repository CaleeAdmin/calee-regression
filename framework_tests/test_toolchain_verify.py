"""Real Flutter toolchain verification for locally-generated selector evidence
(Priority 1, Problem A).

A caller-supplied Flutter string must never become proof of the installed
toolchain. These tests drive ``verify_local_toolchain`` with an injected
``which``/``runner`` so the policy is exercised without a real Flutter install:
the recorded version comes from parsed ``flutter --version`` output, and any
missing/failed command means verification is NOT ok (so the gate BLOCKS).
"""

from __future__ import annotations

import json
import subprocess

from calee_regression import toolchain_verify as tv


def _cm(tmp_path):
    cm = tmp_path / "CaleeMobile"
    (cm / "lib").mkdir(parents=True)
    (cm / "pubspec.yaml").write_text("version: 0.0.23+23\n")
    return cm


def _reg(tmp_path):
    reg = tmp_path / "CaleeMobile-Regression"
    (reg / "ui").mkdir(parents=True)
    (reg / "ui" / "test_selector_contract.py").write_text("# tests\n")
    return reg


def _machine_json(fw="3.44.1", dart="3.5.0"):
    return json.dumps({"frameworkVersion": fw, "dartSdkVersion": dart, "channel": "stable"})


def _fake_runner(script):
    """Build a runner from {label-substring: (returncode, stdout)} keyed by argv."""
    def run(argv, **kwargs):
        joined = " ".join(argv)
        for key, (rc, out) in script.items():
            if key in joined:
                return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return run


def test_parse_flutter_version_machine():
    fw, dart = tv.parse_flutter_version_machine(_machine_json("3.44.1", "3.5.0"))
    assert fw == "3.44.1"
    assert dart == "3.5.0"


def test_parse_flutter_version_machine_garbage():
    assert tv.parse_flutter_version_machine("not json") == (None, None)


def test_blocks_when_flutter_absent(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: None,  # no flutter on PATH
        runner=_fake_runner({}),
        git_sha=lambda p: "f" * 40,
    )
    assert result.ok is False
    assert any("PATH" in p for p in result.problems)
    assert result.flutter_version is None


def test_ok_when_all_commands_pass(tmp_path):
    runner = _fake_runner({
        "--version --machine": (0, _machine_json("3.44.1", "3.5.0")),
        "pub get": (0, ""),
        "analyze": (0, ""),
        "unittest": (0, ""),
    })
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter",
        runner=runner,
        git_sha=lambda p: "a" * 40,
    )
    assert result.ok is True, result.problems
    # The recorded version is the PARSED one, not any caller string.
    assert result.flutter_version == "3.44.1"
    assert result.dart_version == "3.5.0"
    assert result.flutter_path == "/usr/bin/flutter"
    # Every command's argv + exit code is recorded.
    labels = {c.label for c in result.commands}
    assert {"flutter --version", "flutter pub get", "flutter analyze", "selector-contract tests"} <= labels
    assert all(c.exit_code == 0 for c in result.commands)


def test_blocks_on_wrong_actual_flutter_version(tmp_path):
    runner = _fake_runner({
        "--version --machine": (0, _machine_json("3.43.0", "3.4.0")),
        "pub get": (0, ""), "analyze": (0, ""), "unittest": (0, ""),
    })
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=runner, git_sha=lambda p: "a" * 40,
        expected_flutter_version="3.44.1",
    )
    assert result.ok is False
    assert any("not the pinned release toolchain" in p for p in result.problems)


def test_blocks_when_analyze_fails(tmp_path):
    runner = _fake_runner({
        "--version --machine": (0, _machine_json()),
        "pub get": (0, ""),
        "analyze": (1, ""),  # analyzer found problems
        "unittest": (0, ""),
    })
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=runner, git_sha=lambda p: "a" * 40,
    )
    assert result.ok is False
    assert any("analyze" in p for p in result.problems)


def test_blocks_when_selector_tests_fail(tmp_path):
    runner = _fake_runner({
        "--version --machine": (0, _machine_json()),
        "pub get": (0, ""), "analyze": (0, ""),
        "unittest": (1, ""),  # selector-contract tests failed
    })
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=runner, git_sha=lambda p: "a" * 40,
    )
    assert result.ok is False
    assert any("selector-contract tests" in p for p in result.problems)


def test_records_source_shas(tmp_path):
    runner = _fake_runner({
        "--version --machine": (0, _machine_json()),
        "pub get": (0, ""), "analyze": (0, ""), "unittest": (0, ""),
    })
    shas = {"CaleeMobile": "a" * 40, "CaleeMobile-Regression": "b" * 40}
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=runner,
        git_sha=lambda p: shas.get(p.name),
    )
    assert result.caleemobile_sha == "a" * 40
    assert result.regression_sha == "b" * 40


def test_to_dict_is_json_serializable(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: None, runner=_fake_runner({}), git_sha=lambda p: None,
    )
    # Must round-trip through JSON (it is embedded in the gate report).
    json.dumps(result.to_dict())


# --- Priority 4: local-evidence hardening -----------------------------------

_ALL_PASS = {
    "--version --machine": (0, _machine_json("3.44.1", "3.5.0")),
    "pub get": (0, ""), "analyze": (0, ""), "unittest": (0, ""),
}


def test_blocks_when_source_not_a_git_repo(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=_fake_runner(_ALL_PASS),
        git_sha=lambda p: None,  # not a git repo / no HEAD
        is_clean=lambda p: (True, []),
    )
    assert result.ok is False
    assert any("not a Git repository" in p for p in result.problems)


def test_blocks_on_abbreviated_head_sha(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=_fake_runner(_ALL_PASS),
        git_sha=lambda p: "abc1234",  # abbreviated
        is_clean=lambda p: (True, []),
    )
    assert result.ok is False
    assert any("full 40-character Git SHA" in p for p in result.problems)


def test_blocks_on_dirty_worktree_without_waiver(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=_fake_runner(_ALL_PASS),
        git_sha=lambda p: "a" * 40,
        is_clean=lambda p: (False, [" M lib/foo.dart"]) if p.name == "CaleeMobile" else (True, []),
    )
    assert result.ok is False
    assert any("worktree(s) are dirty" in p for p in result.problems)


def test_dirty_worktree_allowed_with_named_waiver(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        dirty_waiver="LOCAL-DEV-2026-07-18: verifying an in-progress selector fix",
        which=lambda name: "/usr/bin/flutter", runner=_fake_runner(_ALL_PASS),
        git_sha=lambda p: "a" * 40,
        is_clean=lambda p: (False, [" M lib/foo.dart"]) if p.name == "CaleeMobile" else (True, []),
    )
    assert result.ok is True, result.problems
    assert result.dirty_sources == ["CaleeMobile"]
    assert "LOCAL-DEV-2026-07-18" in result.dirty_waiver


def test_analyze_uses_fatal_infos(tmp_path):
    captured = {}

    def runner(argv, **kwargs):
        joined = " ".join(argv)
        if "analyze" in joined:
            captured["analyze_argv"] = list(argv)
        for key, (rc, out) in _ALL_PASS.items():
            if key in joined:
                return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=runner,
        git_sha=lambda p: "a" * 40, is_clean=lambda p: (True, []),
    )
    assert "--fatal-infos" in captured["analyze_argv"]


def test_record_digest_protects_the_record(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=_fake_runner(_ALL_PASS),
        git_sha=lambda p: "a" * 40, is_clean=lambda p: (True, []),
    )
    record = result.to_dict()
    assert record["recordDigest"].startswith("sha256:")
    assert tv.verify_toolchain_record(record) == []
    # Tamper an attested field -> digest mismatch BLOCKS.
    record["flutterVersion"] = "9.9.9"
    problems = tv.verify_toolchain_record(record)
    assert any("record digest mismatch" in p for p in problems)


def test_records_exact_verified_source_shas(tmp_path):
    result = tv.verify_local_toolchain(
        _cm(tmp_path), _reg(tmp_path),
        which=lambda name: "/usr/bin/flutter", runner=_fake_runner(_ALL_PASS),
        git_sha=lambda p: ("a" * 40) if p.name == "CaleeMobile" else ("b" * 40),
        is_clean=lambda p: (True, []),
    )
    assert result.ok is True, result.problems
    d = result.to_dict()
    assert d["caleemobileSha"] == "a" * 40
    assert d["regressionSha"] == "b" * 40
