"""WebSocket session cleanup and lifecycle management."""

import asyncio
import logging

from app.core.live_session_store import live_session_store
from app.core.permission_handler import permission_handler
from app.core.user_question_handler import user_question_handler

logger = logging.getLogger(__name__)

# Track active chat sessions for interruption
_active_chat_sessions: dict[str, asyncio.Task] = {}
_active_sessions_lock = asyncio.Lock()

# Track pending cleanup tasks by session_id so they can be cancelled when a
# new query starts for the same session (prevents the race condition where an
# old cleanup fires and removes a newly-started streaming session from memory).
_pending_cleanup_tasks: dict[str, asyncio.Task] = {}

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
# a duplicate/competing query for the same session. Consulted by
# ``drain_next_queued_if_idle`` so only one drain proceeds at a time.
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


def cancel_pending_cleanup(session_id: str) -> bool:
    """Cancel any pending delayed cleanup for a session.

    Should be called when a new query starts for a session, to prevent
    a stale cleanup task from firing during the new query.

    Returns:
        True if a pending cleanup was cancelled, False if none existed.
    """
    task = _pending_cleanup_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
        logger.debug(f"Cancelled pending cleanup for session {session_id}")
        return True
    return False


async def _delayed_session_cleanup(session_id: str, delay_seconds: int = 30) -> None:
    """
    Clean up an ended session from live_session_store after a delay.

    This gives reconnecting clients time to retrieve messages before
    they're removed from memory. After the delay, messages must be
    retrieved from JSONL file instead.

    The task is tracked in ``_pending_cleanup_tasks`` so it can be cancelled
    by ``cancel_pending_cleanup()`` if a new query starts before the delay
    expires.
    """
    current_task = asyncio.current_task()
    try:
        await asyncio.sleep(delay_seconds)
        cleaned = await live_session_store.cleanup_ended_session(session_id)
        if cleaned:
            await cleanup_session_handlers(session_id)
        logger.info(f"Delayed cleanup for session {session_id}: {'removed' if cleaned else 'skipped (still streaming or not found)'}")
    except asyncio.CancelledError:
        logger.debug(f"Delayed cleanup cancelled for session {session_id} (new query started)")
    finally:
        # Only remove ourselves from the tracking dict if WE are still the tracked task.
        # Race condition fix: if schedule_delayed_cleanup() was called again for the same
        # session, the dict now points to the NEW task. Popping blindly would remove it,
        # leaving the new task untracked (unable to be cancelled by cancel_pending_cleanup).
        if _pending_cleanup_tasks.get(session_id) is current_task:
            _pending_cleanup_tasks.pop(session_id, None)


def schedule_delayed_cleanup(session_id: str, delay_seconds: int = 30) -> asyncio.Task:
    """Schedule a delayed cleanup for a session, replacing any existing one.

    This is the single entry-point for scheduling cleanup tasks. It ensures
    only ONE cleanup task is pending per session at any time — scheduling a
    new one automatically cancels the old one.

    Returns:
        The newly-created asyncio.Task.
    """
    # Cancel any existing cleanup task for this session
    cancel_pending_cleanup(session_id)

    task = asyncio.create_task(_delayed_session_cleanup(session_id, delay_seconds))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    _pending_cleanup_tasks[session_id] = task
    return task
