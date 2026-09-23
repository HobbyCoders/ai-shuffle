"""SessionStore protocol + in-memory implementation.

Sessions are branching trees of ``SessionNode``s (one node per Message).
``load_path`` resolves leaf→root and returns root-first — this is the
conversation history for a prompt build. Fork = start appending under an
older node; nothing is copied, so the shared prefix stays byte-identical
(which is what makes rewind/fork cache-preserving).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from fluid.events.model import Event, TextDelta, ThinkingDelta, ToolArgsDelta
from fluid.messages import Message

log = logging.getLogger(__name__)

# Events whose loss costs a gap in the replay log and nothing else: streaming
# progress whose full content the turn's stored node carries. Only these may
# wait in a store's stream batch; every other event is a durable barrier.
RECONSTRUCTIBLE_EVENTS = (TextDelta, ThinkingDelta, ToolArgsDelta)


async def iter_events(store: SessionStore, session_id: str, seq: int) -> AsyncIterator[Event]:
    """Use bounded replay when offered; retain compatibility with older stores."""
    replay = getattr(store, "iter_events", None)
    if callable(replay):
        async for event in replay(session_id, seq):
            yield event
    else:
        for event in await store.events_since(session_id, seq):
            yield event


def replay_source(store: SessionStore) -> Any:
    """The EventBus replay source for ``store``: one bounded page per call.

    The bus asks again from its new cursor until it reaches its ring, so a
    subscriber far behind never pulls the whole history into one list. Stores
    without ``events_page`` keep the unbounded ``events_since``.
    """
    page = getattr(store, "events_page", None)
    return page if callable(page) else store.events_since


async def persist_event(store: SessionStore, event: Event) -> None:
    """Write one sequenced event, letting the store batch reconstructible deltas.

    A store with ``append_stream_event`` may hold a delta in memory until its
    batch fills or a durable event arrives; the durable event then commits the
    held deltas and itself in one transaction, in seq order. Failures raise.
    """
    stream_append = getattr(store, "append_stream_event", None)
    if isinstance(event, RECONSTRUCTIBLE_EVENTS) and callable(stream_append):
        await stream_append(event.session_id, event)
    else:
        await store.append_event(event.session_id, event)


async def flush_events(store: SessionStore, session_id: str) -> None:
    """Commit any deltas ``persist_event`` left batched; a no-op for other stores."""
    flush = getattr(store, "flush_events", None)
    if callable(flush):
        await flush(session_id)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ─── errors ──────────────────────────────────────────────────────────────────


class StoreError(Exception):
    """Base class for session-store failures a caller may want to catch."""


class SessionLocked(StoreError):
    """Another process holds the session's write lock."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} is open in another process")
        self.session_id = session_id


class CorruptSession(StoreError):
    """A stored tree cannot be walked: a node is its own ancestor."""

    def __init__(self, session_id: str, node_id: str) -> None:
        super().__init__(
            f"session {session_id} is corrupt: node {node_id} is its own ancestor"
        )
        self.session_id = session_id
        self.node_id = node_id


# ─── project identity ────────────────────────────────────────────────────────
#
# The session store is one flat global directory, so nothing about a stored
# session says which project it belonged to — and cross-session recall happily
# answered a question about one repo with another repo's internals. Recording a
# project root at creation is what lets SessionSearch scope its sweep.


def _worktree_owner(worktree: Path, dotgit: Path) -> Path | None:
    """The repository a linked worktree belongs to, from its ``.git`` file.

    In a linked worktree ``.git`` is a file reading ``gitdir: <repo>/.git/
    worktrees/<name>``. Resolving through it means work done in a worktree is
    recalled alongside the main checkout instead of looking like a separate
    project. A ``.git`` file that points somewhere else (a submodule) has no
    ``worktrees`` segment and yields None, leaving that directory as its own
    root.
    """
    try:
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = worktree / gitdir
    parts = gitdir.parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "worktrees" and parts[i - 1] == ".git":
            return Path(*parts[: i - 1])
    return None


def project_root_for(cwd: str | os.PathLike[str] | None) -> str | None:
    """The stable project identity of a working directory.

    The nearest enclosing git repository (a linked worktree resolving to the
    repository that owns it), or the directory itself when nothing upstream
    looks like a repo. None only when there is no cwd to go on — which is what
    a legacy session, recorded before this field existed, looks like.
    """
    if not cwd:
        return None
    try:
        start = Path(cwd).resolve()
    except OSError:  # pragma: no cover - defensive
        return None
    for d in (start, *start.parents):
        marker = d / ".git"
        try:
            if marker.is_dir():
                return str(d)
            if marker.is_file():
                return str(_worktree_owner(d, marker) or d)
        except OSError:  # pragma: no cover - unreadable path component
            break
    return str(start)


def same_project(a: str | None, b: str | None) -> bool:
    """True when two recorded roots name the same directory (case per platform)."""
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@dataclass
class SessionNode:
    node_id: str
    parent_id: str | None
    message: Message
    ts: float = field(default_factory=time.time)


def walk_path(
    nodes: dict[str, SessionNode], leaf_node_id: str | None, *, session_id: str
) -> list[SessionNode]:
    """Leaf-to-root walk of ``parent_id`` links, returned root-first.

    A parent id that no node carries ends the walk: the nodes below the gap
    come back as a partial path with a warning, so one damaged session still
    lists and resumes as far as it can. A node that reaches itself again
    raises :class:`CorruptSession` instead of looping forever.
    """
    path: list[SessionNode] = []
    seen: set[str] = set()
    cursor = leaf_node_id
    while cursor is not None:
        if cursor in seen:
            raise CorruptSession(session_id, cursor)
        seen.add(cursor)
        node = nodes.get(cursor)
        if node is None:
            log.warning(
                "session %s: node %s is missing from the log; "
                "using the %d node(s) below it", session_id, cursor, len(path),
            )
            break
        path.append(node)
        cursor = node.parent_id
    path.reverse()
    return path


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
    # The project this session ran in (see ``project_root_for``). None for
    # sessions stored before the field existed — recall treats those as
    # "unknown project", never as "some other project".
    project_root: str | None = None


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
    title: str | None = None  # human-meaningful session name, when the backend has one
    # Recorded project root; None = unknown (legacy session, or a backend that
    # does not track projects). SessionSearch scopes and ranks on it.
    project_root: str | None = None


@dataclass
class SessionSummary:
    """One row of the recent-sessions listing — see ``recent_sessions`` below."""

    session_id: str
    title: str | None = None
    last_ts: float = 0.0  # newest message's epoch timestamp (0.0 if unknown)
    preview: str = ""  # the session's last assistant text, however the backend trims it
    message_count: int = 0  # 0 = unknown
    project_root: str | None = None  # as on SessionSearchHit; None = unknown


@dataclass
class RecallMessage:
    """One message of a recalled session, normalized for display.

    ``text`` is the message's joined text blocks; ``tools`` is one compact
    line per tool interaction ("Name(input…) -> result…"), so a viewer can
    show what a session *did* without replaying full tool payloads.
    """

    role: str
    text: str = ""
    tools: list[str] = field(default_factory=list)
    ts: float = 0.0


@dataclass
class SessionSlice:
    """A window of one stored session — see ``session_slice`` below."""

    session_id: str
    total: int  # messages on the session's active path
    start: int  # 0-based index of ``messages[0]`` on that path
    messages: list[RecallMessage] = field(default_factory=list)
    title: str | None = None
    last_ts: float = 0.0


_TOOL_LINE_CAP = 200


def _compact(text: str, cap: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= cap else flat[: cap - 1] + "…"


def recall_message(message: dict[str, Any], ts: float = 0.0) -> RecallMessage:
    """Normalize one serialized ``Message`` dict into a :class:`RecallMessage`.

    Tool interactions become compact one-liners: a ``tooluseblock`` renders as
    ``Name({input json})``, a ``toolresultblock`` as ``-> preview`` (or
    ``ERROR preview``). Backends whose messages already look like fluid's
    serialized form get session viewing for free; others build
    :class:`RecallMessage` themselves.
    """
    import json

    texts: list[str] = []
    tools: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "text" and block.get("text"):
            texts.append(str(block["text"]))
        elif kind == "tooluseblock":
            try:
                arg = json.dumps(block.get("input") or {}, ensure_ascii=False)
            except (TypeError, ValueError):
                arg = "…"
            tools.append(_compact(f"{block.get('name', '?')}({arg})", _TOOL_LINE_CAP))
        elif kind == "toolresultblock":
            inner = " ".join(
                str(c.get("text", ""))
                for c in (block.get("content") or [])
                if isinstance(c, dict) and c.get("kind") == "text" and c.get("text")
            )
            marker = "ERROR" if block.get("is_error") else "->"
            tools.append(_compact(f"{marker} {inner or '(no text)'}", _TOOL_LINE_CAP))
    return RecallMessage(
        role=str(message.get("role", "?")), text="\n".join(texts), tools=tools, ts=ts
    )


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
    async def create_session(
        self, session_id: str | None = None, *, cwd: str | None = None
    ) -> SessionMeta:
        """Mint a session. ``cwd`` is the working directory it runs in; the
        store records the project root derived from it (``project_root_for``)
        so cross-session recall can scope to one project. Omit it and the
        session is stored with no project — indistinguishable from a legacy
        one, and never excluded on that basis."""
        ...
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
    #     async def recent_sessions(
    #         self,
    #         *,
    #         limit: int = 10,
    #         exclude_session_id: str | None = None,
    #     ) -> list[SessionSummary]: ...
    #
    #     async def session_slice(
    #         self,
    #         session_id: str,
    #         *,
    #         start: int = -10,
    #         count: int = 10,
    #     ) -> SessionSlice | None: ...
    #
    # Deliberately NOT part of the protocol above — a store that keeps no
    # durable record (MemoryStore) cannot answer them, and requiring them
    # would make that store fail the protocol. A store that CAN answer just
    # defines the methods; the loader detects each and injects the bound
    # method as the matching builtin's backend (``search`` +
    # ``recent_sessions`` → SessionSearch, ``session_slice`` → SessionView),
    # so any backing (directory scan, SQL, a vector index) serves the same
    # tools. Together they are the model's episodic memory: search or list to
    # find a session, then open a slice of it.
    #
    # ``search`` contract: ``terms`` arrive already lowercased and are matched
    # as case-insensitive substrings; a text matches if it contains ANY term.
    # ``exclude_session_id`` is the caller's own session — its history belongs
    # to HistorySearch, so skip it. ``limit`` is a hint, not a promise:
    # over-returning is fine, because the tool sorts and trims.
    #
    # ``recent_sessions`` contract: newest first by last activity, own session
    # excluded, ``limit`` a hint like above.
    #
    # ``session_slice`` contract: messages are the session's ACTIVE path
    # (root→head), 0-indexed; ``start`` may be negative to count from the end
    # (clamped, python-slice style); return None for an unknown session.


class MemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionMeta] = {}
        self._nodes: dict[str, dict[str, SessionNode]] = {}
        self._events: dict[str, list[Event]] = {}

    async def create_session(
        self, session_id: str | None = None, *, cwd: str | None = None
    ) -> SessionMeta:
        meta = SessionMeta(
            session_id=session_id or new_id("ses"),
            project_root=project_root_for(cwd),
        )
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
        return walk_path(nodes, leaf_node_id or meta.head_node_id, session_id=session_id)

    async def set_head(self, session_id: str, node_id: str | None) -> None:
        if node_id is not None and node_id not in self._nodes[session_id]:
            raise ValueError(f"session {session_id} has no node {node_id}")
        self._sessions[session_id].head_node_id = node_id

    async def set_cli_session_id(self, session_id: str, cli_session_id: str | None) -> None:
        meta = self._sessions.get(session_id)
        if meta is not None:
            meta.cli_session_id = cli_session_id

    async def append_event(self, session_id: str, event: Event) -> None:
        self._events.setdefault(session_id, []).append(event)

    async def events_since(self, session_id: str, seq: int) -> list[Event]:
        return [e for e in self._events.get(session_id, []) if e.seq > seq]
