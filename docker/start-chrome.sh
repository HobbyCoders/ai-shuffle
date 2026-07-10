#!/bin/bash
# Launches Chromium (headless, offscreen) so the bundled Claude Code CLI's
# "Claude in Chrome" integration (--chrome) has a real browser to drive inside
# the container. (CHROME_EXECUTABLE points at google-chrome-stable, which is a
# symlink to /usr/bin/chromium on both arches — see Dockerfile.)
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

# DevTools (CDP) wiring. Two independent knobs:
#
# CHROME_CDP=1 (default): bind DevTools to a LOCALHOST-only port ($CDP_PORT,
#   default 9221) inside the container. Nothing is published to the network —
#   it is reachable only by the AI Shuffle backend, which proxies it (behind app
#   login) for the in-app "Set up browser" panel. This is how the one-time
#   extension sign-in/pairing is done now: no exposed port, no DevTools URLs.
#   Requires the non-default --user-data-dir above (Chrome 136+ ignores the
#   debug port on the default profile).
#
# CHROME_DEBUG=1 (legacy/manual): ALSO relay that localhost port out to
#   0.0.0.0:${CHROME_DEBUG_PORT:-9222} via socat so you can open Chrome's raw
#   DevTools inspector from another device. Anyone who can reach that port can
#   control the browser — prefer the in-app panel; turn this off after use.
CDP_PORT="${CHROME_CDP_PORT:-9221}"
DEBUG_ARGS=()
if [ "${CHROME_CDP:-1}" = "1" ] || [ "${CHROME_DEBUG:-0}" = "1" ]; then
    DEBUG_ARGS=(--remote-debugging-port="$CDP_PORT")
fi
if [ "${CHROME_DEBUG:-0}" = "1" ]; then
    # The browser DevTools front-end sends an Origin header; allow it for the
    # legacy external path. The backend proxy sends none, so this is not needed
    # for the in-app panel.
    DEBUG_ARGS+=(--remote-allow-origins=*)
    pkill -f "socat.*:$CDP_PORT" 2>/dev/null || true
    socat TCP-LISTEN:"${CHROME_DEBUG_PORT:-9222}",fork,reuseaddr,bind=0.0.0.0 \
          TCP:127.0.0.1:"$CDP_PORT" &
fi

# Memory-reduction flags. This is a long-lived, single-tab headless browser; the
# concern is steady-state RSS, not throughput. Every flag here is safe for
# extensions + native messaging (we deliberately do NOT use --single-process or
# --no-zygote, which break extension/IPC plumbing).
#   --js-flags=--max-old-space-size=512  cap each V8 heap at 512MB so a busy page
#       can't grow the renderer heap unbounded (the main RAM sink for one tab).
#   --renderer-process-limit=2  cap renderer processes — a single setup/agent tab
#       never needs more, and it stops Chrome from spinning up extra processes.
#   --disk-cache-size=33554432  bound the on-disk HTTP cache at 32MB (default is
#       large and grows in the persisted profile volume).
#   --disable-software-rasterizer  we already --disable-gpu; this also stops the
#       SwiftShader fallback allocating a software raster path we never display.
#   --disable-features=Translate,BackForwardCache  Translate loads an unused
#       model; BackForwardCache keeps whole pages alive in memory after
#       navigation — both pure memory cost for this single-tab use.
#
# DO NOT add --disable-background-networking here. It looks like a free memory
# win, but it disables Chromium's extension installer/updater — the very
# subsystem that processes ExtensionInstallForcelist and downloads the
# Claude-in-Chrome CRX. With it set, the extension NEVER force-installs (no CRX
# fetch at all), so /chrome can't connect. (Same reason Puppeteer, which ships
# that flag by default, can't force-install extensions.)
# --disable-features list also drops dbus-coupled subsystems that have no daemon
# to talk to in this container (a suspected trigger of the startup SIGTRAP):
#   DBus                              Chromium's platform dbus integration
#   MediaSessionService               publishes MPRIS over the (absent) session bus
# Kept together with the memory features so there is a single --disable-features.
MEM_ARGS=(
    --js-flags=--max-old-space-size=512
    --renderer-process-limit=2
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
# the extension force-install policy applies regardless of first-run state.
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
    --no-default-browser-check \
    --user-data-dir="$PROFILE" \
    --window-size=1440,900 \
    "${MEM_ARGS[@]}" \
    "${DEBUG_ARGS[@]}" \
    "$@" \
    about:blank
