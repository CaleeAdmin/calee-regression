"""Release-feature scope propagation (Workstream 5).

Covers:
  * the same-run resolver that prefers THIS run's schema-v2 release-config
    feature scope over the legacy config/release-platforms.yaml (the KNOWN GAP
    fix), falling back to legacy only when no schema-v2 bundle exists;
  * the consolidator detecting a mismatch between the release configuration's
    feature scope and the scope the mobile report was actually run with.
"""

from __future__ import annotations

import json

from calee_regression import cli
from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_PASS,
    component_from_feature_scope_consistency,
    detect_feature_scope_mismatch,
)


# ── same-run feature-scope resolver ────────────────────────────────────────


def _write_release_config(report_root, run_id, enabled_features):
    d = report_root / "reports" / "runs" / run_id / "release-config"
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "releaseSelections": {
                    "enabledPlatforms": ["ios"],
                    "enabledFeatures": enabled_features,
                },
            }
        ),
        encoding="utf-8",
    )


def test_resolver_prefers_schema_v2_release_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    _write_release_config(tmp_path, "release-1", ["meals", "onboarding"])
    features, source = cli._resolve_run_release_features("release-1")
    assert features.meals is True
    assert features.onboarding is True
    # A feature NOT in enabledFeatures is not in scope for a schema-v2 release.
    assert features.google_calendar is False
    assert features.kiosk_admin is False
    assert "schema-v2" in source


def test_resolver_falls_back_to_legacy_without_schema_v2(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    # No release-config result for this run -> legacy fallback (absent
    # config/release-platforms.yaml -> every feature mandatory=True).
    features, source = cli._resolve_run_release_features("release-nonexistent")
    assert features.meals is True
    assert features.onboarding is True
    assert features.google_calendar is True
    assert features.kiosk_admin is True
    assert "legacy" in source


# ── consolidation mismatch detection ───────────────────────────────────────


def _mobile_report(platform, release_features):
    return {"platform": platform, "releaseFeatures": release_features, "steps": [], "counts": {}}


def test_no_mismatch_when_scopes_agree():
    profile = {"meals": True, "onboarding": True, "google_calendar": False}
    report = _mobile_report("ios", {"meals": "true", "onboarding": "true", "google_calendar": "false"})
    assert detect_feature_scope_mismatch(profile, [report]) == []


def test_mismatch_detected_when_scope_differs():
    profile = {"meals": True, "onboarding": True, "google_calendar": False}
    # The report was run with meals=false though the release marks it mandatory.
    report = _mobile_report("ios", {"meals": "false", "onboarding": "true", "google_calendar": "false"})
    mismatches = detect_feature_scope_mismatch(profile, [report])
    assert len(mismatches) == 1
    assert "meals" in mismatches[0]


def test_only_common_features_compared():
    profile = {"meals": True, "kiosk_admin": True}  # kiosk not in a mobile report
    report = _mobile_report("android", {"meals": "true"})
    assert detect_feature_scope_mismatch(profile, [report]) == []


def test_consistency_component_blocks_on_mismatch():
    profile = {"meals": True}
    report = _mobile_report("ios", {"meals": "false"})
    component = component_from_feature_scope_consistency(profile, [report])
    assert component.status == STATUS_BLOCKED
    assert component.mandatory is True


def test_consistency_component_passes_when_consistent():
    profile = {"meals": True}
    report = _mobile_report("ios", {"meals": "true"})
    assert component_from_feature_scope_consistency(profile, [report]).status == STATUS_PASS


def test_consistency_component_passes_with_nothing_to_check():
    # No mobile report carries releaseFeatures -> nothing to cross-check, never
    # a manufactured block.
    assert component_from_feature_scope_consistency({"meals": True}, [None]).status == STATUS_PASS
    assert component_from_feature_scope_consistency(None, [_mobile_report("ios", {"meals": "true"})]).status == STATUS_PASS
