"""Immutable same-run verified context for focused-verify (this session's
Workstream 3).

The API/tablet/iPhone child commands must be built ONLY after fixture
preparation has produced a verified backend and fixture identity for THIS
run. This module turns the same-run fixture-preparation report into a frozen
``FocusedVerifiedContext``; construction fails (``FocusedContextError``) when
any mandatory element is missing -- never a default, never a stray
pre-existing environment variable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType


class FocusedContextError(Exception):
    """Mandatory verified context could not be constructed -- the focused run
    must BLOCK before building or running any dependent child."""


@dataclass(frozen=True)
class FocusedVerifiedContext:
    """Everything a focused child is bound to, derived from same-run verified
    evidence. Frozen: once constructed it cannot be mutated, so every child
    command is provably built from the same identity."""

    run_id: str
    release_id: str
    backend: str
    fixture_version: str
    fixture_status: str
    regression_shas: "dict[str, str]" = field(default_factory=dict)
    product_build: "dict[str, object]" = field(default_factory=dict)
    tablet_device_id: "str | None" = None
    ios_device_id: "str | None" = None
    release_features: "dict[str, bool]" = field(default_factory=dict)

    def __post_init__(self):
        # Freeze the mapping fields too, so "immutable" is not just shallow.
        object.__setattr__(self, "regression_shas", MappingProxyType(dict(self.regression_shas)))
        object.__setattr__(self, "product_build", MappingProxyType(dict(self.product_build)))
        object.__setattr__(self, "release_features", MappingProxyType(dict(self.release_features)))

    def to_dict(self) -> dict:
        return {
            "runId": self.run_id,
            "releaseId": self.release_id,
            "verifiedBackend": self.backend,
            "fixtureVersion": self.fixture_version,
            "fixtureStatus": self.fixture_status,
            "regressionShas": dict(self.regression_shas),
            "productBuild": dict(self.product_build),
            "tabletDeviceId": self.tablet_device_id,
            "iosDeviceId": self.ios_device_id,
            "releaseFeatures": dict(self.release_features),
        }


def build_verified_context(
    fixture_report: dict,
    *,
    run_id: str,
    release_id: str,
    regression_shas: "dict[str, str] | None" = None,
    product_build: "dict[str, object] | None" = None,
    tablet_device_id: "str | None" = None,
    ios_device_id: "str | None" = None,
    release_features: "dict[str, bool] | None" = None,
) -> FocusedVerifiedContext:
    """Build the verified context from THIS run's fixture-preparation report.

    The backend and fixture identity come ONLY from the report; a report for
    another run, an unverified fixture, or a missing backend/fixture version
    raises ``FocusedContextError`` (BLOCK). No default is ever substituted.
    """
    if not isinstance(fixture_report, dict):
        raise FocusedContextError("fixture-preparation report is not a JSON object")
    report_run = fixture_report.get("runId")
    if report_run != run_id:
        raise FocusedContextError(
            f"fixture-preparation report belongs to run {report_run!r}, not this run {run_id!r}; "
            f"a stale fixture from another run can never seed a focused context"
        )
    fixture_status = fixture_report.get("fixtureVerificationStatus")
    if fixture_status != "ok":
        raise FocusedContextError(
            f"fixture verification status is {fixture_status!r}, not 'ok'; dependent steps must not "
            f"run against an unverified fixture"
        )
    backend = fixture_report.get("targetEnvironment")
    if not backend:
        raise FocusedContextError(
            "fixture-preparation report records no targetEnvironment backend; a focused run never "
            "falls back to a production default"
        )
    fixture_version = fixture_report.get("fixtureVersion")
    if not fixture_version:
        raise FocusedContextError(
            "fixture-preparation report records no fixtureVersion; children cannot be bound to an "
            "unidentified fixture"
        )
    return FocusedVerifiedContext(
        run_id=run_id,
        release_id=release_id,
        backend=str(backend),
        fixture_version=str(fixture_version),
        fixture_status=str(fixture_status),
        regression_shas=regression_shas or {},
        product_build=product_build or {},
        tablet_device_id=tablet_device_id,
        ios_device_id=ios_device_id,
        release_features=release_features or {},
    )
