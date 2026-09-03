"""Durable end-of-turn side effects (V2 Phase 1).

Every turn-END path (the main chat_ws pump, the background query runner, the
unpark delivery, the drain-pump CLI-turn finalize) calls this to perform the
DURABLE write a finished turn owes the session row:

1. Parked marker — if the turn ends with live background work (a running
   sub-agent or an SDK-backgrounded shell), record the pinning kind on the
   session row; otherwise clear it. This is what lets a later boot tell
   "parked awaiting unpark" apart from idle (boot reconcile force-clears
   stale markers, since no background work survives a restart).

2. Summary sidecar flush — the ``session_store_summaries`` fold is COALESCED
   (finding B4): while a turn streams, at most one fold per ~2s runs inside
   the append path, so the sidecar is normally a little behind when the turn
   ends and stays behind until the next summary READER flushes it. Folding
   here makes it current the moment the turn completes, which is when the
   session list is most likely to be looked at next.

This write is best-effort and never raises: a persistence bug must never
break turn teardown.

Ordering: the parked marker goes FIRST because it is the load-bearing write
(unpark and boot reconcile read it) and it is what a caller waiting on this
coroutine actually needs. The summary flush runs after it, off-loop in a
worker thread, and its failure is swallowed — the fold watermark lives in the
DATABASE, so a skipped flush costs nothing but staleness: the next append or
the next reader folds from exactly where this one stopped.
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

    # 1b. Persist any PR the agent opened/merged this turn so the sidepanel
    # updates without waiting for a GitHub poll. Fire-and-forget: gh must not
    # delay turn teardown.
    try:
        from app.core.session_pr_state import reconcile_session_pr

        task = asyncio.create_task(reconcile_session_pr(session_id))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
    except Exception:
        logger.debug("scheduling PR reconcile failed", exc_info=True)

    # 2. Bring the coalesced session-summary fold current for this session.
    #
    # Deliberately LAST: every user-visible part of the turn is already done
    # (the recorder emitted turn.completed before the runtime called us) and
    # the durable marker above — the only write a caller depends on — has
    # landed. This is a no-op with no DB connection at all when nothing is
    # pending for this session, and one short fold transaction when there is,
    # so the added cost is bounded by ~2s of appends, not by transcript size.
    #
    # Never fatal: the fold watermark is in Postgres, so a failed flush is
    # picked up verbatim by the next append or the next summary reader.
    try:
        from app.core import pg_session_store

        await asyncio.to_thread(
            pg_session_store.flush_summary_folds, session_id
        )
    except Exception:
        logger.debug(
            "turn-end summary fold flush failed for %s", session_id, exc_info=True
        )
