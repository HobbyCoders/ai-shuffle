"""Pending-interaction expiry sweeper (V2 Phase 4, Q4-4).

A ~10s periodic pass that flips every ``pending_interactions`` row
past its TTL to ``expired`` and NOTIFYs each affected session so any in-process
waiter re-reads the durable row (and the FE tidies its stale modal on the next
resync). This is *belt* — the ``interaction_wait`` adapter ALSO honours its own
local ``expires_at`` deadline (asyncio timeout), so a dead sweeper never wedges
a turn (Q4-4). The sweeper is the *braces*: it turns a lapsed-but-unanswered row
durable so peer installs and reconnecting clients agree the prompt is gone.

Start/stop plumbing mirrors ``turn_claim`` exactly:

* started from ``startup.install_v2_turns_wiring``;
* cancelled at teardown from ``startup.shutdown_services`` via a ``sys.modules``
  lookup.

Since F5 the "~10s background loop" is not a loop of its own: ``start()``
registers the expiry pass on the shared sweep driver in
:mod:`app.core.turn_claim`, which runs it every ``SWEEP_INTERVAL_SECONDS`` while
peer installs exist and at the stretched single-install cadence otherwise.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Cleanup cadence (Q4-4: "dedicated standalone ~10s sweeper loop").
SWEEP_INTERVAL_SECONDS = 10.0

async def sweep_once() -> int:
    """Expire every overdue pending interaction; NOTIFY each affected session.

    Returns the number of rows expired. Never raises: a transient DB error on
    one pass must not kill the loop (correctness rides the waiters' local
    deadlines regardless).
    """
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


def start() -> None:
    """Register the expiry pass on the shared sweep driver (idempotent).

    Called from ``startup.install_v2_turns_wiring`` (inside the lifespan
    startup, so a loop is running).

    F5: this used to own a private 10s asyncio task. It is now a
    :class:`app.core.turn_claim.SweepPass`, so it shares one timer with the
    other cross-install loops — same 10s cadence whenever the install registry
    shows a peer, stretched to the single-install cadence when it does not.

    SAFETY of the stretch. The sweeper is *belt*: the ``interaction_wait``
    adapter honours its own local ``expires_at`` deadline, so a late sweep can
    never wedge a turn (Q4-4). What it adds is making the lapse DURABLE so peer
    installs and reconnecting clients agree the prompt is gone — and on an
    install with no peers the only reader is this process and its own browsers,
    which re-read the row on resync. So the stretch delays a tidy-up by at most
    one cadence and changes no correctness property. The moment a peer install
    appears, the tight 10s cadence returns.
    """
    from app.core import turn_claim

    turn_claim.register_pass(
        "interaction_expiry", sweep_once, SWEEP_INTERVAL_SECONDS
    )
    logger.info(
        "interaction_sweeper: registered (every %ss)",
        int(SWEEP_INTERVAL_SECONDS),
    )


def kick() -> None:
    """Run the expiry pass on the next event-loop tick (latency escape hatch)."""
    from app.core import turn_claim

    turn_claim.kick("interaction_expiry")


async def stop() -> None:
    """Unregister the expiry pass (idempotent).

    The driver itself belongs to ``turn_claim`` and is stopped there; dropping
    the pass here keeps a same-process restart clean.
    """
    try:
        from app.core import turn_claim

        turn_claim.unregister_pass("interaction_expiry")
    except Exception:  # noqa: BLE001 — teardown must never raise.
        logger.debug(
            "interaction_sweeper: unregister failed during shutdown", exc_info=True
        )
