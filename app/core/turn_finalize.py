"""Durable end-of-turn side effects (V2 Phase 1).

Every turn-END path (the main chat_ws pump, the background query runner, the
unpark delivery, the drain-pump CLI-turn finalize) calls this to perform the
DURABLE writes a finished turn owes the session row:

1. Parked marker — if the turn ends with live background work (a running
   sub-agent or an SDK-backgrounded shell), record the pinning kind on the
   session row; otherwise clear it. This is what lets a later boot tell
   "parked awaiting unpark" apart from idle (boot reconcile force-clears
   stale markers, since no background work survives a restart).

2. Backstop clear of the "unpark pending" latch — a turn ending means no
   follow-up is mid-handoff for this session. Safe for the unpark path:
   ``_deliver_unpark_impl`` sets the latch AFTER its own end-of-turn call,
   so this never wipes a just-set latch.

Both writes are best-effort and never raise: a persistence bug must never
break turn teardown.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def persist_turn_end_state(session_id: str) -> None:
    """Persist the durable end-of-turn state for ``session_id``. Never raises."""
    # 1. Durable parked marker (set or clear).
    try:
        from app.core.task_registry import task_registry
        from app.db import database

        work = task_registry.get_active_background_work(session_id)
        kind = (work[0].get("kind") or "agent") if work else None
        await asyncio.to_thread(
            database.set_session_parked_task_kind, session_id, kind
        )
    except Exception:
        logger.debug("persisting parked_task_kind failed", exc_info=True)

    # 2. Backstop clear of the "unpark pending" latch (unconditional).
    try:
        from app.core.task_registry import task_registry

        task_registry.clear_unpark_pending(session_id)
    except Exception:
        logger.debug("task_registry.clear_unpark_pending failed", exc_info=True)
