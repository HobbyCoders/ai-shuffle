"""Snooze: a settled chat that goes back to Active on its own.

A snoozed chat is ``is_active = FALSE`` with ``snoozed_until`` set. The wake
time lives on the shared membership record (``session_active``), so a snooze
set on one worker shows and wakes on every worker. Settling or activating
clears it.

Three things wake a snoozed chat, all through the same shared membership
write (:func:`app.network.session_active.write_wake`):

* the wake loop (:func:`maintain_snoozes`) when its time arrives;
* the chat asking the owner for something (a permission or approval prompt,
  an ask-user question or a private-access request), via
  :func:`wake_for_attention`;
* the owner sending it a message, which already reactivates a settled chat
  (``turn_manager._reactivate_for_send``); activating always clears a snooze.
  A spoken request (``voice_delegation``) keeps a plain settled chat settled
  but ends a snooze, and so does the owner's own answer to a question card
  (``user_question_handler``); an Away-mode answer or a dismissal does not.

An archived row (a legacy status) is never woken.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: How often the wake loop looks for due snoozes. The sidebar regroups a row
#: locally at its exact time, so this only bounds the server-side delay.
SWEEP_SECONDS = 15.0
#: A worker that does not run the chat waits this long past the wake time
#: before waking it itself. The running worker normally wakes it first; this
#: covers it being offline without every worker racing to sign the same wake.
PEER_GRACE = timedelta(minutes=2)
#: Rows woken per pass; the next pass picks up any remainder.
_BATCH = 200


def parse(value: Any) -> Optional[datetime]:
    """A stored or wire wake time as an aware UTC datetime; None when unset."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        # Naive values only come from a database session in UTC.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def canonical(value: Any) -> Optional[str]:
    """The signed, stored spelling: ISO-8601 UTC to the second."""
    moment = parse(value)
    if moment is None:
        return None
    return moment.replace(microsecond=0).isoformat()


def validate_future(value: datetime, *, now: Optional[datetime] = None) -> str:
    """Canonical wake time for a new snooze; ValueError unless it is ahead of now."""
    if value.tzinfo is None:
        raise ValueError("snoozed_until needs a timezone")
    stamp = canonical(value)
    assert stamp is not None
    if parse(stamp) <= (now or datetime.now(timezone.utc)):
        raise ValueError("snoozed_until must be in the future")
    return stamp


def runs_work(session: Optional[dict]) -> bool:
    """Whether authorized work already running for this chat may continue.

    True for an active chat and for a snoozed one: a snooze only defers the
    chat in the sidebar, it never stops its running work. A plain settled or
    archived chat stops it. Stop, deadline and budget checks stay separate.
    """
    if not session:
        return False
    if session.get("is_active"):
        return True
    return bool(session.get("snoozed_until")) and session.get("status") != "archived"


def _snoozed_chat(session_id: str) -> Optional[str]:
    """The snoozed chat a needs-you event in ``session_id`` belongs to, if any.

    A delegated worker's prompt surfaces in the chat that started it, so the
    nearest snoozed ancestor counts too.
    """
    from app.db.base import get_db
    with get_db() as conn:
        cur = conn.cursor()
        current: Optional[str] = session_id
        for _ in range(8):
            if not current:
                return None
            cur.execute("SELECT id, snoozed_until, is_active, parent_session_id FROM sessions WHERE id=%s",
                        (current,))
            row = cur.fetchone()
            if row is None:
                return None
            if not row["is_active"] and row["snoozed_until"] is not None:
                return row["id"]
            current = row["parent_session_id"]
    return None


def due_snoozes(now: Optional[datetime] = None) -> list[dict]:
    """Snoozed chats this worker should wake now, soonest first.

    The worker that runs a chat wakes it at its time; any other worker only
    after :data:`PEER_GRACE`, for when the running worker is offline.
    """
    from app.db.base import get_db
    from app.network import control
    now = now or datetime.now(timezone.utc)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM sessions WHERE snoozed_until IS NOT NULL AND snoozed_until <= %s "
            "AND COALESCE(is_active, FALSE) = FALSE AND COALESCE(status, '') <> 'archived' "
            "ORDER BY snoozed_until LIMIT %s",
            (canonical(now), _BATCH))
        rows = [dict(r) for r in cur.fetchall()]
    due = []
    for row in rows:
        at = parse(row["snoozed_until"])
        if at is None:
            continue
        if control.owner_of(row) is not None and at + PEER_GRACE > now:
            continue
        due.append({"id": row["id"], "snoozed_until": canonical(at)})
    return due


async def wake(session_id: str, *, expected: Optional[str] = None, reason: str) -> bool:
    """Move a snoozed chat back to Active and tell open sidebars. True if it moved.

    ``expected`` is the wake time the caller saw: a newer snooze, unsnooze or
    settle made since then wins and nothing changes.
    """
    from app.db.async_database import run_sync
    from app.network import session_active
    row = await run_sync(session_active.write_wake, session_id, expected)
    if row is None:
        return False
    logger.info("Woke snoozed session %s (%s)", session_id, reason)
    await session_active._broadcast(row, session_id)
    return True


async def wake_for_attention(session_id: Optional[str], *, reason: str = "needs the owner") -> bool:
    """The chat now needs the owner (or the owner answered it): end its snooze
    early. Never raises."""
    if not session_id:
        return False
    try:
        from app.db.async_database import run_sync
        chat = await run_sync(_snoozed_chat, session_id)
        if chat is None:
            return False
        return await wake(chat, reason=reason)
    except Exception:  # noqa: BLE001 — a prompt must never fail on its sidebar side effect
        logger.warning("Could not wake snoozed session %s early", session_id, exc_info=True)
        return False


_pending_wakes: set[asyncio.Task] = set()


def wake_soon(session_id: Optional[str]) -> None:
    """Schedule :func:`wake_for_attention` without adding an await to the caller.

    Prompt paths call this, so a cancel or error can never land between a
    prompt being registered and its own cleanup.
    """
    if not session_id:
        return
    try:
        task = asyncio.get_running_loop().create_task(wake_for_attention(session_id))
    except RuntimeError:
        return
    _pending_wakes.add(task)
    task.add_done_callback(_pending_wakes.discard)


async def wake_due_once(now: Optional[datetime] = None) -> int:
    """One pass of the wake loop. Returns how many chats woke."""
    from app.db.async_database import run_sync
    woke = 0
    for item in await run_sync(due_snoozes, now):
        try:
            if await wake(item["id"], expected=item["snoozed_until"], reason="snooze ended"):
                woke += 1
        except Exception:  # noqa: BLE001 — one chat must not stop the others
            logger.warning("Could not wake snoozed session %s", item["id"], exc_info=True)
    return woke


async def maintain_snoozes(interval: float = SWEEP_SECONDS) -> None:
    """Background loop: wake every snoozed chat whose time has come."""
    while True:
        try:
            await wake_due_once()
        except Exception:  # noqa: BLE001 — retry next pass
            logger.warning("Snooze wake pass failed", exc_info=True)
        await asyncio.sleep(interval)
