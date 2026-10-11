"""Repair pre-fix running task projections before event-feed hydration."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def reconcile_task_replay(session_id: str) -> None:
    """Append missing terminals from registry truth, never revive or kill work.

    The admission lock serializes a repair with SendMessage resumes. A fresh
    read inside it prefers live state over the durable mirror, so an old killed
    snapshot cannot settle a newly running assignment. Only task projection
    rows are read, not the session's potentially huge transcript.
    """
    from app.core.task_registry import task_registry
    from app.core.turn_recorder import record_task_settle_frame
    from app.core.turn_scheduler import message_resume_lock
    from app.db.background_tasks import TERMINAL_STATUSES
    from app.db.session_events import get_latest_task_updates

    try:
        updates = await asyncio.to_thread(get_latest_task_updates, session_id)
        for update in updates:
            task_id = update.get("task_id")
            if not task_id or update.get("state") not in ("running", "started"):
                continue
            async with message_resume_lock(task_id):
                task = await asyncio.to_thread(
                    task_registry.get_task, session_id, task_id, durable=True,
                )
                if not task or task.get("status") not in TERMINAL_STATUSES:
                    continue
                await record_task_settle_frame(
                    session_id, task_id, task["status"], task.get("summary"),
                    tool_use_id=task.get("toolUseId") or task_id,
                )
    except Exception:
        # A temporarily unavailable snapshot must not prevent opening a chat.
        logger.warning("Task replay reconciliation failed for %s", session_id, exc_info=True)
