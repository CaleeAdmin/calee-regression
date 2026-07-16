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

echo "Running smoke-tablet — only run this on a PREPARED tablet/emulator with a logged-in demo account."
bash scripts/run_smoke_tablet.sh
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo ""
    echo "PASSED: smoke-tablet"
else
    echo ""
    echo "FAILED: smoke-tablet — open the report (Open Latest Report.command) for details."
fi

read -p "Press Enter to close..."
exit $STATUS
