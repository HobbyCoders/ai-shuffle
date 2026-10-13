#!/usr/bin/env bash
# All platforms share the same setup, update and startup implementation.
set -e
cd -- "$(dirname -- "$0")"
if [ -x venv/bin/python ]; then
    exec venv/bin/python run_local.py "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 run_local.py "$@"
fi
echo "Python 3.12+ is required. Install it, then run ./run_local.sh --setup." >&2
exit 1
