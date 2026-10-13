#!/usr/bin/env bash
# Regenerate requirements.lock, the hash-pinned closure the Docker image and
# the container updater install with `pip install --require-hashes`, and
# requirements-native.lock, the hash-pinned closure of requirements-native.txt
# that the desktop build installs on top of it.
#
# requirements.txt stays the human-edited list of direct dependencies (older
# installed containers still build update sources from it). Run this after
# every change to requirements.txt, requirements-native.txt,
# fluid/pyproject.toml or requirements-build.txt and commit the lock files;
# tests/core/test_requirements_lock.py fails when they drift apart.
#
# --universal writes environment markers instead of resolving for this machine,
# so the lock is valid for the Linux image and any other platform on Python 3.12.
# The native lock is constrained by requirements.lock, so a package both need
# is pinned to the same version and hashes and the two installs never conflict.
set -euo pipefail
cd "$(dirname "$0")/.."
compile() {
  uv pip compile --universal --python-version 3.12 --generate-hashes \
    --custom-compile-command "scripts/lock_requirements.sh" "$@"
}
compile requirements.txt fluid/pyproject.toml requirements-build.txt \
  -o requirements.lock "$@"
compile requirements-native.txt -c requirements.lock \
  --find-links desktop/vendor --only-binary pynput --only-binary evdev-binary \
  -o requirements-native.lock "$@"
