#!/usr/bin/env bash
set -euo pipefail
# Run the BugZapper test suite (stdlib unittest, no install). Picks a python3
# with tkinter so the GUI tests run too; falls back to plain python3 (GUI tests
# then skip). Pass extra args straight through, e.g. ./run_tests.sh -v.

DIR="$(cd "$(dirname "$0")" && pwd)"

PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter" >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
PY="${PY:-python3}"

cd "$DIR"
exec "$PY" -m unittest discover -s tests -p 'test_*.py' "${@:--v}"
