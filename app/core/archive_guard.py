"""Archived chats are read-only transcripts until the owner restores them (#1822).

The rule is read in three kinds of place:

* ``SessionRuntime.execute``, the one point every turn passes before it starts,
  drops a queued turn of an archived session whatever its source. That is the
  guard; everything else only keeps work from being queued that could never run.
* Wake producers (task completions, restart recovery, scheduled wake-ups, peer
  messages, ``turn_manager`` submits) check before they write a turn row.
* Message and resume tools refuse an archived target by name.

Archiving stops the chat's running and parked work through the same calls the
Stop button makes (:func:`stop_work`), so nothing is left behind to wake it.
Restoring (``status`` back to ``active``) lifts every check at once and
releases the Stop latch archiving set (:func:`release_after_restore`).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

ARCHIVED_STATUS = "archived"
#: ``turns.stop_reason`` of a queued turn dropped because its chat is archived.
ARCHIVED_STOP_REASON = "archived"


class SessionArchived(ValueError):
    """Raised when work is submitted to an archived session."""

    def __init__(self, session_id: str, message: Optional[str] = None) -> None:
        self.session_id = session_id
        super().__init__(message or (
            "This chat is archived. Restore it in the app before sending it anything."
        ))


def is_archived_row(row: Optional[Mapping[str, Any]]) -> bool:
    return bool(row) and row.get("status") == ARCHIVED_STATUS


def is_archived(session_id: Optional[str]) -> bool:
    """Whether ``session_id`` is archived. Blocking DB read: call off the loop.

    Fails open: a lookup error must not lose a wake for a chat that is not
    archived, and ``SessionRuntime.execute`` checks again before any turn runs.
    """
    if not session_id:
        return False
    try:
        from app.db import sessions

        return is_archived_row(sessions.get_session(session_id))
    except Exception:  # noqa: BLE001
        logger.debug("archive check failed for %s", session_id, exc_info=True)
        return False


async def is_archived_async(session_id: Optional[str]) -> bool:
    import asyncio

    return await asyncio.to_thread(is_archived, session_id)


def refusal(label: str, action: str) -> str:
    """The reason a tool gives for refusing an archived target."""
    return (f"{label} is archived, so it cannot {action}. Archived chats are "
            "read-only until the owner restores them in the app.")


def log_dropped(session_id: str, source: Optional[str], detail: str = "") -> None:
    """Record a wake that was not started because its chat is archived."""
    logger.warning(
        "Archived session %s: dropped a %s wake%s. It stays archived until the "
        "owner restores it.",
        session_id, source or "unknown", f" ({detail})" if detail else "",
    )


def drop_queued_turns(session_id: str, turn_id: Optional[str] = None) -> int:
    """Abort ``turn_id`` and every other queued turn of an archived session.

    Blocking DB writes: call off the loop. The rows stay in history with
    ``stop_reason='archived'``; a row that raced to live is left alone by the
    guarded transition. Returns how many rows were aborted.
    """
    from app.db import turns as turns_db

    ids = [turn_id] if turn_id else []
    ids += [row["id"] for row in turns_db.get_queued_turns(session_id) or []
            if row.get("id") and row["id"] != turn_id]
    dropped = 0
    for tid in ids:
        if turns_db.remove_queued_turn(tid, ARCHIVED_STOP_REASON) is not None:
            dropped += 1
    return dropped


def release_after_restore(session_id: str) -> None:
    """Make a restored chat fully usable: release the Stop latch archiving set.

    Without this a restored chat still refused peer messages and wake-ups as
    "stopped by the user" until the owner happened to type into it. Restoring
    is the owner's explicit decision to bring the chat back. Never raises.
    """
    try:
        from app.core.task_registry import task_registry

        if task_registry.clear_user_stop(session_id):
            logger.info("Restored session %s: released its stop latch", session_id)
    except Exception:  # noqa: BLE001
        logger.warning("Restored session %s: releasing its stop latch failed",
                       session_id, exc_info=True)


async def stop_work(session_id: str) -> None:
    """Stop an archived chat's work the way the Stop button does.

    The durable running -> interrupting flip (which signals a live runtime),
    then the user-stop sweep: it latches the session, deletes armed wake-ups,
    aborts queued turns, settles parked rows and kills sub-agents, workers and
    Monitor shells. Runs even with no live turn, so parked work stops too.
    Shells ignore STOP_KEEPS_MONITOR_SHELLS here: an archived chat keeps nothing
    running. Never raises: the archive is already committed, and the execute
    guard still refuses anything this missed.
    """
    try:
        from app.core import turn_manager
        from app.core.query_session_lifecycle import stop_all_background_tasks

        await turn_manager.interrupt_async(session_id, "stop", stop_background=True)
        await stop_all_background_tasks(
            session_id, user_stop=True, respect_shell_preference=False,
        )
        logger.info("Archived session %s: stopped its running and parked work", session_id)
    except Exception:  # noqa: BLE001
        logger.warning("Archived session %s: stopping its work failed", session_id,
                       exc_info=True)
