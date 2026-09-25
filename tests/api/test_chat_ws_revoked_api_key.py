"""An API key disabled, deleted or rotated while its chat WebSocket is open loses the socket.

Authentication runs at connect; the handler re-validates an API-user principal
on every inbound frame, every PRINCIPAL_RECHECK_SECONDS without client traffic and at once
when the key's admin routes signal it; it closes with 4001 and
never acts on the frame that arrived after the key changed.
"""
import asyncio
import hashlib

import pytest

import app.api.websocket as ws_api
from app.db import database
from app.db import turns as turns_db
from app.db.base import get_db
from tests.api import test_chat_ws_dispatch_contract as contract
from tests.api.test_chat_ws_dispatch_contract import _Wire, _own_session

ws_env = contract.ws_env  # the chat-socket boundary fixture, shared

KEY = "sk-live-revocation"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class _RecordingWire(_Wire):
    def __init__(self):
        super().__init__()
        self.close_reason = None

    async def _send(self, message):
        if message["type"] == "websocket.close":
            self.close_reason = message.get("reason")
        await super()._send(message)


def _disable():
    database.update_api_user("api-1", is_active=False)


def _rotate():
    with get_db() as conn:
        conn.cursor().execute("UPDATE api_users SET api_key_hash = %s WHERE id = %s", (_hash("sk-rotated"), "api-1"))


def _delete():
    database.delete_api_user("api-1")


async def _open(wire):
    # The REAL authenticate_websocket resolves the key and sets the principal.
    task = asyncio.create_task(ws_api.chat_websocket(wire.ws, token=KEY, device_id="dev-rev"))
    for _ in range(300):
        if wire.sent:
            return task
        assert not task.done(), f"socket closed at connect (code={wire.close_code})"
        await asyncio.sleep(0.01)
    raise AssertionError("no connect snapshot")


async def _close(wire, task):
    if not task.done():
        wire.inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 3.0)


@pytest.mark.parametrize("revoke", [_disable, _rotate, _delete], ids=["disabled", "rotated", "deleted"])
async def test_revoked_key_closes_its_socket_and_rejects_the_queued_message(ws_env, revoke):
    database.create_api_user("api-1", "Key", _hash(KEY))
    sid = _own_session("sess-rev")
    wire = _RecordingWire()
    task = await _open(wire)
    try:
        revoke()
        assert database.get_api_user_by_key_hash(_hash(KEY)) is None
        wire.push({"type": "queue_message", "session_id": sid, "content": "after revoke"})
        await asyncio.wait_for(task, 3.0)
    finally:
        await _close(wire, task)
    assert wire.close_code == 4001
    assert "API key" in (wire.close_reason or "")
    assert turns_db.get_queued_turns(sid) == []


async def test_live_key_keeps_its_socket(ws_env):
    database.create_api_user("api-1", "Key", _hash(KEY))
    sid = _own_session("sess-live")
    wire = _RecordingWire()
    task = await _open(wire)
    try:
        wire.push({"type": "pong"})
        wire.push({"type": "queue_message", "session_id": sid, "content": "still live"})
        for _ in range(300):
            if turns_db.get_queued_turns(sid):
                break
            await asyncio.sleep(0.01)
        assert not task.done()
    finally:
        await _close(wire, task)
    assert [t["prompt"] for t in turns_db.get_queued_turns(sid)] == ["still live"]
    assert wire.close_code in (None, 1000)


# ---------------------------------------------------------------------------
# A client that sends nothing: the server re-checks on its own (R9-10)
# ---------------------------------------------------------------------------

from app.api import api_users as api_users_routes  # noqa: E402
from app.api import chat_ws_handler as cwh  # noqa: E402
from app.core.models import ApiUserUpdate  # noqa: E402


async def _wait_done(task, timeout):
    await asyncio.wait_for(asyncio.shield(task), timeout)


@pytest.mark.parametrize("revoke", [_disable, _rotate, _delete], ids=["disabled", "rotated", "deleted"])
async def test_idle_socket_closes_within_the_recheck_interval(ws_env, revoke):
    ws_env.monkeypatch.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 0.05)
    database.create_api_user("api-1", "Key", _hash(KEY))
    wire = _RecordingWire()
    task = await _open(wire)
    try:
        # A live key survives several re-checks with no client traffic.
        await asyncio.sleep(0.3)
        assert not task.done()
        revoke()  # straight in the database: no route signals the socket
        await _wait_done(task, 2.0)
    finally:
        await _close(wire, task)
    assert wire.close_code == 4001
    assert "API key" in (wire.close_reason or "")
    assert not cwh._api_user_sockets.get("api-1")


async def _route_disable():
    await api_users_routes.update_api_user_endpoint("api-1", ApiUserUpdate(is_active=False), token="t", request=None)


async def _route_rotate():
    await api_users_routes.rotate_api_user_key("api-1", token="t", request=None)


async def _route_delete():
    await api_users_routes.delete_api_user_endpoint("api-1", token="t", request=None)


@pytest.mark.parametrize("route", [_route_disable, _route_rotate, _route_delete],
                         ids=["disable", "rotate", "delete"])
async def test_admin_route_closes_an_idle_socket_immediately(ws_env, route):
    # The periodic re-check is far away: only the route's signal can close it.
    ws_env.monkeypatch.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 3600.0)

    async def _run_sync(fn, *a, **k):
        return fn(*a, **k)

    ws_env.monkeypatch.setattr(api_users_routes, "run_sync", _run_sync)
    database.create_api_user("api-1", "Key", _hash(KEY))
    wire = _RecordingWire()
    task = await _open(wire)
    try:
        await asyncio.sleep(0.05)
        assert not task.done()
        await route()
        await _wait_done(task, 1.0)
    finally:
        await _close(wire, task)
    assert wire.close_code == 4001


async def test_route_signal_keeps_a_socket_whose_key_is_still_live(ws_env):
    ws_env.monkeypatch.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 3600.0)
    database.create_api_user("api-1", "Key", _hash(KEY))
    wire = _RecordingWire()
    task = await _open(wire)
    try:
        assert cwh.recheck_api_user_sockets("api-1") == 1
        await asyncio.sleep(0.2)
        assert not task.done()
    finally:
        await _close(wire, task)
    assert wire.close_code in (None, 1000)
    assert not cwh._api_user_sockets.get("api-1")
