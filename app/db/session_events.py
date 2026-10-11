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

The ONE exception to "append-only" is :func:`compact_session_deltas` — a
retention pass for idle sessions that is *lossless by construction*: it
performs exactly the merge the SSE replay already performs at read time
(``app.api.events._coalesce_deltas``), so the bytes a client receives are
unchanged. Nothing is ever deleted that a replay would have delivered. See its
docstring for the full safety argument.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.db.base import get_db, rows_to_list

# Same Postgres-illegal code-point scrub the Postgres session store applies at its
# own JSON boundary — see the comment block above ``_PG_ILLEGAL_RE`` there for
# why NUL and lone surrogates have to go before an INSERT sees them.
from app.core.pg_session_store import _scrub_pg_illegal

logger = logging.getLogger(__name__)

NOTIFY_CHANNEL = "session_events"
COMPACTION_AUDIT_PREFIX_LIMIT = 100

# Post-commit wake for the worker network, installed by
# ``app.network.startup`` (app.db never imports app.network). Called with
# ``(session_id, collection)`` after a local write to a session's events,
# prompts or run state commits, so peers see it without waiting for the
# scanner's idle backoff. It must be cheap and must not touch the database.
_session_write_hook: Optional[Callable[[str, str], None]] = None


def set_session_write_hook(hook: Optional[Callable[[str, str], None]]) -> None:
    global _session_write_hook
    _session_write_hook = hook


def notify_session_write(session_id: Optional[str], collection: str) -> None:
    """Tell the network a session's shared state changed. Never raises."""
    hook = _session_write_hook
    if hook is None or not session_id:
        return
    try:
        hook(session_id, collection)
    except Exception:
        logger.debug("Session write hook failed for %s", session_id, exc_info=True)


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
    *,
    _conn: Any = None,
) -> Tuple[int, int]:
    """Append a batch of events, returning the ``(first_seq, last_seq)`` range.

    All work happens in one transaction: fence check, atomic seq allocation,
    row inserts, and a best-effort notify. Raises :class:`FenceViolation` if a
    supplied ``run_id`` no longer owns the session, ``ValueError`` if ``events``
    is empty or ``run_id`` is given without ``fence_token``.

    An optional stable ``id`` makes replay inserts idempotent. Duplicate IDs
    still reserve sequence slots; the returned range is the allocation, not
    a guarantee of one new row per slot (replay cursors allow gaps).
    """
    if not events:
        raise ValueError("events must be non-empty")
    if run_id is not None and fence_token is None:
        raise ValueError("fence_token is required when run_id is provided")

    n = len(events)
    # With a caller-owned transaction, the caller also sends the post-commit wake.
    with (get_db() if _conn is None else nullcontext(_conn)) as conn:
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
                event.get("id") or str(uuid.uuid4()),
                session_id,
                first_seq + i,
                run_id,
                event.get("turn_id"),
                event["type"],
                json.dumps(_scrub_pg_illegal(event.get("payload", {}))),
                event.get("command_id"),
            )
            for i, event in enumerate(events)
        ]
        from app.db.base import after_commit
        from app.db.sessions import is_user_message, _record_user_message

        insert_sql = (
            "INSERT INTO session_events "
            "(id, session_id, seq, run_id, turn_id, event_type, payload, command_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING"
        )
        user_params, other_params = [], []
        for event, values in zip(events, params):
            target = user_params if (
                event["type"] == "message.user"
                and is_user_message("user", event.get("payload"))
            ) else other_params
            target.append(values)
        inserted_other = False
        for values in other_params:
            cursor.execute(insert_sql + " RETURNING seq", values)
            inserted_other = cursor.fetchone() is not None or inserted_other
        # The event log is authoritative on the EXECUTING worker. API, relay,
        # connector and automation turns need not have a legacy message row.
        # RETURNING distinguishes delivery from a retry of a stable event id;
        # backfills/copies use separate helpers and never advance the revision.
        user_seq = None
        for values in user_params:
            cursor.execute(insert_sql + " RETURNING seq", values)
            inserted = cursor.fetchone()
            if inserted is not None:
                user_seq = int(inserted["seq"])
        question_notify = None
        if inserted_other or user_seq is not None:
            from app.db.session_questions import record_question

            # Rewinds retain the log epoch but remove causal user anchors in
            # the discarded branch. The question projection reads surviving
            # prose only. Both writes share the append transaction.
            cursor.execute(
                "SELECT seq, payload FROM session_events WHERE session_id = %s "
                "AND seq >= %s AND seq <= %s AND event_type = 'transcript.rewound' ORDER BY seq",
                (session_id, first_seq, last_seq),
            )
            rewinds = cursor.fetchall()
            for rewind in rewinds:
                cut = _parse_payload(rewind["payload"]).get("from_seq")
                if isinstance(cut, int) and not isinstance(cut, bool):
                    cursor.execute(
                        "UPDATE sessions SET last_user_message_seq = NULL, last_user_message_epoch = NULL "
                        "WHERE id = %s AND last_user_message_seq >= %s AND last_user_message_seq < %s",
                        (session_id, cut, rewind["seq"]),
                    )
                    if user_seq is not None and cut <= user_seq < rewind["seq"]:
                        user_seq = None
            question_notify = record_question(cursor, session_id, force_notify=bool(rewinds))
        if user_seq is not None:
            now = datetime.now(timezone.utc).isoformat()
            notify = _record_user_message(cursor, session_id, "user", now, seq=user_seq)
            if notify:
                after_commit(conn, notify)
        elif question_notify:
            after_commit(conn, question_notify)

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

    if _conn is None:
        notify_session_write(session_id, "session_events")
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
                json.dumps(_scrub_pg_illegal(event.get("payload", {}))),
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


def get_latest_task_updates(session_id: str) -> List[Dict[str, Any]]:
    """Latest durable task projection per id, including pre-fix stale starts."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload FROM ("
            "SELECT payload, ROW_NUMBER() OVER ("
            "PARTITION BY payload->>'task_id' ORDER BY seq DESC) AS position "
            "FROM session_events WHERE session_id = %s AND event_type = 'task.updated'"
            ") AS tasks WHERE position = 1",
            (session_id,),
        )
        return [_parse_payload(row["payload"]) for row in cursor.fetchall()]


def get_compaction_events_before(session_id: str, before_seq: int) -> List[Dict[str, Any]]:
    """Newest durable audit cards in an elided prefix, in transcript order.

    Hydration is bounded even when a session has years of compactions.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, session_id, seq, run_id, turn_id, event_type, payload, "
            "command_id, created_at FROM session_events "
            "WHERE session_id = %s AND seq < %s "
            "AND event_type = 'system.compaction_boundary' ORDER BY seq DESC LIMIT %s",
            (session_id, before_seq, COMPACTION_AUDIT_PREFIX_LIMIT),
        )
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["payload"] = _parse_payload(row.get("payload"))
    return list(reversed(rows))


def get_latest_system_note(session_id: str, subtype: str) -> Optional[Dict[str, Any]]:
    """Read the last note of a subtype, including across runtime restarts."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload FROM session_events "
            "WHERE session_id = %s AND event_type = 'system.note' "
            "AND payload ->> 'subtype' = %s ORDER BY seq DESC LIMIT 1",
            (session_id, subtype),
        )
        row = cursor.fetchone()
    return _parse_payload(row["payload"]) if row else None


def get_events_multi(
    cursors: Dict[str, int], limit: int = 500
) -> List[Dict[str, Any]]:
    """Tail SEVERAL sessions in ONE query, each from its own ``seq`` cursor.

    ``cursors`` maps ``session_id -> from_seq``; a row is returned when its
    session is in the map and its ``seq`` exceeds THAT session's cursor. Rows
    come back ordered by ``seq`` then ``session_id``, and ``limit`` is a GLOBAL
    row cap the caller sizes for the whole batch.

    Exists for the ProviderAgent worker feed. Worker fan-out is uncapped, and
    one poller per worker would put N reads per tick through the sync-executor
    pool purely to mirror progress, risking ``PoolError`` bursts when executor
    count outruns pool max. One query per parent turn replaces N.

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


def get_user_seq_for_turn(session_id: str, turn_id: str) -> Optional[int]:
    """First ``message.user`` seq for a durable turn id, or ``None`` if unsafe.

    Fluid's persisted ``TurnStarted`` event gives a direct node-to-turn bridge.
    The first app user row with that same turn id is the real turn start; later
    mid-turn steers reuse the id and are deliberately ignored. Append-only logs
    containing a rewind cannot be sliced safely because obsolete branch events
    remain before the reducer's logical truncation, so they replay in full.
    """
    if not turn_id:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM session_events "
            "WHERE session_id = %s AND event_type = 'transcript.rewound' LIMIT 1",
            (session_id,),
        )
        if cursor.fetchone() is not None:
            return None
        cursor.execute(
            "SELECT MIN(seq) AS seq FROM session_events "
            "WHERE session_id = %s AND event_type = 'message.user' "
            "AND turn_id = %s",
            (session_id, turn_id),
        )
        row = cursor.fetchone() or {}
    seq = row.get("seq")
    return int(seq) if seq is not None else None


# Epoch reported for a session whose log holds no rows (new, or just cleared).
EMPTY_LOG_EPOCH = "0"


def get_log_epoch(session_id: str) -> str:
    """Identity of the session's current event log, for resuming SSE clients.

    It is the id of the log's lowest-seq row. That id is fixed for the life of
    a log and replaced whenever the log is wiped and re-seeded:
    ``clear_session`` deletes every row and the counter, so the next seq 1 gets
    a fresh uuid. A client resuming at ``from_seq`` with an older epoch knows
    its cursor points into a log that no longer exists.

    Derived, not stored: no schema change, and every path that rewrites the log
    is covered without having to remember to bump a column. Delta compaction
    keeps a merged run's FIRST id on its survivor, and the network mirror and
    handoff copy ids verbatim, so neither changes the epoch of a live log.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM session_events WHERE session_id = %s "
            "ORDER BY seq ASC LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
    return str(row["id"]) if row else EMPTY_LOG_EPOCH


def has_event_at(session_id: str, seq: int) -> bool:
    """True if the log still holds a row at exactly ``seq``."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM session_events WHERE session_id = %s AND seq = %s",
            (session_id, seq),
        )
        return cursor.fetchone() is not None


def get_tool_use_seqs(
    session_id: str, from_seq: int = 0
) -> Dict[Tuple[Optional[str], str], int]:
    """Map ``(turn_id, tool_use_id) -> last tool.use seq`` for ``seq > from_seq``.

    The SSE backfill uses it to drop ``tool.input_delta`` rows whose complete
    input a later ``tool.use`` row repeats. Only ``tool.use`` rows have their
    payload read.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT seq, turn_id, payload->>'tool_use_id' AS tool_use_id "
            "FROM session_events WHERE session_id = %s AND seq > %s "
            "AND event_type = 'tool.use'",
            (session_id, from_seq),
        )
        rows = rows_to_list(cursor.fetchall())
    seqs: Dict[Tuple[Optional[str], str], int] = {}
    for row in rows:
        tool_use_id = row.get("tool_use_id")
        if not tool_use_id:
            continue
        key = (row.get("turn_id"), str(tool_use_id))
        seqs[key] = max(int(row["seq"]), seqs.get(key, 0))
    return seqs


def get_watermark(session_id: str, *, at_seq: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Read the materialized log ceiling, epoch and tail identity in one statement.

    Unlike the allocation counter, this reflects replicated clears/deletes too.
    An absent session is unavailable; an existing empty log is (0, "0", "0").
    ``at_seq`` reads the identity at an observed owner's ceiling instead of our
    latest row, so newer local appends do not invalidate that owner's witness.
    """
    tail_filter = " AND seq = %s" if at_seq is not None else ""
    params = (at_seq, session_id) if at_seq is not None else (session_id,)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COALESCE((SELECT MAX(seq) FROM session_events WHERE session_id = s.id), 0) AS seq, "
            "COALESCE((SELECT id FROM session_events WHERE session_id = s.id "
            "ORDER BY seq LIMIT 1), '0') AS epoch, "
            "COALESCE((SELECT id FROM session_events WHERE session_id = s.id"
            + tail_filter + " ORDER BY seq DESC LIMIT 1), '0') AS tail_id "
            "FROM sessions s WHERE s.id = %s",
            params,
        )
        row = cursor.fetchone()
    return {"seq": int(row["seq"]), "epoch": str(row["epoch"]), "tail_id": str(row["tail_id"])} if row else None


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
    an event (turn-claim wake).

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


def copy_session_events(
    source_session_id: str,
    dest_session_id: str,
) -> int:
    """Copy the app event log onto a new session (fork).

    The V2 chat renders from ``session_events``, not from the fluid node tree
    or ``fluid_session_events``. A fork that only copies those leaves the dest
    log empty, so ``history_backfill`` reconstructs a single-turn approximation
    (``turn_id='backfill'``) and the chat collapses every work group into one.

    Rewrites ``session_id``, allocates fresh event ids, preserves ``seq`` /
    ``turn_id`` / ``event_type`` / ``payload`` / ``command_id`` / ``created_at``.
    ``run_id`` is dropped — runs belong to the parent. The dest counter is
    created at ``N`` so later live appends continue after the copied prefix.

    Exclusive-create on the dest counter: if dest already has events, return 0.
    Empty source also returns 0 (no counter is created, so backfill can still
    run).
    """
    if source_session_id == dest_session_id:
        raise ValueError("source and dest session ids must differ")

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT seq, turn_id, event_type, payload, command_id, created_at "
            "FROM session_events WHERE session_id = %s ORDER BY seq",
            (source_session_id,),
        )
        rows = rows_to_list(cursor.fetchall())
        if not rows:
            return 0

        n = len(rows)
        cursor.execute(
            "INSERT INTO session_event_counters (session_id, next_seq) "
            "VALUES (%s, %s) "
            "ON CONFLICT (session_id) DO NOTHING "
            "RETURNING next_seq",
            (dest_session_id, n),
        )
        if cursor.fetchone() is None:
            return 0

        params = []
        for i, row in enumerate(rows, start=1):
            payload = dict(_parse_payload(row.get("payload")))
            if payload.get("session_id") == source_session_id:
                payload["session_id"] = dest_session_id
            params.append(
                (
                    str(uuid.uuid4()),
                    dest_session_id,
                    i,
                    None,
                    row.get("turn_id"),
                    row["event_type"],
                    json.dumps(_scrub_pg_illegal(payload)),
                    row.get("command_id"),
                    row.get("created_at"),
                )
            )
        cursor.executemany(
            "INSERT INTO session_events "
            "(id, session_id, seq, run_id, turn_id, event_type, payload, "
            "command_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            params,
        )
    return n


# ----------------------------------------------------------------------
# Retention: lossless delta compaction for idle sessions
# ----------------------------------------------------------------------

# The delta types the SSE replay already merges at read time
# (``app.api.events._COALESCE_TYPES``). Kept as a literal rather than an import
# so the db layer never reaches up into the api layer; the pairing is asserted
# by tests/db/test_session_events_compaction.py.
COMPACTABLE_DELTA_TYPES: Tuple[str, ...] = (
    "assistant.text_delta",
    "assistant.thinking_delta",
)

# Rows at the tail of a session are never touched, so no plausible live cursor
# can sit inside a compacted run (see the safety argument below).
DEFAULT_KEEP_TAIL_EVENTS = 1000
# How long a session must sit idle before maintenance compacts its deltas.
# Short on purpose: until compaction runs, every open of the session reads and
# decodes one row per 150 ms streaming flush. A resuming client whose cursor
# was merged away is caught by the SSE feed's cursor check and reloaded, so the
# window no longer has to outlast every plausible reconnect.
DELTA_COMPACTION_IDLE_HOURS = 1.0
# Per-call work caps: this runs inside the periodic maintenance transaction.
DEFAULT_MAX_RUNS_PER_SESSION = 500
DEFAULT_MAX_SESSIONS = 25
_MAX_SCAN_ROWS = 50_000

# Round-robin cursor over session ids so successive maintenance passes work
# through the whole table instead of re-scanning the same first N sessions.
_compaction_cursor: str = ""


def _run_key(row: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    """What makes two delta rows part of the same run (mirrors ``_delta_key``)."""
    return (row.get("event_type"), row.get("turn_id"), row.get("run_id"))


def _delta_runs(
    rows: Sequence[Dict[str, Any]], max_runs: int
) -> List[Tuple[int, int]]:
    """Return ``(first_seq, last_seq)`` for each mergeable delta run.

    A run is a maximal group of CONSECUTIVE rows (consecutive in the log's read
    order, which is what the replay coalescer sees — seq numbers need not be
    contiguous) sharing one ``(event_type, turn_id, run_id)`` key, where the
    type is coalescable. Single-row runs are skipped: there is nothing to merge.
    """
    runs: List[Tuple[int, int]] = []
    key: Optional[Tuple[Any, Any, Any]] = None
    first = last = 0
    count = 0
    for row in rows:
        if row.get("event_type") in COMPACTABLE_DELTA_TYPES:
            row_key = _run_key(row)
            if count and row_key == key:
                last = int(row["seq"])
                count += 1
                continue
            if count > 1:
                runs.append((first, last))
                if len(runs) >= max_runs:
                    return runs
            key = row_key
            first = last = int(row["seq"])
            count = 1
            continue
        if count > 1:
            runs.append((first, last))
            if len(runs) >= max_runs:
                return runs
        key, count = None, 0
    if count > 1:
        runs.append((first, last))
    return runs[:max_runs]


def compact_session_deltas(
    session_id: str,
    *,
    keep_tail_events: int = DEFAULT_KEEP_TAIL_EVENTS,
    max_runs: int = DEFAULT_MAX_RUNS_PER_SESSION,
    cursor: Optional[Any] = None,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """Merge historical streaming-delta runs in place. Returns ``(runs, rows)``.

    ``session_events`` grows forever: a streamed answer costs one row per ~100 ms
    chunk, and nothing else prunes them. This is the retention pass, and it
    is deliberately a COMPACTION rather than a deletion.

    What it does, per run of consecutive same-key delta rows: joins their text
    into the run's payload, keeps ONE row carrying the FIRST row's envelope
    (id / created_at / command_id / turn_id / run_id / type) at the LAST row's
    ``seq``, and deletes the rest.

    Safety argument
    ---------------
    * **The wire output is unchanged.** That surviving row is byte-for-byte what
      ``app.api.events._merged_row`` already builds for this run on every
      replay: the coalescer merges consecutive same-key delta rows into one
      frame carrying the joined text, the first row's envelope and the LAST
      row's seq. Compaction therefore removes only rows the replay never emitted
      individually in the first place. It is not a truncation of history and
      does not depend on (nor interact with) the compacted-prefix elision: no
      renderable content is lost even for a session whose Fluid transcript later
      becomes unreadable and whose elision falls back to a full replay.
    * **Partial runs stay safe.** If a run straddles the retention ceiling, the
      part below it merges and the rest stays as individual rows; at read time
      the coalescer merges survivor + remainder into the identical single frame
      with the identical final seq. The same holds for a run split by the
      per-call work cap.
    * **``seq > from_seq`` stays gap-free and duplicate-free.** Seqs are never
      reused or renumbered, and the SSE cursor is ``max(seq)`` of what the client
      was actually sent (``sse-feed.ts``), never an assumption about density —
      so the numeric holes left behind are unobservable. A fresh load
      (``from_seq=0``) and any reconnect whose cursor lies at or below a run's
      first seq, or at or above its last, receive exactly what they did before.
    * **The one divergent case is excluded by the retention ceiling.** A cursor
      landing strictly INSIDE a compacted run would now receive the run's whole
      merged text instead of its tail. Only the last ``keep_tail_events`` seqs
      of a session can hold a live cursor, and those are never touched;
      callers additionally restrict this to idle sessions with no active run
      (:func:`compact_idle_session_deltas`). A client that disconnected
      mid-run and resumes after the run was merged sends a cursor whose row
      no longer exists; the SSE feed checks for exactly that when the client
      sends its log epoch and answers with a ``reset`` and a full reload
      (``app.api.events.event_stream``).
    * **Nothing else reads these rows.** Turn-outcome and worker-progress
      readers reconstruct text from the LAST turn's deltas — inside the retained
      tail. ``get_user_seq_for_turn`` (the elision bridge) reads ``message.user``
      rows, ``has_subagent_started`` reads ``subagent.started``; neither is
      compactable. ``copy_session_events`` (fork) copies the merged rows and
      renders identically.

    Pass ``cursor`` to run inside a caller's transaction (the maintenance job
    does); otherwise this opens its own. ``dry_run`` reports what would change
    without writing.
    """
    if keep_tail_events < 0 or max_runs <= 0:
        return 0, 0

    if cursor is not None:
        return _compact_session_deltas(
            cursor, session_id, keep_tail_events, max_runs, dry_run
        )
    with get_db() as conn:
        return _compact_session_deltas(
            conn.cursor(), session_id, keep_tail_events, max_runs, dry_run
        )


def _compact_session_deltas(
    cur: Any,
    session_id: str,
    keep_tail_events: int,
    max_runs: int,
    dry_run: bool,
) -> Tuple[int, int]:
    cur.execute(
        "SELECT next_seq FROM session_event_counters WHERE session_id = %s",
        (session_id,),
    )
    row = cur.fetchone()
    if not row:
        return 0, 0
    ceiling = int(row["next_seq"]) - int(keep_tail_events)
    if ceiling <= 1:
        return 0, 0

    cur.execute(
        "SELECT seq, event_type, turn_id, run_id FROM session_events "
        "WHERE session_id = %s AND seq <= %s ORDER BY seq ASC LIMIT %s",
        (session_id, ceiling, _MAX_SCAN_ROWS),
    )
    scanned = rows_to_list(cur.fetchall())
    runs = _delta_runs(scanned, max_runs)
    if not runs:
        return 0, 0

    compacted = 0
    deleted = 0
    for first_seq, last_seq in runs:
        cur.execute(
            "SELECT id, seq, payload, command_id, created_at FROM session_events "
            "WHERE session_id = %s AND seq >= %s AND seq <= %s ORDER BY seq ASC",
            (session_id, first_seq, last_seq),
        )
        members = rows_to_list(cur.fetchall())
        if len(members) < 2:
            continue  # raced with a concurrent change — leave it alone
        head = members[0]
        text = "".join(
            (_parse_payload(m.get("payload")).get("text") or "") for m in members
        )
        payload = dict(_parse_payload(head.get("payload")))
        payload["text"] = text
        compacted += 1
        deleted += len(members) - 1
        if dry_run:
            continue
        cur.execute(
            "DELETE FROM session_events "
            "WHERE session_id = %s AND seq >= %s AND seq < %s",
            (session_id, first_seq, last_seq),
        )
        # The survivor becomes the exact row the replay coalescer would have
        # produced: the FIRST row's envelope carried at the LAST row's seq.
        cur.execute(
            "UPDATE session_events SET id = %s, payload = %s, command_id = %s, "
            "created_at = %s WHERE session_id = %s AND seq = %s",
            (
                head["id"],
                json.dumps(_scrub_pg_illegal(payload)),
                head.get("command_id"),
                head.get("created_at"),
                session_id,
                last_seq,
            ),
        )
    return compacted, deleted


def compact_idle_session_deltas(
    older_than_days: float = 7,
    *,
    older_than_hours: Optional[float] = None,
    keep_tail_events: int = DEFAULT_KEEP_TAIL_EVENTS,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    max_runs_per_session: int = DEFAULT_MAX_RUNS_PER_SESSION,
    cursor: Optional[Any] = None,
) -> Dict[str, int]:
    """Compact delta runs for sessions idle longer than the given window.

    Called from ``app.db.database.run_database_maintenance`` (step 2d) with
    ``older_than_hours=DELTA_COMPACTION_IDLE_HOURS``. ``older_than_hours``,
    when given, replaces ``older_than_days``. A non-positive window disables
    the pass and touches nothing.

    Idleness comes from ``sessions.updated_at``, the same signal
    ``prune_fluid_delta_events`` uses: a streaming session bumps it constantly,
    so an active session can never fall inside the cutoff. Sessions with no
    ``sessions`` row are skipped — there is no signal to age them by. Work is
    capped per pass (``max_sessions``) and resumes from a module-level
    round-robin cursor, so a large install is covered over successive passes
    instead of one long transaction.

    Returns ``{"sessions": n, "runs": n, "rows": n}`` (rows = rows removed).
    """
    global _compaction_cursor
    idle_hours = (
        float(older_than_hours)
        if older_than_hours is not None
        else float(older_than_days) * 24
    )
    if idle_hours <= 0 or max_sessions <= 0:
        return {"sessions": 0, "runs": 0, "rows": 0}

    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=idle_hours)
    ).isoformat()

    def _work(cur: Any) -> Dict[str, int]:
        global _compaction_cursor
        candidates = _idle_candidates(
            cur, cutoff, keep_tail_events, max_sessions, _compaction_cursor
        )
        if not candidates and _compaction_cursor:
            # Wrapped past the end — restart the rotation from the beginning.
            _compaction_cursor = ""
            candidates = _idle_candidates(
                cur, cutoff, keep_tail_events, max_sessions, ""
            )
        totals = {"sessions": 0, "runs": 0, "rows": 0}
        for session_id in candidates:
            _compaction_cursor = session_id
            try:
                runs, rows = _compact_session_deltas(
                    cur, session_id, keep_tail_events, max_runs_per_session, False
                )
            except Exception:
                logger.warning(
                    "session_events: delta compaction failed for %s",
                    session_id, exc_info=True,
                )
                continue
            if rows:
                totals["sessions"] += 1
                totals["runs"] += runs
                totals["rows"] += rows
        return totals

    if cursor is not None:
        totals = _work(cursor)
    else:
        with get_db() as conn:
            totals = _work(conn.cursor())

    if totals["rows"]:
        logger.info(
            "session_events: compacted %d delta run(s) into %d fewer row(s) "
            "across %d session(s) idle more than %g hour(s)",
            totals["runs"], totals["rows"], totals["sessions"], idle_hours,
        )
    return totals


def _idle_candidates(
    cur: Any,
    cutoff: str,
    keep_tail_events: int,
    limit: int,
    after_session_id: str,
) -> List[str]:
    """Idle sessions with enough history to be worth compacting, id-ordered."""
    # The lease-valid-active-run exclusion is belt-and-braces on top of the
    # idle window (a streaming session bumps ``sessions.updated_at`` constantly,
    # so it cannot be inside the cutoff anyway) — the predicate is verbatim from
    # app/db/runs.py. It runs over at most ``limit`` candidate rows, not a list
    # page, so the correlated probe is not a hot path.
    cur.execute(
        "SELECT c.session_id AS session_id FROM session_event_counters c "
        "JOIN sessions s ON s.id = c.session_id "
        "WHERE c.next_seq > %s AND s.updated_at < %s AND c.session_id > %s "
        "AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.session_id = c.session_id "
        "AND r.state = 'active' AND r.lease_expires_at >= NOW()) "
        "ORDER BY c.session_id ASC LIMIT %s",
        (int(keep_tail_events) + 1, cutoff, after_session_id, int(limit)),
    )
    return [r["session_id"] for r in rows_to_list(cur.fetchall())]
