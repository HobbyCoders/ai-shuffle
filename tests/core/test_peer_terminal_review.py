"""Review regressions for bounded discovery, receiver-side folders and default-off compatibility."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api import terminals as api
from app.core import terminal_targets as targets, terminals as service
from app.network import control, transport, terminal_relay as relay
from tests.core.test_terminal_targets import open_lease, peers, signed_target, ssh  # noqa: F401
from tests.api.test_peer_terminal_authorization import peer, ctx, owner  # noqa: F401


@pytest.mark.asyncio
async def test_disabled_feature_preserves_no_peer_choices(peers, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: False)
    assert await targets.targets(None) == []


@pytest.mark.asyncio
async def test_ssh_create_never_discovers_peers(ssh, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: True)
    monkeypatch.setattr(targets.runners, "directory", Mock(side_effect=AssertionError("SSH must not discover peers")))
    registry = service.TerminalRegistry()
    terminal = await targets.create(registry, api.CreateTerminal(session_id="chat",
        target=api.TargetRef(kind="ssh", grant_id="grant")), "owner", None)
    await registry.close(terminal.id, by="test")


@pytest.mark.asyncio
async def test_probe_has_total_deadline_and_short_connect_timeout(peers, monkeypatch):  # noqa: F811
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
async def test_create_probes_only_chosen_worker(peers, monkeypatch):  # noqa: F811
    availability = AsyncMock(return_value={"available": False, "reason_code": "peer_unavailable"})
    monkeypatch.setattr(targets, "peer_availability", availability)
    monkeypatch.setattr(targets, "targets", AsyncMock(side_effect=AssertionError("Do not rediscover all targets")))
    body = api.CreateTerminal(target=api.TargetRef(worker_id="peer"))
    with pytest.raises(service.TerminalError) as refused:
        await targets.create(service.TerminalRegistry(), body, "owner", SimpleNamespace(valid=True))
    assert refused.value.code == "target_unavailable"
    assert [call.args[0]["id"] for call in availability.await_args_list] == ["peer"]


@pytest.mark.asyncio
async def test_recipient_uses_local_cwd_validation_and_pinning(peers, monkeypatch, tmp_path):  # noqa: F811
    validator = Mock(return_value=(str(tmp_path), str(tmp_path)))
    monkeypatch.setattr(api, "_validated_terminal_cwd", validator)
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    lease, opened = await open_lease(SimpleNamespace(valid=True), cwd="/owner-selected")
    validator.assert_called_once_with("/owner-selected")
    assert create.await_args.kwargs["cwd_root"] == str(tmp_path)
    assert opened["terminal"]["cwd"] == str(tmp_path)
    await relay.handle({"op": "close", "lease": lease}, "source")


@pytest.mark.asyncio
async def test_folder_changing_during_spawn_forgets_the_source_lease(peers, monkeypatch, tmp_path):  # noqa: F811
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    selected = tmp_path / "selected"
    selected.mkdir()
    original = api._validated_terminal_cwd

    def validate(cwd):
        result = original(cwd)
        selected.rename(tmp_path / "moved")  # Real registry's subsequent pin must fail.
        return result

    monkeypatch.setattr(api, "_validated_terminal_cwd", validate)
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    with pytest.raises(service.TerminalError) as failed:
        await targets.create(service.TerminalRegistry(), api.CreateTerminal(target=await signed_target(cwd=str(selected))),
                             "owner", SimpleNamespace(valid=True))
    assert failed.value.code == "bad_folder"
    create.assert_awaited_once()
    assert peers[0].count == 0
    assert not relay._owners


@pytest.mark.asyncio
async def test_requester_revoke_during_folder_validation_never_spawns(peers, monkeypatch, tmp_path):  # noqa: F811
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    lease = relay.authorize(SimpleNamespace(valid=True), "owner", "peer")

    def validate(cwd):
        relay.forget(lease)
        return str(tmp_path), str(tmp_path)

    monkeypatch.setattr(api, "_validated_terminal_cwd", validate)
    target = await signed_target()
    with pytest.raises(PermissionError, match="Source"):
        await relay.handle({"op": "open", "lease": lease, "proof": target.proof.model_dump()}, "source")
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_policy_off_refuses_the_owner_callback(peers, monkeypatch):  # noqa: F811
    lease = relay.authorize(SimpleNamespace(valid=True), "owner", "peer")
    # This is the callback used by the receiver's live-shell recheck.
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: False)
    assert await relay.handle_owner({"lease": lease}, "peer") == {"authorized": False}
    assert lease not in relay._owners


@pytest.mark.asyncio
async def test_source_policy_off_closes_a_live_shell_on_the_next_recheck(peers, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay, "AUTH_RECHECK_SECONDS", .01)
    lease, _ = await open_lease(SimpleNamespace(valid=True))
    expiry_task = relay._sessions[("source", lease)][3]
    original = control.call

    async def source_policy_off(device, action, body, **kwargs):
        if action == "terminal-owner":
            # Only the source policy is off; the receiver's stays on.
            with monkeypatch.context() as source:
                source.setattr(relay, "peer_targets_enabled", lambda: False)
                return await original(device, action, body, **kwargs)
        return await original(device, action, body, **kwargs)

    monkeypatch.setattr(control, "call", source_policy_off)
    await asyncio.wait_for(expiry_task, 2)
    assert lease not in relay._owners
    assert ("source", lease) not in relay._sessions
    assert peers[0].count == 0


@pytest.mark.asyncio
async def test_source_owner_signing_out_closes_a_live_shell_on_the_next_recheck(peers, monkeypatch):  # noqa: F811
    monkeypatch.setattr(relay, "AUTH_RECHECK_SECONDS", .01)
    request = SimpleNamespace(valid=True)
    lease, _ = await open_lease(request)
    expiry_task = relay._sessions[("source", lease)][3]
    request.valid = False
    await asyncio.wait_for(expiry_task, 2)
    assert ("source", lease) not in relay._sessions
    assert peers[0].count == 0


@pytest.mark.asyncio
async def test_recipient_cwd_refusal_does_not_spawn(peers, monkeypatch, tmp_path):  # noqa: F811
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    create = AsyncMock(wraps=peers[0].create)
    monkeypatch.setattr(peers[0], "create", create)
    _, result = await open_lease(SimpleNamespace(valid=True), cwd="/")
    assert result["error"]["code"] == "bad_folder"
    assert result["error"]["status"] == 400
    assert "workspace" in result["error"]["message"].lower()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_bad_folder_can_be_retried_with_a_new_signed_request(peers, monkeypatch, tmp_path):  # noqa: F811
    from app.core.config import settings
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    source = service.TerminalRegistry()
    request = SimpleNamespace(valid=True)
    with pytest.raises(service.TerminalError) as failed:
        await targets.create(source, api.CreateTerminal(target=await signed_target(cwd="/")), "owner", request)
    assert failed.value.code == "bad_folder" and failed.value.status == 400
    terminal = await targets.create(source, api.CreateTerminal(target=await signed_target(cwd=str(tmp_path))), "owner", request)
    assert terminal.cwd == str(tmp_path)
    await source.close(terminal.id, by="test")
    assert source.count == peers[0].count == 0


def test_source_passes_on_only_the_documented_reply_fields(peer, monkeypatch):  # noqa: F811
    reply = AsyncMock(return_value={"status": "linked", "challenge": "token", "expires_in": 60, "key_id": "k",
                                    "receiver": "forged", "via": "forged", "unexpected": "private payload"})
    monkeypatch.setattr(api, "_peer_call", reply)
    challenge = peer.tc.post("/api/v1/terminals/peer-challenge", json={"worker_id": "destination", "key_id": "k"}).json()
    # The receiver and via are this worker's own record, and cannot be forged by the reply.
    assert challenge == {"status": "linked", "challenge": "token", "expires_in": 60, "receiver": "destination", "via": "source"}
    linked = peer.tc.post("/api/v1/terminals/peer-links", json={"worker_id": "destination", "public_key": "k" * 88, "label": "Office PC"})
    assert linked.status_code == 201 and linked.json() == {"status": "linked", "key_id": "k", "expires_in": 60}
