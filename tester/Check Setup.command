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

bash scripts/doctor.sh
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo ""
    echo "PASSED: setup looks good."
else
    echo ""
    echo "FAILED: setup has problems — see the [ERROR]/[WARN] lines above."
fi

read -p "Press Enter to close..."
exit $STATUS
