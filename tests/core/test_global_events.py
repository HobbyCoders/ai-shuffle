"""Cross-device invalidation contracts, without a real database or app lifespan."""
import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.global_events import GlobalEvents, global_events
from app.core.sync_engine import SyncEngine
from app.db import settings


class Socket:
    def __init__(self):
        self.sent = []
        self.close = AsyncMock()

    async def send_text(self, text):
        self.sent.append(json.loads(text))


async def flush():
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_global_scopes_multiple_sockets_and_lifecycle():
    hub = GlobalEvents()
    sockets = [Socket() for _ in range(4)]
    ids = [hub.register(ws, kind, identity) for ws, kind, identity in zip(
        sockets, ["admin", "admin", "api_user", "api_user"],
        ["owner", "owner", "alice", "bob"],
    )]
    hub.preference_changed("admin", "owner", "theme")
    hub.preference_changed("api_user", "alice", "theme")
    hub.resources_changed("projects")
    hub.lifecycle("alice", {"type": "user_session_updated", "session_id": "private"}, user_type="api_user")
    await flush()
    assert all(ws.sent[0] == {"type": "sync_ready"} for ws in sockets)
    assert sockets[0].sent == sockets[1].sent
    assert len(sockets[3].sent) == 1
    assert not any(e.get("session_id") == "private" for e in sockets[0].sent)
    assert not any(e.get("type") == "resources_changed" for e in sockets[2].sent)
    await hub.unregister(ids[0])
    await hub.unregister(ids[0])
    assert ids[1] in hub.connections
    for socket_id in ids[1:]:
        await hub.unregister(socket_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["created", "updated", "deleted", "cleared"])
async def test_existing_lifecycle_reaches_global_without_chat(event):
    ws = Socket()
    sid = global_events.register(ws, "api_user", "alice")
    engine = SyncEngine()
    args = ["alice", "session"]
    if event == "updated":
        args.append({"name": "changed"})
    try:
        with patch("app.db.database.get_session", return_value={"api_user_id": "alice"}):
            await getattr(engine, "broadcast_user_session_" + event)(*args)
        await flush()
        assert ws.sent[-1]["type"] == "user_session_" + event
    finally:
        await global_events.unregister(sid)


def test_preference_atomic_initialize_reset_and_scope():
    with patch.object(global_events, "preference_changed") as publish:
        first = settings.set_user_preference("admin", "owner", "theme", {"mode": "dark"}, True)
        existing = settings.set_user_preference("admin", "owner", "theme", {"mode": "light"}, True)
        assert existing == first
        assert publish.call_count == 1
        assert settings.set_user_preference("api_user", "alice", "theme", False, True)["value"] is False
        assert settings.set_user_preference("admin", "owner", "theme", None)["value"] is None
        assert settings.delete_user_preference("admin", "owner", "theme")
        assert not settings.delete_user_preference("admin", "owner", "theme")
        assert settings.set_user_preference("admin", "owner", "theme", "reset", True)["value"] == "reset"
        assert publish.call_count == 5


def test_no_preference_event_on_commit_failure():
    @contextmanager
    def failing_db():
        with original_db() as conn:
            yield conn
            raise RuntimeError("commit failed")

    original_db = settings.get_db
    with patch.object(settings, "get_db", failing_db), patch.object(global_events, "preference_changed") as publish:
        with pytest.raises(RuntimeError, match="commit failed"):
            settings.set_user_preference("admin", "owner", "theme", "dark")
        publish.assert_not_called()


def test_global_cookie_auth_and_optional_token():
    from app.api import ws_sync
    app = FastAPI()
    app.include_router(ws_sync.router, prefix="/api/v1")
    async def authenticate(socket, token):
        socket.state.mobile_tool_principal = {'kind':'api_user', 'principal_id':'alice'}
        return True, {'id':'alice'}
    auth = AsyncMock(side_effect=authenticate)
    with patch.object(ws_sync, "authenticate_websocket", auth):
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/ws/global?device_id=same") as first:
                assert first.receive_json() == {"type": "sync_ready"}
                first.send_json({"type": "ping"})
                assert first.receive_json() == {"type": "pong"}
                with client.websocket_connect("/api/v1/ws/global?device_id=same") as second:
                    assert second.receive_json() == {"type": "sync_ready"}
                    global_events.preference_changed("api_user", "alice", "theme")
                    assert first.receive_json() == second.receive_json() == {"type": "preferences_changed", "keys": ["theme"]}
    assert auth.call_args.args[1] is None
    assert not global_events.connections

@pytest.mark.asyncio
async def test_overflow_disconnects_for_reconnect_recovery():
    hub = GlobalEvents()
    ws = Socket()
    async def hung_send(_):
        await asyncio.Event().wait()
    ws.send_text = hung_send
    socket_id = hub.register(ws, "admin", "owner")
    writer = hub.connections[socket_id][3]
    writer._maxsize = 2
    hub.resources_changed("projects")
    hub.resources_changed("agents")
    hub.resources_changed("notes")
    await flush()
    assert writer.death_reason == "queue_overflow"
    assert ws.close.called
    await hub.unregister(socket_id)


@pytest.mark.asyncio
async def test_worker_bridge_and_device_local_exclusion():
    hub = GlobalEvents()
    ws = Socket()
    socket_id = hub.register(ws, "admin", "owner")
    try:
        await asyncio.to_thread(hub.preference_changed, "admin", "owner", "active_session:phone")
        await asyncio.to_thread(hub.preference_changed, "admin", "owner", "hotkeys")
        await flush()
        assert ws.sent == [{"type": "sync_ready"}, {"type": "preferences_changed", "keys": ["hotkeys"]}]
    finally:
        await hub.unregister(socket_id)


def test_preference_rest_put_and_beacon_initialize():
    from app.api import preferences
    app = FastAPI()
    app.include_router(preferences.router)
    app.dependency_overrides[preferences.require_auth] = lambda: "authenticated"
    with patch.object(preferences, "get_user_identity", return_value=("admin", "owner")):
        with TestClient(app) as client:
            first = client.put("/api/v1/preferences/theme?only_if_missing=true", json={"key": "theme", "value": "dark"})
            second = client.post("/api/v1/preferences/theme?only_if_missing=true", json={"key": "theme", "value": "light"})
            assert first.status_code == second.status_code == 200
            assert first.json() == second.json()
            assert client.post("/api/v1/preferences/theme", json={"key": "theme", "value": "light"}).json()["value"] == "light"



def test_global_auth_failure():
    from app.api import ws_sync
    from starlette.websockets import WebSocketDisconnect
    app = FastAPI()
    app.include_router(ws_sync.router)
    with patch.object(ws_sync, "authenticate_websocket", AsyncMock(return_value=(False, None))):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/global?device_id=x") as ws:
                with pytest.raises(WebSocketDisconnect) as error:
                    ws.receive_json()
                assert error.value.code == 4001


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "admin", "same-name"])
async def test_lifecycle_uses_persisted_role_not_id_spelling(owner):
    engine = SyncEngine()
    sockets = [Socket() for _ in range(5)]
    principals = [("admin", "same-name"), ("admin", "same-name"),
                  ("api_user", "same-name"), ("api_user", "same-name"),
                  ("api_user", "admin")]
    ids = [global_events.register(ws, *principal)
           for ws, principal in zip(sockets, principals)]
    payload = {"title": "private lifecycle content"}
    try:
        with patch("app.db.database.get_session", return_value={"api_user_id": owner}):
            await engine.broadcast_user_session_updated(owner or "admin", "private", payload)
        await flush()
        for ws, (kind, identity) in zip(sockets, principals):
            expected = kind == "admin" if owner is None else kind == "api_user" and identity == owner
            assert any(event.get("updates") == payload for event in ws.sent) == expected
    finally:
        for sid in ids:
            await global_events.unregister(sid)


@pytest.mark.asyncio
async def test_unknown_owner_only_invalidates_and_keeps_legacy_lifecycle():
    engine = SyncEngine()
    legacy = Socket()
    await engine.register_user_device("admin", "device", legacy)
    sockets = [Socket(), Socket()]
    ids = [global_events.register(ws, kind, "admin")
           for ws, kind in zip(sockets, ["admin", "api_user"])]
    try:
        with patch("app.db.database.get_session", return_value=None):
            await engine.broadcast_user_session_deleted("admin", "private-id")
        await flush()
        assert legacy.sent[-1] == {"type": "user_session_deleted", "session_id": "private-id"}
        for ws in sockets:
            assert ws.sent == [{"type": "sync_ready"},
                               {"type": "resources_changed", "resources": ["sessions"]}]
    finally:
        await engine.unregister_user_device("admin", "device", legacy)
        for sid in ids:
            await global_events.unregister(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "api_user"])
async def test_notification_explicit_owner_is_role_scoped(role):
    from app.db.notifications import NotificationOwner
    sockets = [Socket(), Socket()]
    ids = [global_events.register(ws, kind, "same")
           for ws, kind in zip(sockets, ["admin", "api_user"])]
    try:
        await SyncEngine().broadcast_notification(NotificationOwner("same", role), {"message": "private"})
        await flush()
        for ws, kind in zip(sockets, ["admin", "api_user"]):
            assert any(e["type"] == "notification" for e in ws.sent) == (kind == role)
    finally:
        for sid in ids:
            await global_events.unregister(sid)


@pytest.mark.asyncio
async def test_shared_device_tabs_have_independent_user_writers():
    engine = SyncEngine()
    first, second = Socket(), Socket()
    one = await engine.register_user_device("user", "same-device", first)
    two = await engine.register_user_device("user", "same-device", second)
    assert one is await engine.register_user_device("user", "same-device", first)
    assert one.writer.alive and two.writer.alive
    assert engine.get_user_device_count("user") == 2
    await engine.broadcast_tabs_updated("user", {"tabs": []})
    await flush()
    assert first.sent == second.sent == [{"type": "tabs_sync", "tabs": [], "activeTabId": None, "source_device_id": None}]
    await engine.unregister_user_device("user", "same-device")
    assert engine.get_user_device_count("user") == 2
    await engine.unregister_user_device("user", "same-device", first)
    await engine.unregister_user_device("user", "same-device", first)
    assert two.writer.alive and engine.get_user_device_count("user") == 1
    await engine.broadcast_tabs_updated("user", {"tabs": ["new"]})
    await flush()
    assert second.sent[-1]["tabs"] == ["new"]
    await engine.unregister_user_device("user", "same-device", second)
    assert engine.get_user_device_count("user") == 0


def test_paired_admin_global_identity_and_ping():
    from app.api import ws_sync
    from app.core.auth import auth_service
    from app.core.pairing import PAIRED_SESSION_PREFIX, hash_controller_session
    from app.db import pairing
    from datetime import datetime, timedelta, timezone
    from starlette.websockets import WebSocketDisconnect
    app = FastAPI()
    app.include_router(ws_sync.router)
    # Both initial authentication and the live principal check read the actual
    # paired session. Mocking only the facade hid this second authorization read.
    paired = pairing.ensure_paired_client_for_mobile({"device_id": "global-controller", "scopes": ["controller"]})
    token = PAIRED_SESSION_PREFIX + "global-test"
    pairing.set_paired_client_session(paired["id"], hash_controller_session(token),
                                      datetime.now(timezone.utc) + timedelta(minutes=5))
    with patch.object(auth_service, "get_admin_username", return_value="same"):
        with TestClient(app) as client:
            with client.websocket_connect("/ws/global?device_id=paired&token=" + token) as ws:
                assert ws.receive_json() == {"type": "sync_ready"}
                assert any(kind == "paired" and identity == paired['id'] for kind, identity, _, _ in global_events.connections.values())
                global_events.preference_changed("paired", paired['id'], "theme")
                assert ws.receive_json() == {"type": "preferences_changed", "keys": ["theme"]}
                ws.send_json({"type": "ping"})
                assert ws.receive_json() == {"type": "pong"}
            pairing.revoke_paired_client(paired["id"])
            with client.websocket_connect("/ws/global?device_id=paired&token=" + token) as ws:
                with pytest.raises(WebSocketDisconnect) as error:
                    ws.receive_json()
                assert error.value.code == 4001


@pytest.mark.asyncio
async def test_untyped_notification_never_uses_referenced_session_as_recipient():
    sockets = [Socket(), Socket()]
    ids = [global_events.register(ws, kind, "admin")
           for ws, kind in zip(sockets, ["admin", "api_user"])]
    try:
        with patch("app.db.database.get_session") as lookup:
            await SyncEngine().broadcast_notification("admin", {"message": "secret", "session_id": "unrelated"})
        lookup.assert_not_called()
        await flush()
        for ws in sockets:
            assert ws.sent == [{"type": "sync_ready"},
                               {"type": "resources_changed", "resources": ["notifications"]}]
    finally:
        for sid in ids:
            await global_events.unregister(sid)


@pytest.mark.asyncio
async def test_publish_none_role_is_not_a_wildcard():
    hub = GlobalEvents()
    sockets = [Socket(), Socket()]
    ids = [hub.register(ws, kind, "same")
           for ws, kind in zip(sockets, ["admin", "api_user"])]
    try:
        hub.publish({"type": "private", "secret": "value"}, user_type=None, user_id="same")
        await flush()
        assert all(ws.sent == [{"type": "sync_ready"}] for ws in sockets)
    finally:
        for sid in ids:
            await hub.unregister(sid)
