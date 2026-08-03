"""Durable per-session wakeups (#598).

A wakeup is a pending "re-fire this prompt at ``fire_at``" record. Two producers
persist here so an app restart rehydrates and re-arms them instead of dropping
the bare in-process ``asyncio.sleep`` timer the old ScheduleWakeup used:

* ``ScheduleWakeup`` (the builtin MCP tool) — an agent-armed heartbeat/continue;
* the idle-at-edge watchdog — a synthetic nudge for a session that sits idle
  above the context-window edge (belt-and-braces backstop for the lazy-compaction
  stall).

``fire_at`` is stored as a unix epoch (seconds, DOUBLE PRECISION) rather than a
TIMESTAMPTZ so the rehydrate "remaining delay" math is timezone- and
test-shim-agnostic (the SQLite conformance shim stores numbers verbatim).

Firing is a *claim*: :func:`claim_wakeup` deletes the row and reports whether it
won the delete, so a rehydrated timer and a peer install can race to fire without
double-firing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.db.base import get_db, get_cursor, rows_to_list
from app.db.compat import ph

logger = logging.getLogger(__name__)


def create_wakeup(
    *,
    id: str,
    session_id: str,
    prompt: str,
    fire_at: float,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    reason: Optional[str] = None,
    source: str = "schedule_wakeup",
    instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert a pending wakeup and return the persisted row as a dict."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""INSERT INTO pending_wakeups
                (id, session_id, profile_id, project_id, prompt, reason, source,
                 instance_id, fire_at)
                VALUES ({_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph})""",
            (id, session_id, profile_id, project_id, prompt, reason, source,
             instance_id, float(fire_at)),
        )
    return {
        "id": id,
        "session_id": session_id,
        "profile_id": profile_id,
        "project_id": project_id,
        "prompt": prompt,
        "reason": reason,
        "source": source,
        "instance_id": instance_id,
        "fire_at": float(fire_at),
    }


def claim_wakeup(wakeup_id: str) -> bool:
    """Atomically claim a wakeup for firing by deleting its row.

    Returns True iff this call won the delete (the row existed and is now gone).
    A losing caller (already fired / claimed by a peer install or a rehydrated
    timer) gets False and must NOT fire.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"DELETE FROM pending_wakeups WHERE id = {_ph}", (wakeup_id,)
        )
        deleted = cursor.rowcount
    return bool(deleted and deleted > 0)


def delete_session_wakeups(session_id: str, source: Optional[str] = None) -> int:
    """Delete a session's pending wakeups (optionally only of one ``source``).

    Returns the number of rows removed. Used to keep the watchdog idempotent
    (clear a stale idle nudge) and to tidy up when a session is deleted.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        if source is None:
            cursor.execute(
                f"DELETE FROM pending_wakeups WHERE session_id = {_ph}",
                (session_id,),
            )
        else:
            cursor.execute(
                f"DELETE FROM pending_wakeups WHERE session_id = {_ph} "
                f"AND source = {_ph}",
                (session_id, source),
            )
        return int(cursor.rowcount or 0)


def get_pending_wakeups(instance_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return pending wakeups (all, or scoped to one install), oldest fire first.

    Boot rehydration passes this install's id so a shared-DB fleet re-arms only
    its own wakeups; each install owns the sessions it pooled.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        if instance_id is None:
            cursor.execute(
                "SELECT * FROM pending_wakeups ORDER BY fire_at ASC"
            )
        else:
            cursor.execute(
                f"SELECT * FROM pending_wakeups WHERE instance_id = {_ph} "
                f"ORDER BY fire_at ASC",
                (instance_id,),
            )
        rows = cursor.fetchall() or []
    return rows_to_list(list(rows))


def session_has_pending(session_id: str, source: Optional[str] = None) -> bool:
    """True iff the session has a pending wakeup (optionally of one ``source``)."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        if source is None:
            cursor.execute(
                f"SELECT 1 FROM pending_wakeups WHERE session_id = {_ph} LIMIT 1",
                (session_id,),
            )
        else:
            cursor.execute(
                f"SELECT 1 FROM pending_wakeups WHERE session_id = {_ph} "
                f"AND source = {_ph} LIMIT 1",
                (session_id, source),
            )
        return cursor.fetchone() is not None


def idle_edge_candidates(instance_id: str, status: str = "active") -> List[Dict[str, Any]]:
    """Sessions eligible for an idle-at-edge watchdog check on this install.

    Structural filters only (the cheap, DB-expressible ones); the caller applies
    the context-fraction gate on ``context_usage_json`` and its in-memory
    cooldown. A candidate is a session that:

    * belongs to this install (``instance_id``);
    * is ``status='active'`` (not streaming/parked/archived — i.e. no live turn);
    * has a persisted context-usage snapshot (``context_usage_json`` present);
    * has NO in-flight turn (no ``turns`` row in a live state); and
    * has NO pending wakeup already armed (so the watchdog never piles a second
      nudge on top of one it — or ScheduleWakeup — already scheduled).
    """
    _ph = ph()
    live_states = ("queued", "running", "interrupting", "draining")
    placeholders = ", ".join([_ph] * len(live_states))
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""
            SELECT id, profile_id, project_id, context_usage_json
            FROM sessions s
            WHERE s.instance_id = {_ph}
              AND s.status = {_ph}
              AND s.context_usage_json IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM turns t
                  WHERE t.session_id = s.id
                    AND t.state IN ({placeholders})
              )
              AND NOT EXISTS (
                  SELECT 1 FROM pending_wakeups w
                  WHERE w.session_id = s.id
              )
            """,
            (instance_id, status, *live_states),
        )
        rows = cursor.fetchall() or []
    return rows_to_list(list(rows))
