"""Append-only per-session event log (v2 core).

Every observable thing that happens in a session is recorded here as an ordered
event. Sequence numbers (``seq``) are dense and monotonic per session, allocated
atomically through the ``session_event_counters`` companion table so a batched
append reserves a contiguous ``[first_seq, last_seq]`` range in one statement.

Appends are optionally *fenced* by the owning run's ``fence_token`` (see
``app.db.runs``): a runner that has been superseded — its run no longer
``active`` or its token bumped — is rejected with :class:`FenceViolation` before
any rows are written, so a stale/lost runner can never interleave events.

The log is append-only by design: there are deliberately no delete/truncate
helpers. Readers tail by ``seq > from_seq``. A best-effort ``pg_notify`` on the
``session_events`` channel wakes the gateway; delivery must not depend on it (the
gateway also polls), and ``pg_notify`` does not exist on the SQLite test shim, so
the notify is guarded and non-fatal.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.db.base import get_db, rows_to_list

logger = logging.getLogger(__name__)

NOTIFY_CHANNEL = "session_events"


class FenceViolation(Exception):
    """Raised when an append is rejected because the run fence no longer holds
    (run missing, not active, or token superseded)."""


def _parse_payload(raw: Any) -> Dict[str, Any]:
    """Parse the payload field, handling both str (test shim TEXT) and dict."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


def append_events(
    session_id: str,
    events: List[Dict[str, Any]],
    run_id: Optional[str] = None,
    fence_token: Optional[int] = None,
) -> Tuple[int, int]:
    """Append a batch of events, returning the ``(first_seq, last_seq)`` range.

    All work happens in one transaction: fence check, atomic seq allocation,
    row inserts, and a best-effort notify. Raises :class:`FenceViolation` if a
    supplied ``run_id`` no longer owns the session, ``ValueError`` if ``events``
    is empty or ``run_id`` is given without ``fence_token``.
    """
    if not events:
        raise ValueError("events must be non-empty")
    if run_id is not None and fence_token is None:
        raise ValueError("fence_token is required when run_id is provided")

    n = len(events)
    with get_db() as conn:
        cursor = conn.cursor()

        # 1. Fence check — reject a stale/superseded runner before writing.
        if run_id is not None:
            cursor.execute(
                "SELECT state, fence_token FROM runs WHERE id = %s FOR SHARE",
                (run_id,),
            )
            row = cursor.fetchone()
            actual = None if row is None else row["fence_token"]
            if row is None or row["state"] != "active" or actual != fence_token:
                raise FenceViolation(
                    f"fence violation for session {session_id!r} run {run_id!r}: "
                    f"expected active token {fence_token!r}, got "
                    f"state={None if row is None else row['state']!r} "
                    f"token={actual!r}"
                )

        # 2. Allocate N contiguous seqs atomically.
        cursor.execute(
            "INSERT INTO session_event_counters (session_id, next_seq) "
            "VALUES (%s, %s) "
            "ON CONFLICT (session_id) DO UPDATE "
            "SET next_seq = session_event_counters.next_seq + %s "
            "RETURNING next_seq",
            (session_id, n, n),
        )
        last_seq = int(cursor.fetchone()["next_seq"])
        first_seq = last_seq - n + 1

        # 3. Insert one row per event.
        params = [
            (
                str(uuid.uuid4()),
                session_id,
                first_seq + i,
                run_id,
                event.get("turn_id"),
                event["type"],
                json.dumps(event.get("payload", {})),
                event.get("command_id"),
            )
            for i, event in enumerate(events)
        ]
        cursor.executemany(
            "INSERT INTO session_events "
            "(id, session_id, seq, run_id, turn_id, event_type, payload, command_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            params,
        )

        # 4. Best-effort notify (guarded: pg_notify is absent on the SQLite
        # shim, and delivery must not depend on it — the gateway also polls).
        try:
            cursor.execute(
                "SELECT pg_notify(%s, %s)",
                (
                    NOTIFY_CHANNEL,
                    json.dumps({"session_id": session_id, "max_seq": last_seq}),
                ),
            )
        except Exception:
            logger.debug("pg_notify on %s failed (non-fatal)", NOTIFY_CHANNEL, exc_info=True)

    return first_seq, last_seq


def append_backfill_events(
    session_id: str,
    events: List[Dict[str, Any]],
) -> Optional[Tuple[int, int]]:
    """Append a legacy-history backfill batch under an exclusive-create gate.

    Unlike :func:`append_events`, this is the ONE-TIME lazy backfill writer
    (``app.core.history_backfill``). It is always unfenced (``run_id=None``) and
    must only win when it is the FIRST writer to allocate this session's seqs —
    otherwise a concurrent recorder append (or a second reader racing the same
    backfill) would interleave a duplicate legacy prefix into the log.

    The gate is the atomic counter INSERT itself: ``ON CONFLICT DO NOTHING
    RETURNING next_seq`` yields the allocated row ONLY when this statement
    created the counter. If the counter already exists (a recorder or a prior
    backfill got there first) no row is returned and we abort without writing,
    returning ``None``. On success returns the ``(first_seq, last_seq)`` range
    (always starting at 1). Raises ``ValueError`` if ``events`` is empty.
    """
    if not events:
        raise ValueError("events must be non-empty")

    n = len(events)
    with get_db() as conn:
        cursor = conn.cursor()

        # Exclusive-create gate: allocate the whole [1, n] range in one shot, but
        # ONLY if this statement is the one that creates the counter row. A
        # returned row proves we won; no row means another writer already
        # allocated seqs for this session — abort silently (the log is no longer
        # empty, so the backfill is unnecessary).
        cursor.execute(
            "INSERT INTO session_event_counters (session_id, next_seq) "
            "VALUES (%s, %s) "
            "ON CONFLICT (session_id) DO NOTHING "
            "RETURNING next_seq",
            (session_id, n),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        last_seq = int(row["next_seq"])
        first_seq = last_seq - n + 1

        # Insert one row per event — always unfenced (run_id=None).
        params = [
            (
                str(uuid.uuid4()),
                session_id,
                first_seq + i,
                None,
                event.get("turn_id"),
                event["type"],
                json.dumps(event.get("payload", {})),
                event.get("command_id"),
            )
            for i, event in enumerate(events)
        ]
        cursor.executemany(
            "INSERT INTO session_events "
            "(id, session_id, seq, run_id, turn_id, event_type, payload, command_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            params,
        )

        # Best-effort notify (guarded — absent on the SQLite shim).
        try:
            cursor.execute(
                "SELECT pg_notify(%s, %s)",
                (
                    NOTIFY_CHANNEL,
                    json.dumps({"session_id": session_id, "max_seq": last_seq}),
                ),
            )
        except Exception:
            logger.debug("pg_notify on %s failed (non-fatal)", NOTIFY_CHANNEL, exc_info=True)

    return first_seq, last_seq


def get_events(
    session_id: str, from_seq: int = 0, limit: int = 500
) -> List[Dict[str, Any]]:
    """Return events with ``seq > from_seq`` ordered ascending, up to ``limit``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, session_id, seq, run_id, turn_id, event_type, payload, "
            "command_id, created_at FROM session_events "
            "WHERE session_id = %s AND seq > %s ORDER BY seq ASC LIMIT %s",
            (session_id, from_seq, limit),
        )
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["payload"] = _parse_payload(row.get("payload"))
    return rows


def get_events_multi(
    cursors: Dict[str, int], limit: int = 500
) -> List[Dict[str, Any]]:
    """Tail SEVERAL sessions in ONE query, each from its own ``seq`` cursor.

    ``cursors`` maps ``session_id -> from_seq``; a row is returned when its
    session is in the map and its ``seq`` exceeds THAT session's cursor. Rows
    come back ordered by ``seq`` then ``session_id``, and ``limit`` is a GLOBAL
    row cap the caller sizes for the whole batch.

    Exists for the ProviderAgent worker feed (multi-provider phase 3, P3-A4).
    Ruling P12 forbids a cap on worker fan-out, and the feed originally ran one
    poller per worker at 1.5s — twenty workers meant ~13 reads/second through
    the sync-executor pool purely to mirror progress, against a codebase with a
    documented history of ``PoolError`` bursts when executor count outruns pool
    max. One query per parent turn replaces N.

    Ordering by ``seq`` first (not ``session_id``) is deliberate: ``seq`` is
    per-session and every worker starts near 1, so a global ``LIMIT`` is shared
    roughly evenly instead of being consumed by whichever session sorts first.

    An empty ``cursors`` yields ``[]`` without touching the DB.
    """
    if not cursors:
        return []
    clauses = " OR ".join(["(session_id = %s AND seq > %s)"] * len(cursors))
    params: List[Any] = []
    for session_id, from_seq in cursors.items():
        params.append(session_id)
        params.append(int(from_seq or 0))
    params.append(int(limit))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, session_id, seq, run_id, turn_id, event_type, payload, "
            "command_id, created_at FROM session_events "
            f"WHERE {clauses} ORDER BY seq ASC, session_id ASC LIMIT %s",
            tuple(params),
        )
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["payload"] = _parse_payload(row.get("payload"))
    return rows


def has_subagent_started(session_id: str, task_id: str) -> bool:
    """True if a ``subagent.started`` event for ``task_id`` exists in the log.

    Used by the ``stop_session_task`` durable force-settle fallback: post-restart
    the registry AND the ``background_tasks`` table may hold nothing for an
    orphaned sub-agent, but its launch event is still in the append-only log and
    the UI card is still rendering it "running". The card joins on
    ``tool_use_id == started task_id`` (see ``SubagentStarted.task_id``). The
    ``task_id`` filter runs SQL-side via the ``payload->>'task_id'`` JSON text
    extractor — supported by both Postgres (JSONB) and the SQLite test shim
    (native ``->>`` on JSON stored as TEXT, sqlite3 ≥ 3.46).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM session_events "
            "WHERE session_id = %s AND event_type = 'subagent.started' "
            "AND payload->>'task_id' = %s LIMIT 1",
            (session_id, task_id),
        )
        return cursor.fetchone() is not None


def get_max_seq(session_id: str) -> int:
    """Return the highest allocated seq for a session (0 if none)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT next_seq FROM session_event_counters WHERE session_id = %s",
            (session_id,),
        )
        row = cursor.fetchone()
    return int(row["next_seq"]) if row else 0


def notify_session(session_id: str) -> None:
    """Fire a best-effort NOTIFY tick on the events channel WITHOUT appending
    an event (V2 Phase 4 turn-claim wake).

    Used by the placement submit/interrupt seams to wake peer runners' claim
    loops instantly instead of waiting out their 5s slow poll. The payload
    carries the session's REAL current max_seq so every existing consumer of
    the channel (SSE re-checks, busy-status re-derivation) sees an honest,
    at-worst-redundant tick — never a fabricated seq. Purely a latency
    optimization: correctness rides the slow poll, so failure here (SQLite
    shim, transient conn error) is swallowed.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT next_seq FROM session_event_counters WHERE session_id = %s",
                (session_id,),
            )
            row = cursor.fetchone()
            max_seq = int(row["next_seq"]) if row else 0
            cursor.execute(
                "SELECT pg_notify(%s, %s)",
                (
                    NOTIFY_CHANNEL,
                    json.dumps({"session_id": session_id, "max_seq": max_seq}),
                ),
            )
    except Exception:
        logger.debug(
            "notify_session on %s failed (non-fatal)", NOTIFY_CHANNEL, exc_info=True
        )
