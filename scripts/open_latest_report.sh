#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR"

if [ ! -d reports ] || [ -z "$(ls -A reports 2>/dev/null)" ]; then
    echo "No reports found in $SCRIPT_DIR/reports yet. Run a suite first." >&2
    exit 1
fi

LATEST="$(ls -1dt reports/*/ | head -n1)"
SUMMARY="${LATEST}summary.html"

if [ ! -f "$SUMMARY" ]; then
    echo "Latest report directory $LATEST has no summary.html." >&2
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
