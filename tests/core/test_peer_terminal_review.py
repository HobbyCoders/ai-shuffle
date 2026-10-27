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
    result = await asyncio.wait_for(targets.peer_availability(peers[1]["runners"][0]), 5)
    assert result["reason_code"] == "peer_unavailable"
    assert observed and all(value is not None and value <= 1 for value in observed)
    assert transport.connect_timeout.get() == previous


@pytest.mark.asyncio
async def test_pending_status_checks_are_concurrent(peers, monkeypatch):
    leases = {relay.authorize(SimpleNamespace(valid=True), "owner", "peer") for _ in range(2)}
    arrived = set()
    in_flight = set()
    both = asyncio.Event()

    async def probe(device, action, body, **kwargs):
        if body["op"] == "probe":
            return {"reason_code": "recipient_approval_required"}
        lease = body["lease"]
        arrived.add(lease)
        in_flight.add(lease)
        try:
            # Arrival alone is insufficient: a serial probe could arrive after
            # its predecessor timed out. Only overlapping calls open the gate.
            if in_flight == leases:
                both.set()
            await both.wait()
            return {"status": "approved", "used": False}
        finally:
            in_flight.remove(lease)

    monkeypatch.setattr(control, "call", probe)
    # A deadlock watchdog, not the evidence of concurrency: the gate is that proof.
    rows = await asyncio.wait_for(targets.targets(None, "owner"), 10)
    assert both.is_set() and arrived == leases and rows[0]["available"]


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




@pytest.mark.asyncio
async def test_spawn_bad_folder_forgets_consumed_source_lease(peers, monkeypatch, tmp_path):
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    selected = tmp_path / "selected"
    selected.mkdir()
    target = await _approved()
    original = api._validated_terminal_cwd

    def validate(cwd):
        result = original(cwd)
        selected.rename(tmp_path / "moved")  # Real registry's subsequent pin must fail.
        return result

    monkeypatch.setattr(api, "_validated_terminal_cwd", validate)
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    with pytest.raises(service.TerminalError) as failed:
        await targets.create(service.TerminalRegistry(), api.CreateTerminal(target=target, cwd=str(selected)),
                             "owner", SimpleNamespace(valid=True))
    assert failed.value.code == "folder_changed"  # Existing picker requests new consent.
    create.assert_awaited_once()
    assert peers[0].count == 0
    assert target.authorization_id not in relay._owners
    assert ("source", target.authorization_id) not in relay._approvals


@pytest.mark.asyncio
async def test_requester_revoke_during_folder_validation_never_spawns(peers, monkeypatch, tmp_path):
    target = await _approved()
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)

    def validate(cwd):
        relay.forget(target.authorization_id)
        return str(tmp_path), str(tmp_path)

    monkeypatch.setattr(api, "_validated_terminal_cwd", validate)
    with pytest.raises(PermissionError, match="Source"):
        await relay.handle({"op": "open", "lease": target.authorization_id}, "source")
    create.assert_not_awaited()
    assert ("source", target.authorization_id) not in relay._approvals


@pytest.mark.asyncio
async def test_source_policy_off_refuses_callback_without_status_poll(peers, monkeypatch):
    target = await _approved()
    # This is the callback used by the recipient's live-shell recheck. There
    # has been no requester status GET to clear the lease first.
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: False)
    assert await relay.handle_owner({"lease": target.authorization_id}, "peer") == {"authorized": False}
    assert target.authorization_id not in relay._owners




@pytest.mark.asyncio
async def test_source_policy_off_closes_live_recipient_without_status_poll(peers, monkeypatch):
    target = await _approved()
    monkeypatch.setattr(relay, "AUTH_RECHECK_SECONDS", .01)
    await relay.handle({"op": "open", "lease": target.authorization_id}, "source")
    expiry_task = relay._sessions[("source", target.authorization_id)][3]
    original = control.call

    async def source_policy_off(device, action, body, **kwargs):
        if action == "terminal-owner":
            # Only the source policy is off; recipient owner checks stay on.
            with monkeypatch.context() as source:
                source.setattr(relay, "peer_targets_enabled", lambda: False)
                return await original(device, action, body, **kwargs)
        return await original(device, action, body, **kwargs)

    monkeypatch.setattr(control, "call", source_policy_off)
    await asyncio.wait_for(expiry_task, 2)
    assert target.authorization_id not in relay._owners
    assert ("source", target.authorization_id) not in relay._sessions
    assert peers[0].count == 0


async def _approved():
    from tests.core.test_terminal_targets import approved_target
    return await approved_target(SimpleNamespace(valid=True))


@pytest.mark.asyncio
async def test_recipient_cwd_refusal_does_not_spawn(peers, monkeypatch, tmp_path):
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    target = await _approved()
    result = await relay.handle({"op": "open", "lease": target.authorization_id,
                                 "options": {"cwd": "/"}}, "source")
    assert result["error"]["code"] == "bad_folder"
    assert result["error"]["status"] == 400
    assert "workspace" in result["error"]["message"].lower()
    assert not relay._approvals[("source", target.authorization_id)]["used"]
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_recipient", [False, True])
async def test_bad_folder_preserves_source_and_recipient_consent_for_retry(peers, monkeypatch, tmp_path, legacy_recipient):
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    original = control.call

    async def recipient(device, action, body, **kwargs):
        result = await original(device, action, body, **kwargs)
        if legacy_recipient and action == "terminal" and "error" in result:
            # Previous recipients preserve unused consent but omit this flag.
            result["error"].pop("retryable", None)
        return result

    monkeypatch.setattr(control, "call", recipient)
    source = service.TerminalRegistry()
    request = SimpleNamespace(valid=True)
    target = await _approved()
    with pytest.raises(service.TerminalError) as failed:
        await targets.create(source, api.CreateTerminal(target=target, cwd="/"), "owner", request)
    assert failed.value.code == "bad_folder" and failed.value.status == 400
    assert target.authorization_id in relay._owners
    assert not relay._approvals[("source", target.authorization_id)]["used"]
    terminal = await targets.create(source, api.CreateTerminal(target=target, cwd=str(tmp_path)), "owner", request)
    assert terminal.cwd == str(tmp_path)
    await source.close(terminal.id, by="test")
    assert source.count == peers[0].count == 0


@pytest.mark.asyncio
async def test_request_owner_callback_has_deadline_and_creates_no_late_request(peers, monkeypatch):
    monkeypatch.setattr(relay, "REQUEST_CALLBACK_SECONDS", .02, raising=False)
    observed = []
    cancelled = asyncio.Event()

    async def blackhole(*args, **kwargs):
        observed.append(transport.connect_timeout.get())
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(control, "call", blackhole)
    previous = transport.connect_timeout.get()
    async with asyncio.timeout(.2):
        with pytest.raises(TimeoutError):
            await relay.handle({"op": "request", "lease": "slow-source-lease-1234567890"}, "source")
    assert cancelled.is_set()
    assert observed and all(value is not None and value <= 1 for value in observed)
    assert transport.connect_timeout.get() == previous
    assert not relay._approvals


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
