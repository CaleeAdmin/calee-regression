#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.." || exit 1

echo "=== Calee Regression — Focused Post-Fix Verification ==="
echo "Runs the permanent focused-verify command: fixture preparation, the"
echo "recurring-calendar tablet scenarios (standard + diagnostic), the focused"
echo "stop-repeating API scenario twice, and a focused iPhone environment check."
echo "This is a diagnostic check — it is NOT a release certification."
echo ""

# shellcheck source=../scripts/ensure_environment.sh
source scripts/ensure_environment.sh
BOOTSTRAP_STATUS=$?
if [ $BOOTSTRAP_STATUS -ne 0 ]; then
    read -p "Press Enter to close..."
    exit $BOOTSTRAP_STATUS
fi

# Credentials come from the environment or the macOS Keychain via the
# framework's credential-provider chain — this launcher NEVER prompts for a
# password and never puts one on a command line.
python -m calee_regression focused-verify --config "$CALEE_TEST_CONFIG"
STATUS=$?

echo ""
case $STATUS in
    0) echo "PASS: every focused check passed (still not a release certification)." ;;
    1) echo "FAILED: a product regression was verified — see the summary above." ;;
    2) echo "INVALID: the invocation/configuration is invalid — nothing was run." ;;
    3) echo "BLOCKED: an environment/tooling problem stopped one or more checks — see above." ;;
    *) echo "BLOCKED: focused verification could not finish — see the messages above." ;;
esac
echo "The run ID and final summary path are printed above."

read -p "Press Enter to close..."
exit $STATUS
