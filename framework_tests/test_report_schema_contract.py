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
from calee_regression.consolidated_report import (
    MOBILE_API_REPORT_TYPES,
    MOBILE_UI_REPORT_TYPES,
    STATUS_BLOCKED,
    STATUS_PASS,
    component_from_api_report,
    diagnostic_tablet_block_reason,
)
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


# ── consumer validates mobile report type + schema (Workstream 1) ──────────


def _mobile_report(report_type=None, schema=1, status="PASS"):
    d = {"counts": {"PASS": 1}, "steps": [{"name": "s", "status": status, "mandatory": True}]}
    if report_type is not None:
        d["reportType"] = report_type
    if schema is not None:
        d["reportSchemaVersion"] = schema
    return d


def test_api_report_with_correct_type_is_consumed():
    report = _mobile_report(report_type="mobile-api-suite")
    c = component_from_api_report("CaleeMobile Client API", report, accepted_types=MOBILE_API_REPORT_TYPES)
    assert c.status == STATUS_PASS


def test_api_report_declaring_wrong_type_is_blocked():
    # A serial-aggregate report accidentally wired to the API component blocks.
    report = _mobile_report(report_type="mobile-serial-aggregate")
    c = component_from_api_report("CaleeMobile Client API", report, accepted_types=MOBILE_API_REPORT_TYPES)
    assert c.status == STATUS_BLOCKED
    assert "reportType" in " ".join(c.detail)


def test_ui_report_with_unsupported_schema_is_blocked():
    report = _mobile_report(report_type="mobile-serial-aggregate", schema=999)
    c = component_from_api_report("CaleeMobile Android UI", report, accepted_types=MOBILE_UI_REPORT_TYPES)
    assert c.status == STATUS_BLOCKED
    assert "reportSchemaVersion" in " ".join(c.detail)


def test_ui_report_serial_aggregate_type_is_accepted():
    report = _mobile_report(report_type="mobile-serial-aggregate")
    c = component_from_api_report("CaleeMobile Android UI", report, accepted_types=MOBILE_UI_REPORT_TYPES)
    assert c.status == STATUS_PASS


def test_legacy_unversioned_report_has_no_type_to_mismatch():
    # A legacy report with no reportType is not itself a *type mismatch*; the
    # producers now always version their reports, so this path is only for old
    # fixtures. It is consumed by count (no envelope to reject).
    report = _mobile_report(report_type=None, schema=None)
    c = component_from_api_report("CaleeMobile Client API", report, accepted_types=MOBILE_API_REPORT_TYPES)
    assert c.status == STATUS_PASS


# ── report paths never collide (run-manifest component references) ─────────


def test_targeted_and_full_suite_component_paths_never_collide(tmp_path):
    ws = run_context.RunWorkspace(tmp_path, "release-1")
    assert ws.component_dir("tablet") != ws.component_dir("tablet-targeted")
    # Both live under the run's own workspace, so provenance stays run-scoped.
    assert ws.is_within(ws.component_dir("tablet"))
    assert ws.is_within(ws.component_dir("tablet-targeted"))


# ── fail-closed certification (Workstream 7) ───────────────────────────────


def _certifying(**extra):
    """A fully certifying tablet report envelope: supported reportType + schema
    + explicit standard certification block. ``**extra`` overrides a field to
    model a defect."""
    return {
        "reportType": reporting.TABLET_REPORT_TYPE,
        "reportSchemaVersion": reporting.TABLET_REPORT_SCHEMA_VERSION,
        **certification_block(DEVICE_INIT_STANDARD),
        **extra,
    }


def test_legacy_report_without_certification_fields_is_blocked():
    # Reversed (Workstream 7): a legacy/unversioned tablet report (no reportType,
    # no schema, no certification fields) is diagnostic-only historical evidence
    # and MUST NOT certify -- it fails closed.
    reason = diagnostic_tablet_block_reason({"passed_count": 1})
    assert reason is not None
    assert "unversioned" in reason.lower() or "legacy" in reason.lower()


def test_explicit_standard_certifies_and_diagnostic_blocks():
    assert diagnostic_tablet_block_reason(_certifying()) is None
    # Envelope present but diagnostic (skip) -> blocked.
    assert diagnostic_tablet_block_reason(
        _certifying(**certification_block(DEVICE_INIT_SKIP))
    ) is not None


def test_bare_certification_block_without_envelope_does_not_certify():
    # A certification_block dict alone (no reportType/schema envelope) is NOT a
    # certifying report -- the envelope is required.
    assert diagnostic_tablet_block_reason(certification_block(DEVICE_INIT_STANDARD)) is not None


def test_unsupported_schema_version_blocks():
    assert diagnostic_tablet_block_reason(_certifying(reportSchemaVersion=999)) is not None
    assert diagnostic_tablet_block_reason(_certifying(reportSchemaVersion=None)) is not None


def test_wrong_report_type_blocks():
    assert diagnostic_tablet_block_reason(_certifying(reportType="tablet-targeted-repeat")) is not None


def test_skip_device_initialization_capability_blocks_even_in_standard_metadata():
    # Standard cert metadata but a skipDeviceInitialization capability present ->
    # inconsistent; blocks.
    assert diagnostic_tablet_block_reason(_certifying(skipDeviceInitialization=True)) is not None
    assert diagnostic_tablet_block_reason(
        _certifying(capabilities={"appium:skipDeviceInitialization": True})
    ) is not None


def test_ambiguous_partial_certification_metadata_blocks():
    # Even WITH a valid envelope, claiming eligibility WITHOUT declaring
    # diagnosticMode (or vice versa) is ambiguous; eligibility is never inferred.
    assert diagnostic_tablet_block_reason(
        {"reportType": reporting.TABLET_REPORT_TYPE,
         "reportSchemaVersion": reporting.TABLET_REPORT_SCHEMA_VERSION,
         "certificationEligible": True}
    ) is not None
    assert diagnostic_tablet_block_reason(
        {"reportType": reporting.TABLET_REPORT_TYPE,
         "reportSchemaVersion": reporting.TABLET_REPORT_SCHEMA_VERSION,
         "diagnosticMode": False}
    ) is not None
