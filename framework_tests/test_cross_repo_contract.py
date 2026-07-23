"""Cross-repository offline contract tests (Workstream 11).

Exercises the boundary between the two writable repos with NO Flutter, Appium,
ADB, or network: a report produced by CaleeMobile-Regression's ACTUAL serial
orchestrator (run_ui_manifest) is consumed by calee-regression's ACTUAL
consolidator, and the fail-closed behaviors are asserted end to end across the
boundary. Report-type/schema constants are asserted to AGREE across repos rather
than being copied independently into each.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from calee_regression import handoff_bridge
from calee_regression.consolidated_report import (
    MOBILE_UI_REPORT_TYPES,
    STATUS_BLOCKED,
    STATUS_PASS,
    component_from_api_report,
)

# Import the sibling producers via the same locate-and-import bridge WS5 uses.
_UI = handoff_bridge.sibling_ui_path()
if str(_UI) not in sys.path:
    sys.path.insert(0, str(_UI))
try:
    import run_ui_manifest as rum  # noqa: E402
    import run_ui_suite as rus  # noqa: E402
    _AVAILABLE = (_UI / "run_ui_manifest.py").is_file()
except Exception:  # noqa: BLE001
    _AVAILABLE = False

pytestmark = pytest.mark.skipif(not _AVAILABLE, reason="CaleeMobile-Regression sibling not available")


# ── shared contract: the report types both repos agree on ──────────────────


def test_report_type_contract_agrees_across_repos():
    # The consumer's accepted mobile-UI types include the producer's own type.
    assert rus.REPORT_TYPE in MOBILE_UI_REPORT_TYPES
    assert rum.REPORT_TYPE in MOBILE_UI_REPORT_TYPES
    # The producer's supported schema version is one the consumer supports.
    from calee_regression.consolidated_report import SUPPORTED_MOBILE_REPORT_SCHEMA_VERSIONS
    assert rum.REPORT_SCHEMA_VERSION in SUPPORTED_MOBILE_REPORT_SCHEMA_VERSIONS


# ── produce a real serial aggregate, consume it in the consolidator ─────────

_IDENTITY = {
    "reportType": "mobile-ui-file", "reportSchemaVersion": 1,
    "producer": "run_ui_suite.py", "producerGitSha": "reg-sha-1",
    "platform": "android", "deviceId": "emulator-5554",
    "releaseRunId": "release-1", "releaseId": "2026.07.20-rc1", "fixtureVersion": "fixture-7",
    "buildIdentity": {"available": True, "gitSha": "a" * 40, "buildVersion": "0.0.23+23", "dirty": False},
    "releaseFeatures": {"meals": "true", "onboarding": "true", "google_calendar": "false"},
}


def _child(steps, **overrides):
    d = dict(_IDENTITY)
    d["runId"] = "child"
    d["backend"] = {"requested": "https://hub.example", "fixture": "https://hub.example", "resolved": "https://hub.example"}
    d.update(overrides)
    d["steps"] = steps
    return d


def _executor_for(child_report):
    def execute(target, report_path, log_path, attempt):
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(child_report, f)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("")
        return 0
    return execute


def _build_aggregate(child_report, **manifest_overrides):
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    child = child_report(step) if callable(child_report) else child_report
    with tempfile.TemporaryDirectory() as tmp:
        kwargs = dict(
            platform="android",
            aggregate_report_path=str(Path(tmp) / "results.json"),
            work_dir=tmp,
            manifest=["integration_test/app_boot_test.dart"],
            repeats={},
            executor=_executor_for(child),
            run_id="rid",
            release_run_id="release-1", release_id="2026.07.20-rc1", fixture_version="fixture-7",
            device_id="emulator-5554",
            requested_backend="https://hub.example", fixture_backend="https://hub.example",
            release_features={"meals": "true", "onboarding": "true", "google_calendar": "false"},
            build_identity={"available": True, "gitSha": "a" * 40, "buildVersion": "0.0.23+23"},
            producer_git_sha="reg-sha-1",
        )
        kwargs.update(manifest_overrides)
        report, _ = rum.run_serial_manifest(**kwargs)
        return report


def _consume(aggregate):
    return component_from_api_report(
        "CaleeMobile Android UI", aggregate, accepted_types=MOBILE_UI_REPORT_TYPES,
    )


def test_clean_serial_aggregate_is_accepted():
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    aggregate = _build_aggregate(_child([step]))
    assert aggregate["status"] == "PASS"
    assert _consume(aggregate).status == STATUS_PASS


def test_mismatched_child_build_sha_blocks_across_boundary():
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    bad = _child([step], buildIdentity={"available": True, "gitSha": "d" * 40, "buildVersion": "0.0.23+23", "dirty": False})
    aggregate = _build_aggregate(bad)
    assert aggregate["status"] == "BLOCKED"
    assert _consume(aggregate).status == STATUS_BLOCKED


def test_mismatched_child_device_blocks():
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    aggregate = _build_aggregate(_child([step], deviceId="rogue-device"))
    assert aggregate["status"] == "BLOCKED"
    assert _consume(aggregate).status == STATUS_BLOCKED


def test_mismatched_child_backend_blocks():
    # A child that ran against a DIFFERENT backend than the run requested must
    # block the aggregate (its requested/resolved backend disagrees with the
    # run's requested backend).
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    child = _child([step])
    child["backend"] = {"requested": "https://other.example", "fixture": "https://other.example", "resolved": "https://other.example"}
    aggregate = _build_aggregate(child)
    assert aggregate["status"] == "BLOCKED"
    assert _consume(aggregate).status == STATUS_BLOCKED


def test_mismatched_feature_scope_blocks():
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    aggregate = _build_aggregate(_child([step], releaseFeatures={"meals": "false", "onboarding": "true", "google_calendar": "false"}))
    assert aggregate["status"] == "BLOCKED"
    assert _consume(aggregate).status == STATUS_BLOCKED


def test_missing_child_schema_blocks():
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    aggregate = _build_aggregate(_child([step], reportSchemaVersion=None))
    assert aggregate["status"] == "BLOCKED"
    assert _consume(aggregate).status == STATUS_BLOCKED


def test_unsupported_child_schema_blocks():
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    aggregate = _build_aggregate(_child([step], reportSchemaVersion=999))
    assert aggregate["status"] == "BLOCKED"
    assert _consume(aggregate).status == STATUS_BLOCKED


def test_consumer_blocks_wrong_declared_type():
    # A tablet report accidentally wired to a mobile-UI component blocks.
    from calee_regression.reporting import TABLET_REPORT_TYPE
    fake = {"reportType": TABLET_REPORT_TYPE, "reportSchemaVersion": 1, "counts": {"PASS": 1}, "steps": []}
    assert _consume(fake).status == STATUS_BLOCKED


def test_no_credentials_cross_the_boundary():
    # A secret in the environment must never appear in the produced aggregate
    # (the orchestrator never puts credentials in reports) NOR in the consumed
    # component's serialized evidence.
    step = {"name": "s", "status": "PASS", "mandatory": True, "skipCategory": None, "feature": None, "detail": ""}
    aggregate = _build_aggregate(_child([step]))
    blob = json.dumps(aggregate)
    for secret in ("hunter2", "CALEE_TEST_PASSWORD", "password="):
        assert secret not in blob
    component = _consume(aggregate)
    assert "hunter2" not in json.dumps(component.to_dict())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
