"""Documentation-vs-executable-policy consistency for docs/SUITE_REFERENCE.md.

The suite reference is prose; the authoritative policy is code. These tests
make the executable policy the single source of truth and fail when the
document drifts from it:

  * the `sync-smoke` row's release-gating cell must agree with the executable
    feature scope (``release_features.synchronization`` is mandatory by default,
    so sync-smoke is release-gating -- the row may not still say "Not yet");
  * the `calendar_appearance` file list the doc quotes must match the actual
    ``calendar_appearance`` suite group in ``suites.py``;
  * the doc's "ten canonical suite profiles" claim must match the number of
    profile rows actually in the table.

No device / network.
"""

from __future__ import annotations

import re
from pathlib import Path

from calee_regression import release_platforms as rp
from calee_regression import suites as suites_mod

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = REPO_ROOT / "docs" / "SUITE_REFERENCE.md"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _table_rows(doc: str) -> "list[str]":
    """The profile rows of the leading suite table: markdown rows that are not
    the header or the |---| separator."""
    rows = []
    for line in doc.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if set(s) <= set("|-: "):  # separator row
            continue
        if s.startswith("| Profile "):  # header
            continue
        rows.append(s)
    return rows


def test_sync_smoke_row_gating_matches_executable_feature_scope():
    features = rp.load_release_features()
    rows = [r for r in _table_rows(_doc()) if r.startswith("| `sync-smoke`")]
    assert len(rows) == 1, "expected exactly one sync-smoke row in the suite table"
    last_cell = rows[0].rstrip().rstrip("|").rsplit("|", 1)[-1].strip().lower()
    if features.synchronization:
        assert "not yet" not in last_cell, (
            "release_features.synchronization defaults to mandatory (sync-smoke IS release-gating), "
            "but SUITE_REFERENCE.md still records its gating status as 'Not yet'."
        )
        assert last_cell.startswith("yes"), (
            "sync-smoke is release-gating per the executable feature scope; the doc row should say so."
        )


def test_calendar_appearance_file_list_matches_suites():
    group = suites_mod.SCENARIO_GROUPS["calendar_appearance"]
    expected_files = {Path(p).name for p in group}
    doc = _doc()
    # The calendar_appearance section quotes each file in backticks.
    section = doc.split("## Draft, non-canonical suite: `calendar_appearance`", 1)[-1]
    section = section.split("## ", 1)[0]
    quoted = set(re.findall(r"`(calendar_appearance_[a-z_]+\.yaml)`", section))
    assert quoted == expected_files, (
        "SUITE_REFERENCE.md's calendar_appearance file list drifted from suites.py "
        f"(doc={sorted(quoted)} vs suites={sorted(expected_files)})."
    )


def test_ten_canonical_profiles_claim_matches_table():
    doc = _doc()
    assert "ten canonical suite profiles" in doc
    rows = _table_rows(doc)
    assert len(rows) == 10, f"the suite table has {len(rows)} profile rows, but the doc claims ten"
