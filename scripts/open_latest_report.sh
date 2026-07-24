#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR"

# Resolve the same canonical report root every other component uses
# (Priority 3), so "Open Latest Report" always looks where a run actually
# wrote its evidence -- never silently falling back to this repo's own
# reports/ when a custom root is configured. Best-effort: this is a
# read-only convenience utility, not a release gate, so an unresolvable
# root falls back to this repo's own reports/ rather than blocking the
# tester from viewing anything.
# Hermetic interpreter (Workstream 1): resolve the repository-owned
# "$CALEE_PYTHON" rather than a bare python from PATH.
# shellcheck source=scripts/lib/hermetic_python.sh
. "$SCRIPT_DIR/scripts/lib/hermetic_python.sh"
_calee_resolve_python "$SCRIPT_DIR"
if [ -z "${CALEE_REPORT_ROOT:-}" ]; then
    if [ -n "${CALEE_PYTHON:-}" ]; then
        CALEE_REPORT_ROOT="$("$CALEE_PYTHON" -m calee_regression report-root 2>/dev/null || echo "$SCRIPT_DIR")"
    else
        CALEE_REPORT_ROOT="$SCRIPT_DIR"
    fi
fi
REPORTS_DIR="$CALEE_REPORT_ROOT/reports"

if [ ! -d "$REPORTS_DIR" ] || [ -z "$(ls -A "$REPORTS_DIR" 2>/dev/null)" ]; then
    echo "No reports found in $REPORTS_DIR yet. Run a suite first." >&2
    exit 1
fi

# reports/latest-run is a convenience symlink `consolidate` (re)creates
# only after a "06 Test Full Calee Solution" run actually finishes -- see
# calee_regression/run_context.py and docs/RELEASE_POLICY.md. It is never
# used to *decide* what belongs to a run (consolidation itself always
# validates every report's run ID against its own workspace); this is
# purely "which finished run should a human look at by default."
if [ -L "$REPORTS_DIR/latest-run" ] && [ -f "$REPORTS_DIR/latest-run/consolidated/consolidated-report.html" ]; then
    SUMMARY="$REPORTS_DIR/latest-run/consolidated/consolidated-report.html"
elif [ -d "$REPORTS_DIR/runs" ] && [ -n "$(ls -A "$REPORTS_DIR/runs" 2>/dev/null)" ]; then
    # No finished full-solution run yet, but at least one run workspace
    # exists (e.g. only "01 Prepare" has been run so far) -- report that
    # plainly rather than falling back to an unrelated single-suite report.
    echo "No finished 'Test Full Calee Solution' run yet ($REPORTS_DIR/latest-run isn't set)." >&2
    echo "Run '06 Test Full Calee Solution' first, or open a single-suite report directly." >&2
    exit 1
else
    # No run workspace at all -- this repo's older single-suite launchers
    # (e.g. "02 Test Calee Tablet" run on its own) still write their own
    # timestamped reports/<suite>-<timestamp>/ directory outside any run
    # workspace. Only reachable here since it's the last remaining case.
    LATEST="$(ls -1dt "$REPORTS_DIR"/*/ 2>/dev/null | head -n1 || true)"
    if [ -z "$LATEST" ]; then
        echo "No reports found in $REPORTS_DIR yet. Run a suite first." >&2
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
