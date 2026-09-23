#!/usr/bin/env bash
# Regenerate requirements.lock, the hash-pinned closure the Docker image and
# the container updater install with `pip install --require-hashes`.
#
# requirements.txt stays the human-edited list of direct dependencies (older
# installed containers still build update sources from it, and the desktop
# build reads it). Run this after every change to requirements.txt,
# fluid/pyproject.toml or requirements-build.txt and commit both files;
# tests/core/test_requirements_lock.py fails when they drift apart.
#
# --universal writes environment markers instead of resolving for this machine,
# so the lock is valid for the Linux image and any other platform on Python 3.12.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv pip compile --universal --python-version 3.12 --generate-hashes \
  --custom-compile-command "scripts/lock_requirements.sh" \
  requirements.txt fluid/pyproject.toml requirements-build.txt \
  -o requirements.lock "$@"
