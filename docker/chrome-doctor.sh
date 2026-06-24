#!/bin/bash
# Diagnoses the "Claude in Chrome" chain inside the ai-shuffle container:
#   google-chrome (with extension) -> native messaging host -> claude bridge
# Run it via: docker exec -u appuser -e HOME=/home/appuser <container> chrome-doctor

# Non-default profile dir (matches docker/start-chrome.sh); the extension installs
# here and start-chrome symlinks $PROFILE/NativeMessagingHosts to the canonical
# ~/.config/google-chrome/NativeMessagingHosts the CLI writes to.
PROFILE="$HOME/chrome-profile"
EXT_ID="fcoeoabgfenejglbffodgkkbkcdhcgfn"
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
MANIFEST_NAME="com.anthropic.claude_code_browser_extension.json"
POLICY="/etc/opt/chrome/policies/managed/claude-extension.json"

pass() { printf '  \033[32m\xe2\x9c\x93\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m\xe2\x9c\x97\033[0m %s\n      -> %s\n' "$1" "$2"; FAILED=1; }

echo "Claude in Chrome diagnostics"
echo "----------------------------"

if pgrep -f 'chrome.*--ozone-platform=headless' >/dev/null 2>&1; then
    pass "Google Chrome is running (headless)"
else
    fail "Chrome is not running" "start it: START_CHROME=1 (entrypoint), or run 'start-chrome' as appuser"
fi

if [ -f "$POLICY" ]; then
    pass "Extension force-install policy present"
else
    fail "Extension policy missing ($POLICY)" "image problem — rebuild the container"
fi

if ls "$PROFILE/Default/Extensions/$EXT_ID"/*/manifest.json >/dev/null 2>&1; then
    pass "Claude in Chrome extension is installed"
else
    fail "Extension not installed yet" "needs network to clients2.google.com; wait ~1 min after Chrome starts, then re-run"
fi

if [ -f "$PROFILE/NativeMessagingHosts/$MANIFEST_NAME" ]; then
    pass "Native messaging manifest present"
else
    fail "Native messaging manifest missing" "run a session with --chrome once (the CLI writes it on first use)"
fi

HOST_BIN="$CFG/chrome/chrome-native-host"
if [ -x "$HOST_BIN" ]; then
    pass "Native messaging host binary present"
else
    fail "Native host binary missing ($HOST_BIN)" "run a session with --chrome once to install it"
fi

# The native host creates this socket when the extension connects to it.
if ls -d /tmp/claude-mcp-browser-bridge-* >/dev/null 2>&1; then
    pass "Extension is talking to the native messaging host"
else
    fail "Extension has not contacted the native messaging host" \
         "ensure the extension is signed in + paired (CHROME_DEBUG sign-in), then retry a --chrome session"
fi

echo
echo "Note: sign-in/pairing state can't be checked from here. If all items pass"
echo "but the agent reports no browser, do the one-time extension sign-in"
echo "(CHROME_DEBUG=1) — see the 'Claude in Chrome (Docker)' docs."

echo
if [ -n "$FAILED" ]; then
    echo "Fix the marked items top to bottom, then retry."
else
    echo "All checks pass."
fi
