#!/bin/bash
# Feidi regression test runner
#
# Runs the unittest-based regression suites against a real transfer.py
# subprocess. Pure stdlib (no pytest dependency).
#
# IMPORTANT: the test harness writes ~160 fake "TestBot" entries into
# feidi_identities.json (see CODE_REVIEW Q-03). To keep the real identity
# store clean, this runner backs up the file before the run and restores
# it on exit (success or failure). No manual cleanup needed.
#
# Usage: bash tests/run_tests.sh
# Exit: propagates the unittest exit code (0 = all passed).

set -u

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

PY="${PYTHON:-python3}"

ID_FILE="$ROOT/feidi_identities.json"
BAK=""
if [ -f "$ID_FILE" ]; then
  BAK="$(mktemp "${TMPDIR:-/tmp}/feidi_identities.XXXXXX.bak")"
  cp -f "$ID_FILE" "$BAK"
fi

cleanup() {
  if [ -n "$BAK" ] && [ -f "$BAK" ]; then
    cp -f "$BAK" "$ID_FILE"
    rm -f "$BAK"
  fi
}
trap cleanup EXIT

# Make sure transfer.py is importable from the repo root.
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

MODULES=(
  tests.test_c01_inflight_quota
  tests.test_c02_startup_recovery
  tests.test_c03_r04_validation_and_limits
  tests.test_s01_session_identity
)

echo "=== Feidi regression tests (unittest) ==="
echo "python: $($PY --version 2>&1)"
echo "identities backup: ${BAK:-<none, file absent>}"
echo ""

"$PY" -m unittest "${MODULES[@]}" -v
rc=$?
exit $rc
