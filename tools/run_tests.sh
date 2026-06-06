#!/usr/bin/env bash
# tools/run_tests.sh — run v8-builder's Python unit tests.
#
# These tests are the regression net for the SEAL AUDITS (the allow-list that
# distinguishes a real V8 symbol from an ICU/zlib/Abseil leak) and the
# single-SHA release gate — the exact logic that has produced false-positives
# in the past (e.g. `u_` matching inside `cpu_`, `absl` matching a v8 method's
# template parameter, the iOS deny-list regression).
#
# Prefers pytest when available (richer output / CI integration); otherwise each
# test file runs standalone via its built-in runner (no third-party deps).
#
# Usage:
#   tools/run_tests.sh            # run everything
#   tools/run_tests.sh -v         # pass-through to pytest if present
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-python3}"

TESTS=(
  seal/test_macho_audit.py
  seal/test_elf_audit.py
  seal/test_coff_audit.py
  tools/test_check_single_sha.py
)

if "$PY" -c "import pytest" >/dev/null 2>&1; then
  echo "==> running unit tests via pytest"
  exec "$PY" -m pytest "${TESTS[@]}" "$@"
fi

echo "==> pytest not installed; running each test file standalone"
fail=0
for t in "${TESTS[@]}"; do
  echo "------------------------------------------------------------"
  if ! "$PY" "$t"; then
    fail=1
  fi
done

echo "------------------------------------------------------------"
if [ "$fail" -ne 0 ]; then
  echo "RESULT: some Python unit tests FAILED"
  exit 1
fi
echo "RESULT: all Python unit tests passed"
