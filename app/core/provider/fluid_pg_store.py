"""PgFluidStore — Postgres-backed implementation of fluid's SessionStore protocol.

Persists fluid's branching session node-tree and durable event log in the app's
PostgreSQL database (tables ``fluid_sessions`` / ``fluid_session_nodes`` /
``fluid_session_events``), giving native fluid sessions cross-restart
durability — resume, fork, and event replay all survive a process restart and
work from any app instance sharing the database.

Semantics mirror ``fluid.store.jsonl.JsonlStore``: per-session lazy load into a
memory cache, then write-through on every mutation. The cache makes the hot
read paths (``load_path`` on every prompt build, ``events_since`` on every
receive loop) synchronous-fast; Postgres writes ride ``run_sync`` so the event
loop never blocks on the driver.

Event appends are idempotent per (session_id, seq) — ``ON CONFLICT DO
NOTHING`` — because fluid's EventBus can replay a tail after reconnect and the
store must not duplicate rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from psycopg2.extras import Json

from app.core import api_user_scope, artifact_access
from app.db.async_database import run_sync
from app.db.base import get_db

# Same Postgres-illegal code-point scrub the Claude session store applies at its
# own jsonb boundary — see the comment block above ``_PG_ILLEGAL_RE`` there for
# why NUL and lone surrogates have to go before an INSERT sees them.
from app.core.pg_session_store import _scrub_pg_illegal

# A model can paste a live DATABASE_URL (password included) into a code-mode
# cell (see app.core.scrub). Every string on its way into
# these two jsonb columns gets its URL-embedded passwords replaced with ``***``
# first. New rows only — existing rows are untouched by design.
from app.core.scrub import scrub_url_credentials

# Side-effect import: registers AI Shuffle's legacy ``SafetyWarning`` shape
# with fluid's event codec. Must precede any ``event_from_dict`` call, or
# sessions persisted before fluid stopped emitting that event fail to decode.
from app.core.provider import legacy_events as _legacy_events  # noqa: F401

from fluid.events.model import Event, event_from_dict, event_to_dict
from fluid.messages import Message
from fluid.store.base import (
    RECONSTRUCTIBLE_EVENTS,
    SessionMeta,
    SessionNode,
    new_id,
    project_root_for,
)
from fluid.store.base import (
    RecallMessage,
    SessionSearchHit,
    SessionSlice,
    SessionSummary,
    message_texts,
    recall_message,
)

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> Dict[str, Any]:
    """jsonb columns arrive as dicts from psycopg2 but as JSON strings from the
    test shim's SQLite — normalize."""
    return value if isinstance(value, dict) else json.loads(value)


# ─── Sync DB primitives (run under run_sync) ─────────────────────────────────

def _create_session_sync(meta: SessionMeta) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO fluid_sessions "
            "(session_id, head_node_id, created_at, cli_session_id, project_root) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
            (meta.session_id, meta.head_node_id, meta.created_at,
             meta.cli_session_id, meta.project_root),
        )


def _load_session_sync(session_id: str) -> Optional[Dict[str, Any]]:
    """Load the node tree without retaining the unbounded event history."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, head_node_id, created_at, cli_session_id, "
            "project_root FROM fluid_sessions WHERE session_id = %s", (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        meta = SessionMeta(**dict(row))
        cur.execute(
            "SELECT node_id, parent_id, ts, message FROM fluid_session_nodes "
            "WHERE session_id = %s", (session_id,),
        )
        rows = cur.fetchall()
    # Decode off the event loop, and release the connection first.
    nodes = {r["node_id"]: SessionNode(
        node_id=r["node_id"], parent_id=r["parent_id"], ts=r["ts"],
        message=Message.from_dict(_as_dict(r["message"])),
    ) for r in rows}
    return {"meta": meta, "nodes": nodes}


def _events_since_sync(session_id: str, seq: int) -> List[Event]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT event FROM fluid_session_events WHERE session_id = %s "
            "AND seq > %s ORDER BY seq", (session_id, seq),
        )
        rows = cur.fetchall()
    return [event_from_dict(_as_dict(r["event"])) for r in rows]


def _events_page_sync(session_id: str, seq: int, through: int, limit: int) -> List[Event]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT event FROM fluid_session_events WHERE session_id = %s "
            "AND seq > %s AND seq <= %s ORDER BY seq LIMIT %s",
            (session_id, seq, through, limit),
        )
        rows = cur.fetchall()
    return [event_from_dict(_as_dict(r["event"])) for r in rows]


# Upper bound for an open-ended page (``seq`` is a PostgreSQL bigint).
_MAX_SEQ = 2**63 - 1


def _last_event_seq_sync(session_id: str) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT seq FROM fluid_session_events WHERE session_id = %s "
            "ORDER BY seq DESC LIMIT 1", (session_id,),
        )
        row = cur.fetchone()
    return int(row["seq"]) if row else 0


def _append_node_sync(session_id: str, node: SessionNode) -> None:
    """Insert the node and advance the head in one transaction (JsonlStore
    parity: append implies head-move)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO fluid_session_nodes "
            "(session_id, node_id, parent_id, ts, message) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id, node_id) DO NOTHING",
            (session_id, node.node_id, node.parent_id, node.ts,
             Json(_scrub_pg_illegal(
                 scrub_url_credentials(node.message.to_dict())))),
        )
        cur.execute(
            "UPDATE fluid_sessions SET head_node_id = %s WHERE session_id = %s",
            (node.node_id, session_id),
        )


def _set_head_sync(session_id: str, node_id: Optional[str]) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE fluid_sessions SET head_node_id = %s WHERE session_id = %s",
            (node_id, session_id),
        )


def _set_cli_session_id_sync(session_id: str, cli_session_id: Optional[str]) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE fluid_sessions SET cli_session_id = %s WHERE session_id = %s",
            (cli_session_id, session_id),
        )


def _append_events_sync(session_id: str, events: List[Event]) -> None:
    # One transaction for consecutive reconstructible deltas and their barrier.
    payloads = [(e.seq, _scrub_pg_illegal(scrub_url_credentials(event_to_dict(e))))
                for e in events]
    with get_db() as conn:
        cur = conn.cursor()
        for seq, payload in payloads:
            cur.execute(
                "INSERT INTO fluid_session_events (session_id, seq, event) "
                "VALUES (%s, %s, %s) ON CONFLICT (session_id, seq) DO NOTHING",
                (session_id, seq, Json(payload)),
            )
            if cur.rowcount == 0:
                logger.warning(
                    "fluid event DROPPED by seq collision: session=%s seq=%s "
                    "type=%s (a row with that seq already exists)",
                    session_id, seq, payload.get("_cls"),
                )


# ─── Cross-session search ────────────────────────────────────────────────────
#
# Two arms, because a session's text lives in one of two places depending on
# which leg wrote it: ``fluid_session_nodes`` holds the native node tree this
# store owns, ``session_store_entries`` holds the Claude-format (delegated)
# transcript. Both are swept in one connection and merged into one per-session
# accumulator.
#
# The SQL is only a PREFILTER — cheap ILIKE over the raw JSON to cut the rows
# the Python side has to parse. The substring test in ``_accumulate`` is the
# real match, which also makes ``%``/``_`` inside a user's term harmless: an
# over-wide LIKE just hands us rows that then fail the Python check.

# The prefilter is bounded by SESSIONS, not rows: a common term in thousands
# of newer rows must not push an older session's match out of the window.
_SEARCH_PREFILTER_SESSIONS = 200  # candidate sessions per arm, best match first
_SEARCH_ROWS_PER_SESSION = 20     # matching rows scored per candidate session
_SEARCH_TEXTS_PER_SESSION = 3  # matched texts kept per session (tool trims too)
_SEARCH_SESSION_CAP = 25       # sessions returned (``limit`` is a hint, not a promise)


def _sse_entry_texts(entry: Dict[str, Any]) -> List[str]:
    """The Python twin of the ``sse_entry_text`` SQL function (app/db/database.py).

    Same rules, same skips — user/assistant only, no meta and no sidechain rows
    — so a search hit means the same thing whichever arm found it.
    """
    if entry.get("type") not in ("user", "assistant"):
        return []
    if entry.get("isMeta") or entry.get("isSidechain"):
        return []
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            str(block["text"])
            for block in content
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ]
    return []


def _entry_ts(value: Any) -> float:
    """``created_at`` is a datetime under psycopg2 and a string under the shim."""
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _accumulate(
    acc: Dict[str, Dict[str, Any]],
    session_id: str,
    role: str,
    texts: List[str],
    ts: float,
    terms: List[str],
) -> None:
    """Score one row's texts into the per-session accumulator (fluid's contract:
    score = total term hits, best = most terms in one text, last_ts = newest
    matched row). The texts kept are the ones holding the most terms."""
    for text in texts:
        lowered = text.lower()
        n_hit = sum(1 for t in terms if t in lowered)
        if not n_hit:
            continue
        slot = acc.setdefault(
            session_id, {"score": 0, "best": 0, "last_ts": 0.0, "texts": []}
        )
        slot["score"] += n_hit
        slot["best"] = max(slot["best"], n_hit)
        slot["last_ts"] = max(slot["last_ts"], ts)
        kept = slot["texts"]
        if len(kept) < _SEARCH_TEXTS_PER_SESSION:
            kept.append((n_hit, role, text))
        else:
            weakest = min(range(len(kept)), key=lambda i: kept[i][0])
            if n_hit > kept[weakest][0]:
                kept[weakest] = (n_hit, role, text)


def _titles_for(cur, session_ids: List[str]) -> Dict[str, str]:
    """Session titles from the app's ``sessions`` table, keyed by recall id.

    ``sessions.sdk_session_id`` holds the store-side id for BOTH legs: the
    fluid ``ses_…`` id on native sessions and the Claude uuid on delegated ones.
    A session another worker runs has no store id here, so it is recalled by
    its app id — one lookup matches either.
    """
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT id, sdk_session_id, title FROM sessions "
        f"WHERE sdk_session_id IN ({placeholders}) OR id IN ({placeholders})",
        ids + ids,
    )
    wanted = set(ids)
    titles: Dict[str, str] = {}
    for row in cur.fetchall():
        if not row.get("title"):
            continue
        for key in (row.get("sdk_session_id"), row.get("id")):
            if key in wanted:
                titles[key] = str(row["title"])
    return titles


def _roots_for(cur, session_ids: List[str]) -> Dict[str, str]:
    """Recorded project roots from ``fluid_sessions``, keyed by session id.

    Only native fluid sessions have a row here; Claude-transcript sessions come back
    absent, which SessionSearch reads as "unknown project" — never excluded.
    """
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT session_id, project_root FROM fluid_sessions "
        f"WHERE session_id IN ({placeholders})",
        ids,
    )
    return {
        row["session_id"]: str(row["project_root"])
        for row in cur.fetchall()
        if row.get("project_root")
    }


# ─── Sessions another worker runs ────────────────────────────────────────────
#
# A chat another worker of the owner's network runs is mirrored here as a
# ``sessions`` row plus its ``session_events``. ``sdk_session_id`` is local-only
# (never mirrored), so the mirror has none and no transcript-store row exists
# on this worker: its recall reads the events, under its app id. Chats this
# worker runs are never read this way — their store rows answer.

_EVENT_TEXT_TYPES = ("message.user", "message.assistant", "assistant.text_delta")
_EVENT_VIEW_TYPES = _EVENT_TEXT_TYPES + ("tool.use", "tool.result")
_WORKER_ID_CHARS = 12


def _my_device_id() -> Optional[str]:
    """This worker's network device id; None when it is not enrolled."""
    try:
        from app.network import ownership

        return ownership.my_device_id()
    except Exception:  # noqa: BLE001 - no network context means no mirrors
        logger.debug("fluid_pg_store: device id unavailable", exc_info=True)
        return None


def _worker_name(device_id: Optional[str]) -> str:
    """The owning worker's name, else a short form of its device id."""
    from app.core.remote_task import worker_display_name

    name = worker_display_name(device_id)
    return name[:_WORKER_ID_CHARS] if name == device_id else name


def _remote_sql(alias: str = "") -> str:
    """WHERE fragment for a sessions row another worker runs (one ``%s``: me)."""
    return (
        f"{alias}sdk_session_id IS NULL AND {alias}owner_device_id IS NOT NULL "
        f"AND {alias}owner_device_id <> %s"
    )


def _in(values: List[str]) -> str:
    return ", ".join(["%s"] * len(values))


def _event_ts(payload: Dict[str, Any], created_at: Any) -> float:
    stamp = payload.get("timestamp")
    return _entry_ts(stamp) if stamp else _entry_ts(created_at)


def _fold_events(rows: List[Dict[str, Any]]) -> List[RecallMessage]:
    """A session's ``session_events`` (oldest first) as RecallMessages.

    The same shape the store arms give: a user message; an assistant message
    whose text is its streamed deltas (or final text) joined, with one line
    per tool call; then a "user" message holding the tool results, as fluid
    records them. Thinking and everything that is not a message is skipped.
    """
    from app.core.harness_reminders import strip_harness_reminders
    from app.db.session_events import _parse_payload

    out: List[RecallMessage] = []
    open_msg: Optional[RecallMessage] = None
    last_delta = False
    for row in rows:
        etype = row["event_type"]
        payload = _parse_payload(row.get("payload"))
        ts = _event_ts(payload, row.get("created_at"))
        if etype == "message.user":
            text = strip_harness_reminders(str(payload.get("content") or ""))
            out.append(RecallMessage(role="user", text=text, ts=ts))
            open_msg, last_delta = None, False
        elif etype in ("assistant.text_delta", "message.assistant"):
            delta = etype == "assistant.text_delta"
            if not delta and payload.get("kind", "text") != "text":
                continue
            piece = str(payload.get("text" if delta else "content") or "")
            if not piece:
                continue
            if open_msg is None or open_msg.role != "assistant":
                open_msg = RecallMessage(role="assistant", ts=ts)
                out.append(open_msg)
                last_delta = False
            if delta and last_delta:
                open_msg.text += piece
            else:
                open_msg.text = f"{open_msg.text}\n{piece}" if open_msg.text else piece
            last_delta = delta
        elif etype == "tool.use":
            if open_msg is None or open_msg.role != "assistant":
                open_msg = RecallMessage(role="assistant", ts=ts)
                out.append(open_msg)
            try:
                arg = json.dumps(payload.get("input") or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                arg = "…"
            open_msg.tools.append(
                _clip(f"{payload.get('name') or '?'}({arg})", _TOOL_LINE_CAP)
            )
            last_delta = False
        elif etype == "tool.result":
            if open_msg is None or open_msg.role != "user":
                open_msg = RecallMessage(role="user", ts=ts)
                out.append(open_msg)
            marker = "ERROR" if payload.get("is_error") else "->"
            preview = str(payload.get("content_preview") or "") or "(no text)"
            open_msg.tools.append(_clip(f"{marker} {preview}", _TOOL_LINE_CAP))
            last_delta = False
    return out


def _event_recall_rows(cur, app_id: str, types=_EVENT_VIEW_TYPES) -> List[RecallMessage]:
    cur.execute(
        "SELECT event_type, payload, created_at FROM session_events "
        f"WHERE session_id = %s AND event_type IN ({_in(list(types))}) ORDER BY seq",
        [app_id, *types],
    )
    return _fold_events([dict(r) for r in cur.fetchall()])


def _event_preview_sync(cur, app_id: str) -> tuple:
    """(last assistant text, message count) for a session another worker runs.

    Tool payloads are left out: the fold's message boundaries do not depend
    on them, and they are most of a session's bytes.
    """
    cur.execute(
        "SELECT event_type, created_at, CASE WHEN event_type IN "
        f"({_in(list(_EVENT_TEXT_TYPES))}) THEN payload ELSE NULL END AS payload "
        "FROM session_events "
        f"WHERE session_id = %s AND event_type IN ({_in(list(_EVENT_VIEW_TYPES))}) "
        "ORDER BY seq",
        [*_EVENT_TEXT_TYPES, app_id, *_EVENT_VIEW_TYPES],
    )
    messages = _fold_events([dict(r) for r in cur.fetchall()])
    for message in reversed(messages):
        if message.role == "assistant" and message.text.strip():
            return _clip(message.text, _PREVIEW_CHARS), len(messages)
    return "", len(messages)


def _exclusion_ids(cur, session_id: Optional[str]) -> List[str]:
    """The calling session under every id recall may know it by."""
    if not session_id:
        return []
    cur.execute(
        "SELECT id, sdk_session_id FROM sessions WHERE id = %s OR sdk_session_id = %s",
        (session_id, session_id),
    )
    ids = {session_id}
    for row in cur.fetchall():
        ids.update(v for v in (row.get("id"), row.get("sdk_session_id")) if v)
    return sorted(ids)


def _hits_sql(column: str, n: int) -> str:
    """How many query terms one row's ``column`` contains (prefilter count)."""
    return " + ".join([f"(CASE WHEN {column} ILIKE %s THEN 1 ELSE 0 END)"] * n)


def _candidate_sessions(
    cur, source: str, source_params: List[Any], column: str, ts: str,
    where: str, where_params: List[Any], patterns: List[str], owner: str = "NULL",
) -> List[Dict[str, Any]]:
    """The arm's best ``_SEARCH_PREFILTER_SESSIONS`` sessions over ``source``:
    most terms in one row first, then the newest match."""
    any_term = " OR ".join([f"{column} ILIKE %s"] * len(patterns))
    cur.execute(
        "SELECT session_id, MAX(h) AS best, MAX(t) AS last, MAX(owner) AS owner FROM ("
        f"SELECT session_id, {ts} AS t, {_hits_sql(column, len(patterns))} AS h, "
        f"{owner} AS owner FROM {source} WHERE {where} AND ({any_term})"
        ") m GROUP BY session_id ORDER BY best DESC, last DESC "
        f"LIMIT {_SEARCH_PREFILTER_SESSIONS}",
        list(patterns) + list(source_params) + list(where_params) + list(patterns),
    )
    return [dict(r) for r in cur.fetchall()]


def _top_rows(cur, table: str, column: str, ts: str, where: str, where_params: List[Any],
              select: str, patterns: List[str]) -> List[Dict[str, Any]]:
    """The strongest ``_SEARCH_ROWS_PER_SESSION`` rows of each of the arm's
    best ``_SEARCH_PREFILTER_SESSIONS`` sessions. Score the stored search text
    first, then fetch JSON only for the capped rows. Break session ties by
    session_id and row ties by the unique row key, independent of scan plan."""
    # Each term's index recheck already establishes one hit. Rechecking all
    # terms again in CASE expressions repeatedly detoasts/scans large rows
    # (including terms whose index did not select the row). UNION ALL retains
    # duplicate patterns and LIKE wildcards exactly as the old hit sum did.
    # Keep only row keys in the materialized CTE, never transcript bytes.
    key = "node_id" if table == "fluid_session_nodes" else "id"
    matches = " UNION ALL ".join(
        f"SELECT session_id, {key}, {ts} AS t, 1 AS h FROM {table} "
        f"WHERE {where} AND {column} ILIKE %s" for _ in patterns
    )
    if len(patterns) > 1:
        matches = (
            f"SELECT session_id, {key}, t, COUNT(*)::integer AS h FROM ({matches}) terms "
            f"GROUP BY session_id, {key}, t"
        )
    params = [p for pattern in patterns for p in [*where_params, pattern]]
    selected = ", ".join(f"d.{name.strip()}" for name in select.split(","))
    cur.execute(
        f"WITH m AS ({matches}), "
        "s AS (SELECT session_id, MAX(h) AS best, MAX(t) AS last FROM m "
        f"GROUP BY session_id ORDER BY best DESC, last DESC, session_id LIMIT {_SEARCH_PREFILTER_SESSIONS}), "
        "r AS (SELECT m.*, ROW_NUMBER() OVER (PARTITION BY m.session_id "
        f"ORDER BY m.h DESC, m.t DESC, m.{key}) AS rn FROM m JOIN s ON s.session_id = m.session_id) "
        f"SELECT {selected} FROM r JOIN {table} d ON d.session_id = r.session_id "
        f"AND d.{key} = r.{key} WHERE rn <= {_SEARCH_ROWS_PER_SESSION} "
        f"ORDER BY r.session_id, r.h DESC, r.t DESC, r.{key}",
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def _first_readable(items: List[Any], reader: Dict[str, Any], cap: int, *, key) -> List[Any]:
    """The first ``cap`` of ``items`` (in order) whose chat ``reader`` may read."""
    kept: List[Any] = []
    for item in items:
        if len(kept) >= cap:
            break
        if artifact_access.readable_store_ids([key(item)], reader):
            kept.append(item)
    return kept


def _search_sync(
    terms: List[str], limit: int, exclude_session_id: Optional[str]
) -> List[SessionSearchHit]:
    patterns = [f"%{t}%" for t in terms]
    acc: Dict[str, Dict[str, Any]] = {}
    workers: Dict[str, str] = {}
    me = _my_device_id()

    with get_db() as conn:
        cur = conn.cursor()
        # Maintenance is opt-in. Old databases (and an incomplete/invalid
        # cache) retain the indexed raw-JSON query with identical semantics.
        from app.db.schema_postgres import session_recall_cache_ready

        cached = session_recall_cache_ready(cur)
        if cached:
            # The heap can contain little but TOAST pointers. PG's default
            # per-tuple cost misses fetching/scanning the stored transcript,
            # making a selective GIN lookup look dearer than a full scan.
            # Price that work only in this recall transaction, without
            # disabling scans or lowering an operator's higher cost setting.
            cur.execute(
                "SELECT set_config('cpu_tuple_cost', "
                "GREATEST(current_setting('cpu_tuple_cost')::numeric, 0.05)::text, true)"
            )
        excluded = _exclusion_ids(cur, exclude_session_id)
        not_excluded = f"session_id NOT IN ({_in(excluded)})" if excluded else "TRUE"

        # Arm A — this store's own native session node tree.
        for row in _top_rows(
            cur, "fluid_session_nodes", "recall_text" if cached else "message::text", "ts", not_excluded, excluded,
            "session_id, ts, message", patterns,
        ):
            message = _as_dict(row["message"])
            _accumulate(
                acc, row["session_id"], str(message.get("role") or "?"),
                message_texts(message), float(row["ts"] or 0.0), terms,
            )

        # Arm B — the Claude-format transcript (subpath IS NULL = the main
        # transcript; sidechain files live under a subpath and are not recall).
        for row in _top_rows(
            cur, "session_store_entries", "recall_text" if cached else "entry::text", "created_at",
            f"subpath IS NULL AND {not_excluded}", excluded,
            "session_id, created_at, entry", patterns,
        ):
            entry = _as_dict(row["entry"])
            _accumulate(
                acc, row["session_id"], str(entry.get("type") or "?"),
                _sse_entry_texts(entry), _entry_ts(row["created_at"]), terms,
            )

        # Arm C — sessions another worker runs, from their mirrored events.
        # A match inside one streamed delta selects the session; the whole
        # folded conversation is then scored, so a phrase split across
        # deltas still counts as one text.
        if me:
            types = list(_EVENT_TEXT_TYPES)
            event_column = "recall_text" if cached else "payload"
            source = (
                f"(SELECT e.session_id, e.created_at, e.{event_column}, s.owner_device_id AS owner "
                "FROM session_events e JOIN sessions s ON s.id = e.session_id "
                f"WHERE {_remote_sql('s.')} AND e.event_type IN ({_in(types)})) ev"
            )
            found = _candidate_sessions(
                cur, source, [me, *types], "recall_text" if cached else "payload::text", "created_at",
                not_excluded, excluded, patterns, owner="owner",
            )
            if found:
                ids = [r["session_id"] for r in found]
                workers = {r["session_id"]: r["owner"] for r in found}
                cur.execute(
                    "SELECT session_id, event_type, payload, created_at FROM session_events "
                    f"WHERE session_id IN ({_in(ids)}) AND event_type IN ({_in(types)}) "
                    "ORDER BY session_id, seq",
                    ids + types,
                )
                by_session: Dict[str, List[Dict[str, Any]]] = {}
                for row in cur.fetchall():
                    by_session.setdefault(row["session_id"], []).append(dict(row))
                for sid, rows in by_session.items():
                    for message in _fold_events(rows):
                        _accumulate(acc, sid, message.role, [message.text], message.ts, terms)

    # An API key's session recalls only its own sessions; the owner, all.
    api_user_id = api_user_scope.viewer_api_user_id(exclude_session_id)
    if api_user_id:
        visible = api_user_scope.visible_store_ids(acc, api_user_id)
        acc = {sid: slot for sid, slot in acc.items() if sid in visible}
    hits = [
        SessionSearchHit(
            session_id=sid,
            score=slot["score"],
            last_ts=slot["last_ts"],
            texts=[(role, text) for _n, role, text in
                   sorted(slot["texts"], key=lambda t: -t[0])],
            best=slot["best"],
        )
        for sid, slot in acc.items()
        if slot["score"] > 0
    ]
    hits.sort(key=lambda h: (-h.best, -h.score, -h.last_ts))
    # A chat a member's device started recalls only what that device may
    # read over REST (#1897). Checked best-first, so only the hits that make
    # the cut cost a lookup.
    reader = artifact_access.chat_reader(exclude_session_id)
    if reader is not None:
        hits = _first_readable(hits, reader, _SEARCH_SESSION_CAP, key=lambda h: h.session_id)
    hits = hits[:_SEARCH_SESSION_CAP]
    if hits:
        with get_db() as conn:
            cur = conn.cursor()
            ids = [h.session_id for h in hits]
            titles = _titles_for(cur, ids)
            roots = _roots_for(cur, ids)
        for hit in hits:
            hit.title = titles.get(hit.session_id)
            hit.project_root = roots.get(hit.session_id)
            if workers.get(hit.session_id):
                hit.worker = _worker_name(workers[hit.session_id])
    return hits


# ─── Recent-session listing + session viewing ────────────────────────────────
#
# The other two recall hooks (fluid's contract comment in store/base.py):
# ``recent_sessions`` backs SessionSearch's no-query listing, ``session_slice``
# backs SessionView. The ``sessions`` app table is the listing's spine — it is
# the one place both legs' sessions carry a title, a project, and a live
# status — while previews and message windows come from whichever transcript
# store the session actually wrote.

_RECENT_LIMIT_CAP = 25
# A member's chat lists what its device may read, so its listing pages
# through newer chats it may not; bounded so a long history stays cheap.
_READER_PAGE = 50
_READER_SCAN_CAP = 500
_PREVIEW_SCAN_ROWS = 25       # newest transcript rows scanned for a preview
_PREVIEW_CHARS = 120
_TOOL_LINE_CAP = 200


def _clip(text: str, cap: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= cap else flat[: cap - 1] + "…"


def _sse_recall_message(entry: Dict[str, Any], ts: float) -> Optional[RecallMessage]:
    """One Claude transcript entry as a RecallMessage, or None for non-messages.

    The Claude arm's twin of ``fluid.store.base.recall_message``: user/assistant
    entries only, sidechain and meta rows invisible, tool_use/tool_result
    blocks compacted to one line each.
    """
    etype = entry.get("type")
    if etype not in ("user", "assistant"):
        return None
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    content = (entry.get("message") or {}).get("content")
    texts: List[str] = []
    tools: List[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                texts.append(str(block["text"]))
            elif btype == "tool_use":
                try:
                    arg = json.dumps(block.get("input") or {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    arg = "…"
                tools.append(_clip(f"{block.get('name', '?')}({arg})", _TOOL_LINE_CAP))
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    inner_text = " ".join(
                        str(c.get("text", ""))
                        for c in inner
                        if isinstance(c, dict) and c.get("type") == "text" and c.get("text")
                    )
                else:
                    inner_text = str(inner or "")
                marker = "ERROR" if block.get("is_error") else "->"
                tools.append(_clip(f"{marker} {inner_text or '(no text)'}", _TOOL_LINE_CAP))
    if not texts and not tools:
        return None
    return RecallMessage(role=str(etype), text="\n".join(texts), tools=tools, ts=ts)


def _fluid_recall_rows(cur, session_id: str) -> Optional[List[RecallMessage]]:
    """The session's active fluid node path as RecallMessages, or None when the
    session is not a fluid one."""
    cur.execute(
        "SELECT head_node_id FROM fluid_sessions WHERE session_id = %s",
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute(
        "SELECT node_id, parent_id, ts, message FROM fluid_session_nodes "
        "WHERE session_id = %s",
        (session_id,),
    )
    nodes = {r["node_id"]: dict(r) for r in cur.fetchall()}
    path: List[Dict[str, Any]] = []
    cursor_id = row["head_node_id"]
    while cursor_id is not None:
        node = nodes.get(cursor_id)
        if node is None:
            break
        path.append(node)
        cursor_id = node["parent_id"]
    path.reverse()
    return [
        recall_message(_as_dict(r["message"]), ts=float(r["ts"] or 0.0))
        for r in path
    ]


def _sse_recall_rows(cur, session_id: str) -> Optional[List[RecallMessage]]:
    """The session's Claude transcript as RecallMessages, or None when absent."""
    cur.execute(
        "SELECT entry, created_at FROM session_store_entries "
        "WHERE session_id = %s AND subpath IS NULL ORDER BY id",
        (session_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    out: List[RecallMessage] = []
    for r in rows:
        msg = _sse_recall_message(_as_dict(r["entry"]), _entry_ts(r["created_at"]))
        if msg is not None:
            out.append(msg)
    return out


def _preview_sync(cur, session_id: str) -> tuple:
    """(last assistant text, message count) for one listed session, cheaply.

    Scans only the newest ``_PREVIEW_SCAN_ROWS`` transcript rows — a listing
    never needs the whole session.
    """
    cur.execute(
        "SELECT COUNT(*) AS n FROM fluid_session_nodes WHERE session_id = %s",
        (session_id,),
    )
    fluid_count = int((cur.fetchone() or {}).get("n") or 0)
    if fluid_count:
        cur.execute(
            "SELECT message FROM fluid_session_nodes WHERE session_id = %s "
            f"ORDER BY ts DESC LIMIT {_PREVIEW_SCAN_ROWS}",
            (session_id,),
        )
        for row in cur.fetchall():
            message = _as_dict(row["message"])
            if message.get("role") != "assistant":
                continue
            texts = [
                str(b.get("text"))
                for b in message.get("content") or []
                if isinstance(b, dict) and b.get("kind") == "text" and b.get("text")
            ]
            if texts:
                return _clip(" ".join(texts), _PREVIEW_CHARS), fluid_count
        return "", fluid_count
    cur.execute(
        "SELECT COUNT(*) AS n FROM session_store_entries "
        "WHERE session_id = %s AND subpath IS NULL",
        (session_id,),
    )
    sse_count = int((cur.fetchone() or {}).get("n") or 0)
    if sse_count:
        cur.execute(
            "SELECT entry, created_at FROM session_store_entries "
            "WHERE session_id = %s AND subpath IS NULL "
            f"ORDER BY id DESC LIMIT {_PREVIEW_SCAN_ROWS}",
            (session_id,),
        )
        for row in cur.fetchall():
            entry = _as_dict(row["entry"])
            if entry.get("type") != "assistant":
                continue
            texts = _sse_entry_texts(entry)
            if texts:
                return _clip(" ".join(texts), _PREVIEW_CHARS), sse_count
    return "", sse_count


def _recent_sync(
    limit: int, exclude_session_id: Optional[str], project_id: Optional[str]
) -> List[SessionSummary]:
    limit = max(1, min(int(limit), _RECENT_LIMIT_CAP))
    api_user_id = api_user_scope.viewer_api_user_id(exclude_session_id)
    # A chat a member's device started lists only what that device may read
    # over REST (#1897); the rule needs the whole row.
    reader = artifact_access.chat_reader(exclude_session_id)
    me = _my_device_id()
    columns = "*" if reader is not None else "id, sdk_session_id, owner_device_id, title, updated_at"
    params: List[Any] = []
    listed = "sdk_session_id IS NOT NULL"
    if me:
        # ...and the sessions another of the owner's workers runs.
        listed = f"({listed} OR ({_remote_sql()}))"
        params.append(me)
    sql = (
        f"SELECT {columns} FROM sessions "
        f"WHERE {listed} "
        "AND COALESCE(hidden, FALSE) = FALSE "
        "AND status NOT IN ('archived', 'failed')"
    )
    if project_id is not None:
        sql += " AND project_id = %s"
        params.append(project_id)
    if api_user_id:
        # (Workers are hidden, so the column alone is the key's listing.)
        sql += " AND api_user_id = %s"
        params.append(api_user_id)
    with get_db() as conn:
        excluded = _exclusion_ids(conn.cursor(), exclude_session_id)
    if excluded:
        sql += (
            f" AND id NOT IN ({_in(excluded)})"
            f" AND COALESCE(sdk_session_id, '') NOT IN ({_in(excluded)})"
        )
        params += excluded + excluded
    sql += " ORDER BY updated_at DESC LIMIT %s OFFSET %s"
    rows: List[Dict[str, Any]] = []
    page = limit if reader is None else _READER_PAGE
    offset = 0
    while True:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(sql, params + [page, offset])
            batch = [dict(r) for r in cur.fetchall()]
        rows += [r for r in batch
                 if artifact_access.chat_reader_may_read(reader, r)]
        offset += page
        if (reader is None or len(rows) >= limit or len(batch) < page
                or offset >= _READER_SCAN_CAP):
            break
    rows = rows[:limit]
    with get_db() as conn:
        cur = conn.cursor()
        roots = _roots_for(cur, [r["sdk_session_id"] for r in rows if r["sdk_session_id"]])
        out: List[SessionSummary] = []
        for row in rows:
            remote = not row["sdk_session_id"]
            store_id = row["id"] if remote else row["sdk_session_id"]
            preview, count = (
                _event_preview_sync(cur, store_id) if remote
                else _preview_sync(cur, store_id)
            )
            out.append(SessionSummary(
                session_id=store_id,
                title=str(row["title"]) if row.get("title") else None,
                last_ts=_entry_ts(row.get("updated_at")),
                preview=preview,
                message_count=count,
                project_root=roots.get(store_id),
                worker=_worker_name(row.get("owner_device_id")) if remote else None,
            ))
    return out


def _resolve_recall_sync(cur, session_id: str):
    """(recall id, messages, sessions row, remote?) for either id, or None.

    The store id answers first (fluid node path, else Claude transcript); an
    app id resolves to its store id, or — for a session another worker runs —
    to its mirrored events.
    """
    for arm in (_fluid_recall_rows, _sse_recall_rows):
        messages = arm(cur, session_id)
        if messages is not None:
            return session_id, messages, None, False
    cur.execute("SELECT id, sdk_session_id, owner_device_id, title FROM sessions WHERE id = %s",
                (session_id,))
    row = cur.fetchone()
    if row is None:
        return None
    row = dict(row)
    if row.get("sdk_session_id"):
        for arm in (_fluid_recall_rows, _sse_recall_rows):
            messages = arm(cur, row["sdk_session_id"])
            if messages is not None:
                return row["sdk_session_id"], messages, row, False
        return None
    me = _my_device_id()
    owner = row.get("owner_device_id")
    if not me or not owner or owner == me:
        return None
    return row["id"], _event_recall_rows(cur, row["id"]), row, True


def _slice_sync(
    session_id: str, start: int, count: int, viewer_session_id: Optional[str] = None,
) -> Optional[SessionSlice]:
    api_user_id = api_user_scope.viewer_api_user_id(viewer_session_id)
    if api_user_id and session_id not in api_user_scope.visible_store_ids(
        [session_id], api_user_id
    ):
        return None  # another principal's session reads as unknown
    # A member's device's chat opens only what that device may read (#1897).
    reader = artifact_access.chat_reader(viewer_session_id)
    if reader is not None and not artifact_access.readable_store_ids([session_id], reader):
        return None
    with get_db() as conn:
        cur = conn.cursor()
        resolved = _resolve_recall_sync(cur, session_id)
        if resolved is None:
            return None
        recall_id, messages, row, remote = resolved
        title = (row or {}).get("title") or _titles_for(cur, [recall_id]).get(recall_id)
    total = len(messages)
    count = max(1, int(count))
    first = max(0, total + start) if start < 0 else min(int(start), total)
    return SessionSlice(
        session_id=recall_id,
        total=total,
        start=first,
        messages=messages[first:first + count],
        title=str(title) if title else None,
        last_ts=max((m.ts for m in messages), default=0.0),
        worker=_worker_name(row.get("owner_device_id")) if remote else None,
    )


# ─── Async SessionStore ──────────────────────────────────────────────────────

class PgFluidStore:
    """fluid ``SessionStore`` over the app's PostgreSQL database."""

    EVENT_CACHE_LIMIT = 128
    STREAM_BATCH_LIMIT = 32
    STREAM_BATCH_BYTES = 64 * 1024
    STREAM_BATCH_SECONDS = 0.05

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionMeta] = {}
        self._nodes: Dict[str, Dict[str, SessionNode]] = {}
        self._events: Dict[str, List[Event]] = {}
        self._event_floor: Dict[str, int] = {}
        self._pending_events: Dict[str, List[Event]] = {}
        self._pending_since: Dict[str, float] = {}
        self._pending_bytes: Dict[str, int] = {}
        self._loaded: set = set()
        self._load_lock = asyncio.Lock()
        self._event_lock = asyncio.Lock()

    async def _ensure_loaded(self, session_id: str) -> bool:
        async with self._load_lock:
            if session_id in self._loaded:
                return session_id in self._sessions
            data = await run_sync(_load_session_sync, session_id)
            # A failed read must be retryable; never publish a half-loaded cache.
            if data is not None:
                self._sessions[session_id] = data["meta"]
                self._nodes[session_id] = data["nodes"]
            self._loaded.add(session_id)
            return data is not None

    def _cache_events(self, session_id: str, events: List[Event], floor: int) -> None:
        tail = events[-self.EVENT_CACHE_LIMIT:]
        self._events[session_id] = tail
        self._event_floor[session_id] = max(floor, tail[0].seq - 1) if len(events) > len(tail) else floor

    async def _write_pending(self, session_id: str, extra: Optional[Event] = None) -> None:
        # Accepted deltas remain retryable until the transaction acknowledges
        # them. The caller owns retrying an unacknowledged durable extra.
        events = list(self._pending_events.get(session_id, []))
        if extra is not None:
            events.append(extra)
        if not events:
            return
        # A lost commit acknowledgement can leave durable rows behind. Never
        # serve a pre-write cache after either success or an uncertain failure.
        self._events.pop(session_id, None)
        self._event_floor.pop(session_id, None)
        await run_sync(_append_events_sync, session_id, events)
        self._pending_events.pop(session_id, None)
        self._pending_since.pop(session_id, None)
        self._pending_bytes.pop(session_id, None)

    async def append_stream_event(self, session_id: str, event: Event) -> None:
        """Optional engine hook (native and delegated, via ``persist_event``);
        only reconstructible deltas may be buffered.

        Flush on the next delta after 50ms, at 32 events, or before a durable
        event/node/replay. No background task or timer survives a parked turn.
        A crash may lose this bounded tail, never a durable lifecycle event.
        """
        if not isinstance(event, RECONSTRUCTIBLE_EVENTS):
            await self.append_event(session_id, event)
            return
        async with self._event_lock:
            pending = self._pending_events.setdefault(session_id, [])
            pending.append(event)
            since = self._pending_since.setdefault(session_id, time.monotonic())
            text = getattr(event, "text", getattr(event, "partial_json", ""))
            # Four bytes per codepoint is a conservative UTF-8 payload bound.
            size = self._pending_bytes.get(session_id, 0) + 4 * len(text)
            self._pending_bytes[session_id] = size
            if (len(pending) >= self.STREAM_BATCH_LIMIT or size >= self.STREAM_BATCH_BYTES
                    or time.monotonic() - since >= self.STREAM_BATCH_SECONDS):
                await self._write_pending(session_id)

    async def flush_events(self, session_id: str) -> None:
        async with self._event_lock:
            await self._write_pending(session_id)

    async def last_event_seq(self, session_id: str) -> int:
        await self.flush_events(session_id)
        return await run_sync(_last_event_seq_sync, session_id)

    # ─── SessionStore protocol ───────────────────────────────────────

    async def create_session(
        self, session_id: Optional[str] = None, *, cwd: Optional[str] = None
    ) -> SessionMeta:
        # Resuming an existing id returns the stored meta (JsonlStore parity:
        # its meta record would be replayed rather than re-created).
        if session_id and await self._ensure_loaded(session_id):
            return self._sessions[session_id]
        meta = SessionMeta(
            session_id=session_id or new_id("ses"),
            project_root=project_root_for(cwd),
        )
        await run_sync(_create_session_sync, meta)
        self._sessions[meta.session_id] = meta
        self._nodes[meta.session_id] = {}
        self._events[meta.session_id] = []
        self._loaded.add(meta.session_id)
        return meta

    async def get_session(self, session_id: str) -> Optional[SessionMeta]:
        if await self._ensure_loaded(session_id):
            return self._sessions[session_id]
        return None

    async def append(self, session_id: str, node: SessionNode) -> None:
        await self.flush_events(session_id)
        await self._ensure_loaded(session_id)
        await run_sync(_append_node_sync, session_id, node)
        self._nodes.setdefault(session_id, {})[node.node_id] = node
        if session_id in self._sessions:
            self._sessions[session_id].head_node_id = node.node_id

    async def get_node(self, session_id: str, node_id: str) -> Optional[SessionNode]:
        await self._ensure_loaded(session_id)
        return self._nodes.get(session_id, {}).get(node_id)

    async def load_path(
        self, session_id: str, leaf_node_id: Optional[str] = None
    ) -> List[SessionNode]:
        await self._ensure_loaded(session_id)
        meta = self._sessions[session_id]
        nodes = self._nodes[session_id]
        cursor = leaf_node_id or meta.head_node_id
        path: List[SessionNode] = []
        while cursor is not None:
            node = nodes[cursor]
            path.append(node)
            cursor = node.parent_id
        path.reverse()
        return path

    async def set_head(self, session_id: str, node_id: Optional[str]) -> None:
        await self._ensure_loaded(session_id)
        if session_id in self._sessions:
            self._sessions[session_id].head_node_id = node_id
        await run_sync(_set_head_sync, session_id, node_id)

    async def set_cli_session_id(
        self, session_id: str, cli_session_id: Optional[str]
    ) -> None:
        """Persist the delegated CLI's ``--resume`` handle for this session.

        DelegatedLoop calls this every time the CLI's ``init`` frame reports a
        new id. Without it the handle only ever lived on the engine object, so
        a restart resumed the CLI with the fluid session id and the turn died.
        """
        await self._ensure_loaded(session_id)
        meta = self._sessions.get(session_id)
        if meta is not None:
            meta.cli_session_id = cli_session_id
        await run_sync(_set_cli_session_id_sync, session_id, cli_session_id)

    async def append_event(self, session_id: str, event: Event) -> None:
        async with self._event_lock:
            await self._write_pending(session_id, event)

    async def iter_events(
        self, session_id: str, seq: int, *, page_size: int = EVENT_CACHE_LIMIT,
    ) -> AsyncIterator[Event]:
        """Replay a finite committed prefix without retaining the whole history.

        Decode each page off-loop and release its connection before yielding.
        Later writes belong to the next replay, even if they arrive mid-page.
        Gaps from retention are legal: the cursor advances by sequence, not count.
        """
        if not 1 <= page_size <= 1024:
            raise ValueError("page_size must be between 1 and 1024")
        async with self._event_lock:
            await self._write_pending(session_id)
            through = await run_sync(_last_event_seq_sync, session_id)
        while seq < through:
            page = await run_sync(_events_page_sync, session_id, seq, through, page_size)
            if not page:
                break
            for event in page:
                yield event
            seq = page[-1].seq

    async def events_page(
        self, session_id: str, seq: int, limit: int = EVENT_CACHE_LIMIT,
    ) -> List[Event]:
        """At most ``limit`` committed events after ``seq``, oldest first.

        The EventBus replay source (``fluid.store.base.replay_source``): a
        subscriber behind the bus ring catches up one page per call instead of
        loading everything after its cursor at once. Batched deltas are
        committed first, so a page never skips an event the bus published.
        """
        if not 1 <= limit <= 1024:
            raise ValueError("limit must be between 1 and 1024")
        async with self._event_lock:
            await self._write_pending(session_id)
        return await run_sync(_events_page_sync, session_id, seq, _MAX_SEQ, limit)

    async def events_since(self, session_id: str, seq: int) -> List[Event]:
        async with self._event_lock:
            await self._write_pending(session_id)
            if session_id in self._event_floor and seq >= self._event_floor[session_id]:
                return [e for e in self._events[session_id] if e.seq > seq]
            events = await run_sync(_events_since_sync, session_id, seq)
            self._cache_events(session_id, events, seq)
            return events

    async def search(
        self,
        terms: List[str],
        *,
        limit: int = 5,
        exclude_session_id: Optional[str] = None,
    ) -> List[SessionSearchHit]:
        """fluid's optional SessionStore recall hook (see
        ``fluid/fluid/store/base.py``) — backs the SessionSearch builtin
        over both the native fluid node tree and the Claude transcript store.
        """
        return await run_sync(_search_sync, list(terms), int(limit), exclude_session_id)

    async def recent_sessions(
        self,
        *,
        limit: int = 10,
        exclude_session_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[SessionSummary]:
        """SessionSearch's no-query listing — newest sessions first, with the
        app's own titles and a last-reply preview. ``project_id`` narrows to
        one project when the host binds it (the tool itself never passes it).
        """
        return await run_sync(_recent_sync, int(limit), exclude_session_id, project_id)

    async def session_slice(
        self,
        session_id: str,
        *,
        start: int = -10,
        count: int = 10,
        viewer_session_id: Optional[str] = None,
    ) -> Optional[SessionSlice]:
        """SessionView's backend — a message window from whichever transcript
        store the session wrote (fluid node path, else Claude transcript).

        ``viewer_session_id`` is the calling session: an API key's session
        may open only its own sessions (the owner's opens any)."""
        return await run_sync(
            _slice_sync, session_id, int(start), int(count), viewer_session_id
        )
