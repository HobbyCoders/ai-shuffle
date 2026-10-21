"""Review regressions for peer consent, bounded discovery and default-off compatibility."""
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api import terminals as api
from app.core import terminal_targets as targets, terminals as service
from app.network import control, engine, terminal_relay as relay, transport
from tests.core.test_terminal_targets import peers, ssh
from tests.api.test_peer_terminal_authorization import peer, ctx, owner


@pytest.mark.asyncio
async def test_disabled_feature_preserves_no_peer_choices(peers, monkeypatch):
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: False)
    assert await targets.targets(None, "owner") == []


@pytest.mark.asyncio
async def test_ssh_create_never_discovers_peers(ssh, monkeypatch):
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: True)
    monkeypatch.setattr(targets.runners, "directory", Mock(side_effect=AssertionError("SSH must not discover peers")))
    registry = service.TerminalRegistry()
    terminal = await targets.create(registry, api.CreateTerminal(session_id="chat",
        target=api.TargetRef(kind="ssh", grant_id="grant")), "owner", None)
    await registry.close(terminal.id, by="test")


@pytest.mark.asyncio
async def test_pending_request_requires_source_owner_callback(peers, monkeypatch):
    monkeypatch.setattr(control, "call", AsyncMock(return_value={"authorized": False}))
    with pytest.raises(PermissionError, match="Source"):
        await relay.handle({"op": "request", "lease": "unrequested-lease-1234567890"}, "source")
    assert not relay._approvals


@pytest.mark.asyncio
async def test_confirmation_identifies_same_request_on_both_workers(peers, monkeypatch):
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(member=lambda device:
        SimpleNamespace(kind="worker", status="active", name="Source laptop")))
    lease = relay.authorize(SimpleNamespace(valid=True), "owner", "peer")
    pending = await relay.handle({"op": "request", "lease": lease}, "source")
    assert re.fullmatch(r"[A-Z2-7]{6}", pending["confirmation_code"])
    assert pending["source_name"] == "Source laptop" and pending["age_seconds"] == 0
    relay._approvals[("source", lease)]["created"] -= 7
    approved = await relay.approve_request(pending["request_id"], SimpleNamespace(valid=True), "owner")
    listed = relay.recipient_requests()[0]
    status = await relay.handle({"op": "status", "lease": lease}, "source")
    assert approved["confirmation_code"] == listed["confirmation_code"] == status["confirmation_code"] == pending["confirmation_code"]
    assert listed["age_seconds"] >= 7


@pytest.mark.asyncio
async def test_probe_has_total_deadline_and_short_connect_timeout(peers, monkeypatch):
    monkeypatch.setattr(targets, "PROBE_TIMEOUT_SECONDS", .02, raising=False)
    observed = []

    async def blackhole(*args, **kwargs):
        observed.append(transport.connect_timeout.get())
        await asyncio.Event().wait()

    monkeypatch.setattr(control, "call", blackhole)
    previous = transport.connect_timeout.get()
    result = await asyncio.wait_for(targets.peer_availability(peers[1]["runners"][0]), .2)
    assert result["reason_code"] == "peer_unavailable"
    assert observed and all(value is not None and value <= 1 for value in observed)
    assert transport.connect_timeout.get() == previous


@pytest.mark.asyncio
async def test_pending_status_checks_are_concurrent(peers, monkeypatch):
    leases = {relay.authorize(SimpleNamespace(valid=True), "owner", "peer") for _ in range(2)}
    arrived = set()
    both = asyncio.Event()

    async def probe(device, action, body, **kwargs):
        if body["op"] == "probe":
            return {"reason_code": "recipient_approval_required"}
        arrived.add(body["lease"])
        if arrived == leases:
            both.set()
        await both.wait()
        return {"status": "approved", "used": False}

    monkeypatch.setattr(control, "call", probe)
    rows = await asyncio.wait_for(targets.targets(None, "owner"), .2)
    assert arrived == leases and rows[0]["available"]


@pytest.mark.asyncio
async def test_create_probes_only_chosen_worker(peers, monkeypatch):
    availability = AsyncMock(return_value={"available": False, "reason_code": "recipient_approval_required"})
    monkeypatch.setattr(targets, "peer_availability", availability)
    monkeypatch.setattr(targets, "targets", AsyncMock(side_effect=AssertionError("Do not rediscover all targets")))
    monkeypatch.setattr(targets.PeerTerminal, "start", AsyncMock())
    body = api.CreateTerminal(target=api.TargetRef(worker_id="peer", authorization_id="chosen-lease"))
    await targets.create(service.TerminalRegistry(), body, "owner", SimpleNamespace(valid=True))
    assert [call.args[0]["id"] for call in availability.await_args_list] == ["peer"]


@pytest.mark.asyncio
async def test_recipient_uses_local_cwd_validation_and_pinning(peers, monkeypatch, tmp_path):
    validator = Mock(return_value=(str(tmp_path), str(tmp_path)))
    monkeypatch.setattr(api, "_validated_terminal_cwd", validator)
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    target = await _approved()
    opened = await relay.handle({"op": "open", "lease": target.authorization_id,
                                 "options": {"cwd": "/owner-selected"}}, "source")
    validator.assert_called_once_with("/owner-selected")
    assert create.await_args.kwargs["cwd_root"] == str(tmp_path)
    assert opened["terminal"]["cwd"] == str(tmp_path)
    await relay.handle({"op": "close", "lease": target.authorization_id}, "source")


async def _approved():
    from tests.core.test_terminal_targets import approved_target
    return await approved_target(SimpleNamespace(valid=True))


@pytest.mark.asyncio
async def test_recipient_cwd_refusal_does_not_spawn(peers, monkeypatch):
    monkeypatch.setattr(api, "_validated_terminal_cwd", Mock(side_effect=service.TerminalError("bad_folder", "Outside workspace", 400)))
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    target = await _approved()
    result = await relay.handle({"op": "open", "lease": target.authorization_id,
                                 "options": {"cwd": "/"}}, "source")
    assert result["error"]["code"] == "bad_folder"
    create.assert_not_awaited()


def test_source_whitelists_peer_response_keys(peer, monkeypatch):
    from app.network import runners
    monkeypatch.setattr(runners, "directory", lambda: {"runners": [{
        "id": "destination", "device_name": "Destination", "is_self": False, "online": True,
        "capabilities": {"terminal": 2}, "endpoints": ["https://peer.example"],
    }]})
    monkeypatch.setattr(api, "_peer_call", AsyncMock(return_value={
        "status": "pending", "request_id": "request", "used": False, "expires_in": 120,
        "confirmation_code": "ABC234", "source_name": "Source", "age_seconds": 0,
        "worker_id": "forged", "authorization_id": "forged", "unexpected": "private payload",
    }))
    tc = owner(peer)
    created = tc.post("/api/v1/terminals/peer-authorizations", json={"worker_id": "destination"})
    assert created.status_code == 201
    body = created.json()
    assert "unexpected" not in body
    assert body["worker_id"] == "destination" and body["authorization_id"] != "forged"
    state = tc.get("/api/v1/terminals/peer-authorizations/" + body["authorization_id"]).json()
    assert "unexpected" not in state and state["worker_id"] == "destination"
    assert state["confirmation_code"] == body["confirmation_code"] == "ABC234"
