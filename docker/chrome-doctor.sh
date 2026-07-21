#!/bin/bash
# Diagnoses the browser-automation chain inside the ai-shuffle container:
#   supervised Chromium (docker/start-chrome.sh) -> loopback CDP :9221 ->
#   the browser-mcp server (connectOverCDP) that agents drive.
# Run it via: docker exec -u appuser -e HOME=/home/appuser <container> chrome-doctor

# Non-default profile dir (matches docker/start-chrome.sh); Chrome 136+ only
# honours --remote-debugging-port outside the default profile.
PROFILE="$HOME/chrome-profile"
CDP_PORT="${CHROME_CDP_PORT:-9221}"

pass() { printf '  \033[32m\xe2\x9c\x93\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m\xe2\x9c\x97\033[0m %s\n      -> %s\n' "$1" "$2"; FAILED=1; }

echo "Browser automation diagnostics"
echo "------------------------------"

if pgrep -f 'chrome.*--ozone-platform=headless' >/dev/null 2>&1; then
    pass "Chromium is running (headless)"
else
    fail "Chromium is not running" "start it: START_CHROME=1 (entrypoint), or run 'start-chrome' as appuser"
fi

# The loopback CDP port is the ONLY surface the browser-mcp server attaches to.
if curl -fsS "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
    pass "CDP endpoint is reachable (127.0.0.1:${CDP_PORT})"
else
    fail "CDP endpoint not reachable (127.0.0.1:${CDP_PORT})" \
         "ensure CHROME_CDP=1 and Chromium finished starting; check /home/appuser/.config/chrome.log"
fi

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
