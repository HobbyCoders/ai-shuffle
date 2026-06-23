"""
Session lifecycle queries: state inspection, interrupt, active/streaming lists.

Extracted from query_engine.py to isolate session lifecycle management.
"""

import logging
from typing import Optional

from app.core.session_pool import SessionState, get_session_pool

logger = logging.getLogger(__name__)


def get_session_state(session_id: str) -> Optional[SessionState]:
    """Get the active session state for a session ID."""
    return get_session_pool().peek(session_id)


async def interrupt_session(session_id: str) -> bool:
    """Interrupt an active streaming session.

    This calls the SDK's interrupt() method which signals the Claude API
    to stop processing. This is more reliable than just cancelling the
    asyncio task, which only takes effect at the next await point.
    """
    # Use peek() to avoid promoting to MRU — interrupt is a read-only check
    state = get_session_pool().peek(session_id)
    if not state:
        logger.warning(f"No active session found for {session_id}")
        return False

    if not state.is_connected:
        logger.warning(f"Session {session_id} is not connected")
        return False

    # Set interrupt flag first - this is checked in the streaming loop as a failsafe
    state.interrupt_requested = True

    # Don't check is_streaming too strictly - there might be race conditions
    # and it's better to try to interrupt anyway
    logger.debug(f"Attempting to interrupt session {session_id} (streaming={state.is_streaming})")

    try:
        await state.client.interrupt()
        # Don't set is_streaming = False here. The streaming loop will set it
        # when it actually finishes processing final messages. Setting it
        # prematurely causes a race where the loop still processes messages
        # but the session appears idle.
        logger.debug(f"Successfully interrupted session {session_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to interrupt session {session_id}: {e}", exc_info=True)
        return False


async def stop_session_task(session_id: str, task_id: str) -> dict:
    """Stop ONE background task (sub-agent OR Monitor shell) in a session.

    Two kinds of task surface in the UI's ``activeTasks`` strip:

    - **shell** (Monitor subprocess): terminated directly via its tracked
      process and marked ``killed`` in the registry immediately.
    - **agent** (background sub-agent): stopped through the SDK client's
      ``stop_task()``. The CLI then emits a ``task_notification`` with status
      ``stopped`` which the streaming loop maps to ``killed`` in the registry —
      so the task drops off the next ``/live`` poll without us pre-marking it.

    Returns ``{"ok": bool, "kind": ..., "detail"?: ...}``. Never raises.
    """
    # 1) Monitor shell task — terminate the subprocess directly.
    from app.mcp.builtin_tools import stop_background_shell_task
    shell = await stop_background_shell_task(task_id, session_id=session_id)
    if shell is not None:
        return {"ok": True, "kind": "shell", **shell}

    # 2) Background sub-agent — ask the SDK to stop it.
    state = get_session_pool().peek(session_id)
    if not state:
        return {"ok": False, "kind": "agent", "detail": "session not active"}
    if not state.is_connected or not state.client:
        return {"ok": False, "kind": "agent", "detail": "session not connected"}

    try:
        await state.client.stop_task(task_id)
        logger.debug(f"Requested SDK stop_task for {task_id} in session {session_id}")
        return {"ok": True, "kind": "agent", "task_id": task_id}
    except Exception as e:
        logger.error(
            f"Failed to stop task {task_id} in session {session_id}: {e}",
            exc_info=True,
        )
        return {"ok": False, "kind": "agent", "detail": str(e)}


def get_active_sessions() -> list:
    """Get list of active session IDs (connected clients)"""
    return get_session_pool().active_session_ids()


def get_streaming_sessions() -> list:
    """Get list of currently streaming session IDs"""
    return get_session_pool().streaming_session_ids()
