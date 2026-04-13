"""WebSocket session cleanup and lifecycle management."""

import asyncio
import logging

from app.core.live_session_store import live_session_store

logger = logging.getLogger(__name__)

# Track active chat sessions for interruption
_active_chat_sessions: dict[str, asyncio.Task] = {}
_active_sessions_lock = asyncio.Lock()

# Track pending cleanup tasks by session_id so they can be cancelled when a
# new query starts for the same session (prevents the race condition where an
# old cleanup fires and removes a newly-started streaming session from memory).
_pending_cleanup_tasks: dict[str, asyncio.Task] = {}


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
