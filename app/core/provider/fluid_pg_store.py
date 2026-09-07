"""PgFluidStore — Postgres-backed implementation of fluid's SessionStore protocol.

Persists fluid's branching session node-tree and durable event log in the app's
PostgreSQL database (tables ``fluid_sessions`` / ``fluid_session_nodes`` /
``fluid_session_events``, schema v70), giving flag-on sessions the same
cross-restart durability the SDK path gets from PgSessionStore — resume, fork,
and event replay all survive a process restart and work from any app instance
sharing the database.

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

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from app.db.async_database import run_sync
from app.db.base import get_db

# Same Postgres-illegal code-point scrub the SDK session store applies at its
# own jsonb boundary — see the comment block above ``_PG_ILLEGAL_RE`` there for
# why NUL and lone surrogates have to go before an INSERT sees them.
from app.core.pg_session_store import _scrub_pg_illegal

# Issue R12: a model pasted a live DATABASE_URL (password included) into a
# code-mode cell and it was persisted verbatim. Every string on its way into
# these two jsonb columns gets its URL-embedded passwords replaced with ``***``
# first. New rows only — existing rows are untouched by design.
from app.core.scrub import scrub_url_credentials

# Side-effect import: registers AI Shuffle's legacy ``SafetyWarning`` shape
# with fluid's event codec. Must precede any ``event_from_dict`` call, or
# sessions persisted before fluid stopped emitting that event fail to decode.
from app.core.provider import legacy_events as _legacy_events  # noqa: F401

from fluid.events.model import Event, event_from_dict, event_to_dict
from fluid.messages import Message
from fluid.store.base import SessionMeta, SessionNode, new_id, project_root_for
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
    """Load meta + all nodes + all events for one session (single trip)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, head_node_id, created_at, cli_session_id, "
            "project_root FROM fluid_sessions WHERE session_id = %s",
            (session_id,),
        )
        meta_row = cur.fetchone()
        if meta_row is None:
            return None
        cur.execute(
            "SELECT node_id, parent_id, ts, message FROM fluid_session_nodes "
            "WHERE session_id = %s",
            (session_id,),
        )
        node_rows = cur.fetchall()
        cur.execute(
            "SELECT event FROM fluid_session_events WHERE session_id = %s "
            "ORDER BY seq",
            (session_id,),
        )
        event_rows = cur.fetchall()
    return {"meta": dict(meta_row), "nodes": [dict(r) for r in node_rows],
            "events": [r["event"] for r in event_rows]}


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


def _append_event_sync(session_id: str, event_dict: Dict[str, Any], seq: int) -> None:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO fluid_session_events (session_id, seq, event) "
            "VALUES (%s, %s, %s) ON CONFLICT (session_id, seq) DO NOTHING",
            # Same scrub as the node path: ToolUse.input / ToolArgsDelta.
            # partial_json / TextDelta.text carry the very same cell text.
            (session_id, seq,
             Json(_scrub_pg_illegal(scrub_url_credentials(event_dict)))),
        )
        # The conflict clause is there for legitimate bus replays, but it also
        # hides real loss: an engine whose in-memory log restarted at seq 1 for
        # a resumed session re-uses numbers that already exist, and its events
        # vanish. Never let that be silent again.
        if cur.rowcount == 0:
            logger.warning(
                "fluid event DROPPED by seq collision: session=%s seq=%s "
                "type=%s (a row with that seq already exists)",
                session_id, seq, event_dict.get("_cls"),
            )


# ─── Cross-session search ────────────────────────────────────────────────────
#
# Two arms, because a session's text lives in one of two places depending on
# which leg wrote it: ``fluid_session_nodes`` holds the native node tree this
# store owns, ``session_store_entries`` holds the SDK/delegated transcript
# mirror. Both are swept in one connection and merged into one per-session
# accumulator.
#
# The SQL is only a PREFILTER — cheap ILIKE over the raw JSON to cut the rows
# the Python side has to parse. The substring test in ``_accumulate`` is the
# real match, which also makes ``%``/``_`` inside a user's term harmless: an
# over-wide LIKE just hands us rows that then fail the Python check.

_SEARCH_ROW_CAP = 2000        # rows pulled per arm before Python scoring
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
    score = total term hits, last_ts = newest matched row)."""
    for text in texts:
        lowered = text.lower()
        n_hit = sum(1 for t in terms if t in lowered)
        if not n_hit:
            continue
        slot = acc.setdefault(session_id, {"score": 0, "last_ts": 0.0, "texts": []})
        slot["score"] += n_hit
        slot["last_ts"] = max(slot["last_ts"], ts)
        if len(slot["texts"]) < _SEARCH_TEXTS_PER_SESSION:
            slot["texts"].append((role, text))


def _titles_for(cur, session_ids: List[str]) -> Dict[str, str]:
    """Session titles from the app's ``sessions`` table, keyed by store id.

    ``sessions.sdk_session_id`` holds the store-side id for BOTH legs: the
    fluid ``ses_…`` id on native sessions and the SDK uuid on delegated ones,
    so one lookup serves every recall arm.
    """
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    placeholders = ", ".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT sdk_session_id, title FROM sessions "
        f"WHERE sdk_session_id IN ({placeholders})",
        ids,
    )
    return {
        row["sdk_session_id"]: str(row["title"])
        for row in cur.fetchall()
        if row.get("title")
    }


def _roots_for(cur, session_ids: List[str]) -> Dict[str, str]:
    """Recorded project roots from ``fluid_sessions``, keyed by session id.

    Only native fluid sessions have a row here; SDK-mirror sessions come back
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


def _search_sync(
    terms: List[str], limit: int, exclude_session_id: Optional[str]
) -> List[SessionSearchHit]:
    patterns = [f"%{t}%" for t in terms]
    acc: Dict[str, Dict[str, Any]] = {}

    with get_db() as conn:
        cur = conn.cursor()

        # Arm A — this store's own native session node tree.
        where = " OR ".join(["message::text ILIKE %s"] * len(patterns))
        params: List[Any] = list(patterns)
        sql = (
            f"SELECT session_id, ts, message FROM fluid_session_nodes "
            f"WHERE ({where})"
        )
        if exclude_session_id:
            sql += " AND session_id != %s"
            params.append(exclude_session_id)
        cur.execute(sql + f" ORDER BY ts DESC LIMIT {_SEARCH_ROW_CAP}", params)
        for row in cur.fetchall():
            message = _as_dict(row["message"])
            _accumulate(
                acc,
                row["session_id"],
                str(message.get("role") or "?"),
                message_texts(message),
                float(row["ts"] or 0.0),
                terms,
            )

        # Arm B — the SDK transcript mirror (subpath IS NULL = the main
        # transcript; sidechain files live under a subpath and are not recall).
        where = " OR ".join(["entry::text ILIKE %s"] * len(patterns))
        params = list(patterns)
        sql = (
            f"SELECT session_id, created_at, entry FROM session_store_entries "
            f"WHERE subpath IS NULL AND ({where})"
        )
        if exclude_session_id:
            sql += " AND session_id != %s"
            params.append(exclude_session_id)
        cur.execute(sql + f" ORDER BY id DESC LIMIT {_SEARCH_ROW_CAP}", params)
        for row in cur.fetchall():
            entry = _as_dict(row["entry"])
            _accumulate(
                acc,
                row["session_id"],
                str(entry.get("type") or "?"),
                _sse_entry_texts(entry),
                _entry_ts(row["created_at"]),
                terms,
            )

    hits = [
        SessionSearchHit(
            session_id=sid,
            score=slot["score"],
            last_ts=slot["last_ts"],
            texts=slot["texts"],
        )
        for sid, slot in acc.items()
        if slot["score"] > 0
    ]
    hits.sort(key=lambda h: (-h.score, -h.last_ts))
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
_PREVIEW_SCAN_ROWS = 25       # newest transcript rows scanned for a preview
_PREVIEW_CHARS = 120
_TOOL_LINE_CAP = 200


def _clip(text: str, cap: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= cap else flat[: cap - 1] + "…"


def _sse_recall_message(entry: Dict[str, Any], ts: float) -> Optional[RecallMessage]:
    """One SDK transcript entry as a RecallMessage, or None for non-messages.

    The SDK arm's twin of ``fluid.store.base.recall_message``: user/assistant
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
    """The session's SDK transcript as RecallMessages, or None when absent."""
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
    with get_db() as conn:
        cur = conn.cursor()
        sql = (
            "SELECT sdk_session_id, title, updated_at FROM sessions "
            "WHERE sdk_session_id IS NOT NULL "
            "AND COALESCE(hidden, FALSE) = FALSE "
            "AND status NOT IN ('archived', 'failed')"
        )
        params: List[Any] = []
        if project_id is not None:
            sql += " AND project_id = %s"
            params.append(project_id)
        if exclude_session_id:
            sql += " AND sdk_session_id != %s"
            params.append(exclude_session_id)
        cur.execute(sql + " ORDER BY updated_at DESC LIMIT %s", params + [limit])
        rows = [dict(r) for r in cur.fetchall()]
        roots = _roots_for(cur, [r["sdk_session_id"] for r in rows])
        out: List[SessionSummary] = []
        for row in rows:
            store_id = row["sdk_session_id"]
            preview, count = _preview_sync(cur, store_id)
            out.append(SessionSummary(
                session_id=store_id,
                title=str(row["title"]) if row.get("title") else None,
                last_ts=_entry_ts(row.get("updated_at")),
                preview=preview,
                message_count=count,
                project_root=roots.get(store_id),
            ))
    return out


def _slice_sync(session_id: str, start: int, count: int) -> Optional[SessionSlice]:
    with get_db() as conn:
        cur = conn.cursor()
        messages = _fluid_recall_rows(cur, session_id)
        if messages is None:
            messages = _sse_recall_rows(cur, session_id)
        if messages is None:
            return None
        title = _titles_for(cur, [session_id]).get(session_id)
    total = len(messages)
    count = max(1, int(count))
    first = max(0, total + start) if start < 0 else min(int(start), total)
    return SessionSlice(
        session_id=session_id,
        total=total,
        start=first,
        messages=messages[first:first + count],
        title=title,
        last_ts=max((m.ts for m in messages), default=0.0),
    )


# ─── Async SessionStore ──────────────────────────────────────────────────────

class PgFluidStore:
    """fluid ``SessionStore`` over the app's PostgreSQL database."""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionMeta] = {}
        self._nodes: Dict[str, Dict[str, SessionNode]] = {}
        self._events: Dict[str, List[Event]] = {}
        self._loaded: set = set()

    async def _ensure_loaded(self, session_id: str) -> bool:
        if session_id in self._loaded:
            return session_id in self._sessions
        self._loaded.add(session_id)
        data = await run_sync(_load_session_sync, session_id)
        if data is None:
            return False
        meta_row = data["meta"]
        meta = SessionMeta(
            session_id=meta_row["session_id"],
            head_node_id=meta_row["head_node_id"],
            created_at=meta_row["created_at"],
            cli_session_id=meta_row.get("cli_session_id"),
            project_root=meta_row.get("project_root"),
        )
        nodes: Dict[str, SessionNode] = {}
        for r in data["nodes"]:
            nodes[r["node_id"]] = SessionNode(
                node_id=r["node_id"],
                parent_id=r["parent_id"],
                message=Message.from_dict(_as_dict(r["message"])),
                ts=r["ts"],
            )
        events = [event_from_dict(_as_dict(e)) for e in data["events"]]
        self._sessions[session_id] = meta
        self._nodes[session_id] = nodes
        self._events[session_id] = events
        return True

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
        self._sessions[meta.session_id] = meta
        self._nodes[meta.session_id] = {}
        self._events[meta.session_id] = []
        self._loaded.add(meta.session_id)
        await run_sync(_create_session_sync, meta)
        return meta

    async def get_session(self, session_id: str) -> Optional[SessionMeta]:
        if await self._ensure_loaded(session_id):
            return self._sessions[session_id]
        return None

    async def append(self, session_id: str, node: SessionNode) -> None:
        await self._ensure_loaded(session_id)
        self._nodes.setdefault(session_id, {})[node.node_id] = node
        if session_id in self._sessions:
            self._sessions[session_id].head_node_id = node.node_id
        await run_sync(_append_node_sync, session_id, node)

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
        await self._ensure_loaded(session_id)
        self._events.setdefault(session_id, []).append(event)
        await run_sync(
            _append_event_sync, session_id, event_to_dict(event),
            getattr(event, "seq", -1),
        )

    async def events_since(self, session_id: str, seq: int) -> List[Event]:
        await self._ensure_loaded(session_id)
        return [e for e in self._events.get(session_id, []) if e.seq > seq]

    async def search(
        self,
        terms: List[str],
        *,
        limit: int = 5,
        exclude_session_id: Optional[str] = None,
    ) -> List[SessionSearchHit]:
        """fluid's optional SessionStore recall hook (see
        ``fluid/fluid/store/base.py``) — backs the SessionSearch builtin
        over both the native fluid node tree and the SDK transcript mirror.
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
    ) -> Optional[SessionSlice]:
        """SessionView's backend — a message window from whichever transcript
        store the session wrote (fluid node path, else SDK mirror)."""
        return await run_sync(_slice_sync, session_id, int(start), int(count))
