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

# Track RSS over time for growth rate detection
_rss_history: list[tuple[float, float]] = []  # (monotonic_time, rss_mb)
_RSS_HISTORY_MAX = 50  # Keep last ~4 hours of samples at 5-min interval


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
    # Prefer psutil (cross-platform, and gives child-process RSS so a leak in the
    # bundled Node CLI / spawned Chrome subprocesses is visible — not just our own
    # heap). Fall back to /proc on Linux so a container without psutil still reports
    # at least its own RSS instead of silently dropping all memory metrics.
    try:
        import psutil
        process = psutil.Process(os.getpid())
        rss_mb = round(process.memory_info().rss / (1024 * 1024), 1)
        metrics["process_rss_mb"] = rss_mb
        metrics["process_threads"] = process.num_threads()
        # Child processes (SDK Node subprocesses, any spawned Chrome, etc.) — count
        # AND sum their RSS, since a child-process leak never shows in our own RSS.
        child_rss_mb = 0.0
        children = process.children(recursive=True)
        for child in children:
            try:
                child_rss_mb += child.memory_info().rss / (1024 * 1024)
            except Exception:
                continue  # child may have exited mid-iteration
        metrics["child_processes"] = len(children)
        metrics["child_rss_mb"] = round(child_rss_mb, 1)
        metrics["tree_rss_mb"] = round(rss_mb + child_rss_mb, 1)
    except ImportError:
        # psutil not installed — fall back to /proc for our own RSS (Linux only).
        try:
            with open(f"/proc/{os.getpid()}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # VmRSS is reported in kB.
                        metrics["process_rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                        break
        except Exception:
            pass  # non-Linux without psutil: no RSS available
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect process metrics: {e}")

    # --- RSS growth rate (detect monotonic memory growth) ---
    rss_mb = metrics.get("process_rss_mb", 0)
    if rss_mb > 0:
        now = time.monotonic()
        _rss_history.append((now, rss_mb))
        # Trim old entries
        while len(_rss_history) > _RSS_HISTORY_MAX:
            _rss_history.pop(0)
        # Calculate growth rate if we have enough samples (at least 10 min of data)
        if len(_rss_history) >= 3:
            oldest_time, oldest_rss = _rss_history[0]
            elapsed_hours = (now - oldest_time) / 3600
            if elapsed_hours > 0.1:  # At least ~6 minutes of data
                growth_mb_per_hour = (rss_mb - oldest_rss) / elapsed_hours
                metrics["rss_growth_mb_per_hour"] = round(growth_mb_per_hour, 2)

    # --- asyncio task count ---
    try:
        all_tasks = asyncio.all_tasks()
        metrics["asyncio_tasks"] = len(all_tasks)
    except Exception:
        pass

    # --- Event loop responsiveness ---
    try:
        loop = asyncio.get_running_loop()
        lag_start = time.monotonic()
        await asyncio.sleep(0)  # Yield to event loop
        lag = time.monotonic() - lag_start
        metrics["event_loop_lag_ms"] = round(lag * 1000, 1)
    except Exception:
        pass

    # --- File descriptors ---
    try:
        fd_path = f"/proc/{os.getpid()}/fd"
        if os.path.isdir(fd_path):
            metrics["open_file_descriptors"] = len(os.listdir(fd_path))
    except Exception:
        pass

    # --- Git caches ---
    try:
        from app.core.git_cache import _git_cache
        metrics["git_cache_entries"] = len(_git_cache._data)
    except Exception:
        pass
    try:
        from app.api.git_helpers import _repo_record_cache
        metrics["repo_record_cache_entries"] = len(_repo_record_cache)
    except Exception:
        pass
    try:
        from app.core.git_native import _fsmonitor_configured
        metrics["fsmonitor_tracked_paths"] = len(_fsmonitor_configured)
    except Exception:
        pass

    # --- Python GC stats (detect reference cycle pressure) ---
    try:
        import gc
        counts = gc.get_count()
        metrics["gc_gen0"] = counts[0]
        metrics["gc_gen1"] = counts[1]
        metrics["gc_gen2"] = counts[2]
        # gc.get_stats() is O(1), unlike gc.get_objects() which is O(n)
        stats = gc.get_stats()
        metrics["gc_gen2_collections"] = stats[2].get("collections", 0)
        metrics["gc_gen2_uncollectable"] = stats[2].get("uncollectable", 0)
    except Exception:
        pass

    # --- Git thread pool ---
    try:
        from app.api.git_helpers import _git_executor
        metrics["git_pool_queue_depth"] = _git_executor._work_queue.qsize()
        metrics["git_pool_threads_active"] = len(_git_executor._threads)
    except Exception:
        pass

    # --- Handler accumulation tracking ---
    try:
        from app.core.permission_handler import permission_handler
        metrics["perm_pending_sessions"] = len(permission_handler._pending_requests)
        metrics["perm_session_rules"] = len(permission_handler._session_rules)
    except Exception:
        pass
    try:
        from app.core.user_question_handler import user_question_handler
        metrics["question_pending_sessions"] = len(user_question_handler._pending_questions)
    except Exception:
        pass
    try:
        from app.core.rate_limiter import rate_limiter
        metrics["rate_limiter_windows"] = len(rate_limiter._windows)
        metrics["rate_limiter_config_cache"] = len(rate_limiter._config_cache)
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

    # RSS growth rate (sustained memory growth indicates leak)
    rss_growth = metrics.get("rss_growth_mb_per_hour", 0)
    if rss_growth > 10:
        warnings.append(f"RSS growing at {rss_growth:.1f} MB/hour - sustained memory leak likely")

    # Event loop lag
    lag = metrics.get("event_loop_lag_ms", 0)
    if lag > 100:
        warnings.append(f"Event loop lag is {lag}ms - asyncio may be congested")

    # File descriptor growth
    fds = metrics.get("open_file_descriptors", 0)
    if fds > 500:
        warnings.append(f"{fds} open file descriptors - possible FD leak")

    # GC uncollectable objects (reference cycles that can't be freed)
    uncollectable = metrics.get("gc_gen2_uncollectable", 0)
    if uncollectable > 0:
        warnings.append(f"{uncollectable} uncollectable objects in GC gen2 - reference cycle leak")

    # Git pool saturation
    git_queue = metrics.get("git_pool_queue_depth", 0)
    if git_queue > 4:
        warnings.append(f"Git thread pool queue depth is {git_queue} - operations are backing up")

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

        try:
            from app.api.ws_cleanup import cleanup_session_handlers
            await cleanup_session_handlers(session_id)
        except Exception as e:
            logger.debug(f"{_PREFIX} Failed to clean session handlers for {session_id}: {e}")


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

    # Clean up done automation tasks from _running
    try:
        from app.core.automation_manager import automation_manager
        async with automation_manager._lock:
            done_auto_ids = [aid for aid, task in automation_manager._running.items() if task.done()]
            for aid in done_auto_ids:
                del automation_manager._running[aid]
            if done_auto_ids:
                cleaned["done_automation_tasks"] = len(done_auto_ids)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to clean automation tasks: {e}")

    # Purge expired git cache entries
    try:
        from app.core.git_cache import _git_cache
        count = _git_cache.purge_expired()
        if count:
            cleaned["git_cache_expired"] = count
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to purge git cache: {e}")

    # Purge expired repo record cache entries
    try:
        from app.api.git_helpers import _repo_record_cache, _REPO_RECORD_TTL
        import time as _time
        now = _time.monotonic()
        expired_keys = [k for k, (_, ts) in _repo_record_cache.items() if (now - ts) > _REPO_RECORD_TTL * 2]
        for k in expired_keys:
            del _repo_record_cache[k]
        if expired_keys:
            cleaned["repo_record_cache_expired"] = len(expired_keys)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to purge repo record cache: {e}")

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
