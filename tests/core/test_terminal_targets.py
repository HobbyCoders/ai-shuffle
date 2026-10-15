"""Remote terminals: real relay handlers with an in-process peer and fake SSH IO."""
import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import terminals as api
from app.core import terminal_targets as targets, terminals as service
from app.core import chat_access
from app.network import control, engine, terminal_relay as relay


@pytest.fixture
def peers(monkeypatch, tmp_path):
    # TRANSPORT-ONLY fixture: production dispatch remains closed until it has
    # independent recipient owner proof. Never use these stubs for auth tests;
    # the malicious-member regressions below exercise the real entry point.
    monkeypatch.setattr(relay, "peer_targets_enabled", lambda: True)
    monkeypatch.setattr(relay, "require_recipient_owner", lambda: None)
    secure_context = control.inbound_https.set(True)
    directory = {"runners": [
        {"id": "peer", "device_name": "Peer", "is_self": False, "online": True, "capabilities": {"terminal": 2}},
        {"id": "old", "device_name": "Old", "is_self": False, "online": True, "capabilities": {"terminal": 1}},
        {"id": "offline", "device_name": "Offline", "is_self": False, "online": False, "capabilities": {"terminal": 2}},
        {"id": "missing", "device_name": "Missing", "is_self": False, "online": True, "capabilities": {}},
    ]}
    monkeypatch.setattr(targets.runners, "directory", lambda: directory)
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(member=lambda device: (
        SimpleNamespace(kind="worker", status="active") if device in ("source", "peer") else None)))
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
    control.inbound_https.reset(secure_context)


@pytest.mark.asyncio
async def test_discovery_requires_online_and_relay_capability(peers):
    assert await targets.targets(None) == [{"kind": "worker", "worker_id": "peer", "name": "Peer", "available": True}]
    for device in ("old", "offline", "missing", "unknown"):
        with pytest.raises(service.TerminalError, match="Choose an online"):
            await targets.create(service.TerminalRegistry(), api.CreateTerminal(target=api.TargetRef(worker_id=device)),
                                 "owner", SimpleNamespace(valid=True))


@pytest.mark.asyncio
async def test_peer_rechecks_membership_target_bound_owner_lease_and_revocation(peers):
    request = SimpleNamespace(valid=True)
    lease = relay.authorize(request, "owner", "peer")
    assert await relay.handle_owner({"lease": lease}, "source") == {"authorized": False}
    with pytest.raises(PermissionError):
        await relay.handle_owner({"lease": lease}, "outsider")
    with pytest.raises(PermissionError):
        await relay.handle({"lease": "x" * 32, "op": "open"}, "source")
    request.valid = False
    with pytest.raises(PermissionError):
        await relay.handle({"lease": lease, "op": "open"}, "source")
    assert peers[0].count == 0


@pytest.mark.asyncio
async def test_peer_open_refusal_preserves_error_and_releases_reservation(peers, tmp_path):
    source = service.TerminalRegistry()
    with pytest.raises(service.TerminalError) as exc:
        await targets.create(source, api.CreateTerminal(target=api.TargetRef(worker_id="peer"),
                             cwd=str(tmp_path / "missing")), "owner", SimpleNamespace(valid=True))
    assert exc.value.code == "bad_folder"
    assert source.count == peers[0].count == 0
    assert not relay._owners and not relay._opening


@pytest.mark.asyncio
async def test_stream_relays_arbitrary_bytes_input_resize_and_close(peers):
    source = service.TerminalRegistry()
    terminal = await targets.create(source, api.CreateTerminal(target=api.TargetRef(worker_id="peer"), shell="sh"),
                                    "owner", SimpleNamespace(valid=True))
    remote = peers[0].list()[0]
    payload = b"\x00\xff\x1b[31mpeer bytes\r\n"
    remote._on_output(payload)
    async with asyncio.timeout(5):
        while payload not in terminal.scrollback.snapshot():
            await asyncio.sleep(.01)
    await terminal.write(b"printf 'RELAY_%s' OK\n")
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
async def test_peer_disconnect_lease_closes_shell(peers, monkeypatch):
    monkeypatch.setattr(relay, "LEASE_SECONDS", .02)
    lease = relay.authorize(SimpleNamespace(valid=True), "owner", "peer")
    await relay.handle({"lease": lease, "op": "open", "options": {"shell": "sh"}}, "source")
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
    broker, grant, output, writes, closed, connect, connection = ssh
    rows = await targets.targets("chat")
    assert rows == [{"kind": "ssh", "grant_id": "grant", "name": "Approved machine", "available": True}]
    registry = service.TerminalRegistry()
    terminal = await targets.create(registry, api.CreateTerminal(session_id="chat", target=api.TargetRef(kind="ssh", grant_id="grant")),
                                    "owner", None)
    connect.assert_awaited_once_with(grant)
    connection.create_process.assert_awaited_once_with(term_type="xterm-256color", term_size=(80, 24), encoding=None, stderr=targets.asyncssh.STDOUT)
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
async def test_revoked_peer_owner_closes_stream_and_refuses_new_input(peers):
    request = SimpleNamespace(valid=True)
    source = service.TerminalRegistry()
    terminal = await targets.create(source, api.CreateTerminal(target=api.TargetRef(worker_id="peer"), shell="sh"), "owner", request)
    request.valid = False
    with pytest.raises(service.TerminalError):
        await terminal.write(b"not delivered")
    await asyncio.wait_for(terminal.task, 5)
    assert terminal.state == service.STATE_EXITED and not relay._owners
    # Destination no longer trusts close either; its existing lease expires.
    for entry in list(relay._sessions.values()):
        entry[3].cancel()
        await asyncio.gather(entry[3], return_exceptions=True)
    await peers[0].shutdown()
    await source.shutdown()

