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
        from app.api.websocket import _active_chat_sessions
        metrics["ws_active_chat_sessions"] = len(_active_chat_sessions)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect websocket metrics: {e}")

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

    # --- Database connection pool (PostgreSQL) ---
    # Distinct from the SDK "session pool" above. db_pool_in_use is the
    # exhaustion/leak indicator: if it sits at db_pool_max the next DB call
    # raises "connection pool exhausted".
    try:
        from app.db.base import pool_stats
        db_stats = pool_stats()
        if db_stats is not None:
            metrics["db_pool_in_use"] = db_stats["in_use"]
            metrics["db_pool_idle"] = db_stats["idle"]
            metrics["db_pool_max"] = db_stats["max"]
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to collect DB pool metrics: {e}")

    # --- Sync Engine ---
    try:
        from app.core.sync_engine import sync_engine
        metrics["sync_connections"] = len(sync_engine._connections)
        metrics["sync_total_devices"] = sum(
            len(devices) for devices in sync_engine._connections.values()
        )
        metrics["sync_user_connections"] = len(sync_engine._user_connections)
        metrics["sync_session_locks"] = len(sync_engine._session_locks)
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

    # Session pool at capacity. pool_max_size is reported by _collect_metrics
    # straight off the live pool; the fallback reads the CONFIGURED size rather
    # than a hardcoded constant, which is what previously went stale when the
    # default moved (it still said 3 long after the default was 8). If neither
    # is available the pool is not up yet — there is nothing to warn about.
    pool_size = metrics.get("pool_size", 0)
    pool_max = metrics.get("pool_max_size")
    if pool_max is None:
        try:
            from app.core.config import settings
            pool_max = getattr(settings, "sdk_session_pool_size", None)
        except Exception:
            pool_max = None
    if pool_max and pool_size >= pool_max:
        warnings.append(f"Session pool at capacity ({pool_size}/{pool_max})")

    # DB connection pool near/at exhaustion. Warn early (>=80%) so a slow
    # slot leak is visible before it surfaces as "connection pool exhausted".
    db_in_use = metrics.get("db_pool_in_use")
    db_max = metrics.get("db_pool_max", 0)
    if db_in_use is not None and db_max and db_in_use >= 0.8 * db_max:
        warnings.append(
            f"DB connection pool near capacity ({db_in_use}/{db_max}) - "
            f"possible slot leak or insufficient db_pool_max"
        )

    # Dead subprocesses in pool (connected flag but process gone)
    pool_dead = metrics.get("pool_dead_subprocesses", 0)
    if pool_dead > 0:
        warnings.append(f"Session pool has {pool_dead} dead subprocess(es) - eviction will clean these")

    # Long-streaming sessions (OBSERVE-ONLY — the monitor does NOT evict these;
    # a long stream may be a legitimately busy multi-subagent orchestration).
    max_stream_secs = metrics.get("pool_max_streaming_seconds", 0)
    if max_stream_secs > 600:  # 10 minutes
        warnings.append(
            f"Longest streaming session: {max_stream_secs:.0f}s "
            f"(observe-only — not evicted; may be a long orchestration)"
        )

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
    """Tear down per-session registry/shell/handler state for evicted pool sessions.

    When the session pool evicts a session (dead subprocess or idle), the
    per-session task registry and Monitor shells must also be cleaned up.
    Without this, registry rows and shell subprocesses accumulate. (V2: there
    is no in-memory message buffer to purge — render state lives in the
    durable event log, and busy truth is the runs table.)

    NOTE: this is NEVER called for live/streaming sessions — the monitor no
    longer force-evicts streaming sessions on a timer (see report_long_streaming).
    """
    # ``lru_force`` (hard-cap eviction of a busy session) and ``dead`` (subprocess
    # died) are the only reasons that can remove a session PARKED on in-flight
    # background work — normal ``lru``/``idle`` eviction skip parked sessions. In
    # those two cases the pooled SDK client was just destroyed, so the parked
    # work can never deliver its terminal notification: the background-work
    # guard below would refuse teardown FOREVER, leaking the registry and
    # stranding the UI's task strip. Force the teardown so the orphaned tasks
    # are reaped to killed.
    force = reason in ("lru_force", "dead")
    for session_id in session_ids:
        try:
            from app.core import streaming_status
            from app.core.turn_finalize import persist_turn_end_state
            from app.core.task_registry import task_registry

            # Durable end-of-turn writes first if still busy (runs-table read) —
            # replaces the old in-memory end_streaming call. The stale run
            # lease itself self-heals via TTL.
            if streaming_status.has_active_run(session_id):
                await persist_turn_end_state(session_id)

            if force:
                # The SDK client is gone — in-flight background work can never
                # finish on its own. Reap it to "killed" so the taskbar shows
                # the failure (the registry retains killed rows for a short
                # window past clear_session — see task_registry.get_visible).
                # Shutdown-origin attribution: a force reap during server
                # teardown is a restart consequence, not a task failure.
                from app.core import startup as _startup
                from app.core.task_registry import (
                    SERVER_SHUTDOWN_SUMMARY,
                    SERVER_SHUTDOWN_REASON,
                )
                _shutdown_reap = getattr(_startup, "is_shutting_down", False)
                reaped = 0
                for rec in task_registry.get_active(session_id):
                    if _shutdown_reap:
                        task_registry.complete_task(
                            session_id, rec["id"], "killed",
                            summary=SERVER_SHUTDOWN_SUMMARY,
                            reason=SERVER_SHUTDOWN_REASON,
                        )
                    else:
                        task_registry.complete_task(session_id, rec["id"], "killed")
                    reaped += 1
                if reaped:
                    logger.warning(
                        f"{_PREFIX} Force-cleanup of session {session_id}: reaped "
                        f"{reaped} orphaned background task(s) to killed "
                        f"(SDK client destroyed; work cannot complete)"
                    )
            if not force and (
                task_registry.has_active_background_work(session_id)
                or task_registry.has_running_monitor_shell(session_id)
            ):
                # A session parked on SDK background work keeps its registry
                # until the work completes (a later turn end, eviction, or
                # sweep clears it) — this only DEFERS removal, never blocks it.
                # Monitor shells defer too: they are OS processes independent
                # of the pooled client (they deliberately don't PIN the pool),
                # and their completion watcher wakes the parked parent through
                # a durable turn — but only while the registry still holds the
                # task's OWN-lane mark. Clearing + reaping here killed a live
                # CI watcher 3.5 minutes into a routine idle eviction and left
                # its task "running" forever (the terminal frame was suppressed
                # as not-own work once the marks were wiped).
                logger.info(
                    f"{_PREFIX} Skipping registry teardown of session "
                    f"{session_id}: active background task(s)"
                )
            else:
                # Drop the registry bucket, then reap any Monitor shell
                # subprocesses this session spawned (they are excluded from the
                # background-work guard, so without an explicit reap they
                # strand: the subprocess keeps running and its reader leaks).
                task_registry.clear_session(session_id)
                # Drop the session's last-turn-recorder entry too — otherwise
                # _last_turn_recorders grows one entry per dead session.
                try:
                    from app.core.turn_recorder import pop_last_turn_recorder
                    pop_last_turn_recorder(session_id)
                except Exception:
                    logger.debug(
                        f"{_PREFIX} last-turn-recorder pop failed", exc_info=True,
                    )
                try:
                    from app.mcp.builtin_tools import reap_session_background_shells
                    reap_session_background_shells(session_id)
                except Exception:
                    logger.debug(
                        f"{_PREFIX} background shell reap scheduling failed",
                        exc_info=True,
                    )
                cleaned_key = f"registry_cleaned_{reason}"
                cleaned[cleaned_key] = cleaned.get(cleaned_key, 0) + 1
        except Exception as e:
            logger.debug(f"{_PREFIX} Failed to clean evicted session {session_id}: {e}")

        try:
            from app.api.ws_cleanup import cleanup_session_handlers
            await cleanup_session_handlers(session_id)
        except Exception as e:
            logger.debug(f"{_PREFIX} Failed to clean session handlers for {session_id}: {e}")


async def _on_pool_eviction(session_id: str, reason: str) -> None:
    """Eviction callback registered on the session pool (see set_eviction_callback).

    Fired from SessionPool._do_evict the moment an LRU or idle eviction removes a
    session, so the task registry + Monitor shells are torn down on the SAME
    event that ends the pooled SDK client — one lifecycle, no separate timer.
    Dead-eviction does NOT route here (it bypasses _do_evict); the periodic
    sweep cleans those via _cleanup_evicted_sessions.
    """
    await _cleanup_evicted_sessions([session_id], {}, reason)


async def _periodic_cleanup() -> dict:
    """Run periodic cleanup sweeps to prevent resource accumulation.

    Returns a dict of cleanup actions taken (for logging).
    """
    cleaned = {}

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
                # Also tear down registry/shell/handler state for dead
                # sessions — it won't be cleaned automatically
                await _cleanup_evicted_sessions(dead, cleaned, "dead")
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to evict dead pool sessions: {e}")

    # Settle orphaned durable background-task rows (periodic sibling of the
    # boot reconcile): a non-terminal row this install owns whose task the
    # in-memory registry no longer knows can never settle on its own — the UI
    # chip it renders would show "running" until the next restart.
    try:
        from app.core.startup_reconcile import settle_stale_background_task_rows
        stale_tasks = await settle_stale_background_task_rows()
        if stale_tasks:
            cleaned["stale_background_task_rows"] = stale_tasks
    except Exception as e:
        logger.debug(f"{_PREFIX} Stale background-task sweep failed: {e}")

    # Evict idle sessions from the session pool.
    #
    # BELT, not braces: the pool's own idle watchdog (started below in
    # start_resource_monitor) evicts ON the deadline, so by the time this
    # 5-minute sweep runs there is normally nothing left to do. It stays because
    # it costs one dict scan and covers the case where the watchdog task died.
    # No ``max_idle_seconds`` argument — passing one would force the FLAT legacy
    # timeout and silently defeat the tiered policy.
    try:
        from app.core.session_pool import get_session_pool, _session_pool
        if _session_pool is not None:
            pool = get_session_pool()
            idle = await pool.evict_idle()
            if idle:
                cleaned["idle_pool_sessions"] = len(idle)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to evict idle pool sessions: {e}")

    # OBSERVE-ONLY: report (do NOT evict) sessions streaming for a long time.
    #
    # We deliberately do NOT force-terminate live/streaming sessions on a timer.
    # Elapsed streaming time cannot distinguish a legitimately busy long
    # orchestration (e.g. one orchestrator driving 30+ subagents overnight) from
    # a genuinely hung session. The previous timer-based eviction SIGTERM'd a
    # healthy orchestrator mid-run in production: it killed the SDK subprocess,
    # discarded the 161-message in-memory transcript, truncated the Postgres
    # mirror, and orphaned the subagent child trees — the watchdog created the
    # very leak it existed to detect. The SDK owns query lifecycle and is the
    # ONLY thing that ends a live session (completion or user interrupt);
    # genuinely dead subprocesses are handled by evict_dead() + the dead-tree
    # reaper below. So here we only emit a warning observation.
    try:
        from app.core.session_pool import get_session_pool, _session_pool
        if _session_pool is not None:
            pool = get_session_pool()
            long_streaming = pool.report_long_streaming(warn_after_seconds=600)
            for session_id, secs in long_streaming:
                logger.warning(
                    f"{_PREFIX} session {session_id} streaming for {secs:.0f}s "
                    f"(observe-only — NOT evicting; SDK owns lifecycle)"
                )
            if long_streaming:
                cleaned["long_streaming_observed"] = len(long_streaming)
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to report long-streaming sessions: {e}")

    # Reap PROVABLY-DEAD process trees: orphaned descendants (e.g. subagent node
    # CLIs) of SDK subprocesses that have ALREADY terminated. This is process
    # hygiene only — it operates exclusively on recorded descendant snapshots of
    # subprocesses that already exited and never signals a live session.
    try:
        from app.core.session_pool import reap_terminated_process_trees
        reaped = reap_terminated_process_trees()
        if reaped:
            cleaned["reaped_orphan_processes"] = reaped
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to reap terminated process trees: {e}")

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


async def reconcile_orphans_once(max_age_days: Optional[int] = None) -> list:
    """Run the orphaned-session reconciler off the event loop.

    Heals sessions whose ``sdk_session_id`` was left NULL by a mid-turn crash but
    whose transcript is intact in the session store (blank history in
    postgres_only mode). Each heal is logged at ERROR by the reconciler so the
    operator is alerted that a crash occurred. Never raises — a reconcile failure
    must not take down the monitor or startup.
    """
    try:
        from app.core.orphan_reconciler import reconcile_orphaned_sessions
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: reconcile_orphaned_sessions(max_age_days=max_age_days),
        )
    except Exception as e:
        logger.error(f"{_PREFIX} orphan reconcile failed: {e}", exc_info=True)
        return []


# Monotonic timestamp of the last orphan-reconcile pass (0 = never run here).
_last_orphan_reconcile: float = 0.0


def _orphan_reconcile_interval() -> float:
    """Configured seconds between periodic orphan-reconcile passes (0 = off)."""
    try:
        from app.core.config import settings
        return float(getattr(settings, "orphan_reconcile_interval_seconds", 3600))
    except Exception:
        return 3600.0


def _orphan_reconcile_due(now: Optional[float] = None) -> bool:
    """True when the periodic orphan reconcile should run on this monitor tick.

    Returns False forever when the interval is configured to 0 (the startup pass
    still runs — this only gates the periodic one).
    """
    global _last_orphan_reconcile
    interval = _orphan_reconcile_interval()
    if interval <= 0:
        return False
    now = time.monotonic() if now is None else now
    if _last_orphan_reconcile and (now - _last_orphan_reconcile) < interval:
        return False
    _last_orphan_reconcile = now
    return True


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

            # Self-heal sessions orphaned by a mid-turn crash. Bounded to recent
            # sessions so the sweep stays cheap; the unbounded backfill runs once
            # at startup. Logs an ERROR per heal (recovery implies a prior crash).
            #
            # F6: this is a RECOVERY sweep, not a hot path, so it runs on its own
            # (hourly by default) cadence rather than every monitor tick. New
            # orphans are prevented at the source by the persist-on-init write,
            # the startup pass covers the whole backlog, and the reconciler
            # itself now memoizes inconclusive ids — so a 5-minute cadence bought
            # nothing but a repeated aggregation over the session store.
            if _orphan_reconcile_due():
                await reconcile_orphans_once(max_age_days=14)

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
    # Wire pool eviction → registry/shell cleanup. This makes LRU + idle
    # eviction (both funnel through SessionPool._do_evict) tear down the
    # per-session task registry and Monitor shells on the same event that ends
    # the pooled SDK client — one lifecycle, no separate timer.
    try:
        from app.core.session_pool import get_session_pool
        get_session_pool().set_eviction_callback(_on_pool_eviction)
    except Exception as e:
        logger.error(f"{_PREFIX} Failed to register pool eviction callback: {e}")

    # Start the pool's deadline-driven idle watchdog (F1). Idle eviction used to
    # ride THIS task's 5-minute tick, which added up to 5 minutes of latency on
    # top of an already long flat timeout — ~35 minutes of parked subprocesses in
    # the worst case. The watchdog wakes exactly on the next eviction deadline
    # (and never while the pool is empty), so RAM comes back on time. The sweep
    # in _periodic_cleanup stays as a belt in case the watchdog task dies.
    try:
        from app.core.session_pool import get_session_pool
        get_session_pool().start_idle_watchdog()
    except Exception as e:
        logger.error(f"{_PREFIX} Failed to start pool idle watchdog: {e}")

    task = asyncio.create_task(_monitor_loop(interval_seconds))
    logger.info(f"{_PREFIX} Started (interval={interval_seconds}s)")
    return task


async def stop_resource_monitor(task: asyncio.Task):
    """Stop the resource monitor (and the pool idle watchdog it started)."""
    try:
        from app.core.session_pool import _session_pool
        if _session_pool is not None:
            await _session_pool.stop_idle_watchdog()
    except Exception as e:
        logger.debug(f"{_PREFIX} Failed to stop pool idle watchdog: {e}")
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
