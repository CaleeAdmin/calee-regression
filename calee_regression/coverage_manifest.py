"""Regression coverage manifest loader, validator, and report generator
(Phase 12).

``coverage/coverage-manifest.yaml`` is the single machine-readable statement
of what each area of the Calee solution has automated, has tested offline, has
verified on a real device, and whether it gates a release. This module:

  * loads and schema-validates it,
  * enforces internal-consistency invariants (a draft is never release-gating;
    an unsupported component is never release-gating; a component's declared
    scenario suite matches its release-gating status against suites.py), and
  * renders a plain-text human-readable coverage report.

``framework_tests/test_coverage_manifest.py`` runs the validator and the
cross-check against the real ``suites.py`` so documentation/suite membership
can never silently drift from the manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import suites

_AUTOMATED_VALUES = {"true", "partial", "draft", "false"}
_BOOL_FIELDS = ("offlineTested", "physicalVerified", "releaseGating", "blocked", "optional", "unsupported")
_DEFAULT_MANIFEST_PATH = suites.REPO_ROOT / "coverage" / "coverage-manifest.yaml"


class CoverageManifestError(Exception):
    pass


@dataclass
class Component:
    name: str
    automated: str
    offline_tested: bool = False
    physical_verified: bool = False
    release_gating: bool = False
    blocked: bool = False
    optional: bool = False
    unsupported: bool = False
    owning_repo: str = ""
    scenario_suite: "str | None" = None
    notes: str = ""

    def status_label(self) -> str:
        """A single plain-language status for the human report."""
        if self.physical_verified:
            return "VERIFIED (physical)"
        if self.unsupported:
            return "UNSUPPORTED"
        if self.optional:
            return "OPTIONAL"
        if self.automated == "draft":
            return "DRAFT (offline only)"
        if self.blocked:
            return "BLOCKED (offline ready)"
        if self.automated in ("true", "partial"):
            return "AUTOMATED (physical pending)"
        return "NOT AUTOMATED"


@dataclass
class CoverageManifest:
    components: "list[Component]" = field(default_factory=list)
    physical_session: bool = False
    path: "Path | None" = None

    def by_name(self, name: str) -> "Component | None":
        for c in self.components:
            if c.name == name:
                return c
        return None


def _parse_component(name: str, raw, errors: "list[str]") -> "Component | None":
    if not isinstance(raw, dict):
        errors.append(f"component {name!r} must be a mapping.")
        return None
    # YAML parses a bare `automated: true`/`false` as a Python bool; normalise
    # those to the string vocabulary so the manifest can read naturally.
    raw_automated = raw.get("automated")
    if isinstance(raw_automated, bool):
        automated = "true" if raw_automated else "false"
    else:
        automated = str(raw_automated)
    if automated not in _AUTOMATED_VALUES:
        errors.append(f"component {name!r}: automated must be one of {sorted(_AUTOMATED_VALUES)} (got {raw.get('automated')!r}).")
    for bf in _BOOL_FIELDS:
        if bf in raw and not isinstance(raw[bf], bool):
            errors.append(f"component {name!r}: {bf} must be a boolean.")
    return Component(
        name=name,
        automated=automated,
        offline_tested=bool(raw.get("offlineTested", False)),
        physical_verified=bool(raw.get("physicalVerified", False)),
        release_gating=bool(raw.get("releaseGating", False)),
        blocked=bool(raw.get("blocked", False)),
        optional=bool(raw.get("optional", False)),
        unsupported=bool(raw.get("unsupported", False)),
        owning_repo=str(raw.get("owningRepo", "")),
        scenario_suite=raw.get("scenarioSuite"),
        notes=str(raw.get("notes", "")),
    )


def validate_manifest(raw) -> "tuple[CoverageManifest, list[str]]":
    """Parse + validate. Returns (manifest, errors). Internal-consistency
    invariants are included in ``errors`` so a single validation pass surfaces
    both schema and logic problems."""
    errors: "list[str]" = []
    if not isinstance(raw, dict) or "components" not in raw:
        return CoverageManifest(), ["coverage manifest must have a top-level 'components' mapping."]

    comps_raw = raw.get("components")
    if not isinstance(comps_raw, dict) or not comps_raw:
        return CoverageManifest(), ["'components' must be a non-empty mapping."]

    components: "list[Component]" = []
    for name, craw in comps_raw.items():
        comp = _parse_component(str(name), craw, errors)
        if comp is not None:
            components.append(comp)

    manifest = CoverageManifest(
        components=components,
        physical_session=bool((raw.get("meta") or {}).get("physicalSession", False)),
    )

    # Internal-consistency invariants.
    for c in components:
        if c.automated == "draft" and c.release_gating:
            errors.append(f"component {c.name!r}: a draft component must not be releaseGating.")
        if c.unsupported and c.release_gating:
            errors.append(f"component {c.name!r}: an unsupported component must not be releaseGating.")
        if c.unsupported and c.automated == "true":
            errors.append(f"component {c.name!r}: an unsupported component cannot be automated: true.")
        if c.physical_verified and not manifest.physical_session:
            errors.append(
                f"component {c.name!r}: physicalVerified is true but meta.physicalSession is false -- "
                f"a component can only be physically verified in a real device session."
            )

    return manifest, errors


def cross_check_against_suites(manifest: CoverageManifest) -> "list[str]":
    """Cross-check every component that names a ``scenarioSuite`` against the
    real suite registry: a release-gating component's suite must resolve inside
    a composite (full-tester/release-technical); a draft component's suite must
    be OUTSIDE those composites. This is what stops a draft scenario being
    quietly slipped into a release suite while the manifest still calls it a
    draft (and vice versa)."""
    problems: "list[str]" = []
    try:
        full_tester = set(str(p) for p in suites.resolve_suite("full-tester"))
        release_technical = set(str(p) for p in suites.resolve_suite("release-technical"))
    except suites.SuiteError as exc:  # pragma: no cover - suites always resolve
        return [f"could not resolve composite suites: {exc}"]
    release_paths = full_tester | release_technical

    for c in manifest.components:
        if not c.scenario_suite:
            continue
        try:
            suite_paths = set(str(p) for p in suites.resolve_suite(c.scenario_suite))
        except suites.SuiteError:
            problems.append(f"component {c.name!r}: scenarioSuite {c.scenario_suite!r} is not a known suite.")
            continue
        in_release = bool(suite_paths & release_paths)
        if c.automated == "draft" and in_release:
            problems.append(
                f"component {c.name!r}: is 'draft' but its suite {c.scenario_suite!r} is inside a release "
                f"composite (full-tester/release-technical). A draft must not be release-gating."
            )
        if c.release_gating and not in_release:
            problems.append(
                f"component {c.name!r}: is releaseGating but its suite {c.scenario_suite!r} is not inside any "
                f"release composite. Either add the suite to a composite or mark the component non-gating."
            )
    return problems


def load_manifest(path: "Path | str | None" = None) -> CoverageManifest:
    path = Path(path) if path else _DEFAULT_MANIFEST_PATH
    if not path.is_file():
        raise CoverageManifestError(f"Coverage manifest not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CoverageManifestError(f"Coverage manifest at {path} is not valid YAML: {exc}") from exc
    manifest, errors = validate_manifest(raw)
    if errors:
        raise CoverageManifestError(
            f"Coverage manifest at {path} has {len(errors)} problem(s):\n" + "\n".join(f"  - {e}" for e in errors)
        )
    manifest.path = path.resolve()
    return manifest


def render_report(manifest: CoverageManifest) -> str:
    """A plain-text human-readable coverage report grouped by status."""
    lines: "list[str]" = []
    lines.append("Calee solution regression coverage")
    lines.append("=" * 34)
    lines.append("")
    lines.append(f"Physical device session: {'yes' if manifest.physical_session else 'NO (physical verification pending)'}")
    lines.append(f"Components: {len(manifest.components)}")
    lines.append("")

    gating = [c for c in manifest.components if c.release_gating]
    lines.append(f"Release-gating components ({len(gating)}):")
    for c in sorted(gating, key=lambda x: x.name):
        lines.append(f"  - {c.name}: {c.status_label()}  [{c.owning_repo}]")
    lines.append("")

    drafts = [c for c in manifest.components if c.automated == "draft"]
    lines.append(f"Draft (offline-only, not release-gating) ({len(drafts)}):")
    for c in sorted(drafts, key=lambda x: x.name):
        lines.append(f"  - {c.name}: {c.status_label()}")
    lines.append("")

    blocked = [c for c in manifest.components if c.blocked]
    lines.append(f"Currently blocked ({len(blocked)}):")
    for c in sorted(blocked, key=lambda x: x.name):
        lines.append(f"  - {c.name}: {c.notes}")
    lines.append("")

    verified = [c for c in manifest.components if c.physical_verified]
    lines.append(f"Physically verified ({len(verified)}):")
    if verified:
        for c in sorted(verified, key=lambda x: x.name):
            lines.append(f"  - {c.name}")
    else:
        lines.append("  (none -- pending a MacBook qualification session)")
    lines.append("")

    return "\n".join(lines)
