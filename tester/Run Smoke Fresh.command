#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.."

if [ ! -f .venv/bin/activate ]; then
    echo "No .venv found. Run this first in Terminal:"
    echo "  python3.11 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'"
    read -p "Press Enter to close..."
    exit 1
fi

source .venv/bin/activate
export CALEE_TEST_CONFIG=config/tester.local.yaml

echo "Running smoke-fresh — use this on a CLEAN emulator/tablet with no account signed in."
bash scripts/run_smoke_fresh.sh
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo ""
    echo "PASSED: smoke-fresh"
else
    echo ""
    echo "FAILED: smoke-fresh — open the report (Open Latest Report.command) for details."
fi

read -p "Press Enter to close..."
exit $STATUS
