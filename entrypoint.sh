#!/bin/bash
set -e

# Get PUID and PGID from environment, default to 99:100 (Unraid)
PUID=${PUID:-99}
PGID=${PGID:-100}

# Check if updates are enabled (default: true)
AUTO_UPDATE=${AUTO_UPDATE:-true}

echo "Starting with PUID=${PUID} and PGID=${PGID}"

# Update GitHub CLI if enabled
# Note: Claude Code CLI is bundled with claude-agent-sdk, updated via pip
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
mkdir -p /home/appuser/.config/claude /home/appuser/.claude /home/appuser/.config/gh
chown -R appuser:appuser /home/appuser/.config /home/appuser/.claude

# =============================================================================
# Claude Code Config Persistence
# =============================================================================
# Claude CLI expects ~/.claude.json at the home directory root, but only
# ~/.claude/ is volume-mounted. Without a symlink, the config is lost on
# every container restart. Fix: store the real file inside the volume and
# symlink to it.
CLAUDE_JSON_REAL="/home/appuser/.claude/.claude.json"
CLAUDE_JSON_LINK="/home/appuser/.claude.json"

if [ ! -L "$CLAUDE_JSON_LINK" ]; then
    # Not a symlink — migrate or restore the file into the volume
    if [ -f "$CLAUDE_JSON_LINK" ]; then
        # Plain file exists outside the volume — move it in
        mv "$CLAUDE_JSON_LINK" "$CLAUDE_JSON_REAL"
        echo "Migrated .claude.json into persistent volume"
    elif [ ! -f "$CLAUDE_JSON_REAL" ]; then
        # No file anywhere — try to restore from the most recent backup
        LATEST_BACKUP=$(ls -t /home/appuser/.claude/backups/.claude.json.backup.* 2>/dev/null | head -1)
        if [ -n "$LATEST_BACKUP" ]; then
            cp "$LATEST_BACKUP" "$CLAUDE_JSON_REAL"
            echo "Restored .claude.json from backup: $LATEST_BACKUP"
        fi
    fi
    # Ensure the target file exists (a dangling symlink causes the CLI
    # subprocess to hang in --input-format stream-json mode because it
    # can't read hasCompletedOnboarding and enters an interactive flow)
    if [ ! -f "$CLAUDE_JSON_REAL" ]; then
        echo '{"hasCompletedOnboarding":true}' > "$CLAUDE_JSON_REAL"
        chown appuser:appuser "$CLAUDE_JSON_REAL"
        echo "Created default .claude.json in persistent volume"
    fi
    ln -sf "$CLAUDE_JSON_REAL" "$CLAUDE_JSON_LINK"
    chown -h appuser:appuser "$CLAUDE_JSON_LINK"
    echo "Symlinked $CLAUDE_JSON_LINK -> $CLAUDE_JSON_REAL"
fi

# Also ensure hasCompletedOnboarding is set in the existing .claude.json
# (covers case where file exists from a previous version without this flag)
if [ -f "$CLAUDE_JSON_REAL" ]; then
    if ! grep -q '"hasCompletedOnboarding"' "$CLAUDE_JSON_REAL" 2>/dev/null; then
        python3 -c "
import json
with open('$CLAUDE_JSON_REAL', 'r') as f:
    data = json.load(f)
data['hasCompletedOnboarding'] = True
with open('$CLAUDE_JSON_REAL', 'w') as f:
    json.dump(data, f, indent=2)
print('Added hasCompletedOnboarding to .claude.json')
" 2>/dev/null || echo "Could not update .claude.json"
    fi
fi

# =============================================================================
# Claude Code Credentials Management
# =============================================================================
# The volume mount for Claude auth is at /home/appuser/.claude
# Credentials are stored after in-app login - no need to copy from root anymore

# Check for existing Claude credentials in the volume
if [ -f /home/appuser/.claude/.credentials.json ]; then
    CREDS_SIZE=$(stat -c%s /home/appuser/.claude/.credentials.json 2>/dev/null || echo "0")
    if [ "$CREDS_SIZE" -gt "10" ]; then
        echo "Claude Code credentials found in volume (${CREDS_SIZE} bytes)"

        # Ensure settings.json has hasCompletedOnboarding to skip CLI onboarding wizard
        SETTINGS_FILE="/home/appuser/.claude/settings.json"
        if [ -f "$SETTINGS_FILE" ]; then
            # Check if hasCompletedOnboarding is already set
            if ! grep -q '"hasCompletedOnboarding"' "$SETTINGS_FILE" 2>/dev/null; then
                echo "Adding hasCompletedOnboarding to settings.json..."
                # Use Python to safely update JSON
                python3 -c "
import json
with open('$SETTINGS_FILE', 'r') as f:
    data = json.load(f)
data['hasCompletedOnboarding'] = True
if 'theme' not in data:
    data['theme'] = 'dark'
with open('$SETTINGS_FILE', 'w') as f:
    json.dump(data, f, indent=2)
print('Settings updated with hasCompletedOnboarding=true')
" 2>/dev/null || echo "Could not update settings.json"
            fi
        else
            # Create settings.json with required fields
            echo "Creating settings.json with hasCompletedOnboarding..."
            echo '{"theme": "dark", "hasCompletedOnboarding": true}' > "$SETTINGS_FILE"
        fi
        chown appuser:appuser "$SETTINGS_FILE" 2>/dev/null || true
    fi
else
    echo "Claude Code: No credentials found - login via web UI required"
fi

# Legacy: Copy Claude credentials from root if they exist (for backwards compatibility)
if [ -f /root/.claude/.credentials.json ] && [ ! -f /home/appuser/.claude/.credentials.json ]; then
    echo "Copying Claude credentials from root to appuser (legacy migration)..."
    cp /root/.claude/.credentials.json /home/appuser/.claude/ 2>/dev/null || true
    cp -r /root/.claude/* /home/appuser/.claude/ 2>/dev/null || true
    chown -R appuser:appuser /home/appuser/.claude
fi

# Also check .config/claude location (newer Claude versions)
if [ -f /root/.config/claude/credentials.json ] && [ ! -f /home/appuser/.config/claude/credentials.json ]; then
    echo "Copying Claude config from root to appuser..."
    mkdir -p /home/appuser/.config/claude
    cp -r /root/.config/claude/* /home/appuser/.config/claude/ 2>/dev/null || true
    chown -R appuser:appuser /home/appuser/.config
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

# Final ownership fix for config directories
chown -R appuser:appuser /home/appuser/.config /home/appuser/.claude 2>/dev/null || true

# =============================================================================
# Claude in Chrome browser
# =============================================================================
# Launch headless Google Chrome as appuser (the same user the app + bundled CLI
# run as) so the native-messaging host the CLI writes to
# /home/appuser/.config/google-chrome/NativeMessagingHosts/ is the one this
# browser reads. Supervised so a crash auto-restarts. Disable with START_CHROME=0.
# One-time extension sign-in/pairing uses CHROME_DEBUG=1 (DevTools on :9222).
if [ "${START_CHROME:-1}" = "1" ]; then
    # The browser profile lives in a NON-default dir (~/chrome-profile) so Chrome
    # 136+ keeps honoring --remote-debugging-port for the one-time CHROME_DEBUG
    # sign-in. Native-messaging manifests still go under ~/.config/google-chrome.
    mkdir -p /home/appuser/chrome-profile \
             /home/appuser/.config/google-chrome/NativeMessagingHosts
    chown -R appuser:appuser /home/appuser/chrome-profile \
             /home/appuser/.config/google-chrome 2>/dev/null || true
    # arm64 runs Chromium (symlinked as google-chrome-stable). Chromium reads user
    # native-messaging host manifests from ~/.config/chromium/NativeMessagingHosts/,
    # but the bundled CLI writes them to ~/.config/google-chrome/NativeMessagingHosts/.
    # Bridge the two so the Claude-in-Chrome extension can reach its host (#14391).
    if readlink -f "${CHROME_EXECUTABLE:-/usr/bin/google-chrome-stable}" 2>/dev/null | grep -qi chromium; then
        mkdir -p /home/appuser/.config/google-chrome/NativeMessagingHosts \
                 /home/appuser/.config/chromium
        ln -sfn /home/appuser/.config/google-chrome/NativeMessagingHosts \
                /home/appuser/.config/chromium/NativeMessagingHosts
        chown -R appuser:appuser /home/appuser/.config/chromium 2>/dev/null || true
    fi
    echo "Starting Chrome for Claude in Chrome (CHROME_DEBUG=${CHROME_DEBUG:-0})..."
    # Supervisor hardening (root cause of the pids-cgroup exhaustion outage):
    #   * Capture the REAL child exit status. The old loop ran `start-chrome ||
    #     true` and then read `$?`, which is always 0 (the status of `|| true`) —
    #     so a Chrome that died by SIGTRAP was logged as "rc=0 (clean)" and
    #     respawned every 5s forever, leaking unreaped grandchildren each time.
    #   * Circuit breaker: after CHROME_MAX_RESTARTS *rapid* failures (each within
    #     CHROME_HEALTHY_SECS of starting) stop retrying and log loudly. A Chrome
    #     that can't even start must never be able to spin the fork loop that
    #     starves the app of PIDs. Set CHROME_MAX_RESTARTS=0 to retry forever.
    #   * Exponential backoff between rapid failures (5s → 10s → 20s ... capped)
    #     instead of a flat 5s, so even below the cap the leak/spin rate is bounded.
    # Note: tini (PID 1) now reaps orphans regardless, so this is defence in depth.
    CHROME_MAX_RESTARTS="${CHROME_MAX_RESTARTS:-8}"
    CHROME_HEALTHY_SECS="${CHROME_HEALTHY_SECS:-60}"
    CHROME_BACKOFF_MAX="${CHROME_BACKOFF_MAX:-300}"
    (
        # This subshell must NOT inherit `set -e`: a failing start-chrome would
        # abort the whole loop before we ever capture its exit status, silently
        # killing the supervisor (and hiding crashes). Explicitly disable it.
        set +e
        fails=0
        backoff=5
        while true; do
            start=$(date +%s)
            setpriv --reuid=appuser --regid=appuser --init-groups \
                env HOME=/home/appuser \
                    CHROME_CDP="${CHROME_CDP:-1}" \
                    CHROME_CDP_PORT="${CHROME_CDP_PORT:-9221}" \
                    CHROME_DEBUG="${CHROME_DEBUG:-0}" \
                    CHROME_DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}" \
                    CHROME_EXECUTABLE="${CHROME_EXECUTABLE:-/usr/bin/google-chrome-stable}" \
                    start-chrome
            rc=$?
            ran=$(( $(date +%s) - start ))
            if [ "$ran" -ge "$CHROME_HEALTHY_SECS" ]; then
                # Chrome stayed up long enough to count as healthy — a later exit
                # is a normal restart, so reset the failure counter and backoff.
                echo "Chrome exited (rc=$rc) after ${ran}s. Restarting in 5s..."
                fails=0
                backoff=5
                sleep 5
                continue
            fi
            fails=$((fails + 1))
            echo "Chrome exited (rc=$rc) after only ${ran}s — rapid failure ${fails}/${CHROME_MAX_RESTARTS}."
            if [ "$CHROME_MAX_RESTARTS" -gt 0 ] && [ "$fails" -ge "$CHROME_MAX_RESTARTS" ]; then
                echo "!!! Chrome crash-looped ${fails} times (rc=$rc). GIVING UP to protect the container's PID budget."
                echo "!!! Claude in Chrome is now DISABLED for this container's lifetime."
                echo "!!! Diagnose with: docker exec -u appuser -e HOME=/home/appuser <container> chrome-doctor"
                echo "!!! To retry after fixing, restart the container (or set CHROME_MAX_RESTARTS=0 to retry forever, START_CHROME=0 to disable)."
                break
            fi
            echo "Restarting Chrome in ${backoff}s (exponential backoff)..."
            sleep "$backoff"
            backoff=$((backoff * 2))
            [ "$backoff" -gt "$CHROME_BACKOFF_MAX" ] && backoff="$CHROME_BACKOFF_MAX"
        done
    ) > /home/appuser/.config/chrome.log 2>&1 &
    echo "Chrome supervisor started (logs: /home/appuser/.config/chrome.log). Diagnose with: chrome-doctor"
else
    echo "START_CHROME=0 — skipping Chrome launch (Claude in Chrome will be unavailable)"
fi

# =============================================================================
# Start Application
# =============================================================================
echo ""
echo "=========================================="
echo "Starting AI Hub as appuser (${PUID}:${PGID})"
echo "=========================================="
exec setpriv --reuid=appuser --regid=appuser --init-groups env HOME=/home/appuser python -m app.main
