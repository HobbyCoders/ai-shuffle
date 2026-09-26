#!/bin/bash
# Launches Chromium (headless, offscreen) so the built-in browser-automation MCP
# server (browser-mcp) has a real browser to drive over CDP inside the container.
# (CHROME_EXECUTABLE points at google-chrome-stable, which is a symlink to
# /usr/bin/chromium on both arches — see Dockerfile.)
#
# Runs as the SAME user as the app (appuser). The browser-mcp server attaches to
# the DevTools (CDP) port this opens via connectOverCDP('http://127.0.0.1:9221').
#
# IMPORTANT: the profile must NOT live in Chrome's DEFAULT data directory
# (~/.config/google-chrome). Chrome 136+ silently ignores --remote-debugging-port
# when --user-data-dir is the default profile, so the CDP port would never listen
# and the browser-mcp server could not attach. We therefore point --user-data-dir
# at a non-default dir — Google's documented workaround
# (https://developer.chrome.com/blog/remote-debugging-port).
set -e

CHROME_BIN="${CHROME_EXECUTABLE:-/usr/bin/google-chrome-stable}"

# No system dbus daemon runs in this container, so Chromium logs
# "Failed to connect to the bus: /run/dbus/system_bus_socket" on startup. That
# line is normally harmless, but some Chromium components (e.g. the
# ComponentUpdater/RemoteAllocator and platform-integration features) can trip a
# CHECK/DCHECK and abort with SIGTRAP ("Trace/breakpoint trap") when the bus is
# missing. Point DBus at an empty address so Chromium takes its no-bus fallback
# path instead of trying (and failing) to reach the well-known system socket.
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-disabled:}"
export DBUS_SYSTEM_BUS_ADDRESS="${DBUS_SYSTEM_BUS_ADDRESS:-disabled:}"
PROFILE="$HOME/chrome-profile"
mkdir -p "$PROFILE"

# A recreated container gets a new hostname, but the persisted profile volume
# may still hold the old container's SingletonLock — Chrome then refuses to
# start ("profile in use by another computer"). Clear the stale lock when no
# Chrome is actually running here. The guard matches on our exact profile dir
# (--user-data-dir=$PROFILE), NOT a generic "chrome --ozone-platform=headless":
# an UNRELATED headless Chrome sharing this box (e.g. a Remotion/Puppeteer render
# also launched with --ozone-platform=headless) would otherwise falsely satisfy
# the old guard and make us SKIP this cleanup — leaving both the stale lock and
# the crashing token DB (below) in place.
#
# SAME GUARD, second job: drop the profile's persisted OAuth2 refresh-token
# stores. If a Google account was ever signed into this browser (e.g. during a
# "Set up browser" session), Chrome persists an encrypted refresh token in the
# webdata DBs below. On the NEXT launch, MutableProfileOAuth2TokenServiceDelegate
# eagerly loads that token (log: "MutablePO2TS::RefreshTokenIsAvailable" right
# before the trap) and decrypts it through OSCrypt. Under --ozone-platform=headless
# there is no Secret Service / keyring / session dbus to satisfy OSCrypt, so the
# decrypt hits a CHECK/IMMEDIATE_CRASH and Chromium aborts with SIGTRAP (rc=133,
# ~0s uptime) — a hard crash-loop the supervisor can't escape.
#
# This is DISTINCT from the first-run crash handled below: a fresh (token-less)
# profile always survives, and Chrome's own self-planted "First Run" sentinel is
# harmless on relaunch (verified) — so it is NOT the sentinel that crashes an
# aged profile, it is the persisted token. The crash was traced by bisecting a
# copy of the real crash-looping profile: removing ONLY these two DBs makes it
# survive; no launch flag (--password-store=basic, --use-mock-keychain,
# --allow-browser-signin=false) nor dbus env (dbus-run-session, unix:path=/dev/null)
# prevents it. This browser is driven purely over CDP by the browser-mcp server,
# which needs no Google sign-in, so discarding a stale token is free — Chrome
# recreates empty DBs on launch.
if ! pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null 2>&1; then
    rm -f "$PROFILE/SingletonLock" \
          "$PROFILE/SingletonSocket" \
          "$PROFILE/SingletonCookie" 2>/dev/null || true
    rm -f "$PROFILE/Default/Web Data" \
          "$PROFILE/Default/Web Data-journal" \
          "$PROFILE/Default/Account Web Data" \
          "$PROFILE/Default/Account Web Data-journal" 2>/dev/null || true
fi

# DevTools (CDP) wiring.
#
# CHROME_CDP=1 (default): bind DevTools to a LOCALHOST-only port ($CDP_PORT,
#   default 9221) inside the container. Nothing is published to the network —
#   it is reachable only in-container by the browser-mcp server, which attaches
#   via connectOverCDP('http://127.0.0.1:9221'). This loopback port is the ONLY
#   CDP surface (there is no external relay). Requires the non-default
#   --user-data-dir above (Chrome 136+ ignores the debug port on the default
#   profile).
CDP_PORT="${CHROME_CDP_PORT:-9221}"
DEBUG_ARGS=()
if [ "${CHROME_CDP:-1}" = "1" ]; then
    DEBUG_ARGS=(--remote-debugging-port="$CDP_PORT")
fi

# Memory-reduction flags. This is a long-lived headless browser driven over CDP;
# the concern is steady-state RSS, not throughput. We deliberately do NOT use
# --single-process or --no-zygote, which break the CDP/IPC plumbing.
#   --js-flags=--max-old-space-size=512  cap each V8 heap at 512MB so a busy page
#       can't grow the renderer heap unbounded (the main RAM sink for one tab).
#   --renderer-process-limit=4  cap renderer processes. Raised from 2 (the old
#       single-tab assumption) to leave headroom for a few concurrent agent tabs
#       driven by browser-mcp. FOLLOW-UP: tune from a load test alongside a soft
#       cap on concurrent browser sessions.
#   --disk-cache-size=33554432  bound the on-disk HTTP cache at 32MB (default is
#       large and grows in the persisted profile volume).
#   --disable-software-rasterizer  we already --disable-gpu; this also stops the
#       SwiftShader fallback allocating a software raster path we never display.
#   --disable-features=Translate,BackForwardCache  Translate loads an unused
#       model; BackForwardCache keeps whole pages alive in memory after
#       navigation — both pure memory cost for this single-tab use.
#
# --disable-features list also drops dbus-coupled subsystems that have no daemon
# to talk to in this container (a suspected trigger of the startup SIGTRAP):
#   DBus                              Chromium's platform dbus integration
#   MediaSessionService               publishes MPRIS over the (absent) session bus
# Kept together with the memory features so there is a single --disable-features.
MEM_ARGS=(
    --js-flags=--max-old-space-size=512
    --renderer-process-limit=4
    --disk-cache-size=33554432
    --disable-software-rasterizer
    --disable-features=Translate,BackForwardCache,DBus,MediaSessionService
)

# Do NOT suppress the first-run flow at all (neither via --no-first-run nor a
# "First Run" sentinel file in the profile dir) — live delta-debugging in this
# container proved BOTH mechanisms deterministically SIGTRAP Chromium 150 under
# --ozone-platform=headless (rc=133, ~0s uptime): 10/10 crashes with either the
# flag or the sentinel file present, 10/10+ clean runs with neither. It is the
# "skip first run" code path itself that crashes, not the specific CLI flag, so
# swapping the flag for the sentinel (an earlier fix attempt) does NOT help.
# Letting Chrome go through its real first-run flow is harmless here: this is a
# fully offscreen headless tab (no visible first-run UI to click through) and
# CDP attach works regardless of first-run state.
# NOTE: this first-run crash only affects an UNINITIALIZED profile. Chrome's own
# "First Run" sentinel, self-planted after a successful first run, is harmless on
# relaunch — so it does NOT explain the rc=133 crash-loop seen on aged persistent
# profiles. That one is the persisted-OAuth-token crash handled by the webdata-DB
# cleanup near the SingletonLock block above.
# --ozone-platform=headless: full browser rendered offscreen — no X server /
#   Xvfb required — while keeping full browser subsystems available for CDP
#   automation (retained from the extension era; deliberately not changed here).
# --no-sandbox: Chrome's user-namespace sandbox is unavailable under Docker's
#   default seccomp profile; the container provides isolation.
# --disable-gpu: render via Chrome's bundled SwiftShader.
#
# Egress proxy: Chromium runs behind browser-mcp's egress-launch.js, a
# loopback-only proxy on a random port that receives EVERY connection the
# browser makes (WebSockets and loopback included; the launcher adds
# --proxy-server and --proxy-bypass-list=<-loopback>). It resolves each host
# once and connects only to addresses the SSRF policy allows, which closes the
# WebSocket and DNS-rebinding gaps the CDP request guard cannot. The launcher
# forwards signals and exits with Chromium's status, so chrome-supervisor sees
# the same exit codes. No launcher, no browser: we never start Chromium without
# its proxy.
EGRESS_ENTRY="${BROWSER_EGRESS_ENTRY:-/opt/browser-mcp/dist/egress-launch.js}"
if [ ! -f "$EGRESS_ENTRY" ]; then
    echo "start-chrome: browser egress proxy missing ($EGRESS_ENTRY); refusing to start Chromium without it" >&2
    exit 1
fi
# The proxy enforces the app's base browser policy (loopback default, the
# worker's deny ports), computed by the app itself so the two cannot drift. If
# that fails twice, fall back to the strict default: public addresses only.
# The command's stderr goes to this script's stderr (chrome.log), so the reason
# is on record, and BROWSER_EGRESS_POLICY_SOURCE makes the launcher label its
# policy line as the fallback. In the fallback Chromium refuses loopback too,
# so a preview the agent starts is reported as "blocked by the browser's egress
# proxy", not as a server that is down.
APP_ROOT="${AIS_APP_ROOT:-/app}"
EGRESS_POLICY=""
EGRESS_POLICY_OK=0
for attempt in 1 2; do
    if EGRESS_POLICY="$(cd "$APP_ROOT" && python -m app.core.browser_egress)"; then
        EGRESS_POLICY_OK=1
        break
    fi
    echo "start-chrome: browser egress policy command failed (attempt $attempt of 2)" >&2
    if [ "$attempt" -lt 2 ]; then sleep 1; fi
done
if [ "$EGRESS_POLICY_OK" = 1 ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            BROWSER_SSRF_ALLOW|BROWSER_SSRF_BLOCK|BROWSER_SSRF_DENY_PORTS|CHROME_CDP_PORT)
                export "$key=$value" ;;
        esac
    done <<< "$EGRESS_POLICY"
    export BROWSER_EGRESS_POLICY_SOURCE=app
else
    echo "start-chrome: could not compute the browser egress policy; FALLBACK: allowing public addresses only (loopback and private addresses are refused)" >&2
    unset BROWSER_SSRF_ALLOW
    export BROWSER_EGRESS_POLICY_SOURCE=fallback
fi
exec node "$EGRESS_ENTRY" -- "$CHROME_BIN" \
    --ozone-platform=headless \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --no-default-browser-check \
    --user-data-dir="$PROFILE" \
    --window-size=1440,900 \
    "${MEM_ARGS[@]}" \
    "${DEBUG_ARGS[@]}" \
    "$@" \
    about:blank
