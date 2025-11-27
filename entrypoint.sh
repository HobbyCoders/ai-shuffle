#!/bin/bash
set -e

# Get PUID and PGID from environment, default to 99:100 (Unraid)
PUID=${PUID:-99}
PGID=${PGID:-100}

# Check if updates are enabled (default: true)
AUTO_UPDATE=${AUTO_UPDATE:-true}

echo "Starting with PUID=${PUID} and PGID=${PGID}"

# Update Claude Code and GitHub CLI if enabled
if [ "$AUTO_UPDATE" = "true" ]; then
    echo "Checking for updates..."

    # Update Claude Code
    echo "Updating Claude Code..."
    npm update -g @anthropic-ai/claude-code 2>/dev/null || echo "Claude Code update check failed (continuing anyway)"

    # Update GitHub CLI
    echo "Updating GitHub CLI..."
    apt-get update -qq && apt-get install -y --only-upgrade gh 2>/dev/null || echo "GitHub CLI update check failed (continuing anyway)"

    # Clean up apt cache
    rm -rf /var/lib/apt/lists/* 2>/dev/null || true

    echo "Update check complete"
    claude --version
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

    # Fix ownership of critical directories
    echo "Updating ownership of /app and /home/appuser"
    chown -R appuser:appuser /app /home/appuser
else
    echo "UID/GID already correct, skipping user modification"
fi

# Ensure config directories exist with correct permissions
mkdir -p /home/appuser/.config/claude /home/appuser/.claude /home/appuser/.config/gh
chown -R appuser:appuser /home/appuser/.config /home/appuser/.claude

# Copy Claude credentials from root if they exist and appuser doesn't have them
if [ -f /root/.claude/.credentials.json ] && [ ! -f /home/appuser/.claude/.credentials.json ]; then
    echo "Copying Claude credentials from root to appuser..."
    # Copy hidden files explicitly, then everything else
    cp /root/.claude/.credentials.json /home/appuser/.claude/ 2>/dev/null || true
    cp -r /root/.claude/* /home/appuser/.claude/ 2>/dev/null || true
    chown -R appuser:appuser /home/appuser/.claude
fi

# Also check .config/claude location
if [ -f /root/.config/claude/credentials.json ] && [ ! -f /home/appuser/.config/claude/credentials.json ]; then
    echo "Copying Claude config from root to appuser..."
    mkdir -p /home/appuser/.config/claude
    cp -r /root/.config/claude/* /home/appuser/.config/claude/ 2>/dev/null || true
    chown -R appuser:appuser /home/appuser/.config
fi

# Copy GitHub CLI auth from root if it exists and appuser doesn't have it
# gh stores auth in ~/.config/gh/hosts.yml
if [ -f /root/.config/gh/hosts.yml ]; then
    echo "Copying GitHub CLI auth from root to appuser..."
    # Always copy if root has auth (volume may be empty or stale)
    cp /root/.config/gh/hosts.yml /home/appuser/.config/gh/ 2>/dev/null || true
    cp -r /root/.config/gh/* /home/appuser/.config/gh/ 2>/dev/null || true
    chown -R appuser:appuser /home/appuser/.config/gh
    echo "GitHub CLI auth copied successfully"
fi

# Configure git to use gh as credential helper
if [ -f /home/appuser/.config/gh/hosts.yml ]; then
    # Set git credential helper to use gh for appuser
    git config --global credential.helper '!gh auth git-credential'
    echo "Git configured to use GitHub CLI for authentication"
fi

# Ensure data and workspace directories are writable
chown -R appuser:appuser /data /workspace

# Switch to appuser and run the application
echo "Starting AI Hub as appuser (${PUID}:${PGID})"
exec setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser python -m app.main
