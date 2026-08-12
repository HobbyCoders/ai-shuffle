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
from typing import Protocol

from loom.messages import Message


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


class SessionStore(Protocol):
    async def create_session(self, session_id: str | None = None) -> SessionMeta: ...
    async def get_session(self, session_id: str) -> SessionMeta | None: ...
    async def append(self, session_id: str, node: SessionNode) -> None: ...
    async def get_node(self, session_id: str, node_id: str) -> SessionNode | None: ...
    async def load_path(self, session_id: str, leaf_node_id: str | None = None) -> list[SessionNode]: ...


class MemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionMeta] = {}
        self._nodes: dict[str, dict[str, SessionNode]] = {}

    async def create_session(self, session_id: str | None = None) -> SessionMeta:
        meta = SessionMeta(session_id=session_id or new_id("ses"))
        self._sessions[meta.session_id] = meta
        self._nodes[meta.session_id] = {}
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
