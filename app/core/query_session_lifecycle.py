"""
Session lifecycle queries: state inspection, interrupt, active/streaming lists.

Extracted from query_engine.py to isolate session lifecycle management.
"""

import asyncio
import logging
from typing import Optional

from app.core.session_pool import SessionState, get_session_pool

logger = logging.getLogger(__name__)

# A busy CLI (e.g. deep in extended thinking) can leave the interrupt control
# request unanswered indefinitely; an unbounded await here hangs the two-phase
# "Send now" handshake past the frontend's confirm window and loses the
# user's message. Bounded so the caller can fall back to drain/force-cancel.
_SDK_INTERRUPT_TIMEOUT_SECONDS = 5.0


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
        await asyncio.wait_for(state.client.interrupt(), timeout=_SDK_INTERRUPT_TIMEOUT_SECONDS)
        # Don't set is_streaming = False here. The streaming loop will set it
        # when it actually finishes processing final messages. Setting it
        # prematurely causes a race where the loop still processes messages
        # but the session appears idle.
        logger.debug(f"Successfully interrupted session {session_id}")
        return True
    except asyncio.TimeoutError:
        # interrupt_requested stays set — the streaming loop's failsafe check
        # can still end the turn, and the caller's drain/force-cancel handles
        # the truly wedged case.
        logger.warning(
            f"Interrupt control request for session {session_id} unanswered after "
            f"{_SDK_INTERRUPT_TIMEOUT_SECONDS}s (CLI busy or wedged)"
        )
        return False
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

    "Ghost" tasks — ones already terminated by outside forces (idle eviction,
    process death, session teardown) but still parked ``running`` in the
    registry with no live client left to stop — are REAPED here: marked
    ``killed`` so they drop off the next ``/live`` poll, and reported as a
    successful stop. Without this the "x" hits a dead session, returns
    ``ok: false``, and the UI strip is left showing a task that can never be
    cleared (its button stuck disabled).

    Returns ``{"ok": bool, "kind": ..., "detail"?: ...}``. Never raises.
    """
    from app.core.task_registry import task_registry

    # Idempotent / stale-UI guard: if the registry already holds this task in a
    # terminal state, the strip is just showing a stale row — report success so
    # the "x" clears it on the next poll instead of erroring.
    existing = task_registry.get_task(session_id, task_id)
    if existing is not None and existing.get("status") != "running":
        return {
            "ok": True,
            "kind": existing.get("kind") or "agent",
            "task_id": task_id,
            "detail": "already finished",
        }

    # 1) Monitor shell task — terminate the subprocess directly.
    from app.mcp.builtin_tools import stop_background_shell_task
    shell = await stop_background_shell_task(task_id, session_id=session_id)
    if shell is not None:
        return {"ok": True, "kind": "shell", **shell}

    # A registry shell task with no tracked subprocess is a ghost (the shell
    # already exited without the registry being told). Reap it rather than
    # mis-routing a shell id to the SDK's agent-task stop (which would error).
    if existing is not None and existing.get("kind") == "shell":
        task_registry.complete_task(session_id, task_id, "killed")
        return {
            "ok": True,
            "kind": "shell",
            "task_id": task_id,
            "detail": "reaped (shell gone)",
        }

    # 2) Background sub-agent — ask the SDK to stop it.
    state = get_session_pool().peek(session_id)
    if not state or not state.is_connected or not state.client:
        # Outside forces tore down the live session / SDK client, but the task
        # can still be parked "running" in the registry — a ghost the UI shows
        # forever with no client left to stop it. Reap it so the close button
        # works: mark it killed (drops off the next /live poll) and report
        # success.
        if existing is not None:
            task_registry.complete_task(session_id, task_id, "killed")
            return {
                "ok": True,
                "kind": existing.get("kind") or "agent",
                "task_id": task_id,
                "detail": "reaped (session gone)",
            }
        # The registry never knew the task and there is no live session — there
        # is nothing to reap and nothing the UI is showing. Report the dead
        # session honestly (distinguishing gone-from-pool vs disconnected).
        if not state:
            return {"ok": False, "kind": "agent", "detail": "session not active"}
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
