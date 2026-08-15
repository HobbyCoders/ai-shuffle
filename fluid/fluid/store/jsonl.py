"""JSONL-backed SessionStore — append-only node log per session.

Layout (one directory per store):
  ``<session_id>.jsonl``         node/head log; first line is a ``meta`` header,
                                 then one line per ``append`` (a ``node`` record),
                                 one ``head`` record whenever the head moves
                                 (append, fork, rewind), and one ``cli_session``
                                 record whenever a delegated backend refreshes its
                                 resume handle.
  ``<session_id>.events.jsonl``  the durable Event log — one sequenced event per
                                 line — so ``events_since`` survives a restart.

Everything is append-only: reloading replays the log in order (last ``head``
record wins), which reconstructs an identical branching tree. Sessions load
lazily on first touch and are cached in memory thereafter.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fluid.events.model import Event, event_from_dict, event_to_dict
from fluid.messages import Message
from fluid.store.base import SessionMeta, SessionNode, new_id


class JsonlStore:
    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, SessionMeta] = {}
        self._nodes: dict[str, dict[str, SessionNode]] = {}
        self._events: dict[str, list[Event]] = {}
        self._loaded: set[str] = set()

    # ─── Paths ───────────────────────────────────────────────────────────

    def _log_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.jsonl"

    def _events_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.events.jsonl"

    def _append_line(self, path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    # ─── Lazy load ───────────────────────────────────────────────────────

    def _ensure_loaded(self, session_id: str) -> bool:
        """Load a session's log from disk once. Returns True if it exists."""
        if session_id in self._loaded:
            return session_id in self._sessions
        self._loaded.add(session_id)
        log = self._log_path(session_id)
        if not log.exists():
            return False

        meta: SessionMeta | None = None
        nodes: dict[str, SessionNode] = {}
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            kind = rec["t"]
            if kind == "meta":
                meta = SessionMeta(
                    session_id=rec["session_id"],
                    head_node_id=rec.get("head_node_id"),
                    created_at=rec.get("created_at", 0.0),
                    cli_session_id=rec.get("cli_session_id"),
                )
            elif kind == "node":
                n = rec["node"]
                nodes[n["node_id"]] = SessionNode(
                    node_id=n["node_id"],
                    parent_id=n["parent_id"],
                    message=Message.from_dict(n["message"]),
                    ts=n.get("ts", 0.0),
                )
            elif kind == "head":
                if meta is not None:
                    meta.head_node_id = rec["head_node_id"]
            elif kind == "cli_session":
                if meta is not None:
                    meta.cli_session_id = rec.get("cli_session_id")
        if meta is None:  # corrupt/empty log
            return False

        events: list[Event] = []
        epath = self._events_path(session_id)
        if epath.exists():
            for line in epath.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(event_from_dict(json.loads(line)))

        self._sessions[session_id] = meta
        self._nodes[session_id] = nodes
        self._events[session_id] = events
        return True

    # ─── SessionStore protocol ───────────────────────────────────────────

    async def create_session(self, session_id: str | None = None) -> SessionMeta:
        meta = SessionMeta(session_id=session_id or new_id("ses"))
        self._sessions[meta.session_id] = meta
        self._nodes[meta.session_id] = {}
        self._events[meta.session_id] = []
        self._loaded.add(meta.session_id)
        self._append_line(
            self._log_path(meta.session_id),
            {"t": "meta", "session_id": meta.session_id,
             "created_at": meta.created_at, "head_node_id": meta.head_node_id,
             "cli_session_id": meta.cli_session_id},
        )
        return meta

    async def get_session(self, session_id: str) -> SessionMeta | None:
        if self._ensure_loaded(session_id):
            return self._sessions[session_id]
        return None

    async def append(self, session_id: str, node: SessionNode) -> None:
        self._ensure_loaded(session_id)
        self._nodes[session_id][node.node_id] = node
        self._sessions[session_id].head_node_id = node.node_id
        path = self._log_path(session_id)
        self._append_line(path, {"t": "node", "node": {
            "node_id": node.node_id, "parent_id": node.parent_id,
            "ts": node.ts, "message": node.message.to_dict(),
        }})
        self._append_line(path, {"t": "head", "head_node_id": node.node_id})

    async def get_node(self, session_id: str, node_id: str) -> SessionNode | None:
        self._ensure_loaded(session_id)
        return self._nodes.get(session_id, {}).get(node_id)

    async def load_path(
        self, session_id: str, leaf_node_id: str | None = None
    ) -> list[SessionNode]:
        self._ensure_loaded(session_id)
        meta = self._sessions[session_id]
        nodes = self._nodes[session_id]
        cursor = leaf_node_id or meta.head_node_id
        path: list[SessionNode] = []
        while cursor is not None:
            node = nodes[cursor]
            path.append(node)
            cursor = node.parent_id
        path.reverse()
        return path

    async def set_head(self, session_id: str, node_id: str | None) -> None:
        self._ensure_loaded(session_id)
        self._sessions[session_id].head_node_id = node_id
        self._append_line(self._log_path(session_id), {"t": "head", "head_node_id": node_id})

    async def set_cli_session_id(self, session_id: str, cli_session_id: str | None) -> None:
        if not self._ensure_loaded(session_id):
            return
        self._sessions[session_id].cli_session_id = cli_session_id
        self._append_line(
            self._log_path(session_id),
            {"t": "cli_session", "cli_session_id": cli_session_id},
        )

    async def append_event(self, session_id: str, event: Event) -> None:
        self._ensure_loaded(session_id)
        self._events.setdefault(session_id, []).append(event)
        self._append_line(self._events_path(session_id), event_to_dict(event))

    async def events_since(self, session_id: str, seq: int) -> list[Event]:
        self._ensure_loaded(session_id)
        return [e for e in self._events.get(session_id, []) if e.seq > seq]
