#!/bin/bash
set -e

# PostgreSQL is not up yet. This atomic marker mirrors the worker setting.
# Until migration writes it, retain only an existing Claude user's install.
claude_cli_keep_installed() {
    local marker="${DATA_DIR:-/data}/claude-cli-keep-installed"
    if [ -f "$marker" ]; then
        [ "$(cat "$marker")" = "1" ]
    else
        command -v claude >/dev/null 2>&1 ||
            [ -n "$(find "${HOME}/.claude" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] ||
            [ -n "$(find "${HOME}/.config/claude" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]
    fi
}

# Direct --user launches cannot mutate accounts. The Python runtime validates
# identity and mount ownership and gives a clear error instead of chowning data.
if [ "$(id -u)" != "0" ]; then
    case "${1:-run}" in
        dump|restore) ;;
        *) if claude_cli_keep_installed; then bash /app/docker/install-claude-cli.sh || true; fi ;;
    esac
    exec python /app/docker/container_runtime.py "$@"
fi

# Get PUID and PGID from environment, default to 99:100 (Unraid)
PUID=${PUID:-99}
PGID=${PGID:-100}
case "$PUID:$PGID" in
    *[!0-9:]*|0:*|*:0|:*|*:) echo "PUID/PGID must be non-root numeric IDs" >&2; exit 1 ;;
esac

echo "Starting with PUID=${PUID} and PGID=${PGID}" >&2

# Get current UID/GID of appuser
CURRENT_UID=$(id -u appuser)
CURRENT_GID=$(id -g appuser)

# Only modify user if PUID/PGID differs from current
if [ "$PUID" != "$CURRENT_UID" ] || [ "$PGID" != "$CURRENT_GID" ]; then
    echo "Adjusting appuser UID:GID from ${CURRENT_UID}:${CURRENT_GID} to ${PUID}:${PGID}" >&2

    # Modify group first
    groupmod -o -g "$PGID" appuser

    # Modify user
    usermod -o -u "$PUID" appuser

    # Fix ownership of critical directories (skip node_modules to avoid slow recursive chown)
    echo "Updating ownership of /app and /home/appuser" >&2
    chown -R appuser:appuser /home/appuser
    # Only chown app code and static assets, not the massive node_modules trees
    chown -R appuser:appuser /app/app
    # Retained DATA_DIR/postgres is validated by the runtime, never chowned.
    chown appuser:appuser /app /app/.env.example 2>/dev/null || true
    find /opt/ai-tools -not -path "*/node_modules/*" -exec chown appuser:appuser {} + 2>/dev/null || true
else
    echo "UID/GID already correct, skipping user modification" >&2
fi

# Maintenance never starts provider setup, Chromium or application services.
case "${1:-run}" in
    dump|restore) exec python /app/docker/container_runtime.py "$@" ;;
esac

# Ensure config directories exist with correct permissions
mkdir -p /home/appuser/.fluid /home/appuser/.config/claude /home/appuser/.claude /home/appuser/.config/gh
chown -R appuser:appuser /home/appuser/.fluid /home/appuser/.config /home/appuser/.claude

# =============================================================================
# Claude Code CLI
# =============================================================================
# The delegated Claude leg runs the stock `claude` from PATH and nothing else.
# Download only while Keep Claude Code installed is on. The CLI then updates
# itself in ~/.local. Never pinned, patched or wrapped here.
mkdir -p /home/appuser/.local/bin
chown appuser:appuser /home/appuser/.local /home/appuser/.local/bin
if HOME=/home/appuser claude_cli_keep_installed; then
    setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
        bash /app/docker/install-claude-cli.sh || true
fi

# =============================================================================
# Provider configuration compatibility
# =============================================================================
# Existing provider files are import sources and delegated-CLI auth state. Never
# move, restore, create, or rewrite them at startup. Older AI Shuffle releases
# stored .claude.json inside the mounted ~/.claude volume. Route the CLI's root
# path into that volume; a missing target remains missing until the CLI writes it.
CLAUDE_JSON_REAL="/home/appuser/.claude/.claude.json"
CLAUDE_JSON_LINK="/home/appuser/.claude.json"
if [ ! -e "$CLAUDE_JSON_LINK" ] && [ ! -L "$CLAUDE_JSON_LINK" ]; then
    ln -s "$CLAUDE_JSON_REAL" "$CLAUDE_JSON_LINK"
    chown -h appuser:appuser "$CLAUDE_JSON_LINK"
    echo "Routed .claude.json into the retained provider volume without creating or rewriting provider data"
fi

echo "Claude: run \`claude auth login\` in the AI Shuffle terminal (or \`docker exec -it -u appuser <container> claude auth login\`). AI Shuffle never stores or refreshes your Claude credentials."

if [ -n "$(find /home/appuser/.claude -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    echo "Legacy Claude data detected and preserved; preview an explicit import from Settings > Providers"
fi

# =============================================================================
# GitHub CLI Credentials Management
# =============================================================================
# The volume mount for GitHub auth is at /home/appuser/.config/gh
# Credentials are stored after in-app login - no need to copy from root anymore

# Check for existing GitHub credentials in the volume
if [ -f /home/appuser/.config/gh/hosts.yml ]; then
    echo "GitHub CLI credentials found in volume"

    # Verify GitHub auth status and configure git credential helper for appuser
    if setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser gh auth status 2>/dev/null; then
        echo "GitHub CLI: Authentication verified"
        # Configure git to use gh as credential helper (as appuser so it goes to appuser's .gitconfig)
        setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
            git config --global credential.helper '!gh auth git-credential'
        echo "Git configured to use GitHub CLI for authentication"
    else
        echo "GitHub CLI: Credentials exist but may be expired - re-login via web UI recommended"
    fi
else
    echo "GitHub CLI: No credentials found - login via web UI required"
fi

# Legacy: Copy GitHub CLI auth from root if it exists (for backwards compatibility)
if [ -f /root/.config/gh/hosts.yml ] && [ ! -f /home/appuser/.config/gh/hosts.yml ]; then
    echo "Copying GitHub CLI auth from root to appuser (legacy migration)..."
    cp /root/.config/gh/hosts.yml /home/appuser/.config/gh/ 2>/dev/null || true
    cp -r /root/.config/gh/* /home/appuser/.config/gh/ 2>/dev/null || true
    chown -R appuser:appuser /home/appuser/.config/gh
    # Configure git credential helper as appuser
    setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
        git config --global credential.helper '!gh auth git-credential'
    echo "GitHub CLI auth copied and git configured"
fi

# =============================================================================
# Git identity
# =============================================================================
# A fresh checkout cannot commit without user.name/user.email, and the failure
# only surfaces at `git commit` — after the work is done. Seed appuser's global
# identity when it is unset; an identity already present in the mounted config
# is never overwritten. Override the defaults with GIT_USER_NAME /
# GIT_USER_EMAIL.
if ! setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
        git config --global --get user.name >/dev/null 2>&1; then
    setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
        git config --global user.name "${GIT_USER_NAME:-AI Shuffle Worker}"
    echo "Git: seeded user.name=${GIT_USER_NAME:-AI Shuffle Worker}"
fi
if ! setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
        git config --global --get user.email >/dev/null 2>&1; then
    setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser \
        git config --global user.email "${GIT_USER_EMAIL:-worker@ai-shuffle.local}"
    echo "Git: seeded user.email=${GIT_USER_EMAIL:-worker@ai-shuffle.local}"
fi

# Ensure AI tools node_modules exist (in case Docker image was built without them)
if [ -d /opt/ai-tools ] && [ -f /opt/ai-tools/package.json ] && [ ! -d /opt/ai-tools/node_modules ]; then
    echo "Installing AI tools Node.js dependencies..."
    cd /opt/ai-tools && npm install --omit=dev 2>/dev/null || echo "AI tools npm install failed (continuing anyway)"
    cd /app
fi

# Mount roots and retained database permissions are handled non-recursively
# by the Python runtime after resolving DATA_DIR through Settings.

# Final ownership fix for active Fluid settings and provider compatibility data.
chown -R appuser:appuser /home/appuser/.fluid /home/appuser/.config /home/appuser/.claude 2>/dev/null || true

# Python owns PostgreSQL, the existing Chromium supervisor, and the app. It
# drops root itself after safely preparing empty mount roots. tini must forward
# only to this supervisor (not -g), so application cleanup precedes PG shutdown.
exec python /app/docker/container_runtime.py "$@"
