#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR"

if [ ! -d reports ] || [ -z "$(ls -A reports 2>/dev/null)" ]; then
    echo "No reports found in $SCRIPT_DIR/reports yet. Run a suite first." >&2
    exit 1
fi

# reports/latest-run is a convenience symlink `consolidate` (re)creates
# only after a "06 Test Full Calee Solution" run actually finishes -- see
# calee_regression/run_context.py and docs/RELEASE_POLICY.md. It is never
# used to *decide* what belongs to a run (consolidation itself always
# validates every report's run ID against its own workspace); this is
# purely "which finished run should a human look at by default."
if [ -L reports/latest-run ] && [ -f reports/latest-run/consolidated/consolidated-report.html ]; then
    SUMMARY="reports/latest-run/consolidated/consolidated-report.html"
elif [ -d reports/runs ] && [ -n "$(ls -A reports/runs 2>/dev/null)" ]; then
    # No finished full-solution run yet, but at least one run workspace
    # exists (e.g. only "01 Prepare" has been run so far) -- report that
    # plainly rather than falling back to an unrelated single-suite report.
    echo "No finished 'Test Full Calee Solution' run yet (reports/latest-run isn't set)." >&2
    echo "Run '06 Test Full Calee Solution' first, or open a single-suite report directly." >&2
    exit 1
else
    # No run workspace at all -- this repo's older single-suite launchers
    # (e.g. "02 Test Calee Tablet" run on its own) still write their own
    # timestamped reports/<suite>-<timestamp>/ directory outside any run
    # workspace. Only reachable here since it's the last remaining case.
    LATEST="$(ls -1dt reports/*/ 2>/dev/null | head -n1 || true)"
    if [ -z "$LATEST" ]; then
        echo "No reports found in $SCRIPT_DIR/reports yet. Run a suite first." >&2
        exit 1
    fi
    SUMMARY="${LATEST}summary.html"
fi

if [ ! -f "$SUMMARY" ]; then
    echo "Latest report directory has no summary.html." >&2
    exit 1
fi

echo "Latest report: $SUMMARY"

if command -v open >/dev/null 2>&1; then
    open "$SUMMARY"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$SUMMARY"
else
    echo "Could not find 'open' or 'xdg-open'. Open this file manually: $SUMMARY"
fi
