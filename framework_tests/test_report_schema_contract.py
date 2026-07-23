"""Cross-report schema/version + backward-compatibility contract (Workstream
8/9).

Every report producer embeds an explicit integer schema version and a report
type, so a consumer never silently reinterprets an older report; the tablet,
targeted-repeat, and consolidation-consumed shapes are pinned here in one
place. Backward compatibility is deliberate: certification eligibility is never
INFERRED for an ambiguous or diagnostic tablet report -- it blocks.
"""

from __future__ import annotations

from pathlib import Path

from calee_regression import reporting, run_context, targeted_repeat
from calee_regression.config import Config
from calee_regression.consolidated_report import diagnostic_tablet_block_reason
from calee_regression.models import (
    DEVICE_INIT_SKIP,
    DEVICE_INIT_STANDARD,
    ScenarioResult,
    SuiteResult,
    certification_block,
)


def _config(tmp_path, **overrides):
    base = dict(
        appium_url="http://127.0.0.1:4723/wd/hub",
        device_name="Tablet",
        udid="emulator-5554",
        apk_path="/tmp/calee.apk",
        app_package="com.viso.calee",
        app_activity=".ui.HomeActivity",
        shell_package="com.viso.caleeshell",
        shell_activity=".ui.LauncherActivity",
        launch_strategy="direct_activity",
        start_action="com.viso.calee.action.START",
        report_dir=str(tmp_path / "reports"),
    )
    base.update(overrides)
    return Config(**base)


def _suite():
    return SuiteResult(
        name="smoke",
        scenarios=[ScenarioResult(name="s", file="scenarios/s.yaml", status="passed")],
        started_at="t0",
        finished_at="t1",
    )


# ── tablet report schema ───────────────────────────────────────────────────


def test_tablet_report_embeds_int_schema_version_and_type(tmp_path):
    rb = reporting.ReportBuilder(_config(tmp_path), run_name="smoke")
    payload = rb._results_payload(_suite())
    assert isinstance(payload["reportSchemaVersion"], int)
    assert payload["reportType"] == reporting.TABLET_REPORT_TYPE
    assert payload["deviceInitializationMode"] == DEVICE_INIT_STANDARD
    assert payload["certificationEligible"] is True
    assert payload["diagnosticMode"] is False


def test_tablet_diagnostic_report_schema(tmp_path):
    rb = reporting.ReportBuilder(
        _config(tmp_path, device_initialization_mode=DEVICE_INIT_SKIP), run_name="smoke"
    )
    payload = rb._results_payload(_suite())
    assert payload["certificationEligible"] is False
    assert payload["diagnosticMode"] is True


# ── targeted-repeat report schema ──────────────────────────────────────────


def test_targeted_report_embeds_schema_version_type_and_certification():
    report = targeted_repeat.build_targeted_report(
        [{"scenario": "s", "repetition": 1, "status": "pass"}],
        scenarios=["s"],
        repeat_count=1,
        stop_on_failure=False,
        device_initialization_mode=DEVICE_INIT_STANDARD,
    )
    assert isinstance(report["reportSchemaVersion"], int)
    assert report["reportType"] == targeted_repeat.TARGETED_REPORT_TYPE
    assert report["certificationEligible"] is True


# ── report paths never collide (run-manifest component references) ─────────


def test_targeted_and_full_suite_component_paths_never_collide(tmp_path):
    ws = run_context.RunWorkspace(tmp_path, "release-1")
    assert ws.component_dir("tablet") != ws.component_dir("tablet-targeted")
    # Both live under the run's own workspace, so provenance stays run-scoped.
    assert ws.is_within(ws.component_dir("tablet"))
    assert ws.is_within(ws.component_dir("tablet-targeted"))


# ── deliberate backward compatibility ──────────────────────────────────────


def test_legacy_report_without_certification_fields_is_allowed():
    # Pre-diagnostic reports (no fields) keep certifying -- the only mode then
    # was standard.
    assert diagnostic_tablet_block_reason({"passed_count": 1}) is None


def test_explicit_standard_certifies_and_diagnostic_blocks():
    assert diagnostic_tablet_block_reason(certification_block(DEVICE_INIT_STANDARD)) is None
    assert diagnostic_tablet_block_reason(certification_block(DEVICE_INIT_SKIP)) is not None


def test_ambiguous_partial_certification_metadata_blocks():
    # A report claiming eligibility WITHOUT declaring diagnosticMode is ambiguous;
    # eligibility must never be inferred -> it blocks.
    assert diagnostic_tablet_block_reason({"certificationEligible": True}) is not None
    assert diagnostic_tablet_block_reason({"diagnosticMode": False}) is not None
