#!/bin/bash
set -e

# Get PUID and PGID from environment, default to 99:100 (Unraid)
PUID=${PUID:-99}
PGID=${PGID:-100}

# Check if updates are enabled (default: true)
AUTO_UPDATE=${AUTO_UPDATE:-true}

echo "Starting with PUID=${PUID} and PGID=${PGID}"

# Update GitHub CLI if enabled. Provider runtimes remain pinned to the image
# and are never rewritten by startup scripts.
if [ "$AUTO_UPDATE" = "true" ]; then
    echo "Checking for updates..."

    # Update GitHub CLI
    echo "Updating GitHub CLI..."
    apt-get update -qq && apt-get install -y --only-upgrade gh 2>/dev/null || echo "GitHub CLI update check failed (continuing anyway)"

    # Clean up apt cache
    rm -rf /var/lib/apt/lists/* 2>/dev/null || true

    echo "Update check complete"
    gh --version
fi

# Get current UID/GID of appuser
CURRENT_UID=$(id -u appuser)
CURRENT_GID=$(id -g appuser)

# Only modify user if PUID/PGID differs from current
if [ "$PUID" != "$CURRENT_UID" ] || [ "$PGID" != "$CURRENT_GID" ]; then
    echo "Adjusting appuser UID:GID from ${CURRENT_UID}:${CURRENT_GID} to ${PUID}:${PGID}"

    # Modify group first
    groupmod -o -g "$PGID" appuser

    # Modify user
    usermod -o -u "$PUID" appuser

    # Fix ownership of critical directories (skip node_modules to avoid slow recursive chown)
    echo "Updating ownership of /app and /home/appuser"
    chown -R appuser:appuser /home/appuser
    # Only chown app code and static assets, not the massive node_modules trees
    chown -R appuser:appuser /app/app /data /workspace
    chown appuser:appuser /app /app/.env.example 2>/dev/null || true
    find /opt/ai-tools -not -path "*/node_modules/*" -exec chown appuser:appuser {} + 2>/dev/null || true
else
    echo "UID/GID already correct, skipping user modification"
fi

# Ensure config directories exist with correct permissions
mkdir -p /home/appuser/.fluid /home/appuser/.config/claude /home/appuser/.claude /home/appuser/.config/gh
chown -R appuser:appuser /home/appuser/.fluid /home/appuser/.config /home/appuser/.claude

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

if [ -f /home/appuser/.claude/.credentials.json ]; then
    echo "Claude CLI credentials found in the retained provider volume"
else
    echo "Claude CLI credentials not found - connect Claude from the web UI if needed"
fi

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

# Ensure AI tools node_modules exist (in case Docker image was built without them)
if [ -d /opt/ai-tools ] && [ -f /opt/ai-tools/package.json ] && [ ! -d /opt/ai-tools/node_modules ]; then
    echo "Installing AI tools Node.js dependencies..."
    cd /opt/ai-tools && npm install --omit=dev 2>/dev/null || echo "AI tools npm install failed (continuing anyway)"
    cd /app
fi

# Ensure data and workspace directories are writable
chown -R appuser:appuser /data /workspace

# Final ownership fix for active Fluid settings and provider compatibility data.
chown -R appuser:appuser /home/appuser/.fluid /home/appuser/.config /home/appuser/.claude 2>/dev/null || true

# =============================================================================
# Browser-automation browser
# =============================================================================
# Launch the headless Chromium as appuser (the same user the app runs as) so the
# built-in browser MCP server (browser-mcp) has a browser to drive over CDP.
# Supervised so a crash auto-restarts.
# The browser profile lives in a NON-default dir (~/chrome-profile) so Chrome
# 136+ keeps honoring --remote-debugging-port (the loopback CDP surface the
# browser-mcp server attaches to).
mkdir -p /home/appuser/chrome-profile
chown -R appuser:appuser /home/appuser/chrome-profile 2>/dev/null || true
echo "Starting Chromium for browser automation (CHROME_CDP=${CHROME_CDP:-1}, port ${CHROME_CDP_PORT:-9221})..."
# The supervisor loop lives in /usr/local/bin/chrome-supervisor (see
# docker/chrome-supervisor.sh) — it captures the real Chrome exit status,
# applies the circuit breaker + exponential backoff, and (crucially) drops
# into slow-retry instead of permanently disabling Chrome, as defense in
# depth against crashes (the known deterministic first-run-suppression
# SIGTRAP is already fixed in start-chrome.sh). We launch it here
# backgrounded with the same log redirection and pass the same env defaults
# through; the supervisor itself does the setpriv drop to appuser.
CHROME_MAX_RESTARTS="${CHROME_MAX_RESTARTS:-8}" \
CHROME_HEALTHY_SECS="${CHROME_HEALTHY_SECS:-60}" \
CHROME_BACKOFF_MAX="${CHROME_BACKOFF_MAX:-300}" \
CHROME_SLOW_RETRY_SECS="${CHROME_SLOW_RETRY_SECS:-300}" \
CHROME_CDP="${CHROME_CDP:-1}" \
CHROME_CDP_PORT="${CHROME_CDP_PORT:-9221}" \
CHROME_DEBUG="${CHROME_DEBUG:-0}" \
CHROME_DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}" \
CHROME_EXECUTABLE="${CHROME_EXECUTABLE:-/usr/bin/google-chrome-stable}" \
    chrome-supervisor > /home/appuser/.config/chrome.log 2>&1 &
echo "Chrome supervisor started (logs: /home/appuser/.config/chrome.log). Diagnose with: chrome-doctor" 

# =============================================================================
# Start Application
# =============================================================================
echo ""
echo "=========================================="
echo "Starting AI Hub as appuser (${PUID}:${PGID})"
echo "=========================================="
exec setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser python -m app.main
