"""SessionStore protocol + in-memory implementation.

Sessions are branching trees of ``SessionNode``s (one node per Message).
``load_path`` resolves leaf→root and returns root-first — this is the
conversation history for a prompt build. Fork = start appending under an
older node; nothing is copied, so the shared prefix stays byte-identical
(which is what makes rewind/fork cache-preserving).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from fluid.events.model import Event
from fluid.messages import Message


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class SessionNode:
    node_id: str
    parent_id: str | None
    message: Message
    ts: float = field(default_factory=time.time)


@dataclass
class SessionMeta:
    session_id: str
    head_node_id: str | None = None  # latest appended node (default parent for new turns)
    created_at: float = field(default_factory=time.time)
    # Backend-owned resume handle for engines that delegate to an external
    # agent process (DelegatedLoop's ``claude --resume <uuid>``). It is NOT the
    # fluid session id and must survive a restart, otherwise a resumed session
    # hands the child a handle it has never seen.
    cli_session_id: str | None = None


@dataclass
class SessionSearchHit:
    """One session's answer to a keyword search — see ``SessionStore.search``.

    ``texts`` carries the matched texts in full (role, text); the caller cuts
    them down to snippets, so a backend never has to guess the presentation.
    """

    session_id: str
    score: int  # total query-term hits summed over the session's matched texts
    last_ts: float = 0.0  # newest matched node's epoch timestamp (0.0 if unknown)
    texts: list[tuple[str, str]] = field(default_factory=list)


def message_texts(message: dict[str, Any]) -> list[str]:
    """Every searchable text in a serialized message (assistant text + tool results)."""
    out: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "text" and block.get("text"):
            out.append(str(block["text"]))
        elif kind == "toolresultblock":
            for inner in block.get("content") or []:
                if isinstance(inner, dict) and inner.get("kind") == "text" and inner.get("text"):
                    out.append(str(inner["text"]))
    return out


class SessionStore(Protocol):
    async def create_session(self, session_id: str | None = None) -> SessionMeta: ...
    async def get_session(self, session_id: str) -> SessionMeta | None: ...
    async def append(self, session_id: str, node: SessionNode) -> None: ...
    async def get_node(self, session_id: str, node_id: str) -> SessionNode | None: ...
    async def load_path(self, session_id: str, leaf_node_id: str | None = None) -> list[SessionNode]: ...
    async def set_head(self, session_id: str, node_id: str | None) -> None:
        """Move the session head (fork/rewind) without appending a node."""
        ...
    async def set_cli_session_id(self, session_id: str, cli_session_id: str | None) -> None:
        """Persist the delegated backend's resume handle on the session meta."""
        ...
    async def append_event(self, session_id: str, event: Event) -> None:
        """Persist a sequenced event for durable, cross-restart replay."""
        ...
    async def events_since(self, session_id: str, seq: int) -> list[Event]:
        """Return persisted events with ``seq`` strictly greater than the cursor."""
        ...

    # ─── Optional: cross-session recall ──────────────────────────────────
    #
    #     async def search(
    #         self,
    #         terms: list[str],
    #         *,
    #         limit: int = 5,
    #         exclude_session_id: str | None = None,
    #     ) -> list[SessionSearchHit]: ...
    #
    # Deliberately NOT part of the protocol above — a store that keeps no
    # durable record (MemoryStore) cannot answer it, and requiring it would
    # make that store fail the protocol. A store that CAN answer just defines
    # the method; the loader detects it and injects the bound method as the
    # SessionSearch builtin's backend, so any backing (directory scan, SQL,
    # a vector index) serves the same tool.
    #
    # Contract: ``terms`` arrive already lowercased and are matched as
    # case-insensitive substrings; a text matches if it contains ANY term.
    # ``exclude_session_id`` is the caller's own session — its history belongs
    # to HistorySearch, so skip it. ``limit`` is a hint, not a promise:
    # over-returning is fine, because the tool sorts and trims.


class MemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionMeta] = {}
        self._nodes: dict[str, dict[str, SessionNode]] = {}
        self._events: dict[str, list[Event]] = {}

    async def create_session(self, session_id: str | None = None) -> SessionMeta:
        meta = SessionMeta(session_id=session_id or new_id("ses"))
        self._sessions[meta.session_id] = meta
        self._nodes[meta.session_id] = {}
        self._events[meta.session_id] = []
        return meta

    async def get_session(self, session_id: str) -> SessionMeta | None:
        return self._sessions.get(session_id)

    async def append(self, session_id: str, node: SessionNode) -> None:
        self._nodes[session_id][node.node_id] = node
        self._sessions[session_id].head_node_id = node.node_id

    async def get_node(self, session_id: str, node_id: str) -> SessionNode | None:
        return self._nodes.get(session_id, {}).get(node_id)

    async def load_path(
        self, session_id: str, leaf_node_id: str | None = None
    ) -> list[SessionNode]:
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
        self._sessions[session_id].head_node_id = node_id

    async def set_cli_session_id(self, session_id: str, cli_session_id: str | None) -> None:
        meta = self._sessions.get(session_id)
        if meta is not None:
            meta.cli_session_id = cli_session_id

    async def append_event(self, session_id: str, event: Event) -> None:
        self._events.setdefault(session_id, []).append(event)

    async def events_since(self, session_id: str, seq: int) -> list[Event]:
        return [e for e in self._events.get(session_id, []) if e.seq > seq]
