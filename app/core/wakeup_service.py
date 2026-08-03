"""Durable wakeups + the idle-at-edge watchdog (#598).

Two problems this module solves, both facets of the edge-of-context stall where
the bundled CLI cuts a turn silently at the compaction boundary and the session
parks forever because no next turn arrives:

1. **Durable wakeups.** ``ScheduleWakeup`` used to be a bare in-process
   ``asyncio.create_task(sleep(...))`` — an app restart dropped every pending
   wake. :func:`schedule` now persists the wake to ``pending_wakeups`` first, and
   :func:`rehydrate_pending` re-arms this install's rows on boot. Firing is a
   *claim* (delete the row, then run) so a rehydrated timer and a peer install
   cannot double-fire.

2. **Idle-at-edge watchdog.** :func:`scan_once` (run on a timer by
   :func:`watchdog_loop`) finds sessions sitting IDLE above the context-window
   edge and arms a synthetic nudge through the SAME wakeup machinery. Firing a
   real turn is what makes the CLI run its deferred compaction — the
   belt-and-braces backstop that covers the cases the opt-in ScheduleWakeup and
   the #557 auto-continue latch miss.

The actual wake re-fires the prompt through
``account_failover.execute_query_with_failover`` — the single seam every
non-streaming caller uses — so a wake is a genuine new turn (CLI-owned, per the
"CLI owns wakes" invariant), not a new dispatch lane.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.db import wakeups as wakeups_db

logger = logging.getLogger(__name__)

# The nudge a watchdog wake fires. Honest + explicitly labeled so it never reads
# as a human instruction when it renders in chat. Its purpose is simply to BE a
# new turn (which triggers the CLI's deferred compaction); the wording steers a
# session with nothing left to do toward a quiet stop.
WATCHDOG_NUDGE_PROMPT = (
    "[auto-continue] This session was idle near the context-window limit; a "
    "compaction has now been triggered. If you had unfinished work, continue it "
    "exactly where you left off. If everything is already complete, stop after a "
    "brief acknowledgement — do not invent new work."
)

# Overdue-wake rehydrate stagger: on boot, wakes whose fire_at already passed are
# re-armed a few seconds apart (capped) instead of all firing at once, so a long
# outage does not stampede the query engine at startup.
_REHYDRATE_STAGGER_SECONDS = 3.0
_REHYDRATE_STAGGER_CAP_SECONDS = 60.0

# Live references to in-flight wake timer tasks (asyncio keeps only weak refs).
_timers: "set[asyncio.Task]" = set()

# In-memory per-session cooldown for the watchdog: session_id -> monotonic ts of
# the last nudge armed. Bounds re-nudging a session whose compaction did not drop
# it back under the fraction. Reset on process restart (at most one extra nudge
# per session after a restart — acceptable for a backstop).
_watchdog_cooldown: "dict[str, float]" = {}

_watchdog_task: "Optional[asyncio.Task]" = None


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Firing + arming
# ---------------------------------------------------------------------------

async def _fire(row: Dict[str, Any]) -> None:
    """Claim and fire one wakeup: delete its row, then re-fire the prompt."""
    wakeup_id = row.get("id")
    session_id = row.get("session_id")
    claimed = await asyncio.to_thread(wakeups_db.claim_wakeup, wakeup_id)
    if not claimed:
        logger.info(
            "wakeup %s (session %s) already claimed/fired — skipping",
            wakeup_id, session_id,
        )
        return
    from app.core import account_failover

    logger.info(
        "wakeup firing: id=%s session=%s source=%s",
        wakeup_id, session_id, row.get("source"),
    )
    await account_failover.execute_query_with_failover(
        prompt=row.get("prompt"),
        profile_id=row.get("profile_id"),
        project_id=row.get("project_id"),
        session_id=session_id,
    )


def _arm(row: Dict[str, Any], delay_seconds: float) -> None:
    """Arm an in-process timer that fires ``row`` after ``delay_seconds``."""
    async def _run() -> None:
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await _fire(row)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a wake failure must never leak.
            logger.exception(
                "wakeup task failed id=%s session=%s",
                row.get("id"), row.get("session_id"),
            )

    task = asyncio.create_task(_run(), name=f"wakeup-{row.get('session_id')}")
    _timers.add(task)
    task.add_done_callback(_timers.discard)


async def schedule(
    *,
    session_id: str,
    profile_id: Optional[str],
    project_id: Optional[str],
    prompt: str,
    delay_seconds: float,
    reason: Optional[str] = None,
    source: str = "schedule_wakeup",
) -> Dict[str, Any]:
    """Persist a wakeup and arm its in-process timer. Returns the persisted row.

    Durable-first: the row is written BEFORE the timer is armed, so a crash in the
    window loses nothing (boot rehydrate re-arms it). ``fire_at`` is an absolute
    unix epoch so rehydrate can compute the remaining delay.
    """
    fire_at = _now_epoch() + max(0.0, float(delay_seconds))
    row = await asyncio.to_thread(
        wakeups_db.create_wakeup,
        id=f"wake-{uuid.uuid4().hex}",
        session_id=session_id,
        prompt=prompt,
        fire_at=fire_at,
        profile_id=profile_id,
        project_id=project_id,
        reason=reason,
        source=source,
        instance_id=settings.effective_instance_id,
    )
    _arm(row, max(0.0, float(delay_seconds)))
    logger.info(
        "wakeup scheduled: id=%s session=%s source=%s fire_in=%.0fs",
        row["id"], session_id, source, max(0.0, float(delay_seconds)),
    )
    return row


async def rehydrate_pending() -> int:
    """Re-arm this install's persisted wakeups on boot. Returns the count.

    Overdue wakes (``fire_at`` in the past — the common restart case) are armed
    with a small staggered delay rather than firing simultaneously.
    """
    try:
        rows = await asyncio.to_thread(
            wakeups_db.get_pending_wakeups, settings.effective_instance_id
        )
    except Exception:  # noqa: BLE001 — rehydrate must never block boot.
        logger.exception("wakeup rehydrate failed to read pending rows")
        return 0

    now = _now_epoch()
    overdue = 0
    for i, row in enumerate(rows):
        try:
            fire_at = float(row.get("fire_at") or 0.0)
        except (TypeError, ValueError):
            fire_at = 0.0
        remaining = fire_at - now
        if remaining <= 0:
            remaining = min(
                _REHYDRATE_STAGGER_SECONDS * overdue, _REHYDRATE_STAGGER_CAP_SECONDS
            )
            overdue += 1
        _arm(row, remaining)
    if rows:
        logger.info(
            "wakeup rehydrate: re-armed %d pending wakeup(s) (%d overdue) for "
            "install %s", len(rows), overdue, settings.effective_instance_id,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Idle-at-edge watchdog
# ---------------------------------------------------------------------------

def _cooldown_active(session_id: str, now_monotonic: float) -> bool:
    last = _watchdog_cooldown.get(session_id)
    if last is None:
        return False
    return (now_monotonic - last) < settings.idle_watchdog_cooldown_seconds


async def scan_once() -> List[str]:
    """One idle-at-edge sweep. Returns the session ids nudged this pass.

    For each structurally-eligible candidate (idle, active, snapshot present, no
    live turn, no pending wakeup — :func:`db.wakeups.idle_edge_candidates`) that
    is over the configured context fraction and off cooldown, arm ONE synthetic
    nudge through the durable wakeup machinery.
    """
    from app.core import compact_continue

    nudged: List[str] = []
    try:
        candidates = await asyncio.to_thread(
            wakeups_db.idle_edge_candidates, settings.effective_instance_id
        )
    except Exception:  # noqa: BLE001 — the loop must survive a bad scan.
        logger.exception("idle-watchdog: candidate scan failed")
        return nudged

    now_monotonic = time.monotonic()
    fraction_gate = settings.idle_watchdog_context_fraction
    for cand in candidates:
        session_id = cand.get("id")
        if not session_id:
            continue
        if _cooldown_active(session_id, now_monotonic):
            continue
        usage = compact_continue._persisted_usage(session_id)
        frac = compact_continue.usage_fraction(usage)
        if frac is None or frac < fraction_gate:
            continue
        logger.info(
            "idle-watchdog: session %s idle at %.1f%% context (>= %.0f%%) — "
            "arming synthetic nudge",
            session_id, frac * 100.0, fraction_gate * 100.0,
        )
        try:
            await schedule(
                session_id=session_id,
                profile_id=cand.get("profile_id"),
                project_id=cand.get("project_id"),
                prompt=WATCHDOG_NUDGE_PROMPT,
                delay_seconds=0.0,
                reason="idle-at-edge watchdog (#598)",
                source="idle_watchdog",
            )
        except Exception:  # noqa: BLE001 — one bad arm must not abort the sweep.
            logger.exception(
                "idle-watchdog: failed to arm nudge for session %s", session_id
            )
            continue
        _watchdog_cooldown[session_id] = now_monotonic
        nudged.append(session_id)
    return nudged


async def watchdog_loop() -> None:
    """Periodic idle-at-edge sweep (started by ``startup.start_background_tasks``)."""
    interval = max(30, int(settings.idle_watchdog_interval_seconds))
    # Let boot settle (and rehydrate finish) before the first sweep.
    await asyncio.sleep(min(30, interval))
    while True:
        try:
            nudged = await scan_once()
            if nudged:
                logger.info(
                    "idle-watchdog: armed %d nudge(s) this sweep", len(nudged)
                )
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            logger.exception("idle-watchdog sweep failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
