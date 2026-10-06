"""The chat WebSocket frame contract, driven end to end through the dispatch table.

Every test here opens the REAL ``/ws/chat`` endpoint function
(``app.api.websocket.chat_websocket``) on a real Starlette ``WebSocket`` whose
ASGI channel is scripted in memory, so each client frame travels the same path
it does in production: ``receive_json`` -> rate limiter -> ``_handlers`` dispatch
-> handler -> ``send_json`` -> the connection's writer -> the wire. Only the
authentication and cross-device fan-out boundaries are patched; the turns
table, the permission and question handlers and the ``pending_interactions``
rows are real (``mock_db`` SQLite shim).

Each assertion pins the exact frame the API reference promises
(docs/content/api-reference/websocket.md), so a change to either side breaks a
test here. ``_seq`` (a delivery-gap diagnostic on every outbound frame) is not
part of the documented contract and is stripped before comparison.

Tests marked ``xfail(strict=True)`` pin a documented or expected behaviour the
code does not have yet; each one names the defect. They start passing (and so
fail strictly) the moment the defect is fixed, so the marker gets removed then.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.websockets import WebSocketState
from starlette.websockets import WebSocket

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_api
from app.core import session_runtime
from app.core.config import settings
from app.core.permission_handler import permission_handler
from app.core.user_question_handler import (
    DISMISSED_KEY,
    SKIPPED_KEY,
)
from app.db import database, interactions
from app.db import turns as turns_db
from tests.api.test_chat_ws_v2_turns import _make_session

# ─── in-memory ASGI wire ─────────────────────────────────────────────────────


class _Wire:
    """The client side of one socket: scripted inbound messages, decoded outbound."""

    def __init__(self):
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.close_code = None
        self.inbound.put_nowait({"type": "websocket.connect"})
        scope = {
            "type": "websocket",
            "path": "/api/v1/ws/chat",
            "raw_path": b"/api/v1/ws/chat",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
            "scheme": "ws",
            "subprotocols": [],
            "state": {},
        }
        self.ws = WebSocket(scope, self._receive, self._send)

    async def _receive(self):
        item = await self.inbound.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def _send(self, message):
        if message["type"] == "websocket.send":
            self.sent.append(json.loads(message["text"]))
        elif message["type"] == "websocket.close":
            self.close_code = message.get("code", 1000)

    def push_text(self, text: str):
        self.inbound.put_nowait({"type": "websocket.receive", "text": text})

    def push(self, frame: dict):
        self.push_text(json.dumps(frame))


def _strip(frame: dict) -> dict:
    frame = dict(frame)
    frame.pop("_seq", None)
    return frame


class _Conn:
    """A connected client: send frames, wait for replies, read the transcript."""

    def __init__(self, wire, task, box, sync):
        self.wire = wire
        self.task = task
        self._box = box
        self.sync = sync
        self._probe = 0

    @property
    def handler(self):
        return self._box["handler"]

    @property
    def raw(self):
        return list(self.wire.sent)

    def frames(self, ftype=None):
        out = [_strip(f) for f in self.wire.sent]
        if ftype is not None:
            out = [f for f in out if f.get("type") == ftype]
        return out

    async def wait_for(self, predicate, timeout=3.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate(self.frames()):
            if self.task.done():
                raise AssertionError(
                    f"connection ended while waiting (frames={self.frames()})"
                )
            if loop.time() >= deadline:
                raise AssertionError(f"timed out waiting (frames={self.frames()})")
            await asyncio.sleep(0.005)
        return self.frames()

    async def roundtrip(self, frame, timeout=3.0):
        """Send ``frame`` and then a probe whose reply is certain.

        Frames on one socket are handled strictly in order, so once the probe's
        ``session_not_found`` is on the wire every reply to ``frame`` is too —
        including the case where ``frame`` gets no reply at all. Returns the
        frames ``frame`` produced (the probe reply excluded).
        """
        self._probe += 1
        probe_id = f"probe-{self._probe}"
        start = len(self.wire.sent)
        if isinstance(frame, str):
            self.wire.push_text(frame)
        else:
            self.wire.push(frame)
        self.wire.push({"type": "load_session", "session_id": probe_id})
        probe = {"type": "session_not_found", "session_id": probe_id}
        await self.wait_for(lambda fs: probe in fs[start:], timeout)
        replies = [_strip(f) for f in self.wire.sent[start:]]
        replies.remove(probe)
        return replies

    async def expect(self, frame, count=1, timeout=3.0):
        """Send ``frame`` and wait until at least ``count`` new frames arrive."""
        start = len(self.wire.sent)
        self.wire.push(frame)
        await self.wait_for(lambda fs: len(fs) - start >= count, timeout)
        return [_strip(f) for f in self.wire.sent[start:]]


@pytest.fixture
def ws_env(mock_db, monkeypatch):
    """Patch the boundaries around the chat socket; keep the core real."""
    sync = MagicMock(name="sync_engine")
    for name in (
        "register_user_device", "unregister_user_device",
        "register_device", "unregister_device",
        "broadcast_session_opened", "broadcast_session_closed",
        "broadcast_tabs_updated", "broadcast_queue_updated",
        "broadcast_message_added", "broadcast_session_updated",
        "broadcast_user_session_created", "broadcast_user_session_updated",
    ):
        setattr(sync, name, AsyncMock(name=name))
    sync.get_device_count = MagicMock(return_value=0)
    monkeypatch.setattr(cwh, "sync_engine", sync)

    async def _run_sync(fn, *a, **k):
        return fn(*a, **k)

    # mock_db is a single-connection SQLite shim: keep the handler's DB calls
    # on the loop thread, exactly as the other chat-socket suites do.
    monkeypatch.setattr(cwh, "run_sync", _run_sync)
    monkeypatch.setattr(
        "app.core.streaming_status.get_user_streaming_sessions", lambda uid: []
    )
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr(
        "app.core.sync_engine.sync_engine.broadcast_user_permission", AsyncMock()
    )
    session_runtime._live_runtimes.clear()
    permission_handler.reset_all()
    yield SimpleNamespace(sync=sync, monkeypatch=monkeypatch)
    session_runtime._live_runtimes.clear()
    permission_handler.reset_all()


@asynccontextmanager
async def _connect(env, *, api_user=None, device_id="dev-1"):
    """Open ``/ws/chat`` through the real endpoint and yield a :class:`_Conn`.

    ``api_user`` is what authentication resolved (``None`` = the owner).
    """
    env.monkeypatch.setattr(
        ws_api, "authenticate_websocket", AsyncMock(return_value=(True, api_user))
    )
    box = {}
    real_cls = ws_api.ChatWebSocketHandler

    def _capture(*a, **k):
        box["handler"] = real_cls(*a, **k)
        return box["handler"]

    env.monkeypatch.setattr(ws_api, "ChatWebSocketHandler", _capture)
    wire = _Wire()
    task = asyncio.create_task(
        ws_api.chat_websocket(wire.ws, token=None, device_id=device_id)
    )
    conn = _Conn(wire, task, box, env.sync)
    try:
        # The connect snapshots (streaming sessions, then pending prompts) are
        # the first two frames on every socket.
        await conn.wait_for(lambda fs: len(fs) >= 2)
        yield conn
    finally:
        if not task.done():
            wire.inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})
        try:
            await asyncio.wait_for(task, 3.0)
        finally:
            if not task.done():
                task.cancel()


def _queue_row(turn, session_id, position):
    """The documented ``queued_messages`` entry for a queued turn row."""
    created = turn["created_at"]
    return {
        "id": turn["id"],
        "session_id": session_id,
        "content": turn["prompt"],
        "profile_id": turn.get("profile_id"),
        "project_id": turn.get("project_id"),
        "position": position,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
    }


def _queue_frame(session_id, rows):
    return {
        "type": "sync",
        "event_type": "queue_updated",
        "session_id": session_id,
        "data": {"queued_messages": rows, "pending_peer_messages": []},
    }


def _live_turn(session_id, prompt="live turn"):
    from app.db import runs

    live = turns_db.enqueue_turn(session_id, "user", prompt=prompt)
    lease = runs.claim_run(session_id, "test-runner", 60)
    turns_db.start_turn(live["id"], run_id=lease["id"])
    return live


def _pending_permission(session_id, request_id, *, tool="Bash", tool_input=None):
    return interactions.create_interaction(
        session_id,
        "permission",
        {
            "request_id": request_id,
            "profile_id": "default",
            "tool_name": tool,
            "tool_input": tool_input if tool_input is not None else {"command": "ls"},
        },
        300,
    )


def _pending_question(session_id, request_id, questions):
    return interactions.create_interaction(
        session_id,
        "question",
        {"request_id": request_id, "tool_use_id": f"tu-{request_id}", "questions": questions},
        300,
    )


def _interaction(row_id):
    rows = [r for r in _all_interactions() if r["id"] == row_id]
    return rows[0] if rows else None


def _all_interactions():
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM pending_interactions")
        out = []
        for row in cur.fetchall():
            row = dict(row)
            for key in ("payload", "answer"):
                if isinstance(row.get(key), str):
                    row[key] = json.loads(row[key])
            out.append(row)
        return out


# ═══ connection lifecycle and the receive loop ═══════════════════════════════


async def test_connect_sends_the_streaming_sessions_snapshot_first(ws_env):
    async with _connect(ws_env) as conn:
        first = conn.raw[0]
        assert _strip(first) == {
            "type": "user_streaming_sessions",
            "sessions": [],
            "session_ids": [],
        }
        assert first["_seq"] == 1
        assert _strip(conn.raw[1]) == {
            "type": "session_attention_snapshot",
            "sessions": [],
        }
    ws_env.sync.register_user_device.assert_awaited_once()
    ws_env.sync.unregister_user_device.assert_awaited_once()


async def test_every_documented_client_type_is_in_the_dispatch_table(ws_env):
    """The client -> server table in the docs, read from the docs themselves."""
    import re
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[2]
           / "docs/content/api-reference/websocket.md").read_text()
    table = doc.split("### Message Types (Client → Server)", 1)[1].split("\n\n", 2)[1]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE))
    assert "tabs_update" in documented
    async with _connect(ws_env) as conn:
        assert set(conn.handler._handlers) == documented


@pytest.mark.parametrize(
    "frame, message",
    [
        ({"type": "no_such_frame", "session_id": "x"}, "Unknown message type: no_such_frame"),
        ({"no_type": True}, "Missing message type"),
        ({"type": None}, "Missing message type"),
        ({"type": ""}, "Missing message type"),
        ({"type": ["query"]}, "Missing message type"),
    ],
    ids=["unknown-type", "missing-type", "null-type", "empty-type", "non-string-type"],
)
async def test_unknown_frame_types_get_an_error_and_the_socket_stays_open(ws_env, frame, message):
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip(frame) == [{"type": "error", "message": message}]
        # Still serving: a later frame is answered normally.
        assert await conn.roundtrip({"type": "close_session"}) == [
            {"type": "session_closed", "session_id": None}]
        assert not conn.task.done()


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"just a string"', "null", "42"])
async def test_a_malformed_frame_gets_an_error_and_the_socket_stays_open(ws_env, text):
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip(text, timeout=1.0)
        assert replies == [
            {"type": "error", "message": "Invalid message: expected a JSON object"}]
        # Still serving: a later frame is answered normally.
        assert await conn.roundtrip({"type": "close_session"}) == [
            {"type": "session_closed", "session_id": None}]
        assert not conn.task.done()


async def test_a_binary_frame_gets_an_error_and_the_socket_stays_open(ws_env):
    async with _connect(ws_env) as conn:
        start = len(conn.wire.sent)
        conn.wire.inbound.put_nowait({"type": "websocket.receive", "bytes": b"{}"})
        await conn.wait_for(lambda fs: len(fs) > start)
        assert conn.frames()[start:] == [
            {"type": "error", "message": "Invalid message: expected a JSON object"}]
        assert not conn.task.done()


async def test_a_flood_of_malformed_frames_closes_the_socket(ws_env):
    """Each malformed frame is answered, up to MAX_MALFORMED_FRAMES on one
    connection; the next one ends it with close code 1007 (invalid payload)."""
    async with _connect(ws_env) as conn:
        for _ in range(cwh.MAX_MALFORMED_FRAMES + 1):
            conn.wire.push_text("{not json")
        await asyncio.wait_for(conn.task, 3.0)
        errors = conn.frames("error")
        assert len(errors) <= cwh.MAX_MALFORMED_FRAMES
        assert conn.wire.close_code == 1007
    ws_env.sync.unregister_user_device.assert_awaited_once()


async def test_malformed_frames_below_the_bound_keep_the_socket(ws_env):
    async with _connect(ws_env) as conn:
        for _ in range(cwh.MAX_MALFORMED_FRAMES):
            conn.wire.push_text("[]")
        await conn.roundtrip({"type": "close_session"})
        assert len(conn.frames("error")) == cwh.MAX_MALFORMED_FRAMES
        assert not conn.task.done()
        assert conn.wire.close_code is None


async def test_rate_limited_frames_are_refused_without_dispatch(ws_env, monkeypatch):
    monkeypatch.setattr(settings, "ws_rate_limit_messages", 3)
    monkeypatch.setattr(settings, "ws_rate_limit_window_seconds", 60.0)
    async with _connect(ws_env) as conn:
        for i in range(4):
            conn.wire.push({"type": "load_session", "session_id": f"s-{i}"})
        await conn.wait_for(lambda fs: len(fs) >= 6)
        replies = conn.frames()[2:]
        # Three dispatched, the fourth refused before it reached a handler.
        assert replies[:3] == [
            {"type": "session_not_found", "session_id": f"s-{i}"} for i in range(3)
        ]
        assert replies[3]["type"] == "error"
        assert "Rate limited" in replies[3]["message"]
        # The refusal does not end the connection.
        assert not conn.task.done()


async def test_rate_limit_error_frame_has_the_documented_shape(ws_env, monkeypatch):
    monkeypatch.setattr(settings, "ws_rate_limit_messages", 1)
    monkeypatch.setattr(settings, "ws_rate_limit_window_seconds", 60.0)
    async with _connect(ws_env) as conn:
        conn.wire.push({"type": "close_session"})
        conn.wire.push({"type": "close_session"})
        await conn.wait_for(lambda fs: len(fs) >= 4)
        assert conn.frames()[2:] == [
            {"type": "session_closed", "session_id": None},
            {"type": "error", "message": "Rate limited: too many messages. Please slow down."},
        ]


async def test_pong_is_exempt_from_the_rate_limit_and_has_no_reply(ws_env, monkeypatch):
    monkeypatch.setattr(settings, "ws_rate_limit_messages", 1)
    monkeypatch.setattr(settings, "ws_rate_limit_window_seconds", 60.0)
    async with _connect(ws_env) as conn:
        for _ in range(5):
            conn.wire.push({"type": "pong"})
        # The first non-pong frame is still inside the budget of one.
        replies = await conn.expect({"type": "load_session", "session_id": "after-pongs"})
        assert replies == [{"type": "session_not_found", "session_id": "after-pongs"}]


async def test_heartbeat_pings_carry_the_documented_shape(ws_env, monkeypatch):
    from app.api import ws_utils

    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        await real_sleep(0.01)

    monkeypatch.setattr(
        ws_utils, "asyncio",
        SimpleNamespace(sleep=_fast_sleep, CancelledError=asyncio.CancelledError),
    )
    async with _connect(ws_env) as conn:
        await conn.wait_for(lambda fs: len([f for f in fs if f["type"] == "ping"]) >= 2)
        pings = conn.frames("ping")
        assert pings[0] == {"type": "ping", "timestamp": None}
        # The client's pong is consumed silently.
        assert all(f["type"] == "ping" for f in await conn.roundtrip({"type": "pong"}))
        seqs = [f["_seq"] for f in conn.raw]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


async def test_idle_timeout_keeps_a_healthy_socket_and_drops_a_dead_one(ws_env):
    _make_session("sess-idle")  # watch_session checks the chat's account (#1877)
    async with _connect(ws_env) as conn:
        # The 60 s receive timeout on a healthy, connected socket just loops.
        conn.wire.inbound.put_nowait(asyncio.TimeoutError())
        assert await conn.roundtrip({"type": "close_session"}) == [
            {"type": "session_closed", "session_id": None}]
        assert not conn.task.done()

        # Once a send has declared the socket dead, the next idle tick exits
        # instead of parking on a zombie, and teardown deregisters the device.
        await conn.roundtrip({"type": "watch_session", "session_id": "sess-idle"})
        conn.handler.connection_healthy = False
        conn.wire.inbound.put_nowait(asyncio.TimeoutError())
        await asyncio.wait_for(conn.task, 2.0)
    ws_env.sync.unregister_device.assert_awaited_with("dev-1", "sess-idle", conn.wire.ws)
    ws_env.sync.unregister_user_device.assert_awaited_once()


async def test_idle_timeout_on_a_disconnected_socket_ends_the_loop(ws_env):
    async with _connect(ws_env) as conn:
        conn.wire.ws.client_state = WebSocketState.DISCONNECTED
        conn.wire.inbound.put_nowait(asyncio.TimeoutError())
        await asyncio.wait_for(conn.task, 2.0)
    ws_env.sync.unregister_user_device.assert_awaited_once()


async def test_an_oversized_frame_is_handled_whole(ws_env):
    """No application-level frame cap exists: the transport bounds frames
    (uvicorn ``ws_max_size`` in app/serve.py). A frame well past typical sizes
    is dispatched intact and the socket stays open."""
    sid = _make_session("sess-big")
    big = "x" * (1024 * 1024)
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip(
            {"type": "queue_message", "session_id": sid, "content": big}
        )
        queued = turns_db.get_queued_turns(sid)
        assert [len(t["prompt"]) for t in queued] == [len(big)]
        assert replies == [_queue_frame(sid, [_queue_row(queued[0], sid, 0)])]
        assert not conn.task.done()


# ═══ session selection: load / watch / close ═════════════════════════════════


async def test_load_session_unknown_replies_session_not_found(ws_env):
    async with _connect(ws_env) as conn:
        replies = await conn.expect({"type": "load_session", "session_id": "nope"})
        assert replies == [{"type": "session_not_found", "session_id": "nope"}]
    ws_env.sync.register_device.assert_not_awaited()


async def test_load_session_without_an_id_is_ignored(ws_env):
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip({"type": "load_session"}) == []


async def test_load_session_replies_history_with_pending_prompts_and_queue(ws_env):
    sid = _make_session("sess-hist")
    _pending_permission(sid, "req-h")
    _pending_question(sid, "q-h", [{"question": "Which?"}])
    queued = turns_db.enqueue_turn(sid, "queue", prompt="later")
    async with _connect(ws_env) as conn:
        replies = await conn.expect({"type": "load_session", "session_id": sid})
        history = replies[0]
        assert history["type"] == "history"
        assert history["session_id"] == sid
        assert history["session"]["id"] == sid
        assert history["messages"] == []
        assert history["isStreaming"] is False
        assert [p["request_id"] for p in history["pendingPermissions"]] == ["req-h"]
        assert [q["request_id"] for q in history["pendingQuestions"]] == ["q-h"]
        assert history["queuedMessages"] == [_queue_row(queued, sid, 0)]
    ws_env.sync.register_device.assert_awaited_with("dev-1", sid, conn.wire.ws)
    ws_env.sync.broadcast_session_opened.assert_awaited_once_with(
        session_id=sid, device_id="dev-1", is_new=False,
    )


async def test_load_session_switch_unregisters_the_previous_session(ws_env):
    first = _make_session("sess-a")
    second = _make_session("sess-b")
    async with _connect(ws_env) as conn:
        await conn.expect({"type": "load_session", "session_id": first})
        await conn.expect({"type": "load_session", "session_id": second})
        assert conn.handler.current_session_id == second
    ws_env.sync.unregister_device.assert_any_await("dev-1", first, conn.wire.ws)


async def test_watch_session_sends_the_queue_as_the_documented_sync_frame(ws_env):
    sid = _make_session("sess-w")
    first = turns_db.enqueue_turn(sid, "user", prompt="one")
    turns_db.enqueue_turn(sid, "failover", prompt="internal, never a chip")
    second = turns_db.enqueue_turn(sid, "queue", prompt="two")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({"type": "watch_session", "session_id": sid})
        assert replies == [
            _queue_frame(sid, [_queue_row(first, sid, 0), _queue_row(second, sid, 1)])
        ]
        # The documented queue entry, key for key.
        assert set(replies[0]["data"]["queued_messages"][0]) == {
            "id", "session_id", "content", "profile_id", "project_id",
            "position", "created_at",
        }
        assert conn.handler.current_session_id == sid


async def test_watch_session_with_an_empty_queue_sends_complete_snapshot(ws_env):
    sid = _make_session("sess-w0")
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip({"type": "watch_session", "session_id": sid}) == [_queue_frame(sid, [])]
        assert conn.handler.current_session_id == sid
    ws_env.sync.register_device.assert_awaited_once_with("dev-1", sid, conn.wire.ws)


async def test_close_session_replies_session_closed_and_forgets_the_session(ws_env):
    sid = _make_session("sess-c")
    _pending_permission(sid, "req-c")
    async with _connect(ws_env) as conn:
        await conn.roundtrip({"type": "watch_session", "session_id": sid})
        replies = await conn.roundtrip({"type": "close_session"})
        assert replies == [{"type": "session_closed", "session_id": sid}]
        assert conn.handler.current_session_id is None
        # get_pending_permissions acts on the selected session; none is left.
        assert await conn.roundtrip({"type": "get_pending_permissions"}) == [{
            "type": "pending_permissions", "session_id": None,
            "pending": [], "deferred": [],
        }]
    ws_env.sync.unregister_device.assert_any_await("dev-1", sid, conn.wire.ws)
    ws_env.sync.broadcast_session_closed.assert_awaited_once_with(
        session_id=sid, device_id="dev-1",
    )


async def test_close_session_with_no_session_selected_replies_a_null_session(ws_env):
    """Docs: close_session "replies session_closed". With no session selected
    there is nothing to close: the reply's session_id is null and no other
    device is told anything."""
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip({"type": "close_session"}) == [
            {"type": "session_closed", "session_id": None}]
    ws_env.sync.broadcast_session_closed.assert_not_awaited()
    ws_env.sync.unregister_device.assert_not_awaited()


async def test_pending_lists_with_no_session_selected_are_empty(ws_env):
    """Docs: get_pending_permissions / get_pending_questions act on the
    selected session; with none selected each replies empty, session_id null."""
    sid = _make_session("sess-none")
    _pending_permission(sid, "req-none")
    _pending_question(sid, "q-none", [{"question": "A?"}])
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip({"type": "get_pending_permissions"}) == [{
            "type": "pending_permissions", "session_id": None,
            "pending": [], "deferred": [],
        }]
        assert await conn.roundtrip({"type": "get_pending_questions"}) == [{
            "type": "pending_questions", "session_id": None, "questions": [],
        }]


async def test_disconnect_deregisters_the_watched_session(ws_env):
    sid = _make_session("sess-dc")
    async with _connect(ws_env) as conn:
        await conn.roundtrip({"type": "watch_session", "session_id": sid})
    assert conn.task.done() and conn.task.exception() is None
    ws_env.sync.unregister_device.assert_awaited_with("dev-1", sid, conn.wire.ws)


# ═══ permission_response / pending / requeue ═════════════════════════════════


async def test_permission_allow_is_acked_with_the_resolution(ws_env):
    sid = _make_session("sess-p1")
    row = _pending_permission(sid, "req-1")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "permission_response", "session_id": sid,
            "request_id": "req-1", "decision": "allow", "remember": "none",
        })
    assert replies == [{
        "type": "permission_response_ack",
        "session_id": sid,
        "request_id": "req-1",
        "result": {"resolved": True, "auto_resolved_count": 0, "auto_resolved_ids": []},
    }]
    answered = _interaction(row["id"])
    assert answered["state"] == "answered"
    assert answered["answer"]["decision"] == "allow"
    assert answered["answered_by_device"] == "dev-1"


async def test_permission_deny_with_a_session_pattern_answers_matching_siblings(ws_env):
    sid = _make_session("sess-p2")
    _pending_permission(sid, "req-a", tool_input={"command": "git status"})
    sibling = _pending_permission(sid, "req-b", tool_input={"command": "git log"})
    other = _pending_permission(sid, "req-c", tool_input={"command": "rm -rf build"})
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "permission_response", "session_id": sid, "request_id": "req-a",
            "decision": "deny", "remember": "session", "pattern": "git *",
        })
    # The remembered rule answered req-b; req-c does not match the pattern.
    assert replies == [
        {
            "type": "permission_queue_update",
            "session_id": sid,
            "resolved_ids": ["req-b"],
            "remaining_count": 1,
        },
        {
            "type": "permission_response_ack",
            "session_id": sid,
            "request_id": "req-a",
            "result": {"resolved": True, "auto_resolved_count": 1,
                       "auto_resolved_ids": ["req-b"]},
        },
    ]
    assert _interaction(sibling["id"])["answer"]["decision"] == "deny"
    assert _interaction(other["id"])["state"] == "pending"
    assert permission_handler.get_session_rules(sid)[0]["tool_pattern"] == "git *"


async def test_permission_defer_ignores_remember_and_pattern(ws_env):
    sid = _make_session("sess-p3")
    row = _pending_permission(sid, "req-d")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "permission_response", "session_id": sid, "request_id": "req-d",
            "decision": "defer", "remember": "session", "pattern": "*",
        })
    assert replies == [
        {
            "type": "permission_queue_update",
            "session_id": sid,
            "resolved_ids": ["req-d"],
            "deferred_ids": ["req-d"],
            "remaining_count": 0,
        },
        {
            "type": "permission_response_ack",
            "session_id": sid,
            "request_id": "req-d",
            "result": {"resolved": True, "deferred": True,
                       "auto_resolved_count": 0, "auto_resolved_ids": []},
        },
    ]
    assert _interaction(row["id"])["answer"] == {"decision": "defer"}
    # No rule was remembered for a deferral.
    assert permission_handler.get_session_rules(sid) == []


async def test_deferred_request_is_listed_then_requeued_as_a_new_prompt(ws_env):
    sid = _make_session("sess-p4")
    _pending_permission(sid, "req-open", tool_input={"command": "pwd"})
    _pending_permission(sid, "req-later", tool_input={"command": "make"})
    async with _connect(ws_env) as conn:
        await conn.roundtrip({"type": "watch_session", "session_id": sid})
        await conn.roundtrip({
            "type": "permission_response", "session_id": sid,
            "request_id": "req-later", "decision": "defer",
        })
        [pending] = await conn.roundtrip({"type": "get_pending_permissions"})
        assert pending["type"] == "pending_permissions"
        assert pending["session_id"] == sid
        assert [(p["request_id"], p["state"]) for p in pending["pending"]] == [
            ("req-open", "pending")]
        assert [(d["request_id"], d["state"], d["tool_input"]) for d in pending["deferred"]] == [
            ("req-later", "deferred", {"command": "make"})]

        replies = await conn.roundtrip(
            {"type": "requeue_deferred_permissions", "session_id": sid}
        )
        requests = [f for f in replies if f["type"] == "permission_request"]
        assert requests == [{
            "type": "permission_request",
            "session_id": sid,
            "request_id": "req-later",
            "tool_name": "Bash",
            "tool_input": {"command": "make"},
            "queue_position": 2,
            "queue_total": 2,
        }]
        # One confirmation, after the re-sent requests.
        assert [f["type"] for f in replies] == [
            "permission_request", "permission_queue_update"]
        assert replies[-1] == {
            "type": "permission_queue_update",
            "session_id": sid,
            "requeued_ids": ["req-later"],
            "remaining_count": 2,
        }

        [after] = await conn.roundtrip({"type": "get_pending_permissions"})
        assert sorted(p["request_id"] for p in after["pending"]) == ["req-later", "req-open"]
        assert after["deferred"] == []


async def test_requeue_with_nothing_deferred_reports_an_empty_update(ws_env):
    sid = _make_session("sess-p5")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip(
            {"type": "requeue_deferred_permissions", "session_id": sid}
        )
    assert replies == [{
        "type": "permission_queue_update",
        "session_id": sid,
        "requeued_ids": [],
        "remaining_count": 0,
    }]


async def test_permission_response_for_an_unknown_request(ws_env):
    sid = _make_session("sess-p6")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "permission_response", "session_id": sid,
            "request_id": "never-asked", "decision": "allow",
        })
    assert replies == [{
        "type": "permission_response_ack",
        "session_id": sid,
        "request_id": "never-asked",
        "result": {"error": "Request not found"},
    }]


async def test_permission_response_for_an_already_answered_request(ws_env):
    sid = _make_session("sess-p7")
    row = _pending_permission(sid, "req-twice")
    async with _connect(ws_env) as conn:
        frame = {"type": "permission_response", "session_id": sid,
                 "request_id": "req-twice", "decision": "allow"}
        await conn.roundtrip(frame)
        second = await conn.roundtrip(dict(frame, decision="deny"))
    assert second == [{
        "type": "permission_response_ack",
        "session_id": sid,
        "request_id": "req-twice",
        "result": {"error": "Request not found"},
    }]
    # The first answer stands.
    assert _interaction(row["id"])["answer"]["decision"] == "allow"


@pytest.mark.parametrize("missing", ["request_id", "decision", "session_id"])
async def test_permission_response_missing_a_field_is_an_error(ws_env, missing):
    frame = {"type": "permission_response", "session_id": "s",
             "request_id": "r", "decision": "allow"}
    frame.pop(missing)
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip(frame) == [
            {"type": "error", "message": "Invalid permission response"}
        ]


async def test_permission_response_is_forwarded_to_the_owning_worker(ws_env, monkeypatch):
    from app.network import control

    forwarded = AsyncMock(return_value={"result": {"resolved": True}})
    monkeypatch.setattr(control, "answer_permission", forwarded)
    monkeypatch.setattr(
        cwh.ChatWebSocketHandler, "_network_owner", AsyncMock(return_value="owner-dev")
    )
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "permission_response", "session_id": "remote-sess",
            "request_id": "req-r", "decision": "defer", "remember": "profile",
            "pattern": "*",
        })
    assert replies == [{
        "type": "permission_response_ack", "session_id": "remote-sess",
        "request_id": "req-r", "result": {"resolved": True},
    }]
    assert forwarded.await_args.args == ("owner-dev",)
    kwargs = forwarded.await_args.kwargs
    assert (kwargs["remember"], kwargs["pattern"], kwargs["answered_by_device"]) == (
        None, None, "dev-1")


async def test_permission_forward_to_an_offline_worker_is_an_error(ws_env, monkeypatch):
    from app.network import control

    monkeypatch.setattr(
        control, "answer_permission",
        AsyncMock(side_effect=control.RunnerUnreachable("worker offline")),
    )
    monkeypatch.setattr(
        cwh.ChatWebSocketHandler, "_network_owner", AsyncMock(return_value="owner-dev")
    )
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "permission_response", "session_id": "remote-sess",
            "request_id": "req-r", "decision": "allow",
        })
    assert replies == [{"type": "error", "session_id": "remote-sess",
                        "message": "worker offline"}]


# ═══ user_question_response / get_pending_questions ══════════════════════════


QUESTIONS = [{"question": "Which database?"}, {"question": "Which port?"}]


async def test_question_answers_are_acked_and_stored(ws_env):
    sid = _make_session("sess-q1")
    row = _pending_question(sid, "q-1", QUESTIONS)
    answers = {"Which database?": "PostgreSQL", "Which port?": "5432"}
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "user_question_response", "session_id": sid,
            "request_id": "q-1", "answers": answers,
        })
    assert replies == [{
        "type": "user_question_response_ack",
        "session_id": sid,
        "request_id": "q-1",
        "success": True,
    }]
    assert _interaction(row["id"])["answer"] == answers


async def test_skipped_questions_resolve_without_answers(ws_env):
    sid = _make_session("sess-q2")
    row = _pending_question(sid, "q-2", QUESTIONS)
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "user_question_response", "session_id": sid, "request_id": "q-2",
            "answers": {"Which database?": "SQLite"}, "skipped": ["Which port?", ""],
        })
    assert replies[0]["success"] is True and "reason" not in replies[0]
    assert _interaction(row["id"])["answer"] == {
        "Which database?": "SQLite", SKIPPED_KEY: ["Which port?"],
    }


async def test_skipping_every_question_still_resolves(ws_env):
    sid = _make_session("sess-q2b")
    row = _pending_question(sid, "q-2b", QUESTIONS)
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "user_question_response", "session_id": sid, "request_id": "q-2b",
            "skipped": ["Which database?", "Which port?"],
        })
    assert replies == [{"type": "user_question_response_ack", "session_id": sid,
                        "request_id": "q-2b", "success": True}]
    assert _interaction(row["id"])["answer"] == {
        SKIPPED_KEY: ["Which database?", "Which port?"]}


async def test_dismissed_question_resolves_without_answers(ws_env):
    sid = _make_session("sess-q3")
    row = _pending_question(sid, "q-3", QUESTIONS)
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "user_question_response", "session_id": sid,
            "request_id": "q-3", "answers": {"Which database?": "ignored"},
            "dismissed": True,
        })
    assert replies == [{"type": "user_question_response_ack", "session_id": sid,
                        "request_id": "q-3", "success": True}]
    assert _interaction(row["id"])["answer"] == {DISMISSED_KEY: True}


async def test_answering_twice_is_not_pending(ws_env):
    sid = _make_session("sess-q4")
    _pending_question(sid, "q-4", QUESTIONS)
    frame = {"type": "user_question_response", "session_id": sid,
             "request_id": "q-4", "answers": {"Which database?": "MySQL"}}
    async with _connect(ws_env) as conn:
        await conn.roundtrip(frame)
        second = await conn.roundtrip(frame)
    assert second == [{
        "type": "user_question_response_ack",
        "session_id": sid,
        "request_id": "q-4",
        "success": False,
        "reason": "not_pending",
    }]


async def test_answering_an_unknown_question_is_not_pending(ws_env):
    sid = _make_session("sess-q5")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "user_question_response", "session_id": sid,
            "request_id": "q-none", "dismissed": True,
        })
    assert replies == [{"type": "user_question_response_ack", "session_id": sid,
                        "request_id": "q-none", "success": False, "reason": "not_pending"}]


@pytest.mark.parametrize(
    "extra",
    [{"answers": ["not", "a", "dict"]}, {"answers": {}}, {"answers": {}, "skipped": "x"}],
    ids=["answers-not-object", "nothing-answered", "skipped-not-list"],
)
async def test_invalid_question_response_is_an_error(ws_env, extra):
    sid = _make_session("sess-q6")
    row = _pending_question(sid, "q-6", QUESTIONS)
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "user_question_response", "session_id": sid, "request_id": "q-6",
            **extra,
        })
    assert replies == [{"type": "error", "message": "Invalid user question response"}]
    assert _interaction(row["id"])["state"] == "pending"


async def test_get_pending_questions_lists_the_watched_sessions_questions(ws_env):
    sid = _make_session("sess-q7")
    _pending_question(sid, "q-7", QUESTIONS)
    async with _connect(ws_env) as conn:
        # Nothing is selected yet: an empty reply.
        assert await conn.roundtrip({"type": "get_pending_questions"}) == [
            {"type": "pending_questions", "session_id": None, "questions": []}]
        await conn.roundtrip({"type": "watch_session", "session_id": sid})
        [reply] = await conn.roundtrip({"type": "get_pending_questions"})
    assert reply["type"] == "pending_questions"
    assert reply["session_id"] == sid
    [question] = reply["questions"]
    assert {k: v for k, v in question.items() if k != "created_at"} == {
        "session_id": sid,
        "request_id": "q-7",
        "tool_use_id": "tu-q-7",
        "questions": QUESTIONS,
        "nonblocking": False,
    }


# ═══ queue: query_queued, queue_message, remove, clear ═══════════════════════


async def test_query_into_a_busy_session_replies_query_queued_and_the_queue(ws_env, monkeypatch):
    sid = _make_session("sess-busy")
    live = _live_turn(sid)
    steer = AsyncMock(return_value=0)
    monkeypatch.setattr(session_runtime, "try_steer_pending", steer)
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({
            "type": "query", "session_id": sid, "prompt": "and the tests",
            "profile": "claude-code",
        })
    queued = turns_db.get_queued_turns(sid)
    assert [t["prompt"] for t in queued] == ["and the tests"]
    assert replies == [
        _queue_frame(sid, [_queue_row(queued[0], sid, 0)]),
        {"type": "query_queued", "session_id": sid},
    ]
    assert turns_db.get_live_turn(sid)["id"] == live["id"]
    steer.assert_awaited_once_with(sid)


async def test_empty_query_is_an_error(ws_env):
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip({"type": "query", "prompt": "   "}) == [
            {"type": "error", "message": "Empty prompt"}
        ]


async def test_queue_message_errors(ws_env):
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip(
            {"type": "queue_message", "session_id": "s", "content": "  "}
        ) == [{"type": "error", "message": "Empty queue content"}]
        assert await conn.roundtrip({"type": "queue_message", "content": "hi"}) == [
            {"type": "error", "message": "No session for queue"}
        ]


async def test_queue_message_for_a_session_with_no_capable_runner(ws_env, monkeypatch):
    from app.core import placement as placement_mod

    sid = _make_session("sess-nr")
    monkeypatch.setattr(
        placement_mod, "resolve_placement",
        lambda row: SimpleNamespace(kind="none", runner_id=None, reason="offline"),
    )
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip(
            {"type": "queue_message", "session_id": sid, "content": "later"}
        )
    assert replies == [{
        "type": "error",
        "message": "No capable device online for this session — its runner is "
                   "offline or lacks the required capabilities.",
    }]
    assert turns_db.get_queued_turns(sid) == []


async def test_remove_queued_message_removes_the_chip(ws_env):
    sid = _make_session("sess-rm")
    keep = turns_db.enqueue_turn(sid, "queue", prompt="keep")
    drop = turns_db.enqueue_turn(sid, "queue", prompt="drop")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip(
            {"type": "remove_queued_message", "session_id": sid, "message_id": drop["id"]}
        )
    assert replies == [_queue_frame(sid, [_queue_row(keep, sid, 0)])]
    assert turns_db.get_turn(drop["id"])["state"] == "aborted"
    ws_env.sync.broadcast_queue_updated.assert_awaited_with(sid, [_queue_row(keep, sid, 0)], pending_peer_messages=[])


async def test_remove_queued_message_already_delivered_stays(ws_env, monkeypatch):
    """Docs: "A queued message the running turn has already read cannot be
    removed; the next queue_updated shows what is left." The live runtime says
    the steer was delivered, so the row is left for the receipt to absorb."""
    sid = _make_session("sess-dl")
    live = _live_turn(sid)
    steered = turns_db.enqueue_turn(sid, "user", prompt="already read")
    runtime = session_runtime.SessionRuntime(dict(live))
    runtime._steer_attempted.add(steered["id"])
    adapter = SimpleNamespace(cancel_steer=AsyncMock(return_value=False))
    monkeypatch.setattr(runtime, "_live_adapter", lambda: adapter)
    session_runtime._live_runtimes[sid] = runtime
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip(
            {"type": "remove_queued_message", "session_id": sid, "message_id": steered["id"]}
        )
    assert turns_db.get_turn(steered["id"])["state"] == "queued"
    assert replies == [_queue_frame(sid, [_queue_row(steered, sid, 0)])]
    adapter.cancel_steer.assert_awaited_once_with(steered["id"])


async def test_remove_queued_message_whose_steer_was_recalled(ws_env, monkeypatch):
    sid = _make_session("sess-rc")
    live = _live_turn(sid)
    steered = turns_db.enqueue_turn(sid, "user", prompt="take it back")
    runtime = session_runtime.SessionRuntime(dict(live))
    runtime._steer_attempted.add(steered["id"])
    adapter = SimpleNamespace(cancel_steer=AsyncMock(return_value=True))
    monkeypatch.setattr(runtime, "_live_adapter", lambda: adapter)
    session_runtime._live_runtimes[sid] = runtime
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip(
            {"type": "remove_queued_message", "session_id": sid, "message_id": steered["id"]}
        )
    assert turns_db.get_turn(steered["id"])["state"] == "aborted"
    assert replies == [_queue_frame(sid, [])]
    assert steered["id"] not in runtime._steer_attempted


async def test_remove_queued_message_without_an_id_is_ignored(ws_env):
    sid = _make_session("sess-rm0")
    turns_db.enqueue_turn(sid, "queue", prompt="stays")
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip(
            {"type": "remove_queued_message", "session_id": sid}
        ) == []
    assert len(turns_db.get_queued_turns(sid)) == 1


async def test_clear_queued_messages_leaves_internal_turns(ws_env):
    sid = _make_session("sess-clr")
    a = turns_db.enqueue_turn(sid, "user", prompt="a")
    b = turns_db.enqueue_turn(sid, "queue", prompt="b")
    internal = turns_db.enqueue_turn(sid, "failover", prompt="retry")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({"type": "clear_queued_messages", "session_id": sid})
        # No session given and none selected: nothing to clear, no reply.
        assert await conn.roundtrip({"type": "clear_queued_messages"}) == []
    assert replies == [_queue_frame(sid, [])]
    assert [turns_db.get_turn(t["id"])["state"] for t in (a, b, internal)] == [
        "aborted", "aborted", "queued"]


# ═══ interrupt_for_query / stop ══════════════════════════════════════════════


async def test_interrupt_for_query_on_an_idle_session_confirms(ws_env):
    sid = _make_session("sess-i1")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({"type": "interrupt_for_query", "session_id": sid})
    assert replies == [{"type": "interrupt_confirmed", "session_id": sid}]


async def test_interrupt_for_query_waits_for_the_live_turn_to_settle(ws_env, monkeypatch):
    sid = _make_session("sess-i2")
    live = _live_turn(sid)
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 5.0)
    async with _connect(ws_env) as conn:
        conn.wire.push({"type": "interrupt_for_query", "session_id": sid})
        await conn.wait_for(
            lambda fs: turns_db.get_turn(live["id"])["state"] == "interrupting"
        )
        # Still draining: no confirmation yet.
        await asyncio.sleep(0.1)
        assert conn.frames("interrupt_confirmed") == []
        turns_db.settle_turn(live["id"], "aborted", "interrupted")
        await conn.wait_for(lambda fs: any(f["type"] == "interrupt_confirmed" for f in fs))
    assert conn.frames("interrupt_confirmed") == [
        {"type": "interrupt_confirmed", "session_id": sid}]


async def test_interrupt_for_query_confirms_after_the_drain_timeout(ws_env, monkeypatch):
    sid = _make_session("sess-i3")
    _live_turn(sid)
    monkeypatch.setattr(cwh, "_INTERRUPT_DRAIN_TIMEOUT_SECONDS", 0.1)
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({"type": "interrupt_for_query", "session_id": sid})
    assert replies == [{"type": "interrupt_confirmed", "session_id": sid}]


async def test_interrupt_for_query_with_no_session(ws_env):
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({"type": "interrupt_for_query"})
    assert replies == [
        {"type": "interrupt_failed", "session_id": None, "message": "No active session"}
    ]


async def test_interrupt_for_query_failure_is_reported(ws_env, monkeypatch):
    from app.core import turn_manager

    monkeypatch.setattr(
        turn_manager, "interrupt_async", AsyncMock(side_effect=RuntimeError("db down"))
    )
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({"type": "interrupt_for_query", "session_id": "s"})
    assert replies == [
        {"type": "interrupt_failed", "session_id": "s", "message": "db down"}
    ]


async def test_stop_forwarded_to_an_offline_worker_offers_a_fork(ws_env, monkeypatch):
    from app.network import control

    monkeypatch.setattr(
        control, "interrupt", AsyncMock(side_effect=control.RunnerUnreachable("gone"))
    )
    monkeypatch.setattr(
        cwh.ChatWebSocketHandler, "_network_owner", AsyncMock(return_value="owner-dev")
    )
    sid = _make_session("sess-fw")
    async with _connect(ws_env) as conn:
        replies = await conn.roundtrip({"type": "stop", "session_id": sid})
    assert replies[0] == {
        "type": "error",
        "session_id": sid,
        "message": "The worker that runs this session is offline (gone). "
                   "You can fork the transcript into a new session on this device.",
        "offer_fork": True,
    }
    # The queue view is re-sent either way.
    assert replies[1:] == [_queue_frame(sid, [])]


# ═══ tabs_update ═════════════════════════════════════════════════════════════


async def test_tabs_update_fans_out_to_the_users_other_devices(ws_env):
    tabs = {"open": ["sess-1", "sess-2"], "active": "sess-2"}
    async with _connect(ws_env) as conn:
        assert await conn.roundtrip({"type": "tabs_update", "tabs_data": tabs}) == []
    ws_env.sync.broadcast_tabs_updated.assert_awaited_once_with(
        user_id="admin", tabs_data=tabs, source_device_id="dev-1",
    )


# ═══ API keys: agent restriction and session ownership ═══════════════════════


API_USER = {"id": "api-1", "name": "key", "is_active": True}


def _api_env(monkeypatch, profile_ids):
    if database.get_api_user("api-1") is None:
        database.create_api_user("api-1", "Key", "hash-api-1")
    user = dict(API_USER, profile_ids=profile_ids)
    monkeypatch.setattr(database, "get_api_user", lambda uid: user if uid == "api-1" else None)
    return user


def _own_session(sid, *, profile_id="default", api_user_id="api-1"):
    from app.db.base import get_db

    _make_session(sid, profile_id=profile_id)
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET api_user_id = %s WHERE id = %s", (api_user_id, sid))
    return sid


async def test_api_key_query_with_an_agent_outside_its_set_is_refused(ws_env, monkeypatch):
    _api_env(monkeypatch, ["default"])
    sid = _own_session("sess-k1")
    async with _connect(ws_env, api_user=API_USER) as conn:
        replies = await conn.roundtrip({
            "type": "query", "session_id": sid, "prompt": "hi", "profile": "other-agent",
        })
    assert replies == [{
        "type": "error",
        "message": "This API key is not allowed to use agent 'other-agent'.",
    }]
    assert turns_db.get_queued_turns(sid) == []


async def test_api_key_with_several_agents_must_name_one(ws_env, monkeypatch):
    _api_env(monkeypatch, ["default", "second"])
    sid = _own_session("sess-k2")
    async with _connect(ws_env, api_user=API_USER) as conn:
        replies = await conn.roundtrip({"type": "query", "session_id": sid, "prompt": "hi"})
    assert replies == [{
        "type": "error",
        "message": "Specify 'profile': this API key may use agents default, second.",
    }]
    assert turns_db.get_queued_turns(sid) == []


async def test_api_key_queue_message_into_a_foreign_agent_session_is_refused(ws_env, monkeypatch):
    _api_env(monkeypatch, ["default"])
    database.create_profile(profile_id="other-agent", name="Other", description=None, config={})
    sid = _own_session("sess-k3", profile_id="other-agent")
    # The key owns the session, but its agent is outside the key's set: the
    # session gate refuses it (the agent scope REST applies) and, the session
    # being the key's own, names the agent. Nothing is queued.
    async with _connect(ws_env, api_user=API_USER) as conn:
        replies = await conn.roundtrip(
            {"type": "queue_message", "session_id": sid, "content": "later"}
        )
    assert replies == [{
        "type": "error",
        "session_id": sid,
        "message": "This API key is not allowed to use agent 'other-agent'.",
    }]
    assert turns_db.get_queued_turns(sid) == []


async def test_api_key_queue_message_into_its_own_session_queues(ws_env, monkeypatch):
    _api_env(monkeypatch, ["default"])
    sid = _own_session("sess-k4")
    async with _connect(ws_env, api_user=API_USER) as conn:
        replies = await conn.roundtrip(
            {"type": "queue_message", "session_id": sid, "content": "later"}
        )
    queued = turns_db.get_queued_turns(sid)
    assert [t["prompt"] for t in queued] == ["later"]
    assert queued[0]["payload"]["api_user_id"] == "api-1"
    assert replies == [_queue_frame(sid, [_queue_row(queued[0], sid, 0)])]


ACCESS_DENIED = "Access denied to this session"

# Every frame that names a session and answers a refusal with an ``error``
# frame (load_session answers session_not_found; interrupt_for_query
# interrupt_failed).
SESSION_FRAMES = [
    {"type": "watch_session"},
    {"type": "stop"},
    {"type": "permission_response", "request_id": "req-f", "decision": "allow"},
    {"type": "requeue_deferred_permissions"},
    {"type": "user_question_response", "request_id": "q-f", "answers": {"A?": "B"}},
    {"type": "queue_message", "content": "later"},
    {"type": "query", "prompt": "hi"},
    {"type": "clear_queued_messages"},
]


@pytest.mark.parametrize(
    "frame",
    SESSION_FRAMES,
    ids=lambda f: f["type"],
)
async def test_api_key_frames_naming_another_principals_session_are_refused(
    ws_env, monkeypatch, frame,
):
    """Docs: every frame that names a session is refused with an ``error``
    frame carrying ``session_id`` and ``message`` when the session belongs to
    the owner or another key — and changes nothing."""
    _api_env(monkeypatch, None)
    sid = _make_session("sess-owner")  # the owner's (no api_user_id)
    perm = _pending_permission(sid, "req-f")
    question = _pending_question(sid, "q-f", [{"question": "A?"}])
    chip = turns_db.enqueue_turn(sid, "queue", prompt="owner's chip")
    async with _connect(ws_env, api_user=API_USER) as conn:
        replies = await conn.roundtrip(dict(frame, session_id=sid))
        assert conn.handler.current_session_id is None
    assert replies == [{"type": "error", "session_id": sid, "message": ACCESS_DENIED}]
    assert _interaction(perm["id"])["state"] == "pending"
    assert _interaction(question["id"])["state"] == "pending"
    assert [t["id"] for t in turns_db.get_queued_turns(sid)] == [chip["id"]]
    ws_env.sync.register_device.assert_not_awaited()


async def test_api_key_load_session_cannot_tell_a_foreign_session_from_an_unknown_one(
    ws_env, monkeypatch,
):
    """load_session answers another principal's session exactly like an id
    that does not exist (``session_not_found``, the REST 404 artifact access
    gives), so a key cannot probe which session ids exist."""
    _api_env(monkeypatch, None)
    sid = _make_session("sess-owner-l")  # the owner's
    database.create_api_user("api-2", "Key 2", "hash-api-2")
    other = _own_session("sess-other-key", api_user_id="api-2")  # another key's
    async with _connect(ws_env, api_user=API_USER) as conn:
        foreign = await conn.roundtrip({"type": "load_session", "session_id": sid})
        other_key = await conn.roundtrip({"type": "load_session", "session_id": other})
        unknown = await conn.roundtrip({"type": "load_session", "session_id": "sess-none"})
        assert conn.handler.current_session_id is None
    assert foreign == [{"type": "session_not_found", "session_id": sid}]
    assert other_key == [{"type": "session_not_found", "session_id": other}]
    assert unknown == [{"type": "session_not_found", "session_id": "sess-none"}]
    ws_env.sync.register_device.assert_not_awaited()


@pytest.mark.parametrize(
    "frame",
    SESSION_FRAMES,
    ids=lambda f: f["type"],
)
async def test_api_key_frames_answer_a_foreign_and_an_unknown_session_alike(
    ws_env, monkeypatch, frame,
):
    """Every other frame refuses an unknown id with the same error frame it
    gives another principal's session."""
    _api_env(monkeypatch, None)
    sid = _make_session("sess-owner-u")
    async with _connect(ws_env, api_user=API_USER) as conn:
        foreign = await conn.roundtrip(dict(frame, session_id=sid))
        unknown = await conn.roundtrip(dict(frame, session_id="sess-none"))
    assert foreign == [{"type": "error", "session_id": sid, "message": ACCESS_DENIED}]
    assert unknown == [{"type": "error", "session_id": "sess-none", "message": ACCESS_DENIED}]


async def test_api_key_load_session_of_its_own_session_outside_its_agents_names_the_agent(
    ws_env, monkeypatch,
):
    _api_env(monkeypatch, ["default"])
    database.create_profile(profile_id="other-agent", name="Other", description=None, config={})
    sid = _own_session("sess-own-agent", profile_id="other-agent")
    async with _connect(ws_env, api_user=API_USER) as conn:
        replies = await conn.roundtrip({"type": "load_session", "session_id": sid})
        assert conn.handler.current_session_id is None
    assert replies == [{
        "type": "error",
        "session_id": sid,
        "message": "This API key is not allowed to use agent 'other-agent'.",
    }]


async def test_api_key_interrupt_for_query_on_a_foreign_session_fails(ws_env, monkeypatch):
    _api_env(monkeypatch, None)
    sid = _make_session("sess-owner-i")
    live = _live_turn(sid)
    async with _connect(ws_env, api_user=API_USER) as conn:
        replies = await conn.roundtrip({"type": "interrupt_for_query", "session_id": sid})
    assert replies == [{"type": "interrupt_failed", "session_id": sid, "message": ACCESS_DENIED}]
    assert turns_db.get_turn(live["id"])["state"] == "running"


async def test_api_key_cannot_remove_a_chip_from_another_session(ws_env, monkeypatch):
    _api_env(monkeypatch, None)
    mine = _own_session("sess-mine")
    theirs = _make_session("sess-theirs")
    chip = turns_db.enqueue_turn(theirs, "queue", prompt="not yours")
    async with _connect(ws_env, api_user=API_USER) as conn:
        # Naming its own session does not make another session's turn id reachable.
        replies = await conn.roundtrip(
            {"type": "remove_queued_message", "session_id": mine, "message_id": chip["id"]}
        )
    assert replies == [{"type": "error", "session_id": mine, "message": ACCESS_DENIED}]
    assert turns_db.get_turn(chip["id"])["state"] == "queued"


async def test_api_key_acts_on_its_own_session(ws_env, monkeypatch):
    _api_env(monkeypatch, None)
    sid = _own_session("sess-own")
    row = _pending_permission(sid, "req-own")
    chip = turns_db.enqueue_turn(sid, "queue", prompt="mine")
    async with _connect(ws_env, api_user=API_USER) as conn:
        assert await conn.roundtrip({"type": "watch_session", "session_id": sid}) == [
            _queue_frame(sid, [_queue_row(chip, sid, 0)])]
        [pending] = await conn.roundtrip({"type": "get_pending_permissions"})
        assert [p["request_id"] for p in pending["pending"]] == ["req-own"]
        ack = await conn.roundtrip({
            "type": "permission_response", "session_id": sid,
            "request_id": "req-own", "decision": "allow",
        })
        removed = await conn.roundtrip(
            {"type": "remove_queued_message", "session_id": sid, "message_id": chip["id"]}
        )
    assert ack[0]["type"] == "permission_response_ack"
    assert ack[0]["result"]["resolved"] is True
    assert _interaction(row["id"])["state"] == "answered"
    assert removed == [_queue_frame(sid, [])]


async def test_api_key_remember_profile_is_kept_to_the_session(ws_env, monkeypatch):
    """Docs: an API key that sends ``remember: "profile"`` gets a rule for its
    own session only, the same as ``"session"``."""
    _api_env(monkeypatch, None)
    sid = _own_session("sess-rp")
    _pending_permission(sid, "req-rp", tool_input={"command": "npm test"})
    async with _connect(ws_env, api_user=API_USER) as conn:
        await conn.roundtrip({
            "type": "permission_response", "session_id": sid, "request_id": "req-rp",
            "decision": "allow", "remember": "profile", "pattern": "npm *",
        })
    assert database.get_permission_rules(profile_id="default") == []
    assert [r["tool_pattern"] for r in permission_handler.get_session_rules(sid)] == ["npm *"]


async def test_owner_remember_profile_persists_an_agent_rule(ws_env):
    sid = _make_session("sess-orp")
    _pending_permission(sid, "req-orp", tool_input={"command": "npm test"})
    async with _connect(ws_env) as conn:
        await conn.roundtrip({
            "type": "permission_response", "session_id": sid, "request_id": "req-orp",
            "decision": "allow", "remember": "profile", "pattern": "npm *",
        })
    rules = database.get_permission_rules(profile_id="default")
    assert [(r["tool_name"], r["tool_pattern"], r["decision"]) for r in rules] == [
        ("Bash", "npm *", "allow")]
    assert permission_handler.get_session_rules(sid) == []
