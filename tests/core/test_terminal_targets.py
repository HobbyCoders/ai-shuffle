"""Remote terminals: real relay handlers and signatures, in-process peer, fake SSH IO."""
import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import terminals as api
from app.core import terminal_targets as targets, terminals as service
from app.core import chat_access
from app.db import terminal_device_links as link_store
from app.network import control, engine, terminal_device_links as links, terminal_relay as relay
from tests.api.peer_device import Device

DEVICE = Device()


@pytest.fixture
def peers(monkeypatch, tmp_path):
    # In-process transport, with the production receiver checks and a real device
    # signature. Owner credentials are covered by the API authorization tests.
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: True)
    secure_context = control.inbound_https.set(True)
    directory = {"runners": [
        {"id": "peer", "device_name": "Peer", "is_self": False, "online": True,
         "capabilities": {"terminal": service.PEER_TERMINAL_VERSION}},
        {"id": "old", "device_name": "Old", "is_self": False, "online": True,
         "capabilities": {"terminal": service.PEER_TERMINAL_VERSION - 1}},
        {"id": "offline", "device_name": "Offline", "is_self": False, "online": False,
         "capabilities": {"terminal": service.PEER_TERMINAL_VERSION}},
        {"id": "missing", "device_name": "Missing", "is_self": False, "online": True, "capabilities": {}},
    ]}
    for row in directory["runners"]:
        row["endpoints"] = ["https://peer.example"]
    monkeypatch.setattr(targets.runners, "directory", lambda: directory)
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="peer", member=lambda device: (
        SimpleNamespace(kind="worker", status="active") if device in ("source", "peer") else None)))
    linked = {DEVICE.key_id: {"key_id": DEVICE.key_id, "public_key": DEVICE.public_key, "label": DEVICE.label,
                              "via_device": "source", "via_name": "Source", "approved_by": "owner"}}
    monkeypatch.setattr(link_store, "get", lambda key_id: linked.get(key_id))
    monkeypatch.setattr(link_store, "touch", lambda key_id: None)
    monkeypatch.setattr(link_store, "delete", lambda key_id: linked.pop(key_id, None) is not None)
    receiver = service.TerminalRegistry()
    monkeypatch.setattr(service, "registry", receiver)
    monkeypatch.setattr(service, "worker_identity", lambda: {"id": "peer", "name": "Peer"})
    monkeypatch.setattr(service, "default_folder", lambda: (str(tmp_path), "workspace"))
    monkeypatch.setattr(api, "require_terminal_owner", lambda request: "owner" if request.valid else "refused")

    async def call(device, action, body, **kwargs):
        assert kwargs["secure"] is True
        if action == "terminal-owner":
            assert device == "source"
            return await relay.handle_owner(body, "peer")
        assert device == "peer" and action == "terminal"
        return await relay.handle(body, "source")

    monkeypatch.setattr(control, "call", call)
    yield receiver, directory
    receiver.kill_all_now()
    relay._owners.clear()
    for entry in relay._sessions.values():
        entry[3].cancel()
    relay._sessions.clear()
    links._challenges.clear()
    control.inbound_https.reset(secure_context)


async def signed_proof(**options):
    answer = await relay.handle({"op": "challenge", "key_id": DEVICE.key_id}, "source")
    assert answer["status"] == "linked", answer
    return DEVICE.proof(answer["challenge"], receiver="peer", **options)


async def signed_target(**options):
    return api.TargetRef(worker_id="peer", proof=await signed_proof(**options))


async def open_lease(request, **options):
    """A shell opened straight on the receiver, as a source worker asks for one."""
    lease = relay.authorize(request, "owner", "peer")
    opened = await relay.handle({"lease": lease, "op": "open", "options": {}, "proof": await signed_proof(**options)}, "source")
    return lease, opened


@pytest.mark.asyncio
async def test_discovery_requires_online_and_relay_capability(peers):
    rows = await targets.targets(None)
    assert {row["worker_id"]: row["reason_code"] for row in rows} == {
        "peer": None, "old": "peer_unsupported_or_disabled",
        "offline": "peer_offline", "missing": "peer_unsupported_or_disabled"}
    assert [row["worker_id"] for row in rows if row["available"]] == ["peer"]
    for device in ("old", "offline", "missing", "unknown"):
        with pytest.raises(service.TerminalError, match="Choose an online"):
            await targets.create(service.TerminalRegistry(), api.CreateTerminal(target=api.TargetRef(worker_id=device)),
                                 "owner", SimpleNamespace(valid=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["peer_terminals_disabled", "not_supported"])
async def test_discovery_probes_recipient_despite_stale_capabilities(peers, monkeypatch, code):
    monkeypatch.setattr(relay, "availability", lambda: {"available": False, "reason_code": code, "reason": "Unavailable"})
    rows = await targets.targets(None)
    assert rows[0]["reason_code"] == code and not rows[0]["available"]


@pytest.mark.asyncio
async def test_discovery_reports_unreachable_and_insecure_peers(peers, monkeypatch):
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=ConnectionError("offline")))
    rows = await targets.targets(None)
    assert rows[0]["reason_code"] == "peer_unavailable"
    peers[1]["runners"][0]["endpoints"] = ["http://peer.example"]
    rows = await targets.targets(None)
    assert rows[0]["reason_code"] == "https_required"


@pytest.mark.asyncio
async def test_peer_rechecks_membership_target_bound_owner_lease_and_device(peers):
    request = SimpleNamespace(valid=True)
    lease = relay.authorize(request, "owner", "peer")
    assert await relay.handle_owner({"lease": lease}, "source") == {"authorized": False}
    with pytest.raises(PermissionError):
        await relay.handle_owner({"lease": lease}, "outsider")
    refused = await relay.handle({"lease": "x" * 32, "op": "open"}, "source")
    assert refused["error"]["code"] == "device_link_required"
    request.valid = False
    with pytest.raises(PermissionError):
        await relay.handle({"lease": lease, "op": "open", "proof": await signed_proof()}, "source")
    assert peers[0].count == 0


@pytest.mark.asyncio
async def test_peer_open_refusal_preserves_error_and_releases_reservation(peers, tmp_path):
    source = service.TerminalRegistry()
    request = SimpleNamespace(valid=True)
    target = await signed_target(cwd=str(tmp_path / "missing"))
    with pytest.raises(service.TerminalError) as exc:
        await targets.create(source, api.CreateTerminal(target=target), "owner", request)
    assert exc.value.code == "bad_folder"
    assert source.count == peers[0].count == 0
    assert not relay._owners and not relay._opening


@pytest.mark.asyncio
async def test_stream_relays_arbitrary_bytes_input_resize_and_close(peers):
    source = service.TerminalRegistry()
    request = SimpleNamespace(valid=True)
    terminal = await targets.create(source, api.CreateTerminal(target=await signed_target()), "owner", request)
    remote = peers[0].list()[0]
    payload = b"\x00\xff\x1b[31mpeer bytes\r\n"
    remote._on_output(payload)
    async with asyncio.timeout(5):
        while payload not in terminal.scrollback.snapshot():
            await asyncio.sleep(.01)
    command = (b"Write-Output ('RELAY_' + 'OK')\r"
               if remote.shell in {"pwsh", "powershell"} else b"printf 'RELAY_%s' OK\n")
    await terminal.write(command)
    async with asyncio.timeout(5):
        while b"RELAY_OK" not in terminal.scrollback.snapshot():
            await asyncio.sleep(.01)
    terminal.resize(100, 40)
    async with asyncio.timeout(5):
        while remote.cols != 100:
            await asyncio.sleep(.01)
    assert remote.rows == 40
    await source.close(terminal.id, by="owner")
    assert peers[0].count == 0 and not relay._owners
    await peers[0].shutdown()


@pytest.mark.asyncio
async def test_immediate_source_close_releases_recipient_before_reader_runs(peers):
    source = service.TerminalRegistry()
    request = SimpleNamespace(valid=True)
    terminal = await targets.create(source, api.CreateTerminal(target=await signed_target()), "owner", request)
    await source.close(terminal.id, by="owner")
    assert peers[0].count == 0
    assert not relay._owners


@pytest.mark.asyncio
async def test_peer_disconnect_lease_closes_shell(peers, monkeypatch):
    monkeypatch.setattr(relay, "LEASE_SECONDS", .02)
    _, opened = await open_lease(SimpleNamespace(valid=True))
    assert "terminal" in opened, opened
    assert peers[0].count == 1
    async with asyncio.timeout(2):
        while peers[0].count:
            await asyncio.sleep(.01)
    await peers[0].shutdown()


@pytest.fixture
def ssh(monkeypatch):
    owner = ("local", None, None, None)
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=owner))
    broker = chat_access.Broker()
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(broker, "_notify", lambda *a, **k: None)
    grant = chat_access.Grant("grant", "chat", owner, "Approved machine", "machine", "", time.time() + 60,
                              writable=True, fingerprint="test-pin", host_key_alg="ssh-ed25519")
    broker.grants[grant.id] = grant
    output = asyncio.Queue()
    writes = []
    process = SimpleNamespace(stdout=SimpleNamespace(read=lambda n: output.get()),
        stdin=SimpleNamespace(write=writes.append, drain=AsyncMock()), exit_status=None,
        change_terminal_size=lambda cols, rows: writes.append((cols, rows)))
    closed = []
    connection = SimpleNamespace(create_process=AsyncMock(return_value=process), close=lambda: closed.append(True))
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(broker, "connection", connect)
    monkeypatch.setattr(targets.runners, "directory", lambda: {"runners": []})
    return broker, grant, output, writes, closed, connect, connection


@pytest.mark.asyncio
async def test_ssh_uses_existing_pinned_connection_and_grant_lifetime(ssh):
    import asyncssh

    broker, grant, output, writes, closed, connect, connection = ssh
    rows = await targets.targets("chat")
    assert rows == [{"kind": "ssh", "grant_id": "grant", "name": "Approved machine", "available": True}]
    registry = service.TerminalRegistry()
    terminal = await targets.create(registry, api.CreateTerminal(session_id="chat", target=api.TargetRef(kind="ssh", grant_id="grant")),
                                    "owner", None)
    connect.assert_awaited_once_with(grant)
    connection.create_process.assert_awaited_once_with(term_type="xterm-256color", term_size=(80, 24), encoding=None, stderr=asyncssh.STDOUT)
    await output.put(b"\xffhost\r\n")
    async with asyncio.timeout(2):
        while not terminal.scrollback.size:
            await asyncio.sleep(.01)
    assert terminal.scrollback.snapshot() == b"\xffhost\r\n"
    await terminal.write(b"echo owner\r")
    terminal.resize(120, 50)
    assert writes == [b"echo owner\r", (120, 50)]
    broker.revoke("chat", "grant")
    await terminal.task
    assert closed and terminal.state == service.STATE_EXITED
    with pytest.raises(service.TerminalError):
        await terminal.write(b"not sent")
    await registry.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "readonly", "wrong-session", "folder", "not-ready"])
async def test_ssh_unapproved_targets_fail_closed(ssh, change):
    broker, grant, _, _, _, connect, _ = ssh
    if change == "expired":
        grant.expires_at = 0
    elif change == "readonly":
        grant.writable = False
    elif change == "wrong-session":
        grant.session_id = "other"
    elif change == "folder":
        grant.kind = "folder"
    else:
        grant.ready = False
    with pytest.raises(service.TerminalError):
        await targets.create(service.TerminalRegistry(), api.CreateTerminal(session_id="chat", target=api.TargetRef(kind="ssh", grant_id="grant")),
                             "owner", None)
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_grant_producer_feeds_terminal_and_immediate_revocation(ssh, monkeypatch):
    broker, _, _, _, closed, connect, connection = ssh
    monkeypatch.setattr(broker, "pin_host_key", AsyncMock())

    class VerifyConnection:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    connect.side_effect = [VerifyConnection(), connection]
    # Drive the real producer, including machine writable=True normalization.
    result = await broker.create("chat", chat_access.GrantInput(
        kind="machine", label="Test SSH", host="192.0.2.8", username="test-user", password="test-only"))
    grant = broker.grants[result["id"]]
    assert grant.writable
    registry = service.TerminalRegistry()
    terminal = await targets.create(registry, api.CreateTerminal(session_id="chat", target=api.TargetRef(kind="ssh", grant_id=grant.id)),
                                    "owner", None)
    # No yield between creating the reader and cancelling it.
    broker.revoke("chat", grant.id)
    await asyncio.gather(terminal.task, return_exceptions=True)
    assert closed and not grant.tasks and terminal.state == service.STATE_EXITED
    await registry.shutdown()


@pytest.mark.asyncio
async def test_revoked_device_closes_stream_and_refuses_new_input(peers):
    request = SimpleNamespace(valid=True)
    source = service.TerminalRegistry()
    terminal = await targets.create(source, api.CreateTerminal(target=await signed_target()), "owner", request)
    assert peers[0].count == 1
    await relay.revoke_device(DEVICE.key_id)
    assert peers[0].count == 0  # Closed by the revocation, not by the next check.
    with pytest.raises(service.TerminalError):
        await terminal.write(b"not delivered")
    await asyncio.wait_for(terminal.task, 5)
    assert terminal.state == service.STATE_EXITED and not relay._owners
    await peers[0].shutdown()
    await source.shutdown()
