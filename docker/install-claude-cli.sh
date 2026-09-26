#!/bin/bash
# Install the stock Claude Code CLI with Anthropic's installer when it is missing.
#
# Runs as appuser at container start. The installer puts the binary under
# ~/.local (on PATH via the Dockerfile) and the CLI keeps itself up to date from
# there. This script never pins a version and never patches, wraps or copies the
# binary.
#
# Idempotent: a `claude` already on PATH is left alone.
# Offline-tolerant: a failed install logs a warning and exits 0 so the container
# still starts; Claude sessions report the missing CLI until it is installed.
set -u

export PATH="${HOME}/.local/bin:${PATH}"

if command -v claude >/dev/null 2>&1; then
    echo "Claude Code CLI: found at $(command -v claude)"
    exit 0
fi

echo "Claude Code CLI: not found, installing with Anthropic's installer"
if timeout 600s bash -c 'set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash'; then
    if command -v claude >/dev/null 2>&1; then
        echo "Claude Code CLI: installed at $(command -v claude). Sign in by running 'claude' in the container."
        exit 0
    fi
    echo "WARNING: Claude Code CLI installer finished but 'claude' is not on PATH" >&2
else
    echo "WARNING: Claude Code CLI install failed (offline?). Claude sessions will not start until it is installed." >&2
fi
echo "WARNING: install it manually as appuser: curl -fsSL https://claude.ai/install.sh | bash" >&2
exit 0
