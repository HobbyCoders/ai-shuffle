#!/bin/bash
# Launches Google Chrome (headless, offscreen) so the bundled Claude Code CLI's
# "Claude in Chrome" integration (--chrome) has a real browser to drive inside
# the container.
#
# Runs as the SAME user as the app/CLI (appuser). The CLI writes its
# native-messaging host manifest to ~/.config/google-chrome/NativeMessagingHosts/.
#
# IMPORTANT: the profile must NOT live in Chrome's DEFAULT data directory
# (~/.config/google-chrome). Chrome 136+ silently ignores --remote-debugging-port
# when --user-data-dir is the default profile, so DevTools (CHROME_DEBUG=1) would
# never listen and the one-time sign-in would be impossible. We therefore point
# --user-data-dir at a non-default dir — Google's documented workaround
# (https://developer.chrome.com/blog/remote-debugging-port). Chrome resolves the
# native-messaging manifest dir relative to the profile, so we symlink it back to
# the canonical ~/.config/google-chrome location the CLI writes to.
set -e

CHROME_BIN="${CHROME_EXECUTABLE:-/usr/bin/google-chrome-stable}"
PROFILE="$HOME/chrome-profile"
NM_DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
mkdir -p "$PROFILE" "$NM_DIR"
# Bridge the CLI-written native-messaging manifest into this non-default profile
# so the Claude-in-Chrome extension can still reach its host (#14391-style).
ln -sfn "$NM_DIR" "$PROFILE/NativeMessagingHosts"

# A recreated container gets a new hostname, but the persisted profile volume
# may still hold the old container's SingletonLock — Chrome then refuses to
# start ("profile in use by another computer"). Clear the stale lock when no
# Chrome is actually running here.
if ! pgrep -f 'chrome.*--ozone-platform=headless' >/dev/null 2>&1; then
    rm -f "$PROFILE/SingletonLock" \
          "$PROFILE/SingletonSocket" \
          "$PROFILE/SingletonCookie" 2>/dev/null || true
fi

# CHROME_DEBUG=1 exposes the browser's DevTools on port 9222 (map it in Docker!)
# so you can see and control this headless browser from any device for the
# one-time sign-in/pairing: open the DevTools inspector and drive the live
# screencast. Chrome only binds DevTools to 127.0.0.1 in non-headless ozone
# mode, so it listens internally on 9221 and socat relays 0.0.0.0:9222 ->
# 127.0.0.1:9221. Anyone on the network can control the browser while this is
# on; turn it off after pairing.
DEBUG_ARGS=()
if [ "${CHROME_DEBUG:-0}" = "1" ]; then
    DEBUG_ARGS=(--remote-debugging-port=9221 --remote-allow-origins=*)
    pkill -f 'socat.*:9221' 2>/dev/null || true
    socat TCP-LISTEN:"${CHROME_DEBUG_PORT:-9222}",fork,reuseaddr,bind=0.0.0.0 \
          TCP:127.0.0.1:9221 &
fi

# --ozone-platform=headless: full browser (extensions, native messaging)
#   rendered offscreen — no X server / Xvfb required. (Plain --headless does
#   NOT load extensions or native messaging, so we must use ozone headless.)
# --no-sandbox: Chrome's user-namespace sandbox is unavailable under Docker's
#   default seccomp profile; the container provides isolation.
# --disable-gpu: render via Chrome's bundled SwiftShader.
exec "$CHROME_BIN" \
    --ozone-platform=headless \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --no-first-run \
    --no-default-browser-check \
    --user-data-dir="$PROFILE" \
    --window-size=1440,900 \
    "${DEBUG_ARGS[@]}" \
    "$@" \
    about:blank
