"""
Plugin Auto-Update Background Task

Periodically syncs marketplaces and updates installed plugins.
Runs as an asyncio background task, similar to session cleanup and DB maintenance.

Settings are stored in ~/.fluid/plugins/auto_update_settings.json and can be
modified via the /api/v1/plugins/auto-update/settings endpoint.

Idle behaviour (F10)
--------------------
* When auto-update is DISABLED the task waits on ``_reschedule_event``
  INDEFINITELY. It used to wake every 5 minutes to re-read a settings file that
  only ever changes through the API — and the API already fires
  :func:`reschedule_auto_update` on every change, so the timer was pure
  overhead: 288 pointless wake-ups and file reads a day to learn nothing.
* Every wait is JITTERED so this task's git+network burst does not line up with
  the other scheduled work on the box (DB maintenance, session cleanup, the
  background sweep). Coinciding bursts are what a weak machine actually feels.
"""

import asyncio
import logging
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level task reference for lifecycle management
_auto_update_task: Optional[asyncio.Task] = None
_reschedule_event: Optional[asyncio.Event] = None

# Fraction of a wait that may be added as random jitter. Applied to the startup
# delay and to every inter-cycle wait so N installs (and this install's other
# periodic tasks) do not synchronise on the same instant.
JITTER_FRACTION = 0.2

# Startup delay before the first check, in seconds (jittered on top).
STARTUP_DELAY_SECONDS = 60


def _jittered(seconds: float) -> float:
    """``seconds`` plus up to ``JITTER_FRACTION`` extra, never less."""
    if seconds <= 0:
        return 0.0
    return seconds * (1.0 + JITTER_FRACTION * random.random())


async def _wait_for_reschedule(timeout: Optional[float]) -> bool:
    """Wait for a settings change, or ``timeout`` seconds (None = forever).

    Returns True when the reschedule event fired, False on timeout. Clearing the
    event here (and only here) keeps the "did settings change?" edge in one
    place.
    """
    assert _reschedule_event is not None
    if timeout is None:
        await _reschedule_event.wait()
        _reschedule_event.clear()
        return True
    try:
        await asyncio.wait_for(_reschedule_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return False
    _reschedule_event.clear()
    return True


async def _plugin_auto_update_loop():
    """
    Background loop that periodically runs the plugin auto-update cycle.

    Reads settings each iteration to pick up changes. The loop can be
    interrupted by setting the _reschedule_event, which causes it to
    re-read settings and adjust the sleep interval immediately.
    """
    global _reschedule_event
    _reschedule_event = asyncio.Event()

    # Initial delay to let the app fully start up (jittered so the first check
    # does not land on top of the other startup-scheduled work).
    await asyncio.sleep(_jittered(STARTUP_DELAY_SECONDS))

    while True:
        try:
            from app.core.plugin_service import get_plugin_service

            service = get_plugin_service()
            settings = service.get_auto_update_settings()

            if not settings.enabled:
                # Auto-update disabled — park on the reschedule event with NO
                # timeout. The settings file is only ever written through the
                # API, which calls reschedule_auto_update() on every change, so
                # a timer here could only ever re-read an unchanged file.
                logger.debug(
                    "[PluginAutoUpdate] Disabled — waiting for a settings change"
                )
                await _wait_for_reschedule(None)
                continue

            interval_seconds = settings.interval_hours * 3600

            # Check if enough time has passed since last check
            should_run = True
            if settings.last_check:
                from datetime import datetime, timezone
                try:
                    last = datetime.fromisoformat(settings.last_check.replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                    if elapsed < interval_seconds:
                        remaining = interval_seconds - elapsed
                        logger.debug(
                            f"[PluginAutoUpdate] Next check in {remaining:.0f}s "
                            f"({remaining/3600:.1f}h)"
                        )
                        await _wait_for_reschedule(_jittered(remaining))
                        should_run = True  # Time's up, run now
                        continue  # Re-enter loop to re-read settings
                except (ValueError, TypeError):
                    pass  # Invalid timestamp, run immediately

            if should_run:
                logger.info("[PluginAutoUpdate] Running auto-update cycle...")
                start = time.monotonic()

                try:
                    summary = await asyncio.to_thread(service.run_auto_update)

                    duration = time.monotonic() - start
                    synced = sum(1 for v in summary.get("synced_marketplaces", {}).values() if v)
                    updated = len(summary.get("updated_plugins", []))
                    available = len(summary.get("updates_available", []))
                    errors = len(summary.get("errors", []))

                    logger.info(
                        f"[PluginAutoUpdate] Completed in {duration:.1f}s: "
                        f"synced={synced} marketplaces, "
                        f"available={available} updates, "
                        f"updated={updated} plugins, "
                        f"errors={errors}"
                    )
                except Exception as e:
                    logger.error(f"[PluginAutoUpdate] Auto-update cycle failed: {e}")

            # Sleep until next interval, interruptible by reschedule.
            await _wait_for_reschedule(_jittered(interval_seconds))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[PluginAutoUpdate] Unexpected error in auto-update loop: {e}")
            # Back off on unexpected errors
            await asyncio.sleep(300)


def start_plugin_auto_update() -> asyncio.Task:
    """Start the plugin auto-update background task. Call from app lifespan."""
    global _auto_update_task
    _auto_update_task = asyncio.create_task(_plugin_auto_update_loop())
    logger.info("[PluginAutoUpdate] Background task started")
    return _auto_update_task


async def stop_plugin_auto_update(task: Optional[asyncio.Task] = None):
    """Stop the plugin auto-update background task. Call from app shutdown."""
    target = task or _auto_update_task
    if target and not target.done():
        target.cancel()
        try:
            await target
        except asyncio.CancelledError:
            pass
    logger.info("[PluginAutoUpdate] Background task stopped")


def reschedule_auto_update():
    """
    Signal the background task to re-read settings immediately.
    Called when auto-update settings are changed via the API.
    """
    if _reschedule_event:
        _reschedule_event.set()
