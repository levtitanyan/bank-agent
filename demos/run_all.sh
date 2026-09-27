#!/usr/bin/env bash
# Run every demo and report a tally.
#
# Offline by default, over trimmed copies of real ACBA pages. Pass --live to
# run the same five against acba.am. Each demo asserts its own claims and exits
# non-zero if one fails, so this script's exit code is the whole answer.
set -uo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="python3"

passed=0
failed=0
failures=()

for demo in demos/0*.py; do
    if "$PYTHON" "$demo" "$@"; then
        passed=$((passed + 1))
    else
        failed=$((failed + 1))
        failures+=("$demo")
    fi
done

echo
echo "=============================================================================="
if [ "$failed" -eq 0 ]; then
    echo "All $passed demos passed."
else
    echo "$passed passed, $failed FAILED:"
    printf '  %s\n' "${failures[@]}"
fi
echo "=============================================================================="
exit "$failed"
