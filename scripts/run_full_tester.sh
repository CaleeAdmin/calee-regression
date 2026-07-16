#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$SCRIPT_DIR"

CONFIG="${CALEE_TEST_CONFIG:-config/tester.local.yaml}"

python -m calee_regression suite --config "$CONFIG" --suite full-tester
