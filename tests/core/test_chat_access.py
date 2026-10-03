"""Resource authorization, real SSH transport, one-shot approval, and file fences."""
import asyncio
import os
import re
from unittest.mock import AsyncMock

import asyncssh
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import chat_access, chat_files
from app.core.chat_access import AccessDenied, Broker, GrantInput

_real_session_owner = chat_access.session_owner


@pytest.fixture
def broker(monkeypatch):
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=("local", "worker")))
    from app.core import chat_vault
    monkeypatch.setattr(chat_vault, "catalog", lambda owner, **kwargs: [])
    return Broker()


@pytest.mark.asyncio
@pytest.mark.parametrize("api_user_id, owner_kind", [(None, "admin"), (None, "account"), ("alice", "api_user")])
async def test_request_producer_invalidates_global_owner_without_private_payload(broker, monkeypatch, api_user_id, owner_kind):
    import json
    from app.core.global_events import global_events
    from app.core import preference_identity
    monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=(None, "worker", api_user_id)))
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: "owner" if owner_kind == "account" else None)
    monkeypatch.setattr(preference_identity, "account_identity", lambda identity: ("account", identity))
    monkeypatch.setattr(preference_identity.owner_auth, "preference_identity", lambda: "owner")
    class Socket:
        def __init__(self):
            self.sent = []
        async def send_text(self, text):
            self.sent.append(json.loads(text))
    principals = [("admin", "owner"), ("api_user", "alice"), ("api_user", "bob"), ("paired", "device"), ("account", "owner"), ("account", "other")]
    sockets = [Socket() for _ in principals]
    ids = [global_events.register(ws, *principal) for ws, principal in zip(sockets, principals)]
    request = asyncio.create_task(broker.request("private-session", "machine", "Private purpose never broadcast"))
    try:
        async with asyncio.timeout(3):
            while not broker.requests:
                await asyncio.sleep(0)
            await asyncio.gather(*broker._notifications)
            for _ in range(12):
                await asyncio.sleep(0)
        expected = {"type": "resources_changed", "resources": ["chat-access:private-session"]}
        recipient = {"admin": 0, "api_user": 1, "account": 4}[owner_kind]
        for index, ws in enumerate(sockets):
            assert ws.sent == [{"type": "sync_ready"}] + ([expected] if index == recipient else [])
        pending_request = next(iter(broker.requests.values()))
        await broker.decline("private-session", pending_request.id)
        with pytest.raises(AccessDenied):
            await request
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.gather(*broker._notifications)
        for socket_id in ids:
            await global_events.unregister(socket_id)


@pytest_asyncio.fixture
async def ssh_target():
    key = asyncssh.generate_private_key("ssh-ed25519")
    commands = []
    started = asyncio.Event()

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True
        def password_auth_supported(self):
            return True
        def validate_password(self, username, password):
            return username == "operator" and password == "test-only-password"

    async def process(proc):
        commands.append(proc.command)
        started.set()
        if proc.command == "hold":
            await proc.stdin.read()
        elif proc.command == "large":
            proc.stdout.write("x" * 100000)
        else:
            proc.stdout.write("remote:" + proc.command)
        proc.exit(7 if proc.command == "fail" else 0)

    server = await asyncssh.create_server(Server, "127.0.0.1", 0,
        server_host_keys=[key], process_factory=process)
    spec = GrantInput(kind="machine", label="Test SSH", host="127.0.0.1",
        port=server.get_port(), username="operator", password="test-only-password",
        fingerprint=key.get_fingerprint("sha256"))
    try:
        yield spec, commands, started
    finally:
        server.close()
        await server.wait_closed()




@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [False, True])
async def test_live_mode_change_releases_pending_private_action_and_rechecks_next_call(broker, ssh_target, monkeypatch, delegated):
    from types import SimpleNamespace

    from app.api.sessions_permissions import apply_permission_mode
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.core.session_pool import SessionState
    from app.mcp import chat_access_sdk as sdk
    from tests.core.test_permission_mode_reuse import _connected_adapter, _NativeEngine

    adapter, options = await _connected_adapter(_NativeEngine(), "default")
    adapter._is_delegated = delegated
    state = SessionState(adapter=adapter, is_connected=True, connect_options=options)
    row = {"id": "chat", "mode": "code", "permission_mode": "default"}
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: state))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: None)
    monkeypatch.setattr("app.network.control._require_owned", lambda sid: row)
    monkeypatch.setattr("app.db.database.get_session", lambda sid: row)
    monkeypatch.setattr("app.db.database.set_session_run_binding", lambda sid, **kw: row.update(kw))
    monkeypatch.setattr(chat_access, "broker", broker)
    monkeypatch.setattr(sdk, "broker", broker)
    tools = {t.name: t for t in recorded_sdk_server_tools(sdk.create_chat_access_server("chat"))}
    spec, commands, _ = ssh_target
    grant = await broker.create("chat", spec)

    async def command(value):
        return await tools["ChatRunCommand"].handler({"grant_id": grant["id"], "command": value})

    task = asyncio.create_task(command("pending"))
    await pending(broker)
    assert not commands
    await apply_permission_mode("chat", "bypassPermissions")
    assert adapter.permission_mode == row["permission_mode"] == "bypassPermissions"
    assert "remote:pending" in str(await asyncio.wait_for(task, 3))
    assert "remote:bypass" in str(await asyncio.wait_for(command("bypass"), 3))
    assert not broker.actions

    # Same handlers, no server rebuild. The effective adapter mode wins over a
    # stale persisted Bypass selection (as on a live turn override).
    await adapter.set_permission_mode("default")
    assert row["permission_mode"] == "bypassPermissions"
    task = asyncio.create_task(command("denied"))
    action = await pending(broker)
    await broker.decide("chat", action.id, False)
    assert "denied" in str(await task)
    for mode in ("acceptEdits", "auto", "default"):
        await apply_permission_mode("chat", mode)
        task = asyncio.create_task(command(mode))
        action = await pending(broker)
        assert commands == ["pending", "bypass"]
        await broker.decide("chat", action.id, False)
        assert "denied" in str(await task)
    broker.revoke("chat", grant["id"])


@pytest.mark.asyncio
async def test_mode_change_during_persisted_lookup_does_not_strand_action(broker, ssh_target, monkeypatch):
    from types import SimpleNamespace

    from app.api.sessions_permissions import apply_permission_mode
    from app.db import async_database, database

    row = {"permission_mode": "default", "mode": "voice"}
    monkeypatch.setattr(database, "get_session", lambda sid: dict(row))
    monkeypatch.setattr(database, "set_session_run_binding", lambda sid, **kw: row.update(kw))
    monkeypatch.setattr("app.network.control._require_owned", lambda sid: dict(row))
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: None)
    monkeypatch.setattr(chat_access, "broker", broker)
    grant = await broker.create("chat", ssh_target[0])
    read, release = asyncio.Event(), asyncio.Event()
    real_run_sync = async_database.run_sync

    async def delayed_snapshot(fn, *args, **kwargs):
        result = await real_run_sync(fn, *args, **kwargs)
        if fn is database.get_session and not read.is_set():
            read.set()
            await release.wait()
        return result

    monkeypatch.setattr(async_database, "run_sync", delayed_snapshot)
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "raced"}))
    try:
        await asyncio.wait_for(read.wait(), 3)
        await apply_permission_mode("chat", "bypassPermissions")
        assert not broker.actions and not ssh_target[1]
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result["stdout"] == "remote:raced"
        assert not broker.actions
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        broker.revoke("chat", grant["id"])


@pytest.mark.asyncio
@pytest.mark.skipif(not chat_files.available(), reason="descriptor-relative Linux folder access required")
@pytest.mark.parametrize("operation", ["command", "write"])
@pytest.mark.parametrize("invalidated", ["expired", "revoked"])
async def test_grant_rechecked_after_bypass_decision(broker, ssh_target, tmp_path, monkeypatch, operation, invalidated):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "bypassPermissions"})
    spec = ssh_target[0] if operation == "command" else GrantInput(
        kind="folder", label="Files", path=str(tmp_path), writable=True)
    grant = await broker.create("chat", spec)
    real_bypasses = chat_access.session_bypasses_confirmation

    async def invalidate_after_decision(session_id):
        bypasses = await real_bypasses(session_id)
        assert bypasses
        if invalidated == "expired":
            broker.grants[grant["id"]].expires_at = 0
        else:
            broker.revoke(session_id, grant["id"])
        return bypasses

    monkeypatch.setattr(chat_access, "session_bypasses_confirmation", invalidate_after_decision)
    args = {"command": "never"} if operation == "command" else {"path": "never.txt", "content": "no"}
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], operation, args)
    assert not ssh_target[1] and not (tmp_path / "never.txt").exists()
    assert not broker.grants and not broker.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated", ["expired", "revoked", "owner"])
async def test_bypass_still_enforces_grant_lifetime_and_owner(broker, ssh_target, monkeypatch, invalidated):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "bypassPermissions"})
    grant = await broker.create("chat", ssh_target[0])
    if invalidated == "expired":
        broker.grants[grant["id"]].expires_at = 0
    elif invalidated == "revoked":
        broker.revoke("chat", grant["id"])
    else:
        monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=("other", "worker")))
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], "command", {"command": "never"})
    assert not ssh_target[1] and not broker.actions and not broker.grants


@pytest.mark.asyncio
async def test_bypass_revocation_terminates_running_command(broker, ssh_target, monkeypatch):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": "bypassPermissions"})
    grant = await broker.create("chat", ssh_target[0])
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "hold"}))
    await asyncio.wait_for(ssh_target[2].wait(), 3)
    assert not broker.actions
    broker.revoke("chat", grant["id"])
    with pytest.raises(AccessDenied):
        await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated", ["expired", "revoked", "denied"])
async def test_switch_to_bypass_never_revives_invalid_pending_action(broker, ssh_target, monkeypatch, invalidated):
    row = {"permission_mode": "default"}
    monkeypatch.setattr("app.db.database.get_session", lambda sid: row)
    grant = await broker.create("chat", ssh_target[0])
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "never"}))
    action = await pending(broker)
    if invalidated == "expired":
        action.expires_at = 0
    elif invalidated == "revoked":
        broker.revoke("chat", grant["id"])
    else:
        await broker.decide("chat", action.id, False)
    row["permission_mode"] = "bypassPermissions"
    await broker.resolve_mode_actions("chat")
    with pytest.raises(AccessDenied):
        await asyncio.wait_for(task, 3)
    assert not ssh_target[1] and not broker.actions
    if grant["id"] in broker.grants:
        broker.revoke("chat", grant["id"])


@pytest.mark.asyncio
@pytest.mark.skipif(not chat_files.available(), reason="descriptor-relative Linux folder access required")
@pytest.mark.parametrize("permission_mode", ["default", "acceptEdits", "bypassPermissions"])
async def test_folder_write_confirmation_mode_preserves_scope(broker, tmp_path, monkeypatch, permission_mode):
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {"permission_mode": permission_mode})
    grant = await broker.create("chat", GrantInput(kind="folder", label="Files", path=str(tmp_path), writable=True))
    task = asyncio.create_task(broker.run("chat", grant["id"], "write", {"path": "ok.txt", "content": "written"}))
    if permission_mode != "bypassPermissions":
        action = await pending(broker)
        assert not (tmp_path / "ok.txt").exists()
        await broker.decide("chat", action.id, True)
    await asyncio.wait_for(task, 3)
    assert (tmp_path / "ok.txt").read_text() == "written"
    assert not broker.actions
    # Without the isolated shell a folder grant runs no commands at all.
    from app.core import chat_shell
    monkeypatch.setattr(chat_shell, "self_test", lambda: (False, "test host"))
    for operation, args in [("write", {"path": "../escape", "content": "no"}), ("command", {"command": "no"})]:
        with pytest.raises(AccessDenied):
            await broker.run("chat", grant["id"], operation, args)
    broker.grants[grant["id"]].writable = False
    with pytest.raises(AccessDenied, match="read-only"):
        await broker.run("chat", grant["id"], "write", {"path": "ok.txt", "content": "no"})
    assert (tmp_path / "ok.txt").read_text() == "written"
    broker.revoke("chat", grant["id"])


async def pending(broker):
    # Event-loop scheduling, bounded: not a timing assumption about an SSH handshake.
    async with asyncio.timeout(3):
        while not broker.actions:
            await asyncio.sleep(0.001)
    return next(iter(broker.actions.values()))


@pytest.mark.asyncio
async def test_real_pinned_ssh_needs_exact_one_shot_approval(broker, ssh_target):
    spec, commands, _ = ssh_target
    grant = await broker.create("chat", spec)
    assert commands == []  # Creating a grant authenticates but executes nothing.
    assert "password" not in grant and "key" not in grant
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "fail"}))
    action = await pending(broker)
    assert action.arguments == {"command": "fail"}
    assert commands == []
    with pytest.raises(AccessDenied):
        await broker.decide("other-chat", action.id, True)
    await broker.decide("chat", action.id, True)
    with pytest.raises(AccessDenied):
        await broker.decide("chat", action.id, True)
    result = await task
    assert result == {"stdout": "remote:fail", "stderr": "", "exit_code": 7, "truncated": False}
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "fail"}))
    action = await pending(broker)
    await broker.decide("chat", action.id, False)
    with pytest.raises(AccessDenied):
        await task
    assert commands == ["fail"]
    broker.revoke("chat", grant["id"])


@pytest.mark.asyncio
async def test_wrong_host_key_or_password_never_creates_grant(broker, ssh_target):
    spec, commands, _ = ssh_target
    other = asyncssh.generate_private_key("ssh-ed25519").get_fingerprint("sha256")
    bad = GrantInput.model_validate({**spec.model_dump(), "fingerprint": other})
    with pytest.raises(AccessDenied, match=re.escape(f"offered: ED25519 {spec.fingerprint}")):
        await broker.create("chat", bad)
    bad = GrantInput.model_validate({**spec.model_dump(), "password": "wrong"})
    with pytest.raises(asyncssh.Error):
        await broker.create("chat", bad)
    assert not broker.grants and not commands


@pytest_asyncio.fixture
async def multi_key_target():
    """A server with ED25519 and RSA keys: a client without known_hosts negotiates RSA first."""
    keys = {alg: asyncssh.generate_private_key(alg) for alg in ("ssh-ed25519", "ssh-rsa")}
    auths = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            auths.append(username)
            return True
        def password_auth_supported(self):
            return True
        def validate_password(self, username, password):
            return username == "operator" and password == "test-only-password"

    server = await asyncssh.create_server(Server, "127.0.0.1", 0, server_host_keys=list(keys.values()))
    spec = GrantInput(kind="machine", label="Test SSH", host="127.0.0.1", port=server.get_port(),
                      username="operator", password="test-only-password")
    try:
        yield spec, {alg: key.get_fingerprint("sha256") for alg, key in keys.items()}, auths
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", ["ssh-ed25519", "ssh-rsa"])
async def test_fingerprint_of_any_offered_key_type_is_accepted(broker, multi_key_target, supplied):
    spec, fingerprints, auths = multi_key_target
    grant = await broker.create("chat", spec.model_copy(update={"fingerprint": fingerprints[supplied]}))
    pinned = broker.grants[grant["id"]]
    assert grant["fingerprint"] == fingerprints[supplied]
    assert pinned.host_key_alg == supplied if supplied == "ssh-ed25519" else pinned.host_key_alg.startswith("rsa-")
    async with await broker.connection(pinned):
        pass  # Later connections negotiate the same pinned key type.
    # Only the credentialed connections reach authentication; key probes stop before it.
    assert set(auths) == {"operator"}
    broker.revoke("chat", grant["id"])


@pytest.mark.asyncio
async def test_blank_fingerprint_trusts_and_pins_the_presented_key(broker, multi_key_target):
    spec, fingerprints, auths = multi_key_target
    grant = await broker.create("chat", spec)
    assert grant["fingerprint"] == fingerprints["ssh-ed25519"]
    assert broker.grants[grant["id"]].host_key_alg == "ssh-ed25519"
    assert set(auths) == {"operator"}
    broker.revoke("chat", grant["id"])


@pytest.mark.asyncio
async def test_mismatched_fingerprint_lists_offered_keys_before_any_sign_in(broker, multi_key_target):
    spec, fingerprints, auths = multi_key_target
    other = asyncssh.generate_private_key("ssh-ed25519").get_fingerprint("sha256")
    with pytest.raises(AccessDenied) as denied:
        await broker.create("chat", spec.model_copy(update={"fingerprint": other}))
    assert fingerprints["ssh-ed25519"] in str(denied.value) and fingerprints["ssh-rsa"] in str(denied.value)
    assert not auths and not broker.grants


@pytest.mark.asyncio
async def test_malformed_fingerprint_is_refused_without_connecting(broker, multi_key_target):
    spec, _, auths = multi_key_target
    with pytest.raises(AccessDenied, match="Leave it blank"):
        await broker.create("chat", spec.model_copy(update={"fingerprint": "ab:cd"}))
    assert not auths


@pytest.mark.asyncio
async def test_revoke_cancels_pending_and_running_work(broker, ssh_target):
    spec, commands, started = ssh_target
    grant = await broker.create("chat", spec)
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "hold"}))
    action = await pending(broker)
    await broker.decide("chat", action.id, True)
    await asyncio.wait_for(started.wait(), 3)
    retained = broker.grants[grant["id"]]
    broker.revoke("chat", grant["id"])
    with pytest.raises(AccessDenied):
        await asyncio.wait_for(task, 3)
    assert retained.key is None and retained.password == ""
    assert not broker.grants and not broker.actions
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], "command", {"command": "again"})


@pytest.mark.asyncio
async def test_pending_revoke_expiry_ownership_restart(broker, ssh_target, monkeypatch):
    spec, commands, _ = ssh_target
    grant = await broker.create("chat", spec)
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "never"}))
    await pending(broker)
    broker.revoke("chat", grant["id"])
    with pytest.raises(AccessDenied):
        await task
    for change in ("expired", "moved"):
        grant = await broker.create("chat", spec)
        if change == "expired":
            broker.grants[grant["id"]].expires_at = 0
        else:
            monkeypatch.setattr(chat_access, "session_owner", AsyncMock(return_value=("new", "worker")))
        with pytest.raises(AccessDenied):
            await broker.get("chat", grant["id"])
    assert not broker.grants and not commands
    with pytest.raises(AccessDenied):
        await Broker().get("chat", grant["id"])


@pytest.mark.asyncio
async def test_output_is_bounded(broker, ssh_target):
    spec, _, _ = ssh_target
    grant = await broker.create("chat", spec)
    task = asyncio.create_task(broker.run("chat", grant["id"], "command", {"command": "large"}))
    action = await pending(broker)
    await broker.decide("chat", action.id, True)
    result = await task
    assert result["truncated"] and len(result["stdout"]) == chat_files.LIMIT
    broker.revoke("chat", grant["id"])


@pytest.mark.asyncio
async def test_api_approval_produces_execution_and_validation_hides_secrets(broker, ssh_target, monkeypatch):
    from app.api import chat_access as api
    spec, commands, _ = ssh_target
    monkeypatch.setattr(api, "broker", broker)
    monkeypatch.setattr(api, "checked", AsyncMock())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: "owner"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        requested = asyncio.create_task(broker.request("chat", "machine", "Inspect the server privately"))
        await asyncio.sleep(0)
        request_id = next(iter(broker.requests))
        endpoint = f"/api/v1/chat-access/chat/requests/{request_id}"
        bad = await client.post(endpoint, json={"password": "MUST-NOT-ECHO"})
        assert bad.status_code == 422 and "MUST-NOT-ECHO" not in bad.text
        body = {**spec.model_dump(exclude=set(chat_access.SECRET_FIELDS)), "password": "test-only-password"}
        response = await client.post(endpoint, json={"connection": body})
        assert response.status_code == 200, response.text
        handle = await requested
        assert set(handle) == {"id", "kind", "expires_at", "writable"}
        grant_id = handle["id"]
        assert (await client.post(endpoint, json={"connection": body})).status_code == 409
        task = asyncio.create_task(broker.run("chat", grant_id, "command", {"command": "inspect"}))
        action = await pending(broker)
        response = await client.post(f"/api/v1/chat-access/chat/actions/{action.id}", json={"allow": True})
        assert response.status_code == 200
        assert (await task)["stdout"] == "remote:inspect"
        assert commands == ["inspect"]
        response = await client.delete(f"/api/v1/chat-access/chat/grants/{grant_id}")
        assert response.status_code == 200


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../b", "a//b", "a/./b", "a\\b", "x\x00y", ""])
def test_paths_fail_closed(path):
    with pytest.raises(AccessDenied):
        chat_files.parts(path)


@pytest.mark.skipif(not chat_files.available(), reason="Linux openat2 confinement required")
def test_folder_links_traversal_atomic_writes_and_bounds(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (root / "link").symlink_to(outside)
    os.link(outside, root / "hard")
    (root / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    fd = chat_files.open_root(str(root))
    try:
        for path in ("link", "hard", "dirlink/outside", "../outside"):
            with pytest.raises((AccessDenied, OSError)):
                chat_files.read(fd, path)
            with pytest.raises((AccessDenied, OSError)):
                chat_files.write(fd, path, "bad")
        chat_files.write(fd, "config.txt", "first")
        chat_files.write(fd, "config.txt", "second")
        assert chat_files.read(fd, "config.txt") == "second"
        assert outside.read_text() == "secret"
        assert "config.txt" in chat_files.listing(fd, ".")
        with pytest.raises(AccessDenied):
            chat_files.write(fd, "too-big", "x" * (chat_files.LIMIT + 1))
    finally:
        os.close(fd)


@pytest.mark.skipif(not chat_files.available(), reason="Linux openat2 confinement required")
@pytest.mark.asyncio
async def test_folder_cannot_run_shell_and_writes_need_grant_and_approval(broker, tmp_path, monkeypatch):
    # Without the isolated shell (tests/core/test_chat_shell.py covers it) a
    # folder command is refused; a host with bubblewrap would otherwise wait
    # for approval here.
    from app.core import chat_shell
    monkeypatch.setattr(chat_shell, "self_test", lambda: (False, "test host"))
    grant = await broker.create("chat", GrantInput(kind="folder", label="Folder", path=str(tmp_path)))
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], "command", {"command": "anything"})
    with pytest.raises(AccessDenied):
        await broker.run("chat", grant["id"], "write", {"path": "x", "content": "no"})
    broker.revoke("chat", grant["id"])
    grant = await broker.create("chat", GrantInput(kind="folder", label="Folder", path=str(tmp_path), writable=True))
    task = asyncio.create_task(broker.run("chat", grant["id"], "write", {"path": "x", "content": "yes"}))
    action = await pending(broker)
    assert not (tmp_path / "x").exists()
    await broker.decide("chat", action.id, True)
    assert await task == {"written": True}
    assert (tmp_path / "x").read_text() == "yes"
    broker.revoke("chat", grant["id"])


def test_preview_cannot_grant_or_approve():
    from app.core.preview_access import mutation_allowed
    for path in ("/api/v1/chat-access/chat/grants", "/api/v1/chat-access/chat/actions/action"):
        assert not mutation_allowed(path, "POST")
        assert mutation_allowed(path, "GET")


def test_tool_surface_has_scoped_tools_not_escape_routes():
    from app.core.session_modes import CHAT_MODE_RESOURCE_TOOLS, narrow_tool_access_for_mode
    from app.core.tool_access import ToolAccess, tool_permitted
    from app.core.provider.harness_adapter import _delegated_allowed
    from app.core.provider.types import SessionOptions
    access = narrow_tool_access_for_mode(ToolAccess(), "chat")
    for name in CHAT_MODE_RESOURCE_TOOLS:
        assert tool_permitted(name, access.allowed, access.disallowed)
        denied = narrow_tool_access_for_mode(ToolAccess(disallowed=[name]), "chat")
        assert not tool_permitted(name, denied.allowed, denied.disallowed)
    for name in ("Bash", "Monitor", "Task", "Write", "Edit", "run_python", "mcp__browser__browser_eval", "mcp__app-config__createHook"):
        assert not tool_permitted(name, access.allowed, access.disallowed)
    options = SessionOptions(model="sonnet", tool_allowlist=access.allowed, extra={"session_mode": "chat"})
    granted = _delegated_allowed(options)
    assert set(CHAT_MODE_RESOURCE_TOOLS) <= set(granted)
    assert "Read" not in granted


def test_chat_options_never_resolve_project_or_load_project_settings(tmp_path, monkeypatch):
    from unittest.mock import patch
    import app.core.query_options as qo
    from app.core.session_modes import CHAT_MODE_RESOURCE_TOOLS
    profile = {"id": "p", "config": {"model": "sonnet", "tools_preset": "claude_code", "tool_selection_mode": "all", "setting_sources": ["user", "project", "local"]}}
    row = {"id": "chat", "mode": "chat", "project_v2_id": "stale", "branch": "wrong"}
    monkeypatch.setattr(qo, "ensure_chat_uploads_dir", lambda: tmp_path)
    with patch.object(qo.database, "get_session", return_value=row), patch.object(qo, "get_security_instructions", return_value=""), patch("app.core.project_resolver.resolve_cwd") as resolve, patch.object(qo, "_rescue_pending_checkout") as rescue:
        options, _ = qo.build_options_from_profile(profile=profile, session_id="chat", project={"id": "stale", "path": "/never"}, overrides={"cwd": "/also-never"})
    resolve.assert_not_called()
    rescue.assert_not_called()
    assert str(options.cwd) == str(tmp_path)
    assert options.setting_sources == []
    assert options.plugins == []
    assert not isinstance(options.system_prompt, dict)
    assert "general-purpose assistant" in options.system_prompt
    assert set(options.mcp_servers) == {"builtins", "chat-access"}
    assert set(CHAT_MODE_RESOURCE_TOOLS) <= set(options.allowed_tools)
    assert "Bash" not in options.allowed_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "code", "chat", "voice"])
@pytest.mark.parametrize("permission_mode", ["default", "acceptEdits", "auto", "bypassPermissions"])
async def test_sdk_handler_obeys_session_mode_after_private_grant(broker, ssh_target, monkeypatch, mode, permission_mode):
    from app.core.chat_access import GrantSubmission
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.db import database
    from app.mcp import chat_access_sdk as sdk
    from app.network import control
    monkeypatch.setattr(sdk, "broker", broker)
    monkeypatch.setattr(chat_access, "session_owner", _real_session_owner)
    monkeypatch.setattr(database, "get_session", lambda sid: {
        "mode": mode, "instance_id": "worker", "permission_mode": permission_mode,
    })
    monkeypatch.setattr(control, "owner_of", lambda row: None)
    spec, commands, _ = ssh_target
    tools = {t.name: t for t in recorded_sdk_server_tools(sdk.create_chat_access_server("chat"))}
    request_task = asyncio.create_task(tools["RequestChatAccess"].handler({"kind": "machine", "purpose": "Inspect server"}))
    async with asyncio.timeout(3):
        while not broker.requests:
            await asyncio.sleep(0)
    assert not request_task.done() and not broker.grants and not commands
    await broker.fulfill("chat", next(iter(broker.requests)), GrantSubmission(connection=spec))
    response = await request_task
    assert "test-only-password" not in str(response)
    grant_id = next(iter(broker.grants))
    task = asyncio.create_task(tools["ChatRunCommand"].handler({"grant_id": grant_id, "command": "sdk"}))
    if permission_mode != "bypassPermissions" or mode == "chat":
        action = await pending(broker)
        assert commands == []
        await broker.decide("chat", action.id, True)
    response = await asyncio.wait_for(task, 3)
    assert "remote:sdk" in response["content"][0]["text"]
    assert not broker.actions
    other = {t.name: t for t in recorded_sdk_server_tools(sdk.create_chat_access_server("other"))}
    response = await other["ChatRunCommand"].handler({"grant_id": grant_id, "command": "escape"})
    assert response["isError"] and commands == ["sdk"]
    broker.revoke("chat", grant_id)


@pytest.mark.asyncio
async def test_private_request_decline_cancel_and_cross_session(broker, ssh_target):
    from app.core.chat_access import GrantSubmission
    task = asyncio.create_task(broker.request("chat", "machine", "Need server access"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    with pytest.raises(AccessDenied):
        await broker.request("chat", "machine", "Duplicate")
    with pytest.raises(AccessDenied):
        await broker.fulfill("other", request.id, GrantSubmission(connection=ssh_target[0]))
    await broker.decline("chat", request.id)
    with pytest.raises(AccessDenied, match="declined"):
        await task
    assert not broker.grants and not broker.requests
    task = asyncio.create_task(broker.request("chat", "machine", "Need server access"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(AccessDenied):
        await broker.fulfill("chat", request.id, GrantSubmission(connection=ssh_target[0]))
    assert not broker.grants and not broker.requests


@pytest.mark.asyncio
async def test_saved_connection_requires_new_request_and_approval(broker, ssh_target, monkeypatch):
    from app.core import chat_vault
    from app.core.chat_access import GrantSubmission
    spec, commands, _ = ssh_target
    saved = {}
    def save(owner, value):
        saved["c" * 32] = value
        return "c" * 32
    monkeypatch.setattr(chat_vault, "save", save)
    monkeypatch.setattr(chat_vault, "load", lambda owner, saved_id: saved[saved_id])
    first = asyncio.create_task(broker.request("chat", "machine", "Initial access"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    blank = spec.model_copy(update={"fingerprint": ""})
    await broker.fulfill("chat", request.id, GrantSubmission(connection=blank, save_connection=True))
    handle = await first
    # A blank fingerprint saves the key that was actually pinned.
    assert saved["c" * 32].fingerprint == spec.fingerprint
    assert set(handle) == {"id", "kind", "expires_at", "writable"}
    broker.revoke("chat", handle["id"])
    assert saved and not broker.grants
    second = asyncio.create_task(broker.request("chat", "machine", "Reuse saved server"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    assert not broker.grants and commands == []
    await broker.fulfill("chat", request.id, GrantSubmission(saved_id="c" * 32))
    next_handle = await second
    assert next_handle["id"] != handle["id"] and commands == []
    broker.revoke("chat", next_handle["id"])


@pytest.mark.asyncio
async def test_saved_list_changes_invalidate_every_owner_session(broker, ssh_target, monkeypatch):
    """Saving or deleting a saved connection refreshes the owner's panels on other sessions too."""
    from app.api import chat_access as api
    from app.core import chat_vault, preference_identity
    from app.core.chat_access import GrantSubmission
    from app.core.global_events import global_events
    from app.core.sync_engine import sync_engine
    owner = AsyncMock(return_value=(None, "worker", None))
    monkeypatch.setattr(chat_access, "session_owner", owner)
    published = []
    monkeypatch.setattr(global_events, "resources_changed",
                        lambda *names, user_type, user_id: published.append((names, user_type, user_id)))
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda principal: ("admin", "owner"))
    monkeypatch.setattr(sync_engine, "_broadcast_user_message", AsyncMock())
    monkeypatch.setattr(chat_vault, "save", lambda owner, value: "c" * 32)
    monkeypatch.setattr(chat_vault, "load", lambda owner, saved_id: ssh_target[0])
    monkeypatch.setattr(chat_vault, "delete", lambda owner, saved_id: None)

    async def drain():
        await asyncio.gather(*broker._notifications)

    requested = asyncio.create_task(broker.request("chat", "machine", "Save this server"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    await broker.fulfill("chat", request.id, GrantSubmission(connection=ssh_target[0], save_connection=True))
    handle = await requested
    await drain()
    assert (("chat-access:chat", "chat-access"), "admin", "owner") in published
    broker.revoke("chat", handle["id"])
    await drain()
    # Grant-only changes stay scoped to their session.
    assert published[-1] == (("chat-access:chat",), "admin", "owner")

    published.clear()
    monkeypatch.setattr(api, "broker", broker)
    monkeypatch.setattr(api, "checked", AsyncMock(return_value={"id": "chat"}))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.require_admin] = lambda: "owner"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
        response = await client.delete(f"/api/v1/chat-access/chat/saved/{'c' * 32}")
    assert response.status_code == 200
    await drain()
    assert published == [(("chat-access:chat", "chat-access"), "admin", "owner")]


@pytest.mark.asyncio
async def test_deleted_saved_connection_cannot_race_into_a_new_grant(broker, ssh_target, monkeypatch):
    from app.core import chat_vault
    from app.core.chat_access import GrantSubmission
    calls = 0
    def load(owner, saved_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ssh_target[0]
        raise AccessDenied("Connection was deleted during the handshake")
    monkeypatch.setattr(chat_vault, "load", load)
    requested = asyncio.create_task(broker.request("chat", "machine", "Reuse a saved connection"))
    await asyncio.sleep(0)
    request = next(iter(broker.requests.values()))
    with pytest.raises(AccessDenied, match="deleted"):
        await broker.fulfill("chat", request.id, GrantSubmission(saved_id="a" * 32))
    assert not broker.grants and not ssh_target[1]
    await broker.decline("chat", request.id)
    with pytest.raises(AccessDenied):
        await requested


@pytest.mark.asyncio
async def test_private_inputs_require_secure_transport_and_real_owner(monkeypatch):
    from starlette.requests import Request
    from fastapi import HTTPException
    from app.api import chat_access as api
    monkeypatch.setattr(api, "require_session", AsyncMock())
    def request(scheme, peer, host, preview=False):
        scope = {"type": "http", "scheme": scheme, "method": "GET", "path": "/",
                 "headers": [(b"host", host.encode())], "server": (host, 443),
                 "client": (peer, 1234), "shuffle.ingress": "local", "state": {}}
        if preview:
            scope["state"]["preview_grant"] = object()
        return Request(scope)
    with pytest.raises(HTTPException) as error:
        await api.checked(request("http", "10.0.0.2", "10.0.0.1"), "chat")
    assert error.value.status_code == 403
    await api.checked(request("http", "127.0.0.1", "localhost"), "chat")
    await api.checked(request("https", "10.0.0.2", "10.0.0.1"), "chat")
    with pytest.raises(HTTPException):
        await api.checked(request("https", "10.0.0.2", "10.0.0.1", preview=True), "chat")


_DESKTOP_TOKEN = "ab" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize("case,scheme,port,peer,host,headers,desktop,accepted", [
    # The desktop app window carries its per-launch listener token.
    ("desktop window", "http", 8000, "127.0.0.1", "127.0.0.1:8000",
     [(b"x-shuffle-desktop-listener", _DESKTOP_TOKEN.encode())], True, True),
    # A separate browser tab on localhost beside the desktop app has no token.
    ("chrome on loopback", "http", 8000, "127.0.0.1", "localhost:8000", [], True, False),
    ("wrong desktop token", "http", 8000, "127.0.0.1", "localhost:8000",
     [(b"x-shuffle-desktop-listener", b"cd" * 32)], True, False),
    ("loopback without desktop", "http", 8000, "127.0.0.1", "localhost:8000", [], False, True),
    ("plaintext LAN", "http", 8000, "10.0.0.2", "10.0.0.1:8000", [], False, False),
    ("LAN forged proto", "http", 8000, "10.0.0.2", "10.0.0.1:8000",
     [(b"x-forwarded-proto", b"https")], False, False),
    ("loopback forged proto", "http", 8000, "127.0.0.1", "localhost:8000",
     [(b"x-forwarded-proto", b"https")], False, False),
    # Managed tunnel: cloudflared reaches only the loopback remote listener.
    ("managed tunnel", "http", 8001, "127.0.0.1", "shuffle-x.hobbycoders.com",
     [(b"x-forwarded-proto", b"https")], False, True),
    ("managed tunnel beside desktop", "http", 8001, "127.0.0.1", "shuffle-x.hobbycoders.com",
     [(b"x-forwarded-proto", b"https")], True, True),
    ("tunnel visitor on http", "http", 8001, "127.0.0.1", "shuffle-x.hobbycoders.com",
     [(b"x-forwarded-proto", b"http")], False, False),
    ("tunnel without proto", "http", 8001, "127.0.0.1", "shuffle-x.hobbycoders.com", [], False, False),
    ("unknown socket", "http", 9999, "127.0.0.1", "localhost:9999",
     [(b"x-forwarded-proto", b"https")], False, False),
    ("TLS socket", "https", 8443, "10.0.0.2", "worker.example.com", [], False, True),
])
async def test_private_access_origins_through_real_ingress(monkeypatch, case, scheme, port, peer,
                                                           host, headers, desktop, accepted):
    """Transport trust comes from the accepting socket, never a client header."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.api import chat_access as api
    from app.core import desktop_listener
    from app.serve import IngressApplication

    monkeypatch.setattr(api, "require_session", AsyncMock(return_value={"id": "chat"}))
    monkeypatch.setattr(desktop_listener, "_configured", desktop)
    monkeypatch.setattr(desktop_listener, "_token", _DESKTOP_TOKEN if desktop else None)
    seen = []

    async def capture(scope, receive, send):
        seen.append(scope)

    # A client cannot pre-set the flag: the wrapper recomputes it per socket.
    await IngressApplication(capture, 8000, 8001)({
        "type": "http", "scheme": scheme, "method": "GET", "path": "/api/v1/chat-access/chat",
        "headers": [(b"host", host.encode()), *headers], "server": ("127.0.0.1", port),
        "client": (peer, 1234), "shuffle.forwarded_https": True, "state": {}}, None, None)
    request = Request(seen[0])
    if accepted:
        assert await api.checked(request, "chat") == {"id": "chat"}, case
        return
    with pytest.raises(HTTPException) as error:
        await api.checked(request, "chat")
    assert error.value.status_code == 403, case
    # Actionable for the common case of a browser tab beside the desktop app.
    assert "desktop app window" in error.value.detail
    assert "HTTPS remote access address" in error.value.detail
    api.require_session.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {"mode": "unexpected"}, {"mode": "voice", "owner_device_id": "peer"}])
async def test_private_access_still_rejects_missing_unknown_or_mirrored_session(monkeypatch, row):
    from app.db import database
    from app.network import control
    monkeypatch.setattr(database, "get_session", lambda sid: row)
    monkeypatch.setattr(control, "owner_of", lambda r: r.get("owner_device_id"))
    with pytest.raises(AccessDenied):
        await _real_session_owner("s")


@pytest.mark.parametrize("mode", [None, "code", "voice"])
def test_code_and_voice_options_include_private_tools_without_chat_restrictions(tmp_path, monkeypatch, mode):
    from unittest.mock import patch
    import app.core.query_options as qo
    from app.core.session_modes import CHAT_MODE_RESOURCE_TOOLS
    profile = {"id": "p", "config": {"model": "sonnet", "tools_preset": "claude_code", "tool_selection_mode": "all", "cwd": str(tmp_path)}}
    with patch.object(qo.database, "get_session", return_value={"id": "s", "mode": mode}), patch.object(qo, "get_security_instructions", return_value=""):
        options, _ = qo.build_options_from_profile(profile=profile, session_id="s", overrides={"cwd": str(tmp_path)})
    assert "chat-access" in options.mcp_servers
    assert set(CHAT_MODE_RESOURCE_TOOLS) <= set(options.allowed_tools)
    assert "private input card" in str(options.system_prompt)
    assert "For repository development, ask the user to switch" not in str(options.system_prompt)
    assert options.tools == {"type": "preset", "preset": "claude_code"}
    profile["config"].update(tool_selection_mode="disallow", disallowed_tools=CHAT_MODE_RESOURCE_TOOLS)
    with patch.object(qo.database, "get_session", return_value={"id": "s", "mode": mode}), patch.object(qo, "get_security_instructions", return_value=""):
        denied, _ = qo.build_options_from_profile(profile=profile, session_id="s", overrides={"cwd": str(tmp_path)})
    assert "chat-access" not in denied.mcp_servers
    assert not set(CHAT_MODE_RESOURCE_TOOLS).intersection(denied.allowed_tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("permission_mode", ["default", "bypassPermissions"])
async def test_voice_bridge_private_grant_and_mode_confirmation(broker, ssh_target, monkeypatch, permission_mode):
    from app.core.chat_access import GrantSubmission
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.core.voice_bridge import VoiceBridge
    from app.mcp import chat_access_sdk as sdk
    monkeypatch.setattr(sdk, "broker", broker)
    monkeypatch.setattr("app.db.database.get_session", lambda sid: {
        "mode": "voice", "permission_mode": permission_mode,
    })
    bridge = VoiceBridge("voice", "test", api_key="unused", model="gpt-realtime", max_call_minutes=1,
                         app_tools=recorded_sdk_server_tools(sdk.create_chat_access_server("voice")))
    task = asyncio.create_task(bridge._tool_app("RequestChatAccess", {"kind":"machine", "purpose":"Inspect server"}))
    await asyncio.sleep(0)
    assert not task.done() and not broker.grants
    spec, commands, _ = ssh_target
    request_id = next(iter(broker.requests))
    await broker.fulfill("voice", request_id, GrantSubmission(connection=spec))
    response = await task
    assert "test-only-password" not in str(response)
    grant_id = next(iter(broker.grants))
    task = asyncio.create_task(bridge._tool_app("ChatRunCommand", {"grant_id":grant_id, "command":"voice"}))
    if permission_mode == "default":
        action = await pending(broker)
        assert not task.done() and not commands
        await broker.decide("voice", action.id, False)
        assert "denied" in str(await task)
        assert not commands
    else:
        assert "remote:voice" in str(await asyncio.wait_for(task, 3))
        assert commands == ["voice"] and not broker.actions
    broker.revoke("voice", grant_id)


def _listing_tools(monkeypatch, broker):
    """ChatResources over the real owner lookup, a stubbed session row and no UI."""
    from app.core import chat_vault
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.db import database
    from app.mcp import chat_access_sdk as sdk
    from app.network import control
    monkeypatch.setattr(chat_access, "session_owner", _real_session_owner)
    monkeypatch.setattr(database, "get_session", lambda sid: {"mode": "chat", "instance_id": "worker"})
    monkeypatch.setattr(control, "owner_of", lambda row: None)
    monkeypatch.setattr(broker, "_notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(sdk, "broker", broker)

    def no_catalog(*args, **kwargs):
        raise AssertionError("ChatResources must not read the saved-connection catalogue")
    monkeypatch.setattr(chat_vault, "catalog", no_catalog)
    return {t.name: t for t in recorded_sdk_server_tools(sdk.create_chat_access_server("chat"))}


@pytest.mark.asyncio
async def test_chat_resources_answers_while_request_pending_and_shared_pool_is_busy(broker, monkeypatch):
    """#1605: the listing must not queue behind the shared default executor.

    A pending RequestChatAccess waits up to 240 s, and every run_sync caller
    in the process shares asyncio's default executor. With that executor
    fully occupied, ChatResources still answers promptly, asks for nothing
    and leaves the pending request untouched.
    """
    import concurrent.futures
    import json
    import threading

    tools = _listing_tools(monkeypatch, broker)
    request_task = asyncio.create_task(
        tools["RequestChatAccess"].handler({"kind": "machine", "purpose": "Inspect server"}))
    async with asyncio.timeout(3):
        while not broker.requests:
            await asyncio.sleep(0.001)

    asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
    release = threading.Event()
    blocker = asyncio.create_task(asyncio.to_thread(release.wait, 30))
    try:
        await asyncio.sleep(0.05)  # the blocker now holds the only shared worker
        response = await asyncio.wait_for(tools["ChatResources"].handler({}), 2)
        assert not response.get("isError"), response
        assert json.loads(response["content"][0]["text"]) == {
            "grants": [], "folder_available": chat_files.available(),
        }
        assert not request_task.done() and len(broker.requests) == 1 and not broker.actions
    finally:
        release.set()
        await blocker
    await broker.decline("chat", next(iter(broker.requests)))
    assert (await request_task)["isError"]


@pytest.mark.asyncio
async def test_chat_resources_returns_bounded_error_when_owner_lookup_stalls(broker, monkeypatch):
    import threading
    from app.db import database

    tools = _listing_tools(monkeypatch, broker)
    release = threading.Event()
    monkeypatch.setattr(database, "get_session", lambda sid: release.wait(30) and {"mode": "chat"})
    monkeypatch.setattr(chat_access, "LISTING_TIMEOUT_SECONDS", 0.2)
    try:
        response = await asyncio.wait_for(tools["ChatResources"].handler({}), 3)
    finally:
        release.set()
    assert response["isError"]
    text = response["content"][0]["text"]
    assert "timed out after 0.2 seconds" in text and "No access was requested" in text
    assert not broker.requests and not broker.actions


@pytest.mark.asyncio
async def test_chat_resources_lists_only_live_grant_metadata(broker, monkeypatch, tmp_path):
    import json
    import time

    tools = _listing_tools(monkeypatch, broker)
    owner = (None, "worker", None)
    live = chat_access.Grant("a" * 32, "chat", owner, "Private label", "folder",
                             str(tmp_path), time.time() + 60, writable=True)
    expired = chat_access.Grant("b" * 32, "chat", owner, "Old", "folder", str(tmp_path), time.time() - 1)
    other = chat_access.Grant("c" * 32, "other", owner, "Other", "folder", str(tmp_path), time.time() + 60)
    for grant in (live, expired, other):
        broker.grants[grant.id] = grant
    response = await asyncio.wait_for(tools["ChatResources"].handler({}), 2)
    text = response["content"][0]["text"]
    assert json.loads(text)["grants"] == [
        {"id": live.id, "kind": "folder", "expires_at": live.expires_at, "writable": True}]
    assert expired.id not in broker.grants and other.id in broker.grants
    assert "Private label" not in text and str(tmp_path) not in text


@pytest.mark.asyncio
async def test_chat_resources_logs_stage_timings_at_info_without_details(broker, monkeypatch, tmp_path, caplog):
    """A prompt listing still leaves a timing line at the default INFO level, so
    a stall outside the listing can be told apart from one inside it (#1605)."""
    import logging
    import time

    tools = _listing_tools(monkeypatch, broker)
    grant = chat_access.Grant("d" * 32, "chat", (None, "worker", None), "Private label", "folder",
                              str(tmp_path), time.time() + 60)
    broker.grants[grant.id] = grant
    with caplog.at_level(logging.INFO, logger="app.core.chat_access"):
        await asyncio.wait_for(tools["ChatResources"].handler({}), 2)
    lines = [r for r in caplog.records if r.getMessage().startswith("ChatResources listing:")]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    text = lines[0].getMessage()
    assert "grants=1" in text and "owner_lookup=" in text and "total=" in text
    assert "Private label" not in text and str(tmp_path) not in text
    broker.revoke("chat", grant.id)
