"""Durable per-session wakeups (#598).

A wakeup is a pending "re-fire this prompt at ``fire_at``" record. Producers
(``ScheduleWakeup``, the usage-limit resume, hand-off resumes, held peer
messages) persist here so an app restart rehydrates and re-arms them instead of
dropping a bare in-process ``asyncio.sleep`` timer.

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
    model: Optional[str] = None,
    source: str = "schedule_wakeup",
    instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert a pending wakeup and return the persisted row as a dict.

    ``model`` is the scheduling turn's effective model; the wake replays it as a
    query-level model override so it never fires on the profile default.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"""INSERT INTO pending_wakeups
                (id, session_id, profile_id, project_id, prompt, reason, model,
                 source, instance_id, fire_at)
                VALUES ({_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph})""",
            (id, session_id, profile_id, project_id, prompt, reason, model,
             source, instance_id, float(fire_at)),
        )
    return {
        "id": id,
        "session_id": session_id,
        "profile_id": profile_id,
        "project_id": project_id,
        "prompt": prompt,
        "reason": reason,
        "model": model,
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


def pop_session_wakeups(
    session_id: str,
    source: Optional[str] = None,
    *,
    exclude_source: Optional[str] = None,
) -> List[str]:
    """Delete a session's pending wakeups and return the deleted row ids.

    The ids are what the caller needs to cancel the matching *in-process*
    timers (:mod:`app.core.wakeup_service` keys its live timer tasks by wakeup
    id), so the delete and the disarm stay in step. Read-then-delete inside one
    connection rather than ``DELETE ... RETURNING`` so the SQLite conformance
    shim runs the same statements as Postgres.

    A peer install's armed timer is unaffected and safe: its
    :func:`claim_wakeup` finds no row and no-ops.

    ``exclude_source`` keeps that source's rows when ``source`` is None.
    A human input (which excludes held_peer_promotion) supersedes timers, not
    the note-bearing handoff_resume. Stop/delete use no exclusion and retire it.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        if source is None:
            # Supersession also covers wakes staged by a moving chat. This
            # serializes with receiver finalization, which locks the same row.
            cursor.execute(f"UPDATE handoff_wakes SET superseded=TRUE, keep_peer=keep_peer AND {_ph} "
                           f"WHERE session_id={_ph} AND finalized=FALSE",
                           (exclude_source == "held_peer_promotion", session_id))
        if source is None and exclude_source is not None:
            cursor.execute(
                f"SELECT id FROM pending_wakeups WHERE session_id = {_ph} "
                f"AND (source IS NULL OR source <> {_ph}) "
                + ("AND (source IS NULL OR source <> 'handoff_resume')"
                   if exclude_source == "held_peer_promotion" else ""),
                (session_id, exclude_source),
            )
            ids = [r["id"] for r in rows_to_list(list(cursor.fetchall() or []))]
            for wakeup_id in ids:
                cursor.execute(
                    f"DELETE FROM pending_wakeups WHERE id = {_ph}", (wakeup_id,)
                )
            return ids
        if source is None:
            cursor.execute(
                f"SELECT id FROM pending_wakeups WHERE session_id = {_ph}",
                (session_id,),
            )
        else:
            cursor.execute(
                f"SELECT id FROM pending_wakeups WHERE session_id = {_ph} "
                f"AND source = {_ph}",
                (session_id, source),
            )
        ids = [r["id"] for r in rows_to_list(list(cursor.fetchall() or []))]
        if not ids:
            return []
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
    return ids


def take_session_wakeups(session_id: str, source: str) -> List[Dict[str, Any]]:
    """Delete a session's pending wakeups of one ``source``; return the rows.

    Oldest ``fire_at`` first. Each row is deleted by id, so a row inserted
    after the read is left for the next caller, and a row a timer already
    claimed is simply not returned.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT * FROM pending_wakeups WHERE session_id = {_ph} "
            f"AND source = {_ph} ORDER BY fire_at ASC",
            (session_id, source),
        )
        rows = rows_to_list(list(cursor.fetchall() or []))
        taken = []
        for row in rows:
            cursor.execute(
                f"DELETE FROM pending_wakeups WHERE id = {_ph}", (row["id"],)
            )
            if cursor.rowcount and cursor.rowcount > 0:
                taken.append(row)
    return taken


def merge_session_wakeup(
    *,
    id: str,
    session_id: str,
    source: str,
    prompt: str,
    fire_at: float,
    prepend: bool = False,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    reason: Optional[str] = None,
    model: Optional[str] = None,
    instance_id: Optional[str] = None,
) -> tuple[Dict[str, Any], List[str]]:
    """Fold a session's pending wakeups of one ``source`` into one new row.

    In one transaction: delete the existing rows, then insert one row whose
    prompt is theirs (oldest ``fire_at`` first) joined with ``prompt`` by a
    blank line (``prompt`` goes first when ``prepend``), and whose ``fire_at``
    is the earliest of theirs, or ``fire_at`` when there were none. A row a
    timer claimed before the delete is not folded in, so its text is never
    written twice; no crash between the delete and the insert can lose the
    old text. Returns the new row and the ids of the rows it replaced.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            f"SELECT id, prompt, fire_at FROM pending_wakeups "
            f"WHERE session_id = {_ph} AND source = {_ph} ORDER BY fire_at ASC",
            (session_id, source),
        )
        existing = rows_to_list(list(cursor.fetchall() or []))
        kept = []
        for row in existing:
            cursor.execute(
                f"DELETE FROM pending_wakeups WHERE id = {_ph}", (row["id"],)
            )
            if cursor.rowcount and cursor.rowcount > 0:
                kept.append(row)
        texts = [str(r.get("prompt") or "") for r in kept]
        parts = [prompt, *texts] if prepend else [*texts, prompt]
        text = "\n\n".join(p.strip() for p in parts if p and p.strip())
        if kept:
            fire_at = min(float(r.get("fire_at") or 0.0) for r in kept)
        cursor.execute(
            f"""INSERT INTO pending_wakeups
                (id, session_id, profile_id, project_id, prompt, reason, model,
                 source, instance_id, fire_at)
                VALUES ({_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph})""",
            (id, session_id, profile_id, project_id, text, reason, model,
             source, instance_id, float(fire_at)),
        )
    row = {
        "id": id,
        "session_id": session_id,
        "profile_id": profile_id,
        "project_id": project_id,
        "prompt": text,
        "reason": reason,
        "model": model,
        "source": source,
        "instance_id": instance_id,
        "fire_at": float(fire_at),
    }
    return row, [str(r["id"]) for r in kept]


def get_session_wakeups(session_id: str, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """A session's pending wakeups (all, or of one ``source``), oldest ``fire_at`` first."""
    _ph = ph()
    with get_db() as conn:
        cursor = get_cursor(conn)
        if source is None:
            cursor.execute(
                f"SELECT * FROM pending_wakeups WHERE session_id = {_ph} ORDER BY fire_at ASC",
                (session_id,),
            )
        else:
            cursor.execute(
                f"SELECT * FROM pending_wakeups WHERE session_id = {_ph} "
                f"AND source = {_ph} ORDER BY fire_at ASC",
                (session_id, source),
            )
        return rows_to_list(list(cursor.fetchall() or []))


def delete_session_wakeups(session_id: str, source: Optional[str] = None) -> int:
    """Delete a session's pending wakeups (optionally only of one ``source``).

    Returns the number of rows removed. Used to keep the watchdog idempotent
    (clear a stale idle nudge) and to tidy up when a session is deleted. Thin
    count-returning wrapper over :func:`pop_session_wakeups`.
    """
    return len(pop_session_wakeups(session_id, source))


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

