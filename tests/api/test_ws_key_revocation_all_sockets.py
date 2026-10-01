"""A revoked API key loses every socket it opened, not only ``/ws/chat``.

``/ws/chat`` has its own watchdog (test_chat_ws_revoked_api_key.py). The
session and global sync sockets, the browser panel, voice audio and streaming
transcription wrap their serving loop in ``app.api.ws_key_watch``: the key's
admin routes signal them, a periodic re-check catches the rest, and a revoked
key's socket closes with 4001 (voice and transcription also drop the upstream
connection the key was paying for).
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api import ws_key_watch

KEY_ID = "api-1"


def _principal(kind="api_user", principal_id=KEY_ID):
    return {"kind": kind, "principal_id": principal_id, "credential_hash": "h",
            "account_id": None, "owner": f"{kind}:{principal_id}"}


@pytest.fixture
def keys(monkeypatch):
    """Authenticate every socket as KEY_ID; ``revoked`` makes its re-check fail."""
    revoked: set = set()

    def validate(value):
        if value and value.get("kind") == "api_user" and value["principal_id"] in revoked:
            raise PermissionError("API key disabled, deleted or rotated")

    monkeypatch.setattr("app.core.mobile_tool_identity.validate", validate)

    async def inline(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(ws_key_watch, "run_sync", inline)

    async def authenticate(websocket, token):
        websocket.state.mobile_tool_principal = _principal()
        return True, {"id": KEY_ID}

    for target in ("app.api.ws_auth.authenticate_websocket",
                   "app.api.ws_sync.authenticate_websocket",
                   "app.api.browser_panel.authenticate_websocket",
                   "app.api.stt_realtime.authenticate_websocket"):
        monkeypatch.setattr(target, authenticate)
    ws_key_watch._watches.clear()
    yield SimpleNamespace(revoked=revoked, monkeypatch=monkeypatch)
    ws_key_watch._watches.clear()


def _await_watch(count=1, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(ws_key_watch._watches.get(KEY_ID, ())) >= count:
            return
        time.sleep(0.01)
    raise AssertionError("the socket never registered with ws_key_watch")


def _revoke_and_expect_4001(env, ws, *, signal=True):
    env.revoked.add(KEY_ID)
    if signal:
        assert ws_key_watch.recheck(KEY_ID) == 1
    with pytest.raises(WebSocketDisconnect) as closed:
        for _ in range(100):
            ws.receive_json()
    assert closed.value.code == 4001


# ---------------------------------------------------------------------------
# /ws/sessions/{id} and /ws/global
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_client(keys):
    from app.api import ws_sync

    sync = MagicMock(name="sync_engine")
    sync.register_device = AsyncMock(return_value=SimpleNamespace(last_activity=None))
    sync.unregister_device = AsyncMock()
    sync.get_session_state = AsyncMock(return_value={})
    sync.get_device_count = MagicMock(return_value=0)
    keys.monkeypatch.setattr(ws_sync, "sync_engine", sync)
    keys.monkeypatch.setattr(ws_sync, "database", SimpleNamespace(
        get_session=lambda sid: {"id": sid}, get_session_messages=lambda sid: []))
    keys.monkeypatch.setattr(ws_sync, "check_session_access_for_api_user", lambda user, row: None)

    async def inline(fn, *a, **k):
        return fn(*a, **k)

    keys.monkeypatch.setattr(ws_sync, "run_sync", inline)
    app = FastAPI()
    app.include_router(ws_sync.router)
    with TestClient(app) as client:
        yield client


def test_session_sync_socket_closes_when_the_key_is_revoked(keys, sync_client):
    with sync_client.websocket_connect("/ws/sessions/s1?device_id=d&token=k") as ws:
        assert ws.receive_json()["event_type"] == "state"
        _await_watch()
        _revoke_and_expect_4001(keys, ws)
    assert not ws_key_watch._watches.get(KEY_ID)


def test_global_socket_closes_when_the_key_is_revoked(keys, sync_client):
    with sync_client.websocket_connect("/ws/global?device_id=d&token=k") as ws:
        assert ws.receive_json()["type"] == "sync_ready"
        _await_watch()
        _revoke_and_expect_4001(keys, ws)
    assert not ws_key_watch._watches.get(KEY_ID)


def test_idle_socket_closes_at_the_periodic_recheck(keys, sync_client):
    keys.monkeypatch.setattr(ws_key_watch, "RECHECK_SECONDS", 0.05)
    with sync_client.websocket_connect("/ws/global?device_id=d&token=k") as ws:
        assert ws.receive_json()["type"] == "sync_ready"
        _await_watch()
        time.sleep(0.2)  # several re-checks of a live key close nothing
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        _revoke_and_expect_4001(keys, ws, signal=False)


def test_signal_keeps_a_socket_whose_key_is_live(keys, sync_client):
    with sync_client.websocket_connect("/ws/global?device_id=d&token=k") as ws:
        assert ws.receive_json()["type"] == "sync_ready"
        _await_watch()
        assert ws_key_watch.recheck(KEY_ID) == 1
        time.sleep(0.1)
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_owner_socket_is_not_watched(keys, sync_client, monkeypatch):
    async def owner(websocket, token):
        websocket.state.mobile_tool_principal = _principal("admin", "install")
        return True, None

    monkeypatch.setattr("app.api.ws_sync.authenticate_websocket", owner)
    monkeypatch.setattr("app.core.preference_identity.principal_preference_identity",
                        lambda p: ("admin", "owner"))
    with sync_client.websocket_connect("/ws/global?device_id=d&token=k") as ws:
        assert ws.receive_json()["type"] == "sync_ready"
        time.sleep(0.05)
        assert ws_key_watch._watches == {}


# ---------------------------------------------------------------------------
# Browser panel
# ---------------------------------------------------------------------------

def test_browser_panel_socket_closes_when_the_key_is_revoked(keys):
    from app.api import browser_panel

    m = keys.monkeypatch
    m.setattr(browser_panel, "database", SimpleNamespace(get_session=lambda sid: {"id": sid}))
    m.setattr(browser_panel, "check_session_access_for_api_user", lambda user, row: None)
    m.setattr(browser_panel.control, "owner_of", lambda row: None)

    async def serve(websocket, session_id):
        await websocket.send_json({"type": "hello"})
        while True:
            try:
                await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return

    m.setattr(browser_panel, "serve_browser_panel", serve)
    app = FastAPI()
    app.include_router(browser_panel.router)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws/browser/s1?token=k") as ws:
            assert ws.receive_json()["type"] == "hello"
            _await_watch()
            _revoke_and_expect_4001(keys, ws)


# ---------------------------------------------------------------------------
# Voice audio: the provider connection goes too
# ---------------------------------------------------------------------------

def test_voice_audio_socket_and_its_upstream_close_when_the_key_is_revoked(keys):
    from app.api import voice
    from app.core import voice_stream

    m = keys.monkeypatch
    m.setattr(voice, "_require_voice_enabled", AsyncMock(return_value={"voice_max_call_minutes": 5}))
    m.setattr(voice, "_load_voice_session", AsyncMock(side_effect=lambda sid, **kw: {"id": sid}))
    m.setattr(voice, "_check_voice_session_access", lambda api_user, row: None)
    m.setattr(voice, "_reject_when_capped", lambda sid: None)
    m.setattr(voice, "session_config", lambda row, settings: {"model": "m", "provider_id": "p"})
    m.setattr(voice, "transport_for", lambda model: "stream")
    m.setattr(voice, "_resolve_provider_row", lambda *a: {})
    m.setattr(voice, "_provider_api_key", AsyncMock(return_value="provider-key"))
    upstream_closed = threading.Event()

    class FakeCall:
        def __init__(self, websocket, row, config, key, minutes):
            self.websocket = websocket
            self.done = asyncio.Event()

        async def run(self):
            await self.websocket.send_json({"type": "ready"})
            await self.done.wait()

        async def close(self):
            upstream_closed.set()
            self.done.set()

    m.setattr(voice_stream, "VoiceStream", FakeCall)
    m.setattr(voice_stream, "calls", {})
    app = FastAPI()
    app.include_router(voice.router)
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/voice/sessions/s1/audio?token=k") as ws:
            assert ws.receive_json()["type"] == "ready"
            _await_watch()
            _revoke_and_expect_4001(keys, ws)
            assert upstream_closed.wait(2.0)


# ---------------------------------------------------------------------------
# Streaming transcription (the owner's OpenAI key pays for it)
# ---------------------------------------------------------------------------

def test_transcription_socket_and_its_upstream_close_when_the_key_is_revoked(keys):
    import websockets.asyncio.client as ws_client

    from app.api import settings_audio, stt_realtime
    from tests.api.test_stt_realtime import _FakeUpstream

    stop = object()

    class Upstream(_FakeUpstream):
        closed = False

        async def close(self):
            Upstream.closed = True
            await self._queue.put(stop)

        async def __anext__(self):
            item = await self._queue.get()
            if item is stop:
                raise StopAsyncIteration
            return item

    m = keys.monkeypatch
    m.setattr(stt_realtime, "_resolve_capability", lambda: {
        "supported": True, "model": "openai/gpt-4o-transcribe", "model_name": "GPT-4o",
        "partials": True, "reason": None})
    m.setattr(settings_audio, "_resolve_openai_api_key", lambda: "sk-owner")

    async def connect(url, **kwargs):
        return Upstream()

    m.setattr(ws_client, "connect", connect)
    app = FastAPI()
    app.include_router(stt_realtime.stt_stream_router, prefix="/api/v1/settings")
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/settings/transcribe/stream?token=k") as ws:
            assert ws.receive_json()["type"] == "ready"
            _await_watch()
            _revoke_and_expect_4001(keys, ws)
    assert Upstream.closed


# ---------------------------------------------------------------------------
# The key's admin routes signal every watched socket
# ---------------------------------------------------------------------------

def test_key_routes_signal_the_watched_sockets(monkeypatch):
    from app.api import api_users, chat_ws_handler

    signalled = []
    monkeypatch.setattr(ws_key_watch, "recheck", lambda uid: signalled.append(("watch", uid)))
    monkeypatch.setattr(chat_ws_handler, "recheck_api_user_sockets",
                        lambda uid: signalled.append(("chat", uid)))
    api_users._close_key_sockets(KEY_ID)
    assert sorted(signalled) == [("chat", KEY_ID), ("watch", KEY_ID)]
