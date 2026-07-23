"""Run-scoped guided-handoff finalization (Workstream 5), fully offline.

Proves the finalization step reuses CaleeMobile-Regression's authoritative
verifier to bind a tester's later guided-checkpoint evidence to THIS run's
already-produced automated mobile aggregate -- without re-running any device --
and fails closed on a mismatched or missing binding, never mutating the
automated evidence.
"""

from __future__ import annotations

import json

import pytest

from calee_regression import handoff_bridge, run_context

# Reuse the sibling module (skip cleanly if the checkout isn't present).
try:
    he = handoff_bridge.load_handoff_evidence_module()
except handoff_bridge.HandoffBridgeError:
    he = None

pytestmark = pytest.mark.skipif(he is None, reason="CaleeMobile-Regression sibling not available")

SHA_MOBILE = "a" * 40
SHA_REG = "c" * 40


def _aggregate(**overrides):
    d = {
        "reportType": "mobile-serial-aggregate", "reportSchemaVersion": 1,
        "status": "PASS",
        "releaseRunId": "release-1", "releaseId": "2026.07.20-rc1",
        "platform": "android", "deviceId": "emulator-5554",
        "producerGitSha": SHA_REG, "fixtureVersion": "fixture-7",
        "backend": {"requested": "https://hub-dev.calee.example", "resolved": "https://hub-dev.calee.example"},
        "buildIdentity": {"available": True, "gitSha": SHA_MOBILE, "buildVersion": "0.0.23+23", "dirty": False},
    }
    d.update(overrides)
    return d


def _write_evidence(path, checkpoint, *, result="PASS", **overrides):
    path.parent.mkdir(parents=True, exist_ok=True)
    attachment = path.parent / "screen.png"
    attachment.write_bytes(b"png-bytes")
    definition = he.CHECKPOINT_DEFINITIONS[checkpoint]
    steps = [name for name, _desc in definition["steps"]]
    evidence = {
        "schemaVersion": he.SCHEMA_VERSION,
        "checkpointType": checkpoint,
        "checkpointDefinitionVersion": definition["version"],
        "releaseRunId": "release-1", "releaseId": "2026.07.20-rc1",
        "correlationId": "corr-1",
        "caleemobileSha": SHA_MOBILE, "caleemobileVersion": "0.0.23+23",
        "regressionSha": SHA_REG,
        "platform": "android", "deviceId": "emulator-5554",
        "deviceModel": "Pixel", "osVersion": "Android 15",
        "requestedBackend": "https://hub-dev.calee.example",
        "resolvedBackend": "https://hub-dev.calee.example",
        "fixtureBackend": "https://hub-dev.calee.example",
        "fixtureVersion": "fixture-7",
        "testAccountId": "regression+disposable-001@calee.example",
        "tester": "Y. Lee",
        "startedAt": "2026-07-22T01:00:00+00:00",
        "completedAt": "2026-07-22T01:10:00+00:00",
        "result": result,
        "completedSteps": list(steps),
        "missingSteps": [],
        "observations": ["checkpoint confirmed"],
        "attachments": [{"path": "screen.png", "sha256": he.sha256_file(attachment)}],
        "recorderVersion": he.RECORDER_VERSION,
    }
    evidence.update(overrides)
    evidence = he.attach_canonical_digest(evidence)
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return evidence


def _workspace(tmp_path, run_id="release-1"):
    ws = run_context.RunWorkspace(tmp_path, run_id)
    ws.ensure_created()
    return ws


def test_finalization_binds_to_automated_run_and_passes(tmp_path):
    ws = _workspace(tmp_path)
    _write_evidence(handoff_bridge.feature_evidence_path(ws, "onboarding"), he.CHECKPOINT_ONBOARDING)
    _write_evidence(handoff_bridge.feature_evidence_path(ws, "google_calendar"), he.CHECKPOINT_GOOGLE_CALENDAR)
    report, status = handoff_bridge.finalize_release_handoff(
        ws, release_features={"onboarding": "true", "google_calendar": "true"},
        aggregates={"android": _aggregate()},
    )
    assert status == "pass"
    assert report["features"]["onboarding"]["checkpointResult"] == "pass"
    assert report["features"]["google_calendar"]["checkpointResult"] == "pass"
    # A NEW immutable finalized aggregate was written; the automated evidence is
    # untouched (we never wrote to a mobile-* component here).
    assert (ws.root / "handoff" / "finalized" / "results.json").is_file()


def test_evidence_from_another_device_blocks(tmp_path):
    ws = _workspace(tmp_path)
    # Evidence recorded on a DIFFERENT device than the automated run used.
    _write_evidence(
        handoff_bridge.feature_evidence_path(ws, "onboarding"), he.CHECKPOINT_ONBOARDING,
        deviceId="some-other-device",
    )
    report, status = handoff_bridge.finalize_release_handoff(
        ws, release_features={"onboarding": "true", "google_calendar": "false"},
        aggregates={"android": _aggregate()},
    )
    assert status == "blocked"
    assert report["features"]["onboarding"]["checkpointResult"] == "blocked"
    assert "device" in report["features"]["onboarding"]["detail"].lower()


def test_evidence_from_another_build_blocks(tmp_path):
    ws = _workspace(tmp_path)
    _write_evidence(
        handoff_bridge.feature_evidence_path(ws, "onboarding"), he.CHECKPOINT_ONBOARDING,
        caleemobileSha="b" * 40,
    )
    _, status = handoff_bridge.finalize_release_handoff(
        ws, release_features={"onboarding": "true", "google_calendar": "false"},
        aggregates={"android": _aggregate()},
    )
    assert status == "blocked"


def test_missing_mandatory_evidence_blocks(tmp_path):
    ws = _workspace(tmp_path)
    # onboarding mandatory but no evidence recorded -> blocked.
    report, status = handoff_bridge.finalize_release_handoff(
        ws, release_features={"onboarding": "true", "google_calendar": "false"},
        aggregates={"android": _aggregate()},
    )
    assert status == "blocked"
    assert report["features"]["onboarding"]["checkpointResult"] == "blocked"
    # google_calendar excluded -> explicit optional skip, not a block.
    assert report["features"]["google_calendar"]["checkpointResult"] == "skip"


def test_credential_in_evidence_is_rejected(tmp_path):
    ws = _workspace(tmp_path)
    # A secret smuggled into observations must be rejected by the reused
    # secret detector -- never certifies.
    _write_evidence(
        handoff_bridge.feature_evidence_path(ws, "onboarding"), he.CHECKPOINT_ONBOARDING,
        observations=["password=hunter2 was entered"],
    )
    _, status = handoff_bridge.finalize_release_handoff(
        ws, release_features={"onboarding": "true", "google_calendar": "false"},
        aggregates={"android": _aggregate()},
    )
    assert status == "blocked"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
