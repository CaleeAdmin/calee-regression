#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR"

if [ ! -d reports ] || [ -z "$(ls -A reports 2>/dev/null)" ]; then
    echo "No reports found in $SCRIPT_DIR/reports yet. Run a suite first." >&2
    exit 1
fi

# Prefer the latest consolidated cross-repo release report, if one exists,
# since that's the most complete picture; otherwise fall back to the latest
# single-suite report this repo produced on its own.
LATEST_CONSOLIDATED="$(ls -1dt reports/consolidated-*/ 2>/dev/null | head -n1 || true)"
if [ -n "$LATEST_CONSOLIDATED" ] && [ -f "${LATEST_CONSOLIDATED}consolidated-report.html" ]; then
    SUMMARY="${LATEST_CONSOLIDATED}consolidated-report.html"
else
    LATEST="$(ls -1dt reports/*/ | head -n1)"
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
