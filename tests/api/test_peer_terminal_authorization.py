"""Recipient-local approval is exercised through real owner HTTP authentication."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import owner_auth
from app.core.config import settings
from app.network import control, engine, terminal_relay as relay
from tests.api.test_terminals_api import ctx, client, owner, MEMBER_PAIRED


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
