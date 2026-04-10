"""
Resource Monitor - Periodic logging of system resource usage.

Logs resource counts (dict sizes, connection counts, pool sizes) at regular
intervals to help diagnose progressive slowdown issues. All metrics are
logged at INFO level with a consistent prefix for easy grep filtering.

Usage:
    from app.core.resource_monitor import start_resource_monitor, stop_resource_monitor

    # In lifespan:
    task = await start_resource_monitor(interval_seconds=300)
    yield
    await stop_resource_monitor(task)
"""

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Prefix all resource monitor logs for easy filtering
_PREFIX = "[ResourceMonitor]"

# Track monitor start time for uptime reporting
_start_time: float = 0.0

# Snapshot counter for trend tracking
_snapshot_count: int = 0


async def _collect_metrics() -> dict:
    """Collect all resource metrics from across the application."""
    metrics = {}

    # --- WebSocket tracking dicts ---
    try:
        from app.api.websocket import (
            _active_chat_sessions,
            _pending_cleanup_tasks,
        )
        metrics["ws_active_chat_sessions"] = len(_active_chat_sessions)
        metrics["ws_pending_cleanup_tasks"] = len(_pending_cleanup_tasks)
        # Check for done tasks that haven't been cleaned up
        done_cleanups = sum(1 for t in _pending_cleanup_tasks.values() if t.done())
        metrics["ws_done_cleanup_tasks"] = done_cleanups
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect websocket metrics: {e}")

    # --- Live Session Store ---
    try:
        from app.core.live_session_store import live_session_store
        metrics["live_sessions"] = len(live_session_store._sessions)
        metrics["live_session_locks"] = len(live_session_store._session_locks)
        # Count streaming vs ended sessions
        streaming = sum(1 for s in live_session_store._sessions.values() if s.is_streaming)
        ended = len(live_session_store._sessions) - streaming
        metrics["live_sessions_streaming"] = streaming
        metrics["live_sessions_ended"] = ended
        # Count total messages across all sessions
        total_msgs = sum(len(s.messages) for s in live_session_store._sessions.values())
        metrics["live_total_messages"] = total_msgs
        # Orphaned locks (locks without a corresponding session)
        orphaned_locks = len(live_session_store._session_locks) - len(live_session_store._sessions)
        metrics["live_orphaned_locks"] = max(0, orphaned_locks)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect live session metrics: {e}")

    # --- Session Pool ---
    try:
        from app.core.session_pool import get_session_pool, _session_pool, is_subprocess_alive
        from datetime import datetime as dt_cls, timezone as tz_cls
        if _session_pool is not None:
            pool = get_session_pool()
            metrics["pool_size"] = len(pool._pool)
            metrics["pool_max_size"] = pool._max_size
            # Count streaming sessions in pool
            pool_streaming = sum(1 for s in pool._pool.values() if s.is_streaming)
            metrics["pool_streaming"] = pool_streaming
            metrics["pool_connected"] = sum(1 for s in pool._pool.values() if s.is_connected)
            # Count dead subprocesses (connected flag set but process is gone)
            pool_dead = sum(
                1 for s in pool._pool.values()
                if s.is_connected and not is_subprocess_alive(s)
            )
            metrics["pool_dead_subprocesses"] = pool_dead
            # Track longest-running streaming session duration
            now_utc = dt_cls.now(tz_cls.utc)
            max_streaming_secs = 0.0
            for s in pool._pool.values():
                if s.is_streaming:
                    started = s.streaming_started_at or s.last_activity
                    duration = (now_utc - started).total_seconds()
                    max_streaming_secs = max(max_streaming_secs, duration)
            if max_streaming_secs > 0:
                metrics["pool_max_streaming_seconds"] = round(max_streaming_secs, 0)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect session pool metrics: {e}")

    # --- Sync Engine ---
    try:
        from app.core.sync_engine import sync_engine
        metrics["sync_connections"] = len(sync_engine._connections)
        metrics["sync_total_devices"] = sum(
            len(devices) for devices in sync_engine._connections.values()
        )
        metrics["sync_user_connections"] = len(sync_engine._user_connections)
        metrics["sync_session_locks"] = len(sync_engine._session_locks)
        metrics["sync_streaming_sessions"] = len(sync_engine._streaming_sessions)
        metrics["sync_session_user_map"] = len(sync_engine._session_user_map)
        metrics["sync_session_project_map"] = len(sync_engine._session_project_map)
        metrics["sync_session_worktree_map"] = len(sync_engine._session_worktree_map)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect sync engine metrics: {e}")

    # --- Automation Manager ---
    try:
        from app.core.automation_manager import automation_manager
        metrics["automation_running"] = len(automation_manager._running)
        # Check for done tasks still in _running
        done_automations = sum(1 for t in automation_manager._running.values() if t.done())
        metrics["automation_done_not_cleaned"] = done_automations
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect automation metrics: {e}")

    # --- Process-level metrics ---
    try:
        import psutil
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        metrics["process_rss_mb"] = round(mem_info.rss / (1024 * 1024), 1)
        metrics["process_threads"] = process.num_threads()
        # Count child processes (SDK subprocesses)
        children = process.children(recursive=True)
        metrics["child_processes"] = len(children)
    except ImportError:
        # psutil not available, skip process metrics
        pass
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect process metrics: {e}")

    # --- asyncio task count ---
    try:
        all_tasks = asyncio.all_tasks()
        metrics["asyncio_tasks"] = len(all_tasks)
    except Exception:
        pass

    return metrics


def _format_metrics(metrics: dict) -> str:
    """Format metrics into a readable log line."""
    parts = []
    for key, value in sorted(metrics.items()):
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _check_warnings(metrics: dict) -> list[str]:
    """Check metrics for concerning values and return warning messages."""
    warnings = []

    # Orphaned locks indicate cleanup failures
    orphaned = metrics.get("live_orphaned_locks", 0)
    if orphaned > 0:
        warnings.append(f"live_session_store has {orphaned} orphaned locks (sessions cleaned but locks remain)")

    # Done cleanup tasks indicate cleanup tasks that completed but weren't removed from dict
    done_cleanups = metrics.get("ws_done_cleanup_tasks", 0)
    if done_cleanups > 0:
        warnings.append(f"websocket has {done_cleanups} completed cleanup tasks still in tracking dict")

    # Done automations still in _running
    done_auto = metrics.get("automation_done_not_cleaned", 0)
    if done_auto > 0:
        warnings.append(f"automation_manager has {done_auto} completed tasks still in _running dict")

    # High memory usage
    rss = metrics.get("process_rss_mb", 0)
    if rss > 500:
        warnings.append(f"Process RSS is {rss}MB - investigate memory usage")

    # Too many asyncio tasks
    task_count = metrics.get("asyncio_tasks", 0)
    if task_count > 100:
        warnings.append(f"{task_count} asyncio tasks active - possible task leak")

    # Too many child processes
    children = metrics.get("child_processes", 0)
    pool_size = metrics.get("pool_size", 0)
    if children > pool_size + 2:  # Allow some slack
        warnings.append(f"{children} child processes but pool size is {pool_size} - possible subprocess leak")

    # Session pool at capacity
    pool_size = metrics.get("pool_size", 0)
    pool_max = metrics.get("pool_max_size", 3)
    if pool_size >= pool_max:
        warnings.append(f"Session pool at capacity ({pool_size}/{pool_max})")

    # Large message accumulation in live sessions
    total_msgs = metrics.get("live_total_messages", 0)
    live_sessions = metrics.get("live_sessions", 0)
    if live_sessions > 0 and total_msgs / max(1, live_sessions) > 500:
        warnings.append(f"Average {total_msgs // max(1, live_sessions)} messages per live session - possible message accumulation")

    # Dead subprocesses in pool (connected flag but process gone)
    pool_dead = metrics.get("pool_dead_subprocesses", 0)
    if pool_dead > 0:
        warnings.append(f"Session pool has {pool_dead} dead subprocess(es) - eviction will clean these")

    # Stuck streaming sessions (streaming for too long)
    max_stream_secs = metrics.get("pool_max_streaming_seconds", 0)
    if max_stream_secs > 600:  # 10 minutes
        warnings.append(f"Longest streaming session: {max_stream_secs:.0f}s - may be stuck")

    return warnings


async def _cleanup_evicted_sessions(session_ids: list[str], cleaned: dict, reason: str) -> None:
    """Clean up live_session_store and sync_engine state for evicted pool sessions.

    When session pool evicts a session (dead subprocess, stuck streaming, idle),
    the corresponding in-memory state in LiveSessionStore and SyncEngine must
    also be cleaned. Without this, message buffers and streaming flags accumulate.
    """
    for session_id in session_ids:
        try:
            from app.core.live_session_store import live_session_store
            if live_session_store.has_session(session_id):
                # End streaming first if still marked as streaming
                if live_session_store.is_streaming(session_id):
                    await live_session_store.end_streaming(
                        session_id, metadata={"cleanup_reason": reason}, interrupted=True
                    )
                # Then clean up the session from memory
                await live_session_store.cleanup_ended_session(session_id)
                cleaned_key = f"live_store_cleaned_{reason}"
                cleaned[cleaned_key] = cleaned.get(cleaned_key, 0) + 1
        except Exception as e:
            logger.debug(f"{_PREFIX} Failed to clean live session {session_id}: {e}")

        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.clear_streaming_state(session_id)
        except Exception as e:
            logger.debug(f"{_PREFIX} Failed to clear sync engine state for {session_id}: {e}")


async def _periodic_cleanup() -> dict:
    """Run periodic cleanup sweeps to prevent resource accumulation.

    Returns a dict of cleanup actions taken (for logging).
    """
    cleaned = {}

    # Sweep orphaned session locks
    try:
        from app.core.live_session_store import live_session_store
        count = await live_session_store.sweep_orphaned_locks()
        if count:
            cleaned["orphaned_locks"] = count
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to sweep orphaned locks: {e}")

    # Sweep stale sync engine maps
    try:
        from app.core.sync_engine import sync_engine
        count = await sync_engine.sweep_stale_maps()
        if count:
            cleaned["stale_sync_maps"] = count
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to sweep sync engine maps: {e}")

    # Clean up done tasks from _active_chat_sessions
    try:
        from app.api.websocket import _active_chat_sessions, _active_sessions_lock
        async with _active_sessions_lock:
            done_ids = [sid for sid, task in _active_chat_sessions.items() if task.done()]
            for sid in done_ids:
                del _active_chat_sessions[sid]
            if done_ids:
                cleaned["done_chat_tasks"] = len(done_ids)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to clean up done chat tasks: {e}")

    # Evict dead subprocesses from the session pool
    try:
        from app.core.session_pool import get_session_pool, _session_pool
        if _session_pool is not None:
            pool = get_session_pool()
            dead = await pool.evict_dead()
            if dead:
                cleaned["dead_pool_sessions"] = len(dead)
                # Also clean up live session store and sync engine state
                # for dead sessions — these hold in-memory message buffers
                # and streaming flags that won't be cleaned automatically
                await _cleanup_evicted_sessions(dead, cleaned, "dead")
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to evict dead pool sessions: {e}")

    # Evict idle sessions from the session pool (idle > 10 minutes)
    try:
        from app.core.session_pool import get_session_pool, _session_pool
        if _session_pool is not None:
            pool = get_session_pool()
            idle = await pool.evict_idle(max_idle_seconds=600)
            if idle:
                cleaned["idle_pool_sessions"] = len(idle)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to evict idle pool sessions: {e}")

    # Evict sessions stuck in streaming state for too long (30 minutes)
    # This is the critical safety net for the main leak: SDK subprocess hangs →
    # is_streaming stays True → evict_idle/evict_dead skip it → memory leak
    try:
        from app.core.session_pool import get_session_pool, _session_pool
        if _session_pool is not None:
            pool = get_session_pool()
            stuck = await pool.evict_stuck_streaming(max_streaming_seconds=1800)
            if stuck:
                cleaned["stuck_streaming_sessions"] = len(stuck)
                logger.warning(
                    f"{_PREFIX} Force-evicted {len(stuck)} stuck streaming session(s): {stuck}"
                )
                # Clean up live session store and sync engine for stuck sessions
                await _cleanup_evicted_sessions(stuck, cleaned, "stuck")
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to evict stuck streaming sessions: {e}")

    # Clean up done tasks from _pending_cleanup_tasks
    try:
        from app.api.websocket import _pending_cleanup_tasks
        done_ids = [sid for sid, task in _pending_cleanup_tasks.items() if task.done()]
        for sid in done_ids:
            _pending_cleanup_tasks.pop(sid, None)
        if done_ids:
            cleaned["done_cleanup_tasks"] = len(done_ids)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to clean done cleanup tasks: {e}")

    # Evict sessions whose worktree directory was deleted externally
    try:
        from app.core.session_pool import get_session_pool, _session_pool
        if _session_pool is not None:
            pool = get_session_pool()
            orphaned = await pool.evict_orphaned_worktree_sessions()
            if orphaned:
                cleaned["orphaned_worktree_sessions"] = len(orphaned)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to evict orphaned worktree sessions: {e}")

    # Clean up expired rate limiter windows and stale config cache
    try:
        from app.core.rate_limiter import rate_limiter
        rate_limiter.cleanup()
        rate_limiter.clear_cache()
        cleaned["rate_limiter_cleanup"] = True
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to clean rate limiter: {e}")

    return cleaned


async def _monitor_loop(interval_seconds: int):
    """Main monitoring loop."""
    global _snapshot_count, _start_time
    _start_time = time.monotonic()

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            _snapshot_count += 1
            uptime_minutes = round((time.monotonic() - _start_time) / 60, 1)

            metrics = await _collect_metrics()
            formatted = _format_metrics(metrics)
            logger.info(f"{_PREFIX} snapshot={_snapshot_count} uptime={uptime_minutes}m {formatted}")

            # Check for warning conditions
            warnings = _check_warnings(metrics)
            for warning in warnings:
                logger.warning(f"{_PREFIX} {warning}")

            # Run periodic cleanup sweeps
            cleaned = await _periodic_cleanup()
            if cleaned:
                logger.info(f"{_PREFIX} Periodic cleanup: {cleaned}")

        except asyncio.CancelledError:
            logger.info(f"{_PREFIX} Monitor stopped after {_snapshot_count} snapshots")
            raise
        except Exception as e:
            logger.error(f"{_PREFIX} Monitor error: {e}", exc_info=True)


async def start_resource_monitor(interval_seconds: int = 300) -> asyncio.Task:
    """Start the resource monitor background task.

    Args:
        interval_seconds: How often to collect and log metrics (default: 5 minutes)

    Returns:
        The background task (for cancellation in shutdown)
    """
    task = asyncio.create_task(_monitor_loop(interval_seconds))
    logger.info(f"{_PREFIX} Started (interval={interval_seconds}s)")
    return task


async def stop_resource_monitor(task: asyncio.Task):
    """Stop the resource monitor."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def log_snapshot_now(reason: str = "manual"):
    """Take an immediate snapshot (useful for on-demand diagnostics)."""
    global _snapshot_count
    _snapshot_count += 1
    uptime_minutes = round((time.monotonic() - _start_time) / 60, 1) if _start_time else 0

    metrics = await _collect_metrics()
    formatted = _format_metrics(metrics)
    logger.info(f"{_PREFIX} snapshot={_snapshot_count} reason={reason} uptime={uptime_minutes}m {formatted}")

    warnings = _check_warnings(metrics)
    for warning in warnings:
        logger.warning(f"{_PREFIX} {warning}")

    return metrics
