"""PgOmniStore — Postgres-backed implementation of omni's SessionStore protocol.

Persists omni's branching session node-tree and durable event log in the app's
PostgreSQL database (tables ``omni_sessions`` / ``omni_session_nodes`` /
``omni_session_events``, schema v70), giving flag-on sessions the same
cross-restart durability the SDK path gets from PgSessionStore — resume, fork,
and event replay all survive a process restart and work from any app instance
sharing the database.

Semantics mirror ``omni.store.jsonl.JsonlStore``: per-session lazy load into a
memory cache, then write-through on every mutation. The cache makes the hot
read paths (``load_path`` on every prompt build, ``events_since`` on every
receive loop) synchronous-fast; Postgres writes ride ``run_sync`` so the event
loop never blocks on the driver.

Event appends are idempotent per (session_id, seq) — ``ON CONFLICT DO
NOTHING`` — because omni's EventBus can replay a tail after reconnect and the
store must not duplicate rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from app.db.async_database import run_sync
from app.db.base import get_db

from omni.events.model import Event, event_from_dict, event_to_dict
from omni.messages import Message
from omni.store.base import SessionMeta, SessionNode, new_id

logger = logging.getLogger(__name__)


def _as_dict(value: Any) -> Dict[str, Any]:
    """jsonb columns arrive as dicts from psycopg2 but as JSON strings from the
    test shim's SQLite — normalize."""
    return value if isinstance(value, dict) else json.loads(value)


# ─── Sync DB primitives (run under run_sync) ─────────────────────────────────

def _create_session_sync(meta: SessionMeta) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO omni_sessions "
            "(session_id, head_node_id, created_at, cli_session_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
            (meta.session_id, meta.head_node_id, meta.created_at,
             meta.cli_session_id),
        )


def _load_session_sync(session_id: str) -> Optional[Dict[str, Any]]:
    """Load meta + all nodes + all events for one session (single trip)."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, head_node_id, created_at, cli_session_id "
            "FROM omni_sessions WHERE session_id = %s",
            (session_id,),
        )
        meta_row = cur.fetchone()
        if meta_row is None:
            return None
        cur.execute(
            "SELECT node_id, parent_id, ts, message FROM omni_session_nodes "
            "WHERE session_id = %s",
            (session_id,),
        )
        node_rows = cur.fetchall()
        cur.execute(
            "SELECT event FROM omni_session_events WHERE session_id = %s "
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
            "INSERT INTO omni_session_nodes "
            "(session_id, node_id, parent_id, ts, message) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (session_id, node_id) DO NOTHING",
            (session_id, node.node_id, node.parent_id, node.ts,
             Json(node.message.to_dict())),
        )
        cur.execute(
            "UPDATE omni_sessions SET head_node_id = %s WHERE session_id = %s",
            (node.node_id, session_id),
        )


def _set_head_sync(session_id: str, node_id: Optional[str]) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE omni_sessions SET head_node_id = %s WHERE session_id = %s",
            (node_id, session_id),
        )


def _set_cli_session_id_sync(session_id: str, cli_session_id: Optional[str]) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE omni_sessions SET cli_session_id = %s WHERE session_id = %s",
            (cli_session_id, session_id),
        )


def _append_event_sync(session_id: str, event_dict: Dict[str, Any], seq: int) -> None:
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO omni_session_events (session_id, seq, event) "
            "VALUES (%s, %s, %s) ON CONFLICT (session_id, seq) DO NOTHING",
            (session_id, seq, Json(event_dict)),
        )


# ─── Async SessionStore ──────────────────────────────────────────────────────

class PgOmniStore:
    """omni ``SessionStore`` over the app's PostgreSQL database."""

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

    async def create_session(self, session_id: Optional[str] = None) -> SessionMeta:
        # Resuming an existing id returns the stored meta (JsonlStore parity:
        # its meta record would be replayed rather than re-created).
        if session_id and await self._ensure_loaded(session_id):
            return self._sessions[session_id]
        meta = SessionMeta(session_id=session_id or new_id("ses"))
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
        a restart resumed the CLI with the omni session id and the turn died.
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
