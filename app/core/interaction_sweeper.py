"""Pending-interaction expiry sweeper (V2 Phase 4, Q4-4).

A dedicated ~10s background loop that flips every ``pending_interactions`` row
past its TTL to ``expired`` and NOTIFYs each affected session so any in-process
waiter re-reads the durable row (and the FE tidies its stale modal on the next
resync). This is *belt* — the ``interaction_wait`` adapter ALSO honours its own
local ``expires_at`` deadline (asyncio timeout), so a dead sweeper never wedges
a turn (Q4-4). The sweeper is the *braces*: it turns a lapsed-but-unanswered row
durable so peer installs and reconnecting clients agree the prompt is gone.

Start/stop plumbing mirrors ``turn_claim`` exactly:

* started flag-gated (``settings.v2_placement``) from
  ``startup.install_v2_turns_wiring``; flag OFF ⇒ the module is never imported
  and nothing sweeps (rows created while ON are stranded-but-inert, cleaned by a
  later ON boot — see plan A6);
* cancelled at teardown from ``startup.shutdown_services`` via a ``sys.modules``
  lookup, so a flag-OFF process stays import-free there too.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# Cleanup cadence (Q4-4: "dedicated standalone ~10s sweeper loop").
SWEEP_INTERVAL_SECONDS = 10.0

_loop_task: Optional[asyncio.Task] = None


async def sweep_once() -> int:
    """Expire every overdue pending interaction; NOTIFY each affected session.

    Returns the number of rows expired. Never raises: a transient DB error on
    one pass must not kill the loop (correctness rides the waiters' local
    deadlines regardless).
    """
    if not settings.v2_placement:
        return 0
    try:
        from app.db import interactions

        rows = await asyncio.to_thread(interactions.expire_due_interactions_detailed)
    except Exception:  # noqa: BLE001
        logger.warning("interaction_sweeper: expiry pass failed", exc_info=True)
        return 0
    if not rows:
        return 0

    # Wake one waiter per affected session so it re-reads the now-``expired``
    # row instead of sitting to its own local deadline.
    from app.core import interaction_wait

    for session_id in {row["session_id"] for row in rows}:
        try:
            await asyncio.to_thread(interaction_wait.notify_changed, session_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "interaction_sweeper: notify for %s failed (non-fatal)",
                session_id,
                exc_info=True,
            )

    # Append a durable ``interaction.expired`` (reason=ttl) per swept row AFTER
    # the notify above (isolation invariant: emission is last and its helper
    # swallows every failure, so it can never wedge the sweep). Each swept row
    # won the guarded UPDATE's RETURNING, so this is exactly-once per row.
    from app.core import interaction_events

    for row in rows:
        request_id = (row.get("payload") or {}).get("request_id")
        await interaction_events.emit_interaction_expired(
            row["session_id"], row["id"], request_id, "ttl"
        )

    logger.debug("interaction_sweeper: expired %d interaction(s)", len(rows))
    return len(rows)


async def sweep_loop() -> None:
    """Run :func:`sweep_once` every ``SWEEP_INTERVAL_SECONDS`` until cancelled."""
    logger.info(
        "interaction_sweeper: loop started (every %ss)",
        int(SWEEP_INTERVAL_SECONDS),
    )
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await sweep_once()
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001 — the loop must survive anything.
            logger.warning("interaction_sweeper: pass failed", exc_info=True)
    logger.info("interaction_sweeper: loop stopped")


def start() -> None:
    """Start the sweeper loop on the running event loop (idempotent).

    Called from ``startup.install_v2_turns_wiring`` (inside the lifespan
    startup, so a loop is running). The task ref is module-held so it is never
    GC'd; ``shutdown_services`` cancels it via :func:`stop`.
    """
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning(
            "interaction_sweeper.start(): no running event loop — not started"
        )
        return
    _loop_task = loop.create_task(sweep_loop())


async def stop() -> None:
    """Cancel the sweeper loop and wait for it to exit (idempotent).

    No-op when the loop was never started (flag OFF, or a test process that
    never called :func:`start`).
    """
    global _loop_task
    task = _loop_task
    _loop_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — teardown must never raise.
        logger.debug("interaction_sweeper: loop raised during shutdown", exc_info=True)
