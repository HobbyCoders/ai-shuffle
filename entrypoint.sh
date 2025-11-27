#!/bin/bash
set -e

# Get PUID and PGID from environment, default to 99:100 (Unraid)
PUID=${PUID:-99}
PGID=${PGID:-100}

echo "Starting with PUID=${PUID} and PGID=${PGID}"

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

# Ensure config directory exists with correct permissions
mkdir -p /home/appuser/.config/claude /home/appuser/.claude
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
if [ -f /root/.config/gh/hosts.yml ] && [ ! -f /home/appuser/.config/gh/hosts.yml ]; then
    echo "Copying GitHub CLI auth from root to appuser..."
    mkdir -p /home/appuser/.config/gh
    cp -r /root/.config/gh/* /home/appuser/.config/gh/ 2>/dev/null || true
    chown -R appuser:appuser /home/appuser/.config/gh
fi

# Configure git to use gh as credential helper for appuser
if [ -f /home/appuser/.config/gh/hosts.yml ]; then
    # Set git credential helper to use gh
    git config --global credential.helper '!gh auth git-credential'
fi

# Ensure data and workspace directories are writable
chown -R appuser:appuser /data /workspace

# Switch to appuser and run the application
echo "Starting AI Hub as appuser (${PUID}:${PGID})"
exec setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser python -m app.main
