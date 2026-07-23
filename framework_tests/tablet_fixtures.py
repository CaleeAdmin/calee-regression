"""Shared tablet-report fixture helpers for the framework tests.

After Workstream 7 a tablet report only certifies a release when it explicitly
declares the certifying envelope -- a supported ``reportType`` + a supported
``reportSchemaVersion`` plus an explicit standard certification block
(``deviceInitializationMode == 'standard'``, ``diagnosticMode == False``,
``certificationEligible == True``). A minimal/legacy tablet dict is diagnostic
only and never certifies, so any fixture that means to model a certifying tablet
run must carry this envelope. Centralized here so the envelope has ONE source of
truth across the tests.
"""

from __future__ import annotations

from calee_regression.models import DEVICE_INIT_STANDARD, certification_block
from calee_regression.reporting import TABLET_REPORT_SCHEMA_VERSION, TABLET_REPORT_TYPE

# The certifying envelope every real, standard-mode tablet report carries.
TABLET_CERTIFYING_ENVELOPE = {
    "reportType": TABLET_REPORT_TYPE,
    "reportSchemaVersion": TABLET_REPORT_SCHEMA_VERSION,
    **certification_block(DEVICE_INIT_STANDARD),
}


def certifying(report: dict) -> dict:
    """Return ``report`` with the certifying envelope merged in (report values
    win only for keys it explicitly sets)."""
    return {**TABLET_CERTIFYING_ENVELOPE, **report}
