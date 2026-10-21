"""Recipient-local approval is exercised through real owner HTTP authentication."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import owner_auth
from app.core.config import settings
from app.network import control, engine, terminal_relay as relay
from tests.api.test_terminals_api import ctx, client, owner, MEMBER_PAIRED, wsc, ws_url, attach, read_until, until


@pytest.fixture
def peer(ctx, monkeypatch):
    monkeypatch.setattr(settings, "network_peer_terminals", True)
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(member=lambda device:
        SimpleNamespace(kind="worker", status="active") if device == "source" else None))
    # Deliberately malicious source: its own assertion must never grant access.
    monkeypatch.setattr(control, "call", AsyncMock(return_value={"authorized": True}))
    secure = control.inbound_https.set(True)
    yield ctx
    relay._approvals.clear()
    relay._owners.clear()
    for entry in relay._sessions.values():
        entry[3].cancel()
    relay._sessions.clear()
    control.inbound_https.reset(secure)


def test_source_http_contract_opens_real_peer_pty_and_existing_websocket(peer, monkeypatch, caplog):
    from app.api import terminals as api
    from app.core import terminals as service
    from app.network import runners

    caplog.set_level("INFO", logger="app.network.terminal_relay")
    source = service.TerminalRegistry()
    monkeypatch.setattr(api, "registry", source)
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(member=lambda device:
        SimpleNamespace(kind="worker", status="active") if device in {"source", "destination"} else None))
    monkeypatch.setattr(runners, "directory", lambda: {"runners": [{
        "id": "destination", "device_name": "Destination", "is_self": False,
        "online": True, "capabilities": {"terminal": 2}, "endpoints": ["https://peer.example"],
    }]})

    async def transport(device, action, body, **options):
        assert options["secure"] is True
        secure = control.inbound_https.set(True)
        try:
            if action == "terminal-owner":
                assert device == "source"
                return await relay.handle_owner(body, "destination")
            assert device == "destination" and action == "terminal"
            return await relay.handle(body, "source")
        finally:
            control.inbound_https.reset(secure)

    monkeypatch.setattr(control, "call", transport)
    tc = owner(peer)
    authorization = tc.post("/api/v1/terminals/peer-authorizations", json={"worker_id": "destination"})
    assert authorization.status_code == 201, authorization.text
    data = authorization.json()
    assert data["status"] == "pending" and data["worker_id"] == "destination"
    request_id, authorization_id = data["request_id"], data["authorization_id"]
    assert tc.post(f"/api/v1/terminals/peer-requests/{request_id}/approve").status_code == 200
    assert tc.get(f"/api/v1/terminals/peer-authorizations/{authorization_id}").json()["status"] == "approved"
    target = next(t for t in tc.get("/api/v1/terminals/targets").json()["targets"]
                  if t.get("worker_id") == "destination")
    assert target["available"] and target["authorization_id"] == authorization_id
    opened = tc.post("/api/v1/terminals", json={"target": {
        "kind": "worker", "worker_id": "destination", "authorization_id": authorization_id}})
    assert opened.status_code == 201, opened.text
    terminal = opened.json()["terminal"]
    with wsc(tc, ws_url(terminal["id"])) as ws:
        attach(ws)
        command = ("Write-Output ('PEER_' + 'OK')\r" if terminal["shell"] in {"pwsh", "powershell"}
                   else "printf 'PEER_%s' OK\n")
        ws.send_json({"type": "input", "data": command})
        read_until(ws, b"PEER_OK")
        ws.send_json({"type": "resize", "cols": 100, "rows": 40})
        assert until(lambda: peer.reg.list()[0].cols == 100)
    assert tc.delete(f"/api/v1/terminals/{terminal['id']}").status_code == 200
    assert source.count == peer.reg.count == 0
    assert not relay._owners
    audit = "\n".join(r.getMessage() for r in caplog.records if r.name == "app.network.terminal_relay")
    for event in ("requested", "approved", "opened", "closed"):
        assert "peer terminal " + event in audit
    assert authorization_id not in audit and peer.owner not in audit and "PEER_OK" not in audit


@pytest.mark.asyncio
async def test_recipient_requires_own_owner_approval_and_rechecks_revocation(peer):
    lease = "source-chosen-lease-1234567890"
    pending = await relay.handle({"op": "request", "lease": lease}, "source")
    assert pending["status"] == "pending"
    with pytest.raises(PermissionError, match="recipient"):
        await relay.handle({"op": "open", "lease": lease}, "source")
    request_id = pending["request_id"]
    url = f"/api/v1/terminals/peer-requests/{request_id}/approve"
    for tc in (client(peer), client(peer, bearer="agent-api-key"), client(peer, bearer=MEMBER_PAIRED)):
        assert tc.post(url).status_code in (401, 403)
    assert owner(peer).post(url).json()["status"] == "approved"
    opened = await relay.handle({"op": "open", "lease": lease, "options": {}}, "source")
    assert "terminal" in opened
    terminal_id = opened["terminal"]["id"]
    assert peer.reg.get(terminal_id)
    owner_auth.revoke_local_session(peer.owner)
    with pytest.raises(PermissionError, match="recipient"):
        await relay.handle({"op": "write", "lease": lease, "data": "ZWNobyBubw=="}, "source")
    assert peer.reg.count == 0
    with pytest.raises(PermissionError):
        await relay.handle({"op": "open", "lease": lease}, "source")


@pytest.mark.asyncio
async def test_recipient_grant_is_single_use_target_bound_and_revocable(peer):
    lease = "single-use-lease-1234567890"
    pending = await relay.handle({"op": "request", "lease": lease}, "source")
    tc = owner(peer)
    assert tc.post(f"/api/v1/terminals/peer-requests/{pending['request_id']}/approve").status_code == 200
    with pytest.raises(PermissionError):
        await relay.handle({"op": "open", "lease": lease}, "outsider")
    await relay.handle({"op": "open", "lease": lease}, "source")
    assert (await relay.handle({"op": "close", "lease": lease}, "source"))["closed"]
    with pytest.raises(PermissionError):
        await relay.handle({"op": "open", "lease": lease}, "source")


@pytest.mark.asyncio
async def test_recipient_expiry_and_explicit_revoke_close_terminal(peer, monkeypatch):
    lease = "bounded-lease-123456789012345"
    pending = await relay.handle({"op": "request", "lease": lease}, "source")
    tc = owner(peer)
    request_id = pending["request_id"]
    assert tc.post(f"/api/v1/terminals/peer-requests/{request_id}/approve").status_code == 200
    await relay.handle({"op": "open", "lease": lease}, "source")
    assert tc.delete(f"/api/v1/terminals/peer-requests/{request_id}").status_code == 200
    assert peer.reg.count == 0
    with pytest.raises(PermissionError):
        await relay.handle({"op": "resize", "lease": lease, "cols": 120, "rows": 40}, "source")


@pytest.mark.asyncio
async def test_pending_expiry_and_disabled_probe(peer):
    lease = "pending-lease-123456789012345"
    pending = await relay.handle({"op": "request", "lease": lease}, "source")
    relay._approvals[("source", lease)]["expires"] = 0
    response = owner(peer).post(f"/api/v1/terminals/peer-requests/{pending['request_id']}/approve")
    assert response.status_code == 403
    settings.network_peer_terminals = False
    assert (await relay.handle({"op": "probe"}, "source"))["reason_code"] == "peer_terminals_disabled"


@pytest.mark.asyncio
async def test_replaced_approval_during_spawn_cannot_authorize_old_open(peer, monkeypatch):
    lease = "replace-during-spawn-123456789"
    pending = await relay.handle({"op": "request", "lease": lease}, "source")
    tc = owner(peer)
    assert tc.post(f"/api/v1/terminals/peer-requests/{pending['request_id']}/approve").status_code == 200
    create = peer.reg.create

    async def replace_approval(**options):
        terminal = await create(**options)
        await relay.revoke_request(pending["request_id"])
        replacement = await relay.handle({"op": "request", "lease": lease}, "source")
        assert tc.post(f"/api/v1/terminals/peer-requests/{replacement['request_id']}/approve").status_code == 200
        return terminal

    monkeypatch.setattr(peer.reg, "create", replace_approval)
    with pytest.raises(PermissionError, match="replaced"):
        await relay.handle({"op": "open", "lease": lease}, "source")
    assert peer.reg.count == 0
