#!/bin/bash
# Supervises the headless browser-automation Chromium (docker/start-chrome.sh)
# inside the ai-shuffle container so a crash auto-restarts. Launched backgrounded
# by entrypoint.sh (START_CHROME=1) with its stdout/stderr redirected to
# /home/appuser/.config/chrome.log. Installed in the image as
# /usr/local/bin/chrome-supervisor (mirrors docker/start-chrome.sh).
#
# Supervisor hardening (root cause of the pids-cgroup exhaustion outage):
#   * Capture the REAL child exit status. The old loop ran `start-chrome ||
#     true` and then read `$?`, which is always 0 (the status of `|| true`) —
#     so a Chrome that died by SIGTRAP was logged as "rc=0 (clean)" and
#     respawned every 5s forever, leaking unreaped grandchildren each time.
#   * Circuit breaker: after CHROME_MAX_RESTARTS *rapid* failures (each within
#     CHROME_HEALTHY_SECS of starting) STOP fast-retrying and drop into a
#     slow-retry mode. A Chrome that can't even start must never be able to
#     spin the fork loop that starves the app of PIDs. Set CHROME_MAX_RESTARTS=0
#     to fast-retry forever (never enter slow mode).
#   * Exponential backoff between rapid failures (5s → 10s → 20s ... capped)
#     instead of a flat 5s, so even below the cap the leak/spin rate is bounded.
# Note: tini (PID 1) now reaps orphans regardless, so this is defence in depth.
#
# Why slow-retry instead of a permanent "GIVE UP" latch (the previous bug):
#   The container's headless Chromium used to die with SIGTRAP (rc=133) at
#   launch, ~0s uptime, no FATAL log line. Root-caused via live delta-debugging
#   (2026-07-09): it is the "skip first run" code path itself that
#   deterministically crashes Chromium 150 under --ozone-platform=headless —
#   reproduced 10/10 via the --no-first-run flag AND, separately, 10/10 via a
#   "First Run" sentinel file in the profile dir (both suppress the same
#   first-run flow), versus 10/10+ clean runs with neither present. It was NOT
#   an intermittent/flag-independent "crash window" as previously believed.
#   Fixed in start-chrome.sh by dropping --no-first-run and NOT substituting any
#   other first-run-suppression mechanism — Chrome just runs its real first-run
#   flow, which is harmless for a fully offscreen headless tab. This retry
#   machinery is kept as defense in depth for any other future crash mode: a
#   permanent latch would leave Chrome DISABLED for the container's whole
#   lifetime once the restart budget is burned through, whereas slow-retry keeps
#   probing at a rate far below anything that could exhaust the pids cgroup, so
#   Chrome self-heals if a transient issue ever recurs.
#
# Testability: we only wrap start-chrome in `setpriv` when running as root
# (uid 0), because that privilege drop is only meaningful/possible then. When
# run as a non-root user (e.g. the regression test harness, or the appuser)
# start-chrome is invoked directly. This lets the script be exercised end to
# end without root while preserving the identical root behavior in the image.
set +e

CHROME_MAX_RESTARTS="${CHROME_MAX_RESTARTS:-8}"
CHROME_HEALTHY_SECS="${CHROME_HEALTHY_SECS:-60}"
CHROME_BACKOFF_MAX="${CHROME_BACKOFF_MAX:-300}"
# Slow-retry cadence once the rapid-failure budget is spent. Paced far below
# anything that could exhaust the 8192-pid cgroup (one bounded launch per cycle).
CHROME_SLOW_RETRY_SECS="${CHROME_SLOW_RETRY_SECS:-300}"
# Initial fast-retry backoff. Overridable only so tests can shrink it; the image
# default matches the historical flat-5s starting point.
CHROME_BACKOFF_INITIAL="${CHROME_BACKOFF_INITIAL:-5}"

# Launch one Chrome instance (a single bounded process, foreground — we wait for
# it and capture its real exit status). As root we drop to appuser via setpriv,
# passing the same env defaults the entrypoint used to inline; otherwise (tests,
# already-appuser) we exec start-chrome directly.
launch_chrome() {
    if [ "$(id -u)" = "0" ]; then
        setpriv --reuid=appuser --regid=appuser --init-groups \
            env HOME=/home/appuser \
                CHROME_CDP="${CHROME_CDP:-1}" \
                CHROME_CDP_PORT="${CHROME_CDP_PORT:-9221}" \
                CHROME_DEBUG="${CHROME_DEBUG:-0}" \
                CHROME_DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}" \
                CHROME_EXECUTABLE="${CHROME_EXECUTABLE:-/usr/bin/google-chrome-stable}" \
                start-chrome
    else
        CHROME_CDP="${CHROME_CDP:-1}" \
        CHROME_CDP_PORT="${CHROME_CDP_PORT:-9221}" \
        CHROME_DEBUG="${CHROME_DEBUG:-0}" \
        CHROME_DEBUG_PORT="${CHROME_DEBUG_PORT:-9222}" \
        CHROME_EXECUTABLE="${CHROME_EXECUTABLE:-/usr/bin/google-chrome-stable}" \
            start-chrome
    fi
}

fails=0
backoff="$CHROME_BACKOFF_INITIAL"
slow_mode=0
slow_attempts=0
while true; do
    start=$(date +%s)
    launch_chrome
    rc=$?
    ran=$(( $(date +%s) - start ))

    if [ "$ran" -ge "$CHROME_HEALTHY_SECS" ]; then
        # Chrome stayed up long enough to count as healthy — a later exit is a
        # normal restart, so reset the failure counter and backoff. This is also
        # the only exit from slow-retry mode: once the crash window passes and
        # Chrome runs healthily, we return to fast supervision.
        if [ "$slow_mode" = "1" ]; then
            echo "Chrome recovered (rc=$rc) after ${ran}s — leaving slow-retry mode, resuming normal supervision."
        else
            echo "Chrome exited (rc=$rc) after ${ran}s. Restarting in ${CHROME_BACKOFF_INITIAL}s..."
        fi
        fails=0
        backoff="$CHROME_BACKOFF_INITIAL"
        slow_mode=0
        slow_attempts=0
        sleep "$CHROME_BACKOFF_INITIAL"
        continue
    fi

    if [ "$slow_mode" = "1" ]; then
        # Already in slow-retry mode: keep it quiet — one short line per attempt.
        slow_attempts=$((slow_attempts + 1))
        echo "Chrome slow-retry attempt ${slow_attempts} failed (rc=$rc after ${ran}s). Next try in ${CHROME_SLOW_RETRY_SECS}s..."
        sleep "$CHROME_SLOW_RETRY_SECS"
        continue
    fi

    fails=$((fails + 1))
    echo "Chrome exited (rc=$rc) after only ${ran}s — rapid failure ${fails}/${CHROME_MAX_RESTARTS}."

    if [ "$CHROME_MAX_RESTARTS" -gt 0 ] && [ "$fails" -ge "$CHROME_MAX_RESTARTS" ]; then
        # Rapid-failure budget spent. DO NOT latch Chrome off permanently (the
        # old bug). The known deterministic cause of repeated SIGTRAP (rc=133)
        # crashes — suppressing Chromium 150's first-run flow (via either the
        # --no-first-run flag or a "First Run" sentinel file) under
        # --ozone-platform=headless — is already fixed in start-chrome.sh (it no
        # longer suppresses first-run at all), so this path is now defense in
        # depth against any other/future crash cause — drop to a slow, bounded
        # retry that self-heals instead of giving up.
        echo "!!! Chrome crash-looped ${fails} times (rc=$rc). Entering SLOW-RETRY mode to protect the container's PID budget."
        echo "!!! rc=133 (SIGTRAP) was previously caused by suppressing Chromium 150's first-run flow on headless — fixed in start-chrome.sh; this is now a defense-in-depth path for any other crash cause."
        echo "!!! Chrome will keep retrying every ${CHROME_SLOW_RETRY_SECS}s and auto-recover if the cause is transient (a healthy run resets it)."
        echo "!!! Diagnose with: docker exec -u appuser -e HOME=/home/appuser <container> chrome-doctor"
        echo "!!! (CHROME_MAX_RESTARTS=0 fast-retries forever; START_CHROME=0 disables Chrome entirely.)"
        slow_mode=1
        slow_attempts=0
        sleep "$CHROME_SLOW_RETRY_SECS"
        continue
    fi

    echo "Restarting Chrome in ${backoff}s (exponential backoff)..."
    sleep "$backoff"
    backoff=$((backoff * 2))
    [ "$backoff" -gt "$CHROME_BACKOFF_MAX" ] && backoff="$CHROME_BACKOFF_MAX"
done
