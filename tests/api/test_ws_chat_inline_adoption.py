"""
Regression tests for the new-session ADOPTION TOKEN (``request_id``) echo on the
LIVE browser route — ``app/api/websocket.py``'s inline ``/ws/chat`` handler.

WHY THIS FILE EXISTS (the bug it guards against): commit 561c3975
("fix(session): authenticated new-session adoption") taught the frontend to mint
a ``request_id`` onto a NEW-session ``query`` frame and to adopt a minted session
ONLY when the echoed token matches (``ws-message-handlers.ts``). The backend echo
was implemented ONLY on the class-based ``ChatWebSocketHandler`` (the headless
path). But the browser connects to ``/api/v1/ws/chat`` — the SEPARATE, inline
handler in ``app/api/websocket.py`` — which was never updated:

- its ``query`` branch never read ``data.get("request_id")``,
- its ``session_created`` frame carried no ``request_id``,
- its inline ``run_query`` sent the first ``start`` frame with no ``request_id``.

Result: for every NEW chat in the web UI the frontend dropped both frames (its
adoption predicate ``createdRequestId === internal.pendingRequestId`` failed), the
client never bound the session, and the UI hung on "New Chat" with no output.

These tests drive the REAL inline route through its ``query`` branch (mirroring how
``tests/api/test_chat_ws_adoption.py`` captures the spawned ``run_query`` via a
recording ``create_task``) and pin the contract, matching ``chat_ws_handler``'s
frame shape EXACTLY (the ``request_id`` key is OMITTED when there is no token —
never null):

1. A new-session ``query`` frame carrying ``request_id`` produces a REAL
   ``session_created`` frame echoing that exact token, and threads the SAME token
   into the spawned ``run_query`` (which echoes it on the first ``start`` frame).
2. A new-session ``query`` frame with NO ``request_id`` produces a
   ``session_created`` that OMITS the key, and threads ``None`` into ``run_query``.
3. An EXISTING-session ``query`` (session_id set) threads NO token even if a stray
   ``request_id`` is present — the token only authenticates NEW-session adoption.
4. The queued-message drain (server-initiated, ``deferred_delivery=True``) passes
   NO ``request_id`` so its ``start`` frame can never be adopted.

No Postgres or real SDK client is required — every boundary is mocked.
"""

import asyncio
import json
import inspect
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.websockets import WebSocketState

import app.api.websocket as ws_mod


# ---------------------------------------------------------------------------
# Fake WebSocket that yields one scripted inbound frame then "disconnects".
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal Starlette-WebSocket stand-in. Yields the scripted inbound frames
    from ``receive_json`` (one per call), then raises ``WebSocketDisconnect`` so the
    route's ``while True`` message loop ends. Outbound ``send_text`` payloads are
    captured for assertions on the REAL frames the route builds."""

    def __init__(self, inbound):
        self._inbound = list(inbound)
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[dict] = []
        self.cookies = {}

    async def accept(self):
        return None

    async def receive_json(self):
        if self._inbound:
            return self._inbound.pop(0)
        self.client_state = WebSocketState.DISCONNECTED
        raise ws_mod.WebSocketDisconnect(code=1000)

    async def send_text(self, text: str):
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = ""):
        self.client_state = WebSocketState.DISCONNECTED


class _RecordingCreateTask:
    """Captures each coroutine handed to asyncio.create_task WITHOUT running it, so
    the spawned inline ``run_query``'s bound ``request_id`` can be inspected from its
    frame locals — the same technique test_chat_ws_adoption.py uses. Returns a
    done-looking MagicMock task so the route's registry bookkeeping is happy.

    The coroutine's ``cr_frame`` locals are SNAPSHOT at capture time because the
    frame's locals are cleared once the coroutine is closed during teardown."""

    def __init__(self):
        self.coros = []
        self.snapshots = []  # (qualname, dict(f_locals)) per captured coroutine

    def __call__(self, coro):
        qn = getattr(coro, "__qualname__", "")
        # Only intercept the inline run_query spawns; schedule everything else (the
        # no-op ping loop) as a REAL task so the route's `await ping_task` teardown
        # doesn't choke on a MagicMock.
        if not (qn.endswith(".run_query") or qn == "run_query"):
            return asyncio.ensure_future(coro)
        self.coros.append(coro)
        frame = getattr(coro, "cr_frame", None)
        f_locals = dict(frame.f_locals) if frame is not None else {}
        self.snapshots.append((qn, f_locals))
        task = MagicMock()
        task.done.return_value = True
        task.cancel.return_value = None
        return task

    def close_all(self):
        for c in self.coros:
            try:
                c.close()
            except Exception:
                pass


def _frames_of_type(ws: _FakeWebSocket, frame_type: str):
    return [f for f in ws.sent if isinstance(f, dict) and f.get("type") == frame_type]


def _run_query_locals(created: _RecordingCreateTask):
    """Snapshot f_locals of every captured inline run_query coroutine (bound args)."""
    return [
        f_locals
        for qn, f_locals in created.snapshots
        if qn.endswith(".run_query") or qn == "run_query"
    ]


def _drive_route_patches(stack: ExitStack, created: _RecordingCreateTask):
    """Patch every boundary the query branch touches so a single ``query`` frame
    runs through it and spawns (but does not execute) run_query."""
    stack.enter_context(
        patch.object(ws_mod, "authenticate_websocket", new=AsyncMock(return_value=(True, None)))
    )

    async def _noop_ping(*a, **k):
        return None

    stack.enter_context(patch.object(ws_mod, "ping_loop", new=_noop_ping))
    # Capture the spawned run_query coroutine instead of scheduling it.
    stack.enter_context(patch.object(ws_mod.asyncio, "create_task", new=created))

    sync = stack.enter_context(patch.object(ws_mod, "sync_engine"))
    sync.register_user_device = AsyncMock()
    sync.unregister_user_device = AsyncMock()
    sync.register_device = AsyncMock()
    sync.unregister_device = AsyncMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_user_session_created = AsyncMock()
    sync.broadcast_session_updated = AsyncMock()
    sync.clear_streaming_state = AsyncMock()
    sync.get_user_streaming_sessions = MagicMock(return_value=[])
    # Disconnect-teardown bookkeeping (returns ints so the > 0 / == 0 checks work).
    sync.get_device_count = MagicMock(return_value=1)

    store = stack.enter_context(patch.object(ws_mod, "live_session_store"))
    store.get_session = MagicMock(return_value=None)
    store.has_session = MagicMock(return_value=False)
    store.is_streaming = MagicMock(return_value=False)
    store.cleanup_session = AsyncMock()

    db = stack.enter_context(patch.object(ws_mod, "database"))
    db.get_session = MagicMock(return_value={"sdk_session_id": None, "title": "x"})

    async def _fake_run_sync(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None

    stack.enter_context(patch.object(ws_mod, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    stack.enter_context(patch.object(ws_mod, "cleanup_session_handlers", new=AsyncMock()))

    # Isolate the active-session registry + lock.
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new={}))
    stack.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))


async def _drive(inbound):
    """Drive the real inline chat_websocket query branch with a fake WS, capturing
    the REAL session_created frame and the spawned run_query args."""
    ws = _FakeWebSocket(inbound)
    created = _RecordingCreateTask()
    with ExitStack() as stack:
        _drive_route_patches(stack, created)
        try:
            await ws_mod.chat_websocket(ws, token="tok", device_id="dev-1")
        finally:
            created.close_all()
    return ws, created


# ---------------------------------------------------------------------------
# 1. New-session query WITH a token → session_created echoes it + threads it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_query_echoes_request_id_and_threads_it():
    ws, created = await _drive([
        {
            "type": "query",
            "prompt": "brand new browser chat",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            "request_id": "req-1700000000-abc",
        }
    ])

    created_frames = _frames_of_type(ws, "session_created")
    assert len(created_frames) == 1, f"expected one session_created, got {created_frames}"
    assert created_frames[0].get("request_id") == "req-1700000000-abc"

    # The SAME token is threaded into the spawned run_query so its first `start`
    # frame echoes it.
    rq = _run_query_locals(created)
    assert len(rq) == 1, f"expected one run_query spawn, got {len(rq)}"
    assert rq[0].get("request_id") == "req-1700000000-abc"


# ---------------------------------------------------------------------------
# 2. New-session query WITHOUT a token → session_created OMITS the key + None.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_query_without_token_omits_key_and_threads_none():
    ws, created = await _drive([
        {
            "type": "query",
            "prompt": "new chat, legacy client with no token",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            # no request_id
        }
    ])

    created_frames = _frames_of_type(ws, "session_created")
    assert len(created_frames) == 1
    assert "request_id" not in created_frames[0], "session_created must omit key when no token"

    rq = _run_query_locals(created)
    assert len(rq) == 1
    assert rq[0].get("request_id") is None


# ---------------------------------------------------------------------------
# 3. Existing-session query threads NO token even if a stray one is present.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_session_query_threads_no_token():
    ws, created = await _drive([
        {
            "type": "query",
            "prompt": "follow-up on an open session",
            "session_id": "sess-existing",
            "profile": "claude-code",
            "project": None,
            "request_id": "req-should-be-ignored",
        }
    ])

    # No session_created for an existing session.
    assert _frames_of_type(ws, "session_created") == []

    rq = _run_query_locals(created)
    assert len(rq) == 1
    assert rq[0].get("request_id") is None, "existing-session run_query must carry no token"


# ---------------------------------------------------------------------------
# 4. Source-wiring guards — signature + deferred-drain call site.
# ---------------------------------------------------------------------------


def test_inline_run_query_signature_accepts_request_id():
    """The inline run_query must accept a ``request_id`` keyword (default None) so
    ONLY the explicit client query threads a token; every other caller omits it."""
    src = inspect.getsource(ws_mod.chat_websocket)
    assert "request_id: Optional[str] = None" in src, (
        "inline run_query must take request_id: Optional[str] = None"
    )
    assert 'request_id = data.get("request_id")' in src
    assert "request_id=request_id" in src


def test_deferred_drain_call_site_passes_no_token():
    """The queued-message drain (server-initiated, deferred_delivery=True) must NOT
    thread a request_id — its start frame must stay non-adoptable. Guard the exact
    call site so a future edit can't leak a token into it."""
    src = inspect.getsource(ws_mod.chat_websocket)
    assert "deferred_delivery=True" in src
    for line in src.splitlines():
        if "run_query(" in line and "deferred_delivery=True" in line:
            assert "request_id" not in line, (
                f"deferred drain call site must not thread a token: {line.strip()}"
            )
