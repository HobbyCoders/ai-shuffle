"""
Plugin Auto-Update Background Task

Periodically syncs marketplaces and updates installed plugins.
Runs as an asyncio background task, similar to session cleanup and DB maintenance.

Settings are stored in ~/.fluid/plugins/auto_update_settings.json and can be
modified via the /api/v1/plugins/auto-update/settings endpoint.
"""

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level task reference for lifecycle management
_auto_update_task: Optional[asyncio.Task] = None
_reschedule_event: Optional[asyncio.Event] = None


async def _plugin_auto_update_loop():
    """
    Background loop that periodically runs the plugin auto-update cycle.

    Reads settings each iteration to pick up changes. The loop can be
    interrupted by setting the _reschedule_event, which causes it to
    re-read settings and adjust the sleep interval immediately.
    """
    global _reschedule_event
    _reschedule_event = asyncio.Event()

    # Initial delay to let the app fully start up
    await asyncio.sleep(60)

    while True:
        try:
            from app.core.plugin_service import get_plugin_service

            service = get_plugin_service()
            settings = service.get_auto_update_settings()

            if not settings.enabled:
                # Auto-update disabled — sleep for 5 minutes then re-check
                logger.debug("[PluginAutoUpdate] Disabled, sleeping 5m before re-check")
                try:
                    await asyncio.wait_for(_reschedule_event.wait(), timeout=300)
                    _reschedule_event.clear()
                except asyncio.TimeoutError:
                    pass
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
                        try:
                            await asyncio.wait_for(_reschedule_event.wait(), timeout=remaining)
                            _reschedule_event.clear()
                        except asyncio.TimeoutError:
                            pass
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

            # Sleep until next interval, interruptible by reschedule
            try:
                await asyncio.wait_for(_reschedule_event.wait(), timeout=interval_seconds)
                _reschedule_event.clear()
            except asyncio.TimeoutError:
                pass

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
