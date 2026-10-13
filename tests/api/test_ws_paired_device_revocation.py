"""An unpaired device loses every socket it holds, like a revoked API key.

A paired device (principal ``paired:<id>``: the Android app, the mobile shell)
authenticates its sockets at connect only. ``/ws/chat`` and every
``ws_key_watch`` socket re-validate it every PRINCIPAL_RECHECK_SECONDS /
RECHECK_SECONDS and at once when the unpair routes signal them; an unpaired
device's sockets close with 4001, while the owner's and a still-paired
device's stay open. Authentication and validation are real (paired-client rows
in the test database).
"""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.websocket as ws_api
from app.api import chat_ws_handler as cwh
from app.api import pairing as pairing_routes
from app.api import ws_key_watch, ws_sync
from app.core import mobile_tool_identity as identity
from app.db import database
from app.db.base import get_db
from tests.api import test_chat_ws_dispatch_contract as contract
from tests.api.test_chat_ws_dispatch_contract import _Wire

ws_env = contract.ws_env  # the chat-socket boundary fixture, shared

TOKEN_A = "shc_device-a-session-token"
TOKEN_B = "shc_device-b-session-token"
OWNER = "owner-token"


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _pair(client_id, token):
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.cursor().execute(
            """
            INSERT INTO paired_clients
                (id, device_id, device_name, platform, client_type, scopes,
                 session_token_hash, session_expires_at, paired_at, last_seen,
                 revoked_at, created_at, updated_at)
            VALUES (%s, %s, %s, 'web', 'browser', '["controller"]', %s, %s, %s, %s, NULL, %s, %s)
            """,
            (client_id, "dev-" + client_id, client_id, _hash(token),
             (now + timedelta(days=1)).isoformat(), now.isoformat(), now.isoformat(),
             now.isoformat(), now.isoformat()),
        )


class _RecordingWire(_Wire):
    def __init__(self):
        super().__init__()
        self.close_reason = None

    async def _send(self, message):
        if message["type"] == "websocket.close":
            self.close_reason = message.get("reason")
        await super()._send(message)


@pytest.fixture
def paired(ws_env):
    """Device A (to be unpaired) and device B (stays paired), real auth; OWNER is the owner."""
    m = ws_env.monkeypatch

    async def _inline(fn, *a, **k):
        return fn(*a, **k)

    for module in (ws_key_watch, ws_sync, pairing_routes):
        m.setattr(module, "run_sync", _inline)
    real_auth = ws_api.authenticate_websocket

    async def authenticate(websocket, token):
        if token == OWNER:
            websocket.state.mobile_tool_principal = identity.principal("admin", "install", OWNER)
            return True, None
        return await real_auth(websocket, token)

    m.setattr(ws_api, "authenticate_websocket", authenticate)
    m.setattr(ws_sync, "authenticate_websocket", authenticate)
    sync = MagicMock(name="sync_engine")
    sync.register_device = AsyncMock(return_value=SimpleNamespace(last_activity=None))
    sync.unregister_device = AsyncMock()
    sync.get_session_state = AsyncMock(return_value={})
    sync.get_device_count = MagicMock(return_value=0)
    m.setattr(ws_sync, "sync_engine", sync)
    m.setattr(ws_sync, "database", SimpleNamespace(
        get_session=lambda sid: {"id": sid}, get_session_messages=lambda sid: []))
    # Revocation is what these tests pin, on a stub chat with no account
    # record; the transcript rule is tests/api/test_session_transcript_account_rule.py.
    m.setattr(ws_sync, "socket_may_read_transcript", lambda principal, session: True)
    _pair("pcl_a", TOKEN_A)
    _pair("pcl_b", TOKEN_B)
    ws_key_watch._watches.clear()
    cwh._api_user_sockets.clear()
    yield ws_env
    ws_key_watch._watches.clear()
    cwh._api_user_sockets.clear()


async def _wait_for(predicate, what, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


async def _open_chat(token, device_id):
    wire = _RecordingWire()
    task = asyncio.create_task(ws_api.chat_websocket(wire.ws, token=token, device_id=device_id))
    await _wait_for(lambda: wire.sent or task.done(), "no chat connect snapshot")
    assert not task.done(), f"chat socket closed at connect (code={wire.close_code})"
    return wire, task


async def _open_sync(token):
    wire = _RecordingWire()
    task = asyncio.create_task(
        ws_sync.session_sync_websocket(wire.ws, "s1", device_id="sync-" + token[:8], token=token))
    await _wait_for(lambda: any(f.get("event_type") == "state" for f in wire.sent) or task.done(),
                    "no sync state snapshot")
    assert not task.done(), f"sync socket closed at connect (code={wire.close_code})"
    return wire, task


async def _hang_up(wire, task):
    if not task.done():
        wire.inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, 3.0)


async def _open_all():
    """Device A's chat and sync sockets, device B's chat socket, the owner's chat socket."""
    sockets = {
        "a_chat": await _open_chat(TOKEN_A, "dev-a-chat"),
        "a_sync": await _open_sync(TOKEN_A),
        "b_chat": await _open_chat(TOKEN_B, "dev-b-chat"),
        "owner_chat": await _open_chat(OWNER, "dev-owner"),
    }
    await _wait_for(lambda: len(ws_key_watch._watches.get("paired:pcl_a", ())) == 1,
                    "device A's sync socket never registered with ws_key_watch")
    assert len(cwh._api_user_sockets.get("paired:pcl_a", ())) == 1
    assert len(cwh._api_user_sockets.get("paired:pcl_b", ())) == 1
    return sockets


async def _expect_a_closed_others_open(sockets, timeout):
    for name in ("a_chat", "a_sync"):
        wire, task = sockets[name]
        await _wait_for(lambda: wire.close_code is not None, f"{name} never closed", timeout)
        assert wire.close_code == 4001, name
        # The client answers the close frame; the serving loop then ends.
        wire.inbound.put_nowait({"type": "websocket.disconnect", "code": 4001})
        await asyncio.wait_for(asyncio.shield(task), 1.0)
        assert "revoked" in (wire.close_reason or ""), name
    await asyncio.sleep(0.2)  # several more re-checks of the live principals
    for name in ("b_chat", "owner_chat"):
        wire, task = sockets[name]
        assert not task.done(), f"{name} closed (code={wire.close_code})"
    assert not cwh._api_user_sockets.get("paired:pcl_a")
    assert not ws_key_watch._watches.get("paired:pcl_a")


async def test_unpaired_in_the_db_closes_the_device_sockets_within_the_recheck(paired):
    paired.monkeypatch.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 0.05)
    paired.monkeypatch.setattr(ws_key_watch, "RECHECK_SECONDS", 0.05)
    sockets = await _open_all()
    try:
        await asyncio.sleep(0.3)  # a live device survives several re-checks
        assert not any(task.done() for _, task in sockets.values())
        assert database.revoke_paired_client("pcl_a")  # no route: nothing signals
        await _expect_a_closed_others_open(sockets, 2.0)
    finally:
        for wire, task in sockets.values():
            await _hang_up(wire, task)


async def test_unpair_route_closes_the_device_sockets_immediately(paired):
    # The periodic re-check is far away: only the route's signal can close them.
    paired.monkeypatch.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 3600.0)
    paired.monkeypatch.setattr(ws_key_watch, "RECHECK_SECONDS", 3600.0)
    sockets = await _open_all()
    try:
        response = await pairing_routes.revoke_client("pcl_a", _="t")
        assert response.status_code == 204
        await _expect_a_closed_others_open(sockets, 1.0)
    finally:
        for wire, task in sockets.values():
            await _hang_up(wire, task)


async def test_a_replaced_session_token_closes_the_old_socket(paired):
    paired.monkeypatch.setattr(ws_key_watch, "RECHECK_SECONDS", 3600.0)
    paired.monkeypatch.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 3600.0)
    wire, task = await _open_chat(TOKEN_A, "dev-a-chat")
    try:
        with get_db() as conn:
            conn.cursor().execute("UPDATE paired_clients SET session_token_hash = %s WHERE id = %s",
                                  (_hash("shc_rotated"), "pcl_a"))
        assert ws_key_watch.recheck_paired_client("pcl_a") == 0
        assert cwh.recheck_paired_client_sockets("pcl_a") == 1
        await asyncio.wait_for(asyncio.shield(task), 1.0)
    finally:
        await _hang_up(wire, task)
    assert wire.close_code == 4001


async def test_signal_keeps_a_device_that_is_still_paired(paired):
    paired.monkeypatch.setattr(cwh, "PRINCIPAL_RECHECK_SECONDS", 3600.0)
    wire, task = await _open_chat(TOKEN_B, "dev-b-chat")
    try:
        ws_key_watch.close_paired_client_sockets("pcl_b")
        await asyncio.sleep(0.2)
        assert not task.done()
    finally:
        await _hang_up(wire, task)
    assert wire.close_code in (None, 1000)


def test_revalidate_maps_a_live_refusal_but_not_an_outage(monkeypatch):
    from fastapi import HTTPException

    principal = {"kind": "paired", "principal_id": "pcl_x", "credential_hash": "h", "account_id": None}
    for status, expected in ((401, PermissionError), (403, PermissionError), (503, HTTPException)):
        def refuse(value, status=status):
            raise HTTPException(status_code=status, detail="Mobile authorization revoked")

        monkeypatch.setattr(identity, "validate", refuse)
        with pytest.raises(expected):
            identity.revalidate(principal)


def test_watch_key_covers_api_keys_and_paired_devices_only():
    assert ws_key_watch.watch_key({"kind": "api_user", "principal_id": "k"}) == "k"
    assert ws_key_watch.watch_key({"kind": "paired", "principal_id": "p"}) == "paired:p"
    assert ws_key_watch.watch_key({"kind": "admin", "principal_id": "install"}) is None
    assert ws_key_watch.watch_key(None) is None


async def test_mobile_device_delete_route_signals_the_paired_device(monkeypatch):
    from app.api import mobile

    async def _inline(fn, *a, **k):
        return fn(*a, **k)

    signalled = []
    monkeypatch.setattr(mobile, "run_sync", _inline)
    monkeypatch.setattr(mobile, "db", SimpleNamespace(
        get_mobile_device=lambda did: {"device_id": did, "paired_client_id": "pcl_m"},
        revoke_paired_client=lambda cid: True,
        delete_mobile_device=lambda did: True))
    monkeypatch.setattr(ws_key_watch, "close_paired_client_sockets", signalled.append)
    await mobile.delete_device("mob-1", token="t")
    assert signalled == ["pcl_m"]
