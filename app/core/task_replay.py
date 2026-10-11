"""Repair pre-fix running task projections before event-feed hydration."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def reconcile_task_replay(session_id: str) -> None:
    """Repair only current assignments on the authoritative local worker.

    The admission lock excludes local SendMessage resumes. The DB transaction
    re-reads the projection inside that lock, fences ownership transfers and
    deduplicates concurrent readers/processes before appending a repair.
    """
    from app.core.config import settings
    from app.core.task_registry import task_registry
    from app.core.turn_scheduler import message_resume_lock
    from app.db.session_events import get_latest_task_updates
    from app.db.task_replay import repair_task_projection
    from app.network.runners import self_device_id

    try:
        updates = await asyncio.to_thread(get_latest_task_updates, session_id)
        for update in updates:
            task_id = update.get("task_id")
            if not task_id or update.get("state") not in ("running", "started"):
                continue
            async with message_resume_lock(task_id):
                # In-memory running work wins while its durable mirror catches up.
                task = task_registry.get_task(session_id, task_id)
                if task and task.get("status") in ("running", "started"):
                    continue
                await asyncio.to_thread(
                    repair_task_projection, session_id, task_id,
                    instance_id=settings.effective_instance_id,
                    device_id=self_device_id(),
                )
    except Exception:
        # A temporarily unavailable snapshot must not prevent opening a chat.
        logger.warning("Task replay reconciliation failed for %s", session_id, exc_info=True)
