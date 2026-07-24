"""Mac qualification-handoff plan (Workstream 6).

Proves the plan is concrete, ordered, secret-free, never uses a literal
``<RUN_ID>`` in a command, distinguishes diagnostic from certification, never
silently narrows the release scope, and reuses host-capabilities.
"""

from __future__ import annotations

import json
import re
import types

from click.testing import CliRunner

from calee_regression import host_capabilities as hc
from calee_regression import qualification_plan as qp
from calee_regression import release_platforms as rp
from calee_regression.cli import main


def _which(avail):
    s = set(avail)
    return lambda n: (f"/usr/bin/{n}" if n in s else None)


def _cloud_host(**over):
    kw = dict(which=_which(set()), env={}, system="Linux", machine="x86_64", release="6", hostname="vm")
    kw.update(over)
    return hc.gather_host_capabilities(**kw)


def _mac_host(tools=("adb", "appium", "flutter", "xcrun", "security", "idevice_id"), **over):
    kw = dict(
        which=_which(set(tools)),
        env={"CALEE_API_BASE": "https://hub-dev.example", "CALEE_TEST_EMAIL": "t@e.co", "CALEE_TEST_PASSWORD": "x"},
        system="Darwin", machine="arm64", release="23", hostname="mac",
        adb_runner=lambda a: types.SimpleNamespace(stdout="List of devices attached\nA26603\tdevice\n"),
        idevice_runner=lambda a: types.SimpleNamespace(stdout="00008120\n"),
    )
    kw.update(over)
    return hc.gather_host_capabilities(**kw)


def _plan(**over):
    kw = dict(host=_cloud_host(), git_runner=lambda a: types.SimpleNamespace(stdout="deadbeef\n"))
    kw.update(over)
    return qp.build_plan(**kw)


# ── shape + ordering ────────────────────────────────────────────────────────
def test_plan_is_ordered_and_has_the_core_steps():
    plan = _plan()
    ids = [s["id"] for s in plan["steps"]]
    assert ids[0] == "host-capabilities"          # confirm host first
    assert ids[1] == "preflight"                    # read-only preflight next
    assert "focused-verify" in ids and "release-run" in ids
    assert ids[-1] == "export-evidence"             # sanitized bundle last


def test_diagnostic_and_certification_are_distinguished():
    plan = _plan()
    by_id = {s["id"]: s for s in plan["steps"]}
    assert by_id["focused-verify"]["phase"] == qp.PHASE_DIAGNOSTIC
    assert by_id["release-run"]["phase"] == qp.PHASE_CERTIFICATION
    assert "NO certification" in plan["diagnosticVsCertification"]["focusedDiagnostic"]


def test_fixture_mutation_and_readonly_are_labelled():
    by_id = {s["id"]: s for s in _plan()["steps"]}
    assert by_id["preflight"]["readOnly"] is True and by_id["preflight"]["mutatesFixture"] is False
    assert by_id["focused-verify"]["mutatesFixture"] is True
    assert by_id["release-run"]["mutatesFixture"] is True
    assert by_id["export-evidence"]["readOnly"] is True


# ── the hard rules ──────────────────────────────────────────────────────────
def test_no_command_uses_a_literal_run_id_placeholder():
    plan = _plan()
    for s in plan["steps"]:
        assert "<RUN_ID>" not in s["command"]
        assert not re.search(r"<[A-Z_]+>", s["command"]), s["command"]
        # a run-id-bearing command either GENERATES the id into the shell
        # variable or REFERENCES it -- never a hand-substituted placeholder.
        if "--run-id" in s["command"] or "CALEE_RUN_ID" in s["command"]:
            assert ("$CALEE_RUN_ID" in s["command"]) or ('export CALEE_RUN_ID=' in s["command"])


def test_every_framework_command_uses_hermetic_interpreter():
    for s in _plan()["steps"]:
        if "-m calee_regression" in s["command"]:
            assert '"$CALEE_PYTHON" -m calee_regression' in s["command"], s["command"]


def test_plan_never_contains_a_secret_value():
    secret = "SECRET-passw0rd"
    plan = _plan(host=_mac_host(env={
        "CALEE_API_BASE": "https://hub-dev.example",
        "CALEE_TEST_EMAIL": "person@example.com", "CALEE_TEST_PASSWORD": secret,
    }))
    blob = json.dumps(plan) + qp.render_markdown(plan)
    assert secret not in blob
    assert "person@example.com" not in blob
    # but credential SOURCE categories ARE named
    names = {c["name"] for c in plan["requiredCredentials"]}
    assert "regression_password" in names and "backend" in names


# ── scope is never silently narrowed ────────────────────────────────────────
def test_mandatory_android_without_device_is_surfaced_not_dropped():
    # cloud host: Android mandatory (default scope) but no Android device.
    plan = _plan(host=_cloud_host())
    assert any("ANDROID_DEVICE_REQUIRED" in b for b in plan["blockingActions"])
    assert plan["platformScope"]["android"] is True  # still in scope


def test_offline_host_is_told_to_move_to_the_mac():
    plan = _plan(host=_cloud_host())
    assert plan["executionCapability"] == hc.OFFLINE_FRAMEWORK_ONLY
    assert any("OFFLINE_FRAMEWORK_ONLY" in b for b in plan["blockingActions"])


def test_equipped_mac_has_no_offline_block_and_sees_devices():
    plan = _plan(host=_mac_host())
    assert not any("OFFLINE_FRAMEWORK_ONLY" in b for b in plan["blockingActions"])
    assert not any("ANDROID_DEVICE_REQUIRED" in b for b in plan["blockingActions"])


def test_kiosk_step_present_only_when_mandatory():
    feats_off = rp.ReleaseFeatures(kiosk_admin=False)
    ids = [s["id"] for s in _plan(features=feats_off)["steps"]]
    assert "kiosk-admin" not in ids
    feats_on = rp.ReleaseFeatures(kiosk_admin=True)
    plan_on = _plan(features=feats_on)
    kiosk = [s for s in plan_on["steps"] if s["id"] == "kiosk-admin"]
    assert kiosk and kiosk[0]["requiresKioskAuthorization"] is True
    assert any("KIOSK_AUTHORIZATION_REQUIRED" in b for b in plan_on["blockingActions"])


def test_records_actual_shas_never_assumes():
    plan = _plan(git_runner=lambda a: types.SimpleNamespace(stdout="cafef00d\n"))
    assert plan["requiredRepositories"]["calee-regression"] == "cafef00d"


# ── CLI ─────────────────────────────────────────────────────────────────────
def test_cli_qualification_plan_json():
    res = CliRunner().invoke(main, ["qualification-plan", "--format", "json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["report"] == "qualification-plan"
    assert payload["steps"]


def test_cli_qualification_plan_markdown_has_ordered_table():
    res = CliRunner().invoke(main, ["qualification-plan", "--format", "markdown"])
    assert res.exit_code == 0, res.output
    assert "Ordered steps" in res.output and "Blocking actions" in res.output
