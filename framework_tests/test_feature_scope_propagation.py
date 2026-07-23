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

import pytest
from click.testing import CliRunner

from calee_regression import cli
from calee_regression.cli import main
from calee_regression.consolidated_report import (
    STATUS_BLOCKED,
    STATUS_PASS,
    component_from_feature_scope_consistency,
    detect_feature_scope_mismatch,
)
from calee_regression.models import EXIT_BLOCKED


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


# ── fail-closed: an existing-but-invalid same-run report never falls back ───


def _write_raw_release_config(report_root, run_id, text):
    d = report_root / "reports" / "runs" / run_id / "release-config"
    d.mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(text, encoding="utf-8")


def test_malformed_same_run_report_blocks_not_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    _write_raw_release_config(tmp_path, "release-1", "{ this is not valid json ")
    with pytest.raises(cli.FeatureScopeBlocked):
        cli._resolve_run_release_features("release-1")


def test_unsupported_schema_version_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    _write_raw_release_config(
        tmp_path, "release-1",
        json.dumps({"schemaVersion": 3, "releaseSelections": {"enabledFeatures": ["meals"]}}),
    )
    with pytest.raises(cli.FeatureScopeBlocked):
        cli._resolve_run_release_features("release-1")


def test_non_object_report_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    _write_raw_release_config(tmp_path, "release-1", json.dumps([1, 2, 3]))
    with pytest.raises(cli.FeatureScopeBlocked):
        cli._resolve_run_release_features("release-1")


def test_tampered_digest_blocks(tmp_path, monkeypatch):
    from calee_regression import release_config
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    selections = {"enabledPlatforms": ["ios"], "enabledFeatures": ["meals"]}
    good_digest = release_config.release_selections_digest(selections)
    # Tamper: change the selections after the digest was computed.
    tampered = {"enabledPlatforms": ["ios"], "enabledFeatures": ["meals", "onboarding"]}
    _write_raw_release_config(
        tmp_path, "release-1",
        json.dumps({"schemaVersion": 2, "releaseSelections": tampered, "releaseConfigDigest": good_digest}),
    )
    with pytest.raises(cli.FeatureScopeBlocked):
        cli._resolve_run_release_features("release-1")


def test_matching_digest_is_accepted(tmp_path, monkeypatch):
    from calee_regression import release_config
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    selections = {"enabledPlatforms": ["ios"], "enabledFeatures": ["meals"]}
    digest = release_config.release_selections_digest(selections)
    _write_raw_release_config(
        tmp_path, "release-1",
        json.dumps({"schemaVersion": 2, "releaseSelections": selections, "releaseConfigDigest": digest}),
    )
    scope = cli._resolve_run_feature_scope("release-1")
    assert scope["features"].meals is True
    assert scope["digest"] == digest
    assert scope["schema"] == 2
    assert scope["featureMap"]["meals"] is True
    assert scope["featureMap"]["onboarding"] is False


def test_command_emits_full_scope_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    _write_release_config(tmp_path, "release-1", ["meals", "onboarding"])
    result = CliRunner().invoke(main, ["release-feature-scope", "--run-id", "release-1"])
    assert result.exit_code == 0, result.output
    out = result.output
    # Source, schema, digest, and ALL FIVE feature variables emitted together.
    assert "CALEE_RELEASE_FEATURE_SOURCE=" in out
    assert "CALEE_RELEASE_FEATURE_SCHEMA=" in out
    assert "CALEE_RELEASE_FEATURE_DIGEST=" in out
    for name in ("SYNCHRONIZATION", "MEALS", "ONBOARDING", "GOOGLE_CALENDAR", "KIOSK_ADMIN"):
        assert f"CALEE_RELEASE_FEATURE_{name}=" in out
    assert "CALEE_RELEASE_FEATURE_MEALS=true" in out
    assert "CALEE_RELEASE_FEATURE_GOOGLE_CALENDAR=false" in out


def test_command_blocks_and_exits_nonzero_on_malformed_report(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_resolved_report_root", lambda config_path=None: tmp_path)
    _write_raw_release_config(tmp_path, "release-1", "{ not json ")
    result = CliRunner().invoke(main, ["release-feature-scope", "--run-id", "release-1"])
    # Exit status is non-zero (captured BEFORE eval), AND the emitted line itself
    # exits non-zero when eval'd -- both fail closed.
    assert result.exit_code == EXIT_BLOCKED
    assert "exit" in result.output
    assert "blocked" in result.output.lower()


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
