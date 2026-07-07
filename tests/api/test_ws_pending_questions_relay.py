"""
Regression tests for the cross-install ``get_pending_questions`` relay reply shape
— ``app/api/websocket.py``'s inline ``/ws/chat`` handler.

WHY THIS FILE EXISTS (the bug it guards against): for a REMOTE (cluster) session the
pending question lives in the OWNING install's in-memory handler, so the local
handler is always empty. The route relays the read to the owner. The PRE-FIX
handler swallowed a relay failure (``InstallOffline`` / any ``Exception``) with only a
log line and then replied with a SUCCESS-shaped ``{"type": "pending_questions",
"questions": []}`` — indistinguishable from an authoritative "owner says none
pending". The frontend REPLACES ``pendingQuestions`` whenever the request_id sets
differ, so on every reconnect/session-switch resync a transient relay failure wiped
a locally-open question modal (losing the in-progress answer).

THE FIX (guarded here): a FAILED relay flags the reply ``stale: true`` and keeps
whatever (non-authoritative, local) list we have; the frontend leaves its modal
untouched when it sees the marker. A SUCCESSFUL relay — even with ZERO questions —
is authoritative and must OMIT the marker so an empty list still clears stale
modals (and old clients ignore the absent key gracefully).

These tests drive the REAL inline route through a ``watch_session`` frame (to bind
``current_session_id``) followed by a ``get_pending_questions`` frame, mocking
``cloud_relay`` to raise (failure) or return (success). No Postgres or real SDK
client is required — every boundary is mocked.
"""

import asyncio
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.websockets import WebSocketState

import app.api.chat_ws_handler as cwh
import app.api.websocket as ws_mod


# ---------------------------------------------------------------------------
# Fake WebSocket that yields scripted inbound frames then "disconnects".
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


def _frames_of_type(ws: _FakeWebSocket, frame_type: str):
    return [f for f in ws.sent if isinstance(f, dict) and f.get("type") == frame_type]


# A remote (cluster) session dict — is_remote_session() is mocked True regardless,
# but keep a realistic shape.
_REMOTE_SESSION = {"id": "sess-remote", "sdk_session_id": "sdk-1", "title": "x"}


def _drive_route_patches(stack: ExitStack, relay_mock):
    """Patch every boundary the watch_session + get_pending_questions branches touch
    so the two frames run through the route. ``relay_mock`` is installed as
    ``cloud_relay.relay_get_pending_questions``; ``is_remote_session`` returns True."""
    stack.enter_context(
        patch.object(ws_mod, "authenticate_websocket", new=AsyncMock(return_value=(True, None)))
    )

    async def _noop_ping(*a, **k):
        return None

    stack.enter_context(patch.object(ws_mod, "ping_loop", new=_noop_ping))

    # Post-flip (STEP 5): the route is a shim over ChatWebSocketHandler, which
    # resolves collaborators from cwh module globals — mirror the SAME mock
    # objects into both namespaces.
    sync = stack.enter_context(patch.object(ws_mod, "sync_engine"))
    stack.enter_context(patch.object(cwh, "sync_engine", new=sync))
    sync.register_user_device = AsyncMock()
    sync.unregister_user_device = AsyncMock()
    sync.register_device = AsyncMock()
    sync.unregister_device = AsyncMock()
    sync.is_device_watching_session = MagicMock(return_value=False)
    sync.get_user_streaming_sessions = MagicMock(return_value=[])
    sync.get_device_count = MagicMock(return_value=1)
    sync._connections = {}

    store = stack.enter_context(patch.object(ws_mod, "live_session_store"))
    stack.enter_context(patch.object(cwh, "live_session_store", new=store))
    store.get_session = MagicMock(return_value=None)
    store.has_session = MagicMock(return_value=False)
    store.is_streaming = MagicMock(return_value=False)
    store.cleanup_session = AsyncMock()

    # Local in-memory question handler is always empty for a remote session — that
    # is the non-authoritative fallback set the fix must NOT reply with bare on
    # failure.
    uqh = stack.enter_context(patch.object(ws_mod, "user_question_handler"))
    stack.enter_context(patch.object(cwh, "user_question_handler", new=uqh))
    uqh.get_pending_questions = MagicMock(return_value=[])

    db = stack.enter_context(patch.object(ws_mod, "database"))
    stack.enter_context(patch.object(cwh, "database", new=db))
    db.get_session = MagicMock(return_value=_REMOTE_SESSION)
    db.get_queued_messages = MagicMock(return_value=[])

    async def _fake_run_sync(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None

    _run_sync = AsyncMock(side_effect=_fake_run_sync)
    stack.enter_context(patch.object(ws_mod, "run_sync", new=_run_sync))
    stack.enter_context(patch.object(cwh, "run_sync", new=_run_sync))
    stack.enter_context(patch.object(ws_mod, "cleanup_session_handlers", new=AsyncMock()))

    # Isolate the active-session registry + lock.
    stack.enter_context(patch.object(ws_mod, "_active_chat_sessions", new={}))
    stack.enter_context(patch.object(ws_mod, "_active_sessions_lock", new=asyncio.Lock()))

    # The branch imports cloud_relay lazily via `from app.core import cloud_relay`,
    # which binds the ``cloud_relay`` ATTRIBUTE on the already-imported ``app.core``
    # package (not sys.modules). So patch that attribute directly. Give the fake a
    # real InstallOffline exception class so the branch's `except InstallOffline`
    # matches, plus our is_remote/relay mocks.
    import app.core as core_pkg
    from app.core import cloud_relay as real_cloud_relay

    fake_relay = MagicMock()
    fake_relay.InstallOffline = real_cloud_relay.InstallOffline
    fake_relay.is_remote_session = MagicMock(return_value=True)
    fake_relay.relay_get_pending_questions = relay_mock
    stack.enter_context(patch.object(core_pkg, "cloud_relay", fake_relay))
    stack.enter_context(patch.dict("sys.modules", {"app.core.cloud_relay": fake_relay}))


async def _drive(relay_mock, session_id="sess-remote"):
    """Drive the real inline route: bind current_session_id via watch_session, then
    issue get_pending_questions. Returns the captured pending_questions frames."""
    ws = _FakeWebSocket([
        {"type": "watch_session", "session_id": session_id},
        {"type": "get_pending_questions"},
    ])
    with ExitStack() as stack:
        _drive_route_patches(stack, relay_mock)
        await ws_mod.chat_websocket(ws, token="tok", device_id="dev-1")
    return ws


# ---------------------------------------------------------------------------
# 1. Relay raises InstallOffline → reply carries stale:true, keeps local list.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_offline_marks_reply_stale():
    # A relay that raises InstallOffline (the owning install is unreachable).
    from app.core import cloud_relay as real_cloud_relay
    relay = AsyncMock(side_effect=real_cloud_relay.InstallOffline("owner offline"))

    ws = await _drive(relay)

    replies = _frames_of_type(ws, "pending_questions")
    assert len(replies) == 1, f"expected one pending_questions reply, got {replies}"
    reply = replies[0]
    # The relay-failure reply MUST carry the stale marker so the frontend leaves a
    # locally-open modal untouched instead of wiping it.
    assert reply.get("stale") is True, f"offline relay must mark reply stale: {reply}"
    # It replies with the local (empty) fallback — not authoritative.
    assert reply.get("questions") == []
    assert reply.get("session_id") == "sess-remote"


# ---------------------------------------------------------------------------
# 2. Relay raises a generic Exception → reply still carries stale:true.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_generic_error_marks_reply_stale():
    relay = AsyncMock(side_effect=RuntimeError("relay blew up"))

    ws = await _drive(relay)

    replies = _frames_of_type(ws, "pending_questions")
    assert len(replies) == 1
    assert replies[0].get("stale") is True, "a generic relay error must also mark the reply stale"


# ---------------------------------------------------------------------------
# 3. Relay SUCCEEDS with questions → NO marker, questions passed through.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_success_with_questions_omits_marker():
    owner_questions = [
        {"request_id": "q-1", "tool_use_id": "tool-1", "questions": []},
    ]
    relay = AsyncMock(return_value=owner_questions)

    ws = await _drive(relay)

    replies = _frames_of_type(ws, "pending_questions")
    assert len(replies) == 1
    reply = replies[0]
    assert "stale" not in reply, f"a successful relay must OMIT the stale marker: {reply}"
    assert reply.get("questions") == owner_questions


# ---------------------------------------------------------------------------
# 4. Relay SUCCEEDS with ZERO questions → NO marker (authoritative empty must
#    still clear stale modals on the frontend).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_success_empty_omits_marker():
    relay = AsyncMock(return_value=[])

    ws = await _drive(relay)

    replies = _frames_of_type(ws, "pending_questions")
    assert len(replies) == 1
    reply = replies[0]
    # An authoritative empty list is the deliberate stale-modal-clearing path — it
    # must NOT carry the marker.
    assert "stale" not in reply, f"an authoritative empty reply must OMIT the marker: {reply}"
    assert reply.get("questions") == []
