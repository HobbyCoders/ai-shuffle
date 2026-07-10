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
         "ensure the extension is signed in (use the in-app 'Set up browser' panel), then retry a --chrome session"
fi

# Pairing pin: the CLI persists chromeExtension.pairedDeviceId in ~/.claude.json
# once a specific browser is pinned. Optional — with a single browser on the
# account the CLI auto-selects it — but pinning makes selection deterministic
# when another browser is also signed into the same Claude account.
CLAUDE_JSON="$HOME/.claude.json"
PAIRED_ID="$(grep -o '"pairedDeviceId"[[:space:]]*:[[:space:]]*"[^"]*"' "$CLAUDE_JSON" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/')"
if [ -n "$PAIRED_ID" ]; then
    PAIRED_NAME="$(grep -o '"pairedDeviceName"[[:space:]]*:[[:space:]]*"[^"]*"' "$CLAUDE_JSON" 2>/dev/null | sed 's/.*"\([^"]*\)"$/\1/')"
    pass "Pinned to a browser (${PAIRED_NAME:-$PAIRED_ID})"
else
    # Not a failure — auto-select still works with one browser.
    printf '  \033[33m~\033[0m %s\n      -> %s\n' \
        "No browser pinned (pairedDeviceId unset)" \
        "fine with one browser; use the in-app 'Pair this browser' button to pin it deterministically"
fi

echo
echo "Note: if all items pass but the agent reports no browser, sign the"
echo "extension in via the in-app 'Set up browser' panel, then click"
echo "'Pair this browser' — see the 'Claude in Chrome (Docker)' docs."

echo
echo "Resolved issue: Chrome SIGTRAP at launch (rc=133)"
echo "--------------------------------------------------"
echo "Chromium here used to sometimes die with rc=133 (SIGTRAP) at ~0s uptime and"
echo "no FATAL log line. Root-caused via live delta-debugging: suppressing"
echo "Chromium 150's first-run flow under --ozone-platform=headless"
echo "deterministically crashes it — reproduced 10/10 via the --no-first-run flag"
echo "AND, separately, 10/10 via a 'First Run' sentinel file in the profile dir"
echo "(an earlier fix attempt that swapped one suppression mechanism for the"
echo "other and still crashed), versus 10/10+ clean runs with first-run NOT"
echo "suppressed at all. It was NOT the flag-independent intermittent 'crash"
echo "window' previously suspected. Fixed in start-chrome.sh by dropping"
echo "--no-first-run and not suppressing first-run any other way — Chrome just"
echo "runs its real first-run flow, harmless for a fully offscreen headless tab."
echo "The supervisor (chrome-supervisor) still keeps its slow-retry self-healing"
echo "behavior (after CHROME_MAX_RESTARTS rapid failures) as defense in depth"
echo "against any other/future crash cause. Watch it recover in"
echo "/home/appuser/.config/chrome.log."

echo
if [ -n "$FAILED" ]; then
    echo "Fix the marked items top to bottom, then retry."
else
    echo "All checks pass."
fi
