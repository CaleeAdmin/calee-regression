"""Run-scoped guided-handoff evidence integration (Workstream 5).

The AUTHORITATIVE guided-checkpoint recorder/verifier lives in
CaleeMobile-Regression (``ui/handoff_evidence.py`` -- schema + secret detection +
binding rules). This module never re-implements any of that: it locates the
sibling checkout, imports that module, and adds only the calee-regression-side
glue -- run-scoped canonical paths and a finalization step that binds a tester's
later guided-checkpoint evidence to THIS run's already-produced (immutable)
automated mobile results without re-running any device.

Canonical run-scoped evidence layout (same-run only -- never a "latest" file):

    reports/runs/<run-id>/handoff/onboarding/evidence.json
    reports/runs/<run-id>/handoff/google-calendar/evidence.json
    reports/runs/<run-id>/handoff/finalized/results.json   (produced here)
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

# feature name -> (canonical subdir, checkpoint-type attribute on handoff_evidence)
HANDOFF_FEATURES = {
    "onboarding": ("onboarding", "CHECKPOINT_ONBOARDING"),
    "google_calendar": ("google-calendar", "CHECKPOINT_GOOGLE_CALENDAR"),
}


class HandoffBridgeError(RuntimeError):
    """The sibling CaleeMobile-Regression handoff module could not be located or
    imported -- never guessed around, always surfaced (fail closed)."""


def sibling_ui_path(repo_root: "Path | None" = None) -> Path:
    """The CaleeMobile-Regression/ui directory whose handoff_evidence module we
    reuse. Overridable with CALEE_MOBILE_REGRESSION_PATH (pointing at the
    CaleeMobile-Regression checkout), else the sibling next to this repo."""
    override = os.environ.get("CALEE_MOBILE_REGRESSION_PATH")
    if override:
        return Path(override) / "ui"
    root = repo_root or Path(__file__).resolve().parent.parent
    return root.parent / "CaleeMobile-Regression" / "ui"


def load_handoff_evidence_module(repo_root: "Path | None" = None):
    """Import (and return) CaleeMobile-Regression's ``handoff_evidence`` module,
    the single source of truth for evidence schema + secret detection + binding.
    Raises HandoffBridgeError when it cannot be found."""
    ui_path = sibling_ui_path(repo_root)
    module_file = ui_path / "handoff_evidence.py"
    if not module_file.is_file():
        raise HandoffBridgeError(
            f"CaleeMobile-Regression handoff_evidence module not found at {module_file}. "
            "Set CALEE_MOBILE_REGRESSION_PATH or check out CaleeMobile-Regression next to calee-regression."
        )
    ui_str = str(ui_path)
    if ui_str not in sys.path:
        sys.path.insert(0, ui_str)
    try:
        return importlib.import_module("handoff_evidence")
    except Exception as exc:  # noqa: BLE001 -- surface an import problem, never guess around it
        raise HandoffBridgeError(f"could not import CaleeMobile-Regression handoff_evidence: {exc}") from exc


def run_handoff_dir(workspace) -> Path:
    return workspace.root / "handoff"


def feature_evidence_path(workspace, feature: str) -> Path:
    """The canonical run-scoped evidence path for a feature -- same-run only."""
    subdir = HANDOFF_FEATURES[feature][0]
    return run_handoff_dir(workspace) / subdir / "evidence.json"


def discover_same_run_evidence(workspace) -> dict:
    """The canonical same-run evidence paths that actually EXIST, per feature.
    Never selects a "latest" file -- only this run's own canonical paths."""
    found = {}
    for feature in HANDOFF_FEATURES:
        path = feature_evidence_path(workspace, feature)
        if path.is_file():
            found[feature] = path
    return found


def expected_binding_from_aggregate(he, aggregate: dict, *, checkpoint_type: str):
    """Build an ExpectedBinding from an AUTOMATED mobile aggregate report, so
    the tester's later checkpoint evidence is bound to the device/build/backend
    the automated run ACTUALLY exercised (not a re-supplied CLI value)."""
    build = aggregate.get("buildIdentity") or {}
    backend = aggregate.get("backend") or {}
    return he.ExpectedBinding(
        release_run_id=aggregate.get("releaseRunId"),
        release_id=aggregate.get("releaseId"),
        caleemobile_sha=build.get("gitSha"),
        caleemobile_version=build.get("buildVersion"),
        regression_sha=aggregate.get("producerGitSha"),
        platform=aggregate.get("platform"),
        device_id=aggregate.get("deviceId"),
        requested_backend=backend.get("requested"),
        fixture_version=aggregate.get("fixtureVersion"),
        checkpoint_type=checkpoint_type,
    )


def finalize_release_handoff(
    workspace,
    *,
    release_features: dict,
    aggregates: dict,
    repo_root: "Path | None" = None,
):
    """Finalize a release's guided-handoff evidence WITHOUT re-running devices.

    Reads the immutable automated mobile aggregate(s) (``aggregates``: {platform:
    aggregate_dict}), verifies each in-scope handoff feature's same-run evidence
    binds to that automated run, and produces a NEW immutable finalized
    aggregate. NEVER mutates the automated evidence. Returns
    (finalized_report, status).

    ``release_features`` is {feature: 'true'|'false'} -- a mandatory feature with
    NO same-run evidence BLOCKS; an optional (excluded) feature is an explicit
    optional skip.
    """
    he = load_handoff_evidence_module(repo_root)
    status_map = {he.RESULT_PASS: "pass", he.RESULT_FAIL: "fail", he.RESULT_BLOCKED: "blocked"}
    features_out = {}
    verdicts = []

    # The single automated run the checkpoints attach to: prefer whichever
    # platform aggregate is present (they must agree on identity -- the serial
    # aggregate already reconciled that per platform).
    primary_aggregate = next((a for a in aggregates.values() if isinstance(a, dict)), {})

    for feature, (_subdir, checkpoint_attr) in HANDOFF_FEATURES.items():
        mandatory = str(release_features.get(feature, "true")).lower() != "false"
        checkpoint_type = getattr(he, checkpoint_attr)
        path = feature_evidence_path(workspace, feature)
        if not path.is_file():
            if mandatory:
                features_out[feature] = {
                    "checkpointType": checkpoint_type, "mandatory": True,
                    "checkpointResult": "blocked",
                    "detail": "mandatory guided-checkpoint evidence is missing for this run "
                              "(record it under the run's canonical handoff path); never inferred.",
                }
                verdicts.append("blocked")
            else:
                features_out[feature] = {
                    "checkpointType": checkpoint_type, "mandatory": False,
                    "checkpointResult": "skip",
                    "detail": "feature not in release scope; explicit optional skip.",
                }
            continue
        try:
            evidence = he.load_evidence(path)
        except ValueError as exc:
            features_out[feature] = {
                "checkpointType": checkpoint_type, "mandatory": mandatory,
                "checkpointResult": "blocked",
                "detail": f"guided-checkpoint evidence could not be read: {exc}",
            }
            verdicts.append("blocked")
            continue
        expected = expected_binding_from_aggregate(he, primary_aggregate, checkpoint_type=checkpoint_type)
        problems = he.verify_evidence(evidence, base_dir=path.resolve().parent, expected=expected)
        if problems:
            features_out[feature] = {
                "checkpointType": checkpoint_type, "mandatory": mandatory,
                "checkpointResult": "blocked",
                "evidenceDigest": he.canonical_payload_digest(evidence),
                "detail": "guided-checkpoint evidence rejected (does not bind to this automated run): "
                          + "; ".join(problems),
            }
            verdicts.append("blocked")
            continue
        checkpoint_status = status_map.get(evidence.get("result"), "blocked")
        features_out[feature] = {
            "checkpointType": checkpoint_type, "mandatory": mandatory,
            "checkpointResult": checkpoint_status,
            "evidenceDigest": he.canonical_payload_digest(evidence),
            "checkpointDefinitionVersion": evidence.get("checkpointDefinitionVersion"),
            "detail": f"guided-checkpoint evidence is valid and bound; recorded result {evidence.get('result')}.",
        }
        verdicts.append(checkpoint_status)

    # Overall: FAIL dominates, then BLOCKED, else PASS.
    if any(v == "fail" for v in verdicts):
        status = "fail"
    elif any(v == "blocked" for v in verdicts):
        status = "blocked"
    else:
        status = "pass"

    finalized = {
        "reportSchemaVersion": 1,
        "reportType": "release-handoff-finalized",
        "producer": "calee_regression.handoff_bridge",
        "runId": workspace.run_id,
        "automatedAggregates": {p: {"status": (a or {}).get("status"), "deviceId": (a or {}).get("deviceId")}
                                for p, a in aggregates.items()},
        "features": features_out,
        "status": status,
    }
    # Write to a NEW immutable location; never touch the automated evidence.
    finalized_dir = run_handoff_dir(workspace) / "finalized"
    finalized_dir.mkdir(parents=True, exist_ok=True)
    out_path = finalized_dir / "results.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(finalized, f, indent=2)
        f.write("\n")
    return finalized, status
