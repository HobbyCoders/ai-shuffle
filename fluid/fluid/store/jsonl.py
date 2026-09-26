"""JSONL-backed SessionStore — append-only node log per session.

Layout (one directory per store):
  ``<session_id>.jsonl``         node/head log; first line is a ``meta`` header
                                 (session id, creation time, and the project
                                 root the session ran in, which is what lets
                                 cross-session recall scope to one project),
                                 then one ``node`` record per ``append`` — it
                                 carries ``"head": true`` because an append
                                 also moves the head, so losing the tail of the
                                 file cannot leave a node on disk that the head
                                 no longer points at — a ``head`` record when
                                 the head moves without an append (fork,
                                 rewind), and a ``cli_session`` record whenever
                                 a delegated backend refreshes its resume
                                 handle. Older logs carry a separate ``head``
                                 record after every node; those still replay.
  ``<session_id>.events.jsonl``  the durable Event log — one sequenced event per
                                 line — so ``events_since`` survives a restart.
  ``<session_id>.lock``          advisory write lock, held by the process that
                                 has the session open for writing.

Everything is append-only: reloading replays the log in order (last head move
wins), which reconstructs an identical branching tree. Sessions load lazily on
first touch and are cached in memory thereafter; the events file loads on the
first ``events_since``/``append_event``, not with the log, because listings
never need it.

Durability rules:

* Every record is one write on an append-only handle (``FILE_APPEND_DATA`` on
  Windows, ``O_APPEND`` elsewhere), so two writers cannot tear a line.
* A session is write-locked per process the first time a store writes to it.
  A second process gets :class:`SessionLocked`; a second store in the same
  process shares the lock.
* A line that does not parse is skipped with one warning per file. When it is
  the last line of the file it is a torn append (the process died mid-write);
  the store cuts it off at its next write to that session so the log is clean
  again.
* ``FLUID_STORE_FSYNC=1`` (or ``fsync=True``) fsyncs after every record.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import time
import weakref
from itertools import chain
from pathlib import Path

from fluid.events.model import Event, event_from_dict, event_to_dict
from fluid.messages import Message
from fluid.store.base import (
    CorruptSession,
    RecallMessage,
    SessionLocked,
    SessionMeta,
    SessionNode,
    SessionSearchHit,
    SessionSlice,
    SessionSummary,
    clip_text,
    message_texts,
    new_id,
    project_root_for,
    recall_message,
    walk_path,
)

__all__ = ["CorruptSession", "JsonlStore", "SessionLocked"]

log = logging.getLogger(__name__)

_MAX_SESSIONS_SCANNED = 500
_MAX_MATCHES_PER_SESSION = 3
# Sessions kept parsed in memory at once (the live session always among them).
_MAX_CACHED_SESSIONS = 8
# Hits given a title, past the caller's own limit. The caller re-ranks (project
# scope pushes other projects' sessions down or out), so a hit that shows up on
# screen is not necessarily one of the top ``limit`` by score — titling only
# those left the rows the user actually sees unlabelled. Titling costs a full
# session load, so the window is bounded rather than unlimited.
_TITLE_FILL = 20
_TITLE_CAP = 80
_PREVIEW_CAP = 120


# ─── Append-only writes ──────────────────────────────────────────────────────

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _FILE_APPEND_DATA = 0x0004
    _SYNCHRONIZE = 0x00100000
    _SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write, delete
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x80

    def _open_append(path: Path) -> int:
        """An fd whose every write lands at end-of-file in one step.

        The C runtime's ``O_APPEND`` seeks to the end and then writes — two
        steps, and another process can write in between (measured: four
        appenders tore about one line in twelve). A handle opened with only
        ``FILE_APPEND_DATA`` makes the kernel do the append, position ignored.
        """
        handle = _k32.CreateFileW(
            str(path), _FILE_APPEND_DATA | _SYNCHRONIZE, _SHARE_ALL, None,
            _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        return msvcrt.open_osfhandle(handle, os.O_BINARY)

else:

    def _open_append(path: Path) -> int:
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        return os.open(path, flags, 0o644)


def _write_record(path: Path, record: dict, *, fsync: bool) -> None:
    """One record, one write, newline included."""
    data = (json.dumps(record) + "\n").encode("utf-8")
    fd = _open_append(path)
    try:
        written = os.write(fd, data)
        if written != len(data):  # pragma: no cover - regular files write whole
            raise OSError(f"short write to {path}: {written} of {len(data)} bytes")
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


# ─── Per-session write locks ─────────────────────────────────────────────────
#
# One OS lock per lock file per process, reference-counted so several stores
# in one process (tests reopen the same directory to prove a reload) share it.
# The OS drops the lock when the process dies, so a crash never leaves a
# session locked.


class _Lock:
    __slots__ = ("fd", "refs")

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.refs = 1


_HELD: dict[str, _Lock] = {}


def _lock_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _acquire_lock(path: Path, session_id: str) -> None:
    key = _lock_key(path)
    held = _HELD.get(key)
    if held is not None:
        held.refs += 1
        return
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        if sys.platform == "win32":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise SessionLocked(session_id) from exc
    _HELD[key] = _Lock(fd)


def _unlock(held: _Lock) -> None:
    try:
        if sys.platform == "win32":
            msvcrt.locking(held.fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(held.fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(held.fd)


def _release_lock(path: Path) -> None:
    key = _lock_key(path)
    held = _HELD.get(key)
    if held is None:
        return
    held.refs -= 1
    if held.refs > 0:
        return
    del _HELD[key]
    _unlock(held)


def _release_all() -> None:
    for key in list(_HELD):
        _unlock(_HELD.pop(key))


atexit.register(_release_all)


def _release_store_locks(directory: Path, held: set[str]) -> None:
    """Finalizer body: drop every lock a store still holds (close or GC)."""
    for session_id in list(held):
        _release_lock(directory / f"{session_id}.lock")
    held.clear()


# ─── Log parsing ─────────────────────────────────────────────────────────────


def _lines(data: bytes):
    """``(line_no, byte_offset, raw_line)`` for every non-blank line."""
    offset = 0
    for line_no, raw in enumerate(data.split(b"\n"), start=1):
        start = offset
        offset += len(raw) + 1
        if raw.strip():
            yield line_no, start, raw


def _load_json_line(raw: bytes) -> dict:
    rec = json.loads(raw.decode("utf-8"))
    if not isinstance(rec, dict):
        raise TypeError("record is not an object")
    return rec


_UNSET = object()


class JsonlStore:
    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        lock: bool = True,
        fsync: bool | None = None,
    ) -> None:
        """``lock=False`` skips the per-session write lock (read-only tooling,
        tests that deliberately race writers). ``fsync`` defaults to the
        ``FLUID_STORE_FSYNC=1`` environment switch."""
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, SessionMeta] = {}
        self._nodes: dict[str, dict[str, SessionNode]] = {}
        self._events: dict[str, list[Event]] = {}
        self._loaded: set[str] = set()  # sessions whose log parsed; never a failed load
        self._events_loaded: set[str] = set()
        self._lock = lock
        self._fsync = (
            fsync if fsync is not None else os.environ.get("FLUID_STORE_FSYNC") == "1"
        )
        self._held: set[str] = set()  # sessions this store holds the write lock for
        # Loaded sessions, least recently used first. A cross-session search
        # or listing touches many logs; without a bound every one of them,
        # images included, stayed in memory for the life of the process.
        self._recent: list[str] = []
        # Torn trailing lines seen while parsing, cut off at the next write.
        self._torn: dict[Path, tuple[int, bytes]] = {}
        self._finalizer = weakref.finalize(self, _release_store_locks, self._dir, self._held)

    # ─── Paths ───────────────────────────────────────────────────────────

    def _log_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.jsonl"

    def _events_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.events.jsonl"

    def _lock_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.lock"

    # ─── Writing ─────────────────────────────────────────────────────────

    def lock_session(self, session_id: str) -> None:
        """Take the session's write lock now rather than at the first write.

        The CLI calls this right after resolving ``--resume``/``--continue``
        so a session open elsewhere is refused before a turn starts, not in
        the middle of one. Raises :class:`SessionLocked`.
        """
        if not self._lock or session_id in self._held:
            return
        _acquire_lock(self._lock_path(session_id), session_id)
        self._held.add(session_id)

    def _writable(self, session_id: str) -> None:
        self.lock_session(session_id)
        self._repair_tail(self._log_path(session_id))
        self._repair_tail(self._events_path(session_id))

    def _repair_tail(self, path: Path) -> None:
        """Cut a torn trailing line off, if the file still ends with it."""
        torn = self._torn.pop(path, None)
        if torn is None:
            return
        offset, fragment = torn
        try:
            with path.open("r+b") as fh:
                fh.seek(offset)
                if fh.read() != fragment:
                    return  # someone else wrote since we looked; leave it
                fh.truncate(offset)
        except OSError as exc:
            log.warning("%s: could not cut off the torn last line: %s", path.name, exc)
            return
        log.warning("%s: cut off a torn last line (%d bytes)", path.name, len(fragment))

    def _append_line(self, path: Path, record: dict) -> None:
        _write_record(path, record, fsync=self._fsync)

    def close(self) -> None:
        """Release every write lock this store holds."""
        _release_store_locks(self._dir, self._held)

    async def aclose(self) -> None:
        self.close()

    # ─── Lazy load ───────────────────────────────────────────────────────

    def _ensure_loaded(self, session_id: str) -> bool:
        """Load a session's log from disk once. Returns True if it exists.

        Only a successful parse is cached, so a log that fails to read today
        (locked, unreadable) is tried again on the next call.
        """
        if session_id in self._loaded:
            self._touch(session_id)
            return True
        path = self._log_path(session_id)
        if not path.exists():
            return False
        parsed = self._parse_log(session_id, path)
        if parsed is None:
            return False
        meta, nodes = parsed
        self._sessions[session_id] = meta
        self._nodes[session_id] = nodes
        self._loaded.add(session_id)
        self._touch(session_id)
        return True

    def _touch(self, session_id: str) -> None:
        """Mark a session as just used; drop the least recent past the cap.

        A session this store holds the write lock for is the live one and is
        never dropped. A dropped session is only forgotten in memory: the next
        access parses its log again.
        """
        if session_id in self._recent:
            self._recent.remove(session_id)
        self._recent.append(session_id)
        while len(self._recent) > _MAX_CACHED_SESSIONS:
            victim = next((s for s in self._recent if s not in self._held), None)
            if victim is None:
                return
            self._recent.remove(victim)
            self._loaded.discard(victim)
            self._events_loaded.discard(victim)
            self._nodes.pop(victim, None)
            self._events.pop(victim, None)

    def _parse_log(
        self, session_id: str, path: Path
    ) -> tuple[SessionMeta, dict[str, SessionNode]] | None:
        data = path.read_bytes()
        meta: SessionMeta | None = None
        nodes: dict[str, SessionNode] = {}
        head = _UNSET
        cli_session = _UNSET
        bad: list[int] = []
        duplicates: list[int] = []
        last_line = 0
        last_bad: tuple[int, int] | None = None  # (line_no, offset)
        for line_no, offset, raw in _lines(data):
            last_line = line_no
            try:
                rec = _load_json_line(raw)
                kind = rec["t"]
                if kind == "meta":
                    meta = SessionMeta(
                        session_id=rec.get("session_id") or session_id,
                        head_node_id=rec.get("head_node_id"),
                        created_at=rec.get("created_at", 0.0),
                        cli_session_id=rec.get("cli_session_id"),
                        project_root=rec.get("project_root"),
                    )
                elif kind == "node":
                    n = rec["node"]
                    node_id = n["node_id"]
                    if node_id in nodes:
                        duplicates.append(line_no)
                        continue
                    nodes[node_id] = SessionNode(
                        node_id=node_id,
                        parent_id=n.get("parent_id"),
                        message=Message.from_dict(n["message"]),
                        ts=n.get("ts", 0.0),
                    )
                    if rec.get("head"):
                        head = node_id
                elif kind == "head":
                    head = rec["head_node_id"]
                elif kind == "cli_session":
                    cli_session = rec.get("cli_session_id")
            except (ValueError, KeyError, TypeError):
                bad.append(line_no)
                last_bad = (line_no, offset)
        torn = last_bad is not None and last_bad[0] == last_line
        if torn:
            self._torn[path] = (last_bad[1], data[last_bad[1]:])
        self._report_bad_lines(path, bad, torn)
        if duplicates:
            log.warning(
                "%s: ignored %d duplicate node record(s), first at line %d",
                path.name, len(duplicates), duplicates[0],
            )
        if meta is None:
            if not nodes and head is _UNSET and cli_session is _UNSET:
                return None  # nothing recognisable: not a session
            created = min((n.ts for n in nodes.values()), default=0.0)
            if not created:
                try:
                    created = path.stat().st_mtime
                except OSError:
                    created = 0.0
            log.warning("%s: no meta header; using defaults for session %s", path.name, session_id)
            meta = SessionMeta(session_id=session_id, created_at=created)
        if head is not _UNSET:
            meta.head_node_id = head
        if cli_session is not _UNSET:
            meta.cli_session_id = cli_session
        return meta, nodes

    @staticmethod
    def _report_bad_lines(path: Path, bad: list[int], torn: bool) -> None:
        if not bad:
            return
        log.warning(
            "%s: skipped %d unreadable line(s), first at line %d%s",
            path.name, len(bad), bad[0],
            "; the last line is a torn append and is cut off at the next write" if torn else "",
        )

    def _ensure_events_loaded(self, session_id: str) -> None:
        if session_id in self._events_loaded:
            return
        events: list[Event] = []
        path = self._events_path(session_id)
        if path.exists():
            data = path.read_bytes()
            bad: list[int] = []
            last_line = 0
            last_bad: tuple[int, int] | None = None
            for line_no, offset, raw in _lines(data):
                last_line = line_no
                try:
                    events.append(event_from_dict(_load_json_line(raw)))
                except (ValueError, KeyError, TypeError):
                    bad.append(line_no)
                    last_bad = (line_no, offset)
            torn = last_bad is not None and last_bad[0] == last_line
            if torn:
                self._torn[path] = (last_bad[1], data[last_bad[1]:])
            self._report_bad_lines(path, bad, torn)
        self._events[session_id] = events
        self._events_loaded.add(session_id)

    # ─── SessionStore protocol ───────────────────────────────────────────

    async def create_session(
        self, session_id: str | None = None, *, cwd: str | None = None
    ) -> SessionMeta:
        meta = SessionMeta(
            session_id=session_id or new_id("ses"),
            project_root=project_root_for(cwd),
        )
        self._writable(meta.session_id)
        self._sessions[meta.session_id] = meta
        self._nodes[meta.session_id] = {}
        self._events[meta.session_id] = []
        self._loaded.add(meta.session_id)
        self._events_loaded.add(meta.session_id)
        self._touch(meta.session_id)
        self._append_line(
            self._log_path(meta.session_id),
            {"t": "meta", "session_id": meta.session_id,
             "created_at": meta.created_at, "head_node_id": meta.head_node_id,
             "cli_session_id": meta.cli_session_id,
             "project_root": meta.project_root},
        )
        return meta

    async def get_session(self, session_id: str) -> SessionMeta | None:
        if self._ensure_loaded(session_id):
            return self._sessions[session_id]
        return None

    async def append(self, session_id: str, node: SessionNode) -> None:
        if not self._ensure_loaded(session_id):
            raise KeyError(session_id)
        self._writable(session_id)
        self._nodes[session_id][node.node_id] = node
        self._sessions[session_id].head_node_id = node.node_id
        # Node and head move in one record: a lost tail cannot leave the node
        # on disk with the head still pointing at its parent.
        self._append_line(self._log_path(session_id), {
            "t": "node",
            "node": {
                "node_id": node.node_id, "parent_id": node.parent_id,
                "ts": node.ts, "message": node.message.to_dict(),
            },
            "head": True,
        })

    async def get_node(self, session_id: str, node_id: str) -> SessionNode | None:
        self._ensure_loaded(session_id)
        return self._nodes.get(session_id, {}).get(node_id)

    async def load_path(
        self, session_id: str, leaf_node_id: str | None = None
    ) -> list[SessionNode]:
        self._ensure_loaded(session_id)
        meta = self._sessions[session_id]
        nodes = self._nodes[session_id]
        return walk_path(nodes, leaf_node_id or meta.head_node_id, session_id=session_id)

    async def set_head(self, session_id: str, node_id: str | None) -> None:
        if not self._ensure_loaded(session_id):
            raise KeyError(session_id)
        if node_id is not None and node_id not in self._nodes[session_id]:
            raise ValueError(f"session {session_id} has no node {node_id}")
        self._writable(session_id)
        self._sessions[session_id].head_node_id = node_id
        self._append_line(
            self._log_path(session_id),
            {"t": "head", "head_node_id": node_id, "ts": time.time()},
        )

    async def set_cli_session_id(self, session_id: str, cli_session_id: str | None) -> None:
        if not self._ensure_loaded(session_id):
            return
        self._writable(session_id)
        self._sessions[session_id].cli_session_id = cli_session_id
        self._append_line(
            self._log_path(session_id),
            {"t": "cli_session", "cli_session_id": cli_session_id},
        )

    async def append_event(self, session_id: str, event: Event) -> None:
        self._ensure_events_loaded(session_id)
        self._writable(session_id)
        self._events[session_id].append(event)
        self._append_line(self._events_path(session_id), event_to_dict(event))

    async def events_since(self, session_id: str, seq: int) -> list[Event]:
        self._ensure_events_loaded(session_id)
        return [e for e in self._events[session_id] if e.seq > seq]

    # ─── Optional: cross-session recall ──────────────────────────────────

    def _logs_newest_first(self) -> list[Path]:
        return [
            p for p in sorted(
                self._dir.glob("*.jsonl"),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            if not p.name.endswith(".events.jsonl")
        ][:_MAX_SESSIONS_SCANNED]

    async def search(
        self,
        terms: list[str],
        *,
        limit: int = 5,
        exclude_session_id: str | None = None,
    ) -> list[SessionSearchHit]:
        """Keyword sweep over every stored session log — SessionSearch's backend.

        Line-oriented over the JSONL — no index, no dependencies — and bounded
        to the newest ``_MAX_SESSIONS_SCANNED`` logs, which is plenty for a
        personal store of hundreds of sessions. ``limit`` is a hint only: the
        full sorted hit list comes back and the caller trims it. The file IO is
        synchronous, matching the rest of this store.
        """
        hits: list[SessionSearchHit] = []
        for log_path in self._logs_newest_first():
            session_id = log_path.name[: -len(".jsonl")]
            if session_id == exclude_session_id:
                continue
            hit = self._scan_log(log_path, session_id, terms)
            if hit.score:
                hits.append(hit)
        hits.sort(key=lambda h: (-h.score, -h.last_ts))
        for hit in hits[: max(limit, _TITLE_FILL)]:
            hit.title = self._session_title(hit.session_id)
        return hits

    async def recent_sessions(
        self,
        *,
        limit: int = 10,
        exclude_session_id: str | None = None,
    ) -> list[SessionSummary]:
        """Newest sessions first — the SessionSearch no-query listing's backend.

        Loads at most ``limit`` sessions (newest logs by mtime), so the cost
        tracks what the caller will actually show. A session whose log cannot
        be loaded or walked is skipped, never fatal to the listing.
        """
        limit = max(1, limit)
        out: list[SessionSummary] = []
        for log_path in self._logs_newest_first():
            if len(out) >= limit:
                break
            session_id = log_path.name[: -len(".jsonl")]
            if session_id == exclude_session_id:
                continue
            try:
                if not self._ensure_loaded(session_id):
                    continue
                path = await self.load_path(session_id)
            except (OSError, CorruptSession) as exc:
                log.warning("session %s skipped from the listing: %s", session_id, exc)
                continue
            preview = ""
            for node in reversed(path):
                if node.message.role == "assistant" and node.message.text().strip():
                    preview = clip_text(node.message.text(), _PREVIEW_CAP)
                    break
            out.append(SessionSummary(
                session_id=session_id,
                title=self._session_title(session_id),
                last_ts=path[-1].ts if path else 0.0,
                preview=preview,
                message_count=len(path),
                project_root=self._sessions[session_id].project_root,
            ))
        return out

    async def session_slice(
        self,
        session_id: str,
        *,
        start: int = -10,
        count: int = 10,
    ) -> SessionSlice | None:
        """A message window of one session — the SessionView builtin's backend."""
        if not self._ensure_loaded(session_id):
            return None
        path = await self.load_path(session_id)
        total = len(path)
        count = max(1, count)
        first = max(0, total + start) if start < 0 else min(start, total)
        window = path[first:first + count]
        messages: list[RecallMessage] = [
            recall_message(n.message.to_dict(), ts=n.ts) for n in window
        ]
        return SessionSlice(
            session_id=session_id,
            total=total,
            start=first,
            messages=messages,
            title=self._session_title(session_id),
            last_ts=path[-1].ts if path else 0.0,
        )

    def _session_title(self, session_id: str) -> str | None:
        """First user text on the active path, clipped — the session's de facto name."""
        try:
            if not self._ensure_loaded(session_id):
                return None
            meta = self._sessions[session_id]
            path = walk_path(self._nodes[session_id], meta.head_node_id, session_id=session_id)
        except (OSError, CorruptSession):
            return None
        for node in path:
            if node.message.role == "user" and node.message.text().strip():
                return clip_text(node.message.text(), _TITLE_CAP)
        return None

    def _scan_log(self, path: Path, session_id: str, terms: list[str]) -> SessionSearchHit:
        """One log's contribution to the sweep, in a single streamed pass.

        The header line carries the session's project root, so scoping costs
        nothing beyond the read the scan was doing anyway. A log whose first
        line is not a header (hand-written, truncated) is scanned whole and
        reports no project — which reads as "unknown", not "elsewhere".
        """
        hit = SessionSearchHit(session_id=session_id, score=0)
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                rest = fh
                header = next(fh, "")
                try:
                    record = json.loads(header) if header.strip() else None
                except ValueError:
                    record = None
                if isinstance(record, dict) and record.get("t") == "meta":
                    hit.project_root = record.get("project_root")
                else:
                    rest = chain([header], fh)
                for line in rest:
                    if not any(t in line.lower() for t in terms):
                        continue  # cheap line-level prefilter before JSON parsing
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict) or rec.get("t") != "node":
                        continue
                    node = rec.get("node") or {}
                    message = node.get("message") if isinstance(node, dict) else None
                    if not isinstance(message, dict):
                        continue  # parsable but not a node record; the loader skips it too
                    try:
                        hit.last_ts = max(hit.last_ts, float(node.get("ts") or 0.0))
                    except (TypeError, ValueError):
                        pass
                    try:
                        texts = message_texts(message)
                    except TypeError:  # content that is not a list of blocks
                        continue
                    for text in texts:
                        lowered = text.lower()
                        n_hit = sum(1 for t in terms if t in lowered)
                        if not n_hit:
                            continue
                        hit.score += n_hit
                        if len(hit.texts) < _MAX_MATCHES_PER_SESSION:
                            hit.texts.append((message.get("role", "?"), text))
        except OSError:
            return SessionSearchHit(session_id=session_id, score=0)
        return hit
