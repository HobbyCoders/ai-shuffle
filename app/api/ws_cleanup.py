"""WebSocket session cleanup and lifecycle management."""

import asyncio
import logging

from app.core.permission_handler import permission_handler
from app.core.user_question_handler import user_question_handler

logger = logging.getLogger(__name__)

# Track active chat sessions for interruption
_active_chat_sessions: dict[str, asyncio.Task] = {}
_active_sessions_lock = asyncio.Lock()

# Track per-session "drain pump" tasks (see ChatWebSocketHandler._drain_notifications).
# A drain pump is a between-turns reader of a session's pooled SDK client that
# catches a slow background sub-agent's terminal TaskNotificationMessage AFTER the
# parent turn's receive_response() loop already returned at the parent ResultMessage.
#
# CRITICAL SINGLE-READER INVARIANT: at most ONE coroutine may read a given
# session's pooled SDK client at any time. The two readers are (R1) a user turn's
# receive_response() inside stream_to_websocket, and (R2) the drain pump's
# receive_messages(). The pump runs ONLY between turns (is_streaming == False) and
# MUST be stopped before any new turn begins — see stop_drain_pump().
_drain_pumps: dict[str, asyncio.Task] = {}


async def stop_drain_pump(session_id: str) -> bool:
    """Cancel and await any between-turns drain pump for a session.

    MUST be called at the start of every turn (before the turn connects to /
    queries the pooled SDK client) so the drain pump's receive_messages() is not
    reading the same client that the turn's receive_response() is about to read —
    that would violate the single-reader invariant.

    Idempotent: a no-op if no pump exists or it is already done. Swallows the
    pump's CancelledError so the caller never has to.

    Returns:
        True if a live pump was cancelled, False otherwise.
    """
    task = _drain_pumps.pop(session_id, None)
    if task is None:
        return False
    if task.done():
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug(f"Drain pump for session {session_id} raised on cancel", exc_info=True)
    logger.debug(f"Stopped drain pump for session {session_id}")
    return True


async def superseded_by_successor(session_id: str) -> bool:
    """True when a task OTHER than the caller is registered as the session's
    active turn in ``_active_chat_sessions``.

    A finishing turn's ``finally`` teardown calls this before its store-state
    cleanup heuristics (``has_session && is_streaming`` → ``cleanup_session``).
    Those heuristics exist to reap the turn's OWN dangling streaming state, but
    the session can be handed to a successor turn while the finally is still
    unwinding — the drain pump's unpark follow-up (``_deliver_unpark_impl``)
    fires ~1.5s after a background task's at-rest terminal notification, inside
    the finally's window (DB fetches + up-to-1s heartbeat-teardown stall,
    longest exactly when the origin socket died to a page refresh). The
    successor registers itself here BEFORE its ``start_streaming`` can run, so
    when the store reads as streaming AND this returns True, that streaming
    state is the successor's — cleaning it up would wipe the live session +
    task registry out from under the running follow-up, which then streams
    into a void (new messages appear only via the durable transcript on
    reload, /live reports not-streaming forever).
    """
    current = asyncio.current_task()
    async with _active_sessions_lock:
        registered = _active_chat_sessions.get(session_id)
    return registered is not None and registered is not current


async def stop_all_drain_pumps() -> None:
    """Cancel and await every drain pump. Called on shutdown."""
    for session_id in list(_drain_pumps.keys()):
        try:
            await stop_drain_pump(session_id)
        except Exception:
            logger.debug(f"Error stopping drain pump for {session_id}", exc_info=True)

# Module-level set tracking sessions with a pending ``interrupt_for_query``
# (two-phase interrupt + query). Consulted by the streaming query's finally
# block to skip automatic queue-drain processing so the in-flight interrupt
# path is not raced by a successor task. Shared across websocket.py (primary
# chat endpoint) and chat_ws_handler.py (HeadlessChatHandler / run_query).
_interrupt_pending_sessions: set[str] = set()

# Module-level set tracking sessions the user explicitly stopped. Consulted
# by the streaming query's finally block to skip queue-drain processing when
# the user intended to halt (auto-processing a queued message would surprise
# the user). Entry is cleared by the finally-block consumer.
_stopped_sessions: set[str] = set()

# Module-level set tracking sessions that are mid-drain — i.e. a queued
# message has been claimed and a successor query is being set up, but the
# task is not yet registered in ``_active_chat_sessions``. Guarded by
# ``_active_sessions_lock``. Without this reservation there is a TOCTOU
# window between "the session looks idle" and "the successor task is
# registered" during which a second drain (or an incoming query) can start
# a duplicate/competing query for the same session. Claimed/released by
# ``ChatWebSocketHandler._process_next_queued`` so only one drain proceeds
# at a time.
_draining_sessions: set[str] = set()


async def cleanup_session_handlers(session_id: str) -> None:
    """Clean up session-keyed data from global handler singletons.

    Called alongside live_session_store cleanup to prevent unbounded
    growth of _pending_requests, _session_rules, and _pending_questions
    dicts over the process lifetime.
    """
    try:
        await permission_handler.cancel_all_requests(session_id)
        permission_handler.clear_session_rules(session_id)
    except Exception:
        logger.debug(f"Permission handler cleanup failed for {session_id}", exc_info=True)
    try:
        await user_question_handler.cancel_all(session_id)
    except Exception:
        logger.debug(f"Question handler cleanup failed for {session_id}", exc_info=True)
    # Stop any between-turns drain pump so it can't outlive the session it was
    # reading (the pooled SDK client is being torn down alongside this cleanup).
    try:
        await stop_drain_pump(session_id)
    except Exception:
        logger.debug(f"Drain pump cleanup failed for {session_id}", exc_info=True)


# NOTE: Session cleanup is no longer driven by a standalone delayed timer.
# A session's live-store message buffer + task registry are now torn down on
# pool eviction (LRU or idle), wired via SessionPool.set_eviction_callback →
# resource_monitor._on_pool_eviction → _cleanup_evicted_sessions. This binds
# buffer lifetime to pool membership (ONE lifecycle) and inherently respects
# in-flight background sub-agents, because the pool refuses to evict a session
# with active background work (SessionPool._has_background_work). The old 30s
# _delayed_session_cleanup / schedule_delayed_cleanup / cancel_pending_cleanup
# machinery — which raced the drain pump and could tear a parked session down
# out from under a running sub-agent — has been removed.
