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

# Ensure data and workspace directories are writable
chown -R appuser:appuser /data /workspace

# Switch to appuser and run the application with proper HOME
echo "Starting AI Hub as appuser (${PUID}:${PGID})"
exec gosu appuser env HOME=/home/appuser python -m app.main
