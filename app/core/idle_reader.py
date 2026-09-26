"""The between-turns drain reader for an escalated interrupt.

When an interrupt escalates and the wedged runner is KEPT (not evicted) because
background work is still running, its turn is left ``draining``. This reader
waits for that background work to resolve, then evicts the pooled client and
settles the turn ``aborted`` (drain mode, ``drain_turn_id`` set). Without it,
``reap_expired`` would requeue the user turn after ``execute()`` released its
run lease, and the scheduler would re-run it from scratch.

SINGLE-READER INVARIANT: the reader acquires the session's **reader token**
(:func:`app.core.session_runtime.get_reader_lock`) for its whole run, exactly as
a turn pump does — so a starting turn (which stops the idle reader AND takes the
token) can never touch the pooled client concurrently with this reader.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# How often drain mode re-checks whether the wedged client is safe to evict.
# Tests shrink it.
IDLE_READ_POLL_SECONDS = 2.0

# ---------------------------------------------------------------------------
# Eviction.
# ---------------------------------------------------------------------------

async def _evict_broken_client(session_id: str) -> None:
    """Evict a wedged pooled client: ``remove_dead`` if the subprocess is gone,
    else ``remove_broken`` (subprocess alive but its read loop is unusable).
    Never raises."""
    try:
        from app.core.session_pool import get_session_pool, is_subprocess_alive

        pool = get_session_pool()
        state = pool.peek(session_id)
        if state is None:
            return
        if is_subprocess_alive(state):
            await pool.remove_broken(session_id)
        else:
            await pool.remove_dead(session_id)
    except Exception:  # noqa: BLE001
        logger.debug("idle_reader: evict failed for %s", session_id, exc_info=True)


# ---------------------------------------------------------------------------
# The reader.
# ---------------------------------------------------------------------------

async def run_idle_reader(session_id: str, *, drain_turn_id: str) -> None:
    """Settle ``drain_turn_id`` once the session's wedged client is safe to evict.

    Holds the session's reader token for the whole run (single-reader invariant).
    Never raises out to the caller — a reader failure must never break the
    process.
    """
    from app.core import session_runtime

    # Mark the pooled client as held by an active between-turns reader BEFORE
    # anything can await, so the pool's LRU/idle eviction guards skip this
    # session for the reader's whole run (it reads via peek, so last_activity
    # never bumps). Cleared in the finally below, so a crashed/cancelled reader
    # can never leave it stuck. peek (not get): marking must not perturb LRU
    # order.
    pool_state = None
    try:
        from app.core.session_pool import get_session_pool

        pool_state = get_session_pool().peek(session_id)
    except Exception:  # noqa: BLE001
        pool_state = None
    if pool_state is not None:
        pool_state.idle_reader_active = True

    lock = session_runtime.get_reader_lock(session_id)
    try:
        async with lock:
            await _drain(session_id, drain_turn_id=drain_turn_id)
    except asyncio.CancelledError:
        # Stopped by SessionRuntime.stop_idle_reader (a turn is starting) — exit
        # quietly; the turn owns the client now.
        logger.debug("idle_reader: cancelled for %s", session_id)
        raise
    except Exception:  # noqa: BLE001
        logger.warning("idle_reader: run failed for %s", session_id, exc_info=True)
    finally:
        if pool_state is not None:
            pool_state.idle_reader_active = False
        session_runtime._drop_idle_reader_self(session_id)


async def _drain(session_id: str, *, drain_turn_id: str) -> None:
    """Settle a draining turn once its wedge resolves (reader token held)."""
    from app.core.session_pool import get_session_pool

    state = get_session_pool().peek(session_id)  # peek = no LRU promote
    if state is None or not state.adapter.is_alive() or state.is_streaming:
        logger.info(
            "idle_reader: no drainable client (state=%s alive=%s streaming=%s) [session=%s]",
            state is not None,
            bool(state and state.adapter.is_alive()),
            bool(state and state.is_streaming),
            session_id,
        )
        return
    from app.core.session_runtime import _should_evict_after_escalation

    while not _should_evict_after_escalation(session_id):
        await asyncio.sleep(IDLE_READ_POLL_SECONDS)
    logger.info(
        "idle_reader: drain wedge resolved — evicting + settling turn %s aborted "
        "[session=%s]", drain_turn_id, session_id,
    )
    await _evict_broken_client(session_id)
    await _settle_drained(session_id, drain_turn_id)


async def _settle_drained(session_id: str, turn_id: str) -> None:
    """Settle a drained turn ``aborted`` and fire the settlement callback.

    Idempotent: ``turns.settle_turn`` no-ops (returns ``None``) for an
    already-terminal turn, in which case the callback does NOT fire again.
    """
    from app.core import session_runtime
    from app.db import turns as turns_db

    try:
        settled = await asyncio.to_thread(
            turns_db.settle_turn, turn_id, "aborted", "drain_stale_result"
        )
    except Exception:  # noqa: BLE001
        logger.warning("idle_reader: settle_turn(aborted) failed for %s", turn_id, exc_info=True)
        return
    if settled is not None:
        await session_runtime._fire_on_turn_settled(session_id, turn_id, "aborted")
