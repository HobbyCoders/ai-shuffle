"""Resource authorization, real SSH transport, one-shot approval, and file fences."""
import asyncio
import os
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
    for updates in ({"fingerprint": other}, {"password": "wrong"}):
        bad = GrantInput.model_validate({**spec.model_dump(), **updates})
        with pytest.raises(asyncssh.Error):
            await broker.create("chat", bad)
    assert not broker.grants and not commands


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
        body = {**spec.model_dump(exclude={"password", "private_key", "passphrase"}), "password": "test-only-password"}
        response = await client.post(endpoint, json={"connection": body})
        assert response.status_code == 200, response.text
        handle = await requested
        assert set(handle) == {"id", "kind", "expires_at", "writable"}
        grant_id = handle["id"]
        assert (await client.post(endpoint, json={"connection": body})).status_code == 400
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
async def test_folder_cannot_run_shell_and_writes_need_grant_and_approval(broker, tmp_path):
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
async def test_sdk_handler_uses_broker_not_runtime_autoapproval(broker, ssh_target, monkeypatch, mode):
    from app.mcp import chat_access_sdk as sdk
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    monkeypatch.setattr(sdk, "broker", broker)
    from app.db import database
    from app.network import control
    # Restore the real admission check and exercise each stored lane, including
    # legacy NULL Code sessions. Bypass never enters the broker's decision path.
    monkeypatch.setattr(chat_access, "session_owner", _real_session_owner)
    monkeypatch.setattr(database, "get_session", lambda sid: {
        "mode": mode, "instance_id": "worker", "permission_mode": "bypassPermissions",
    })
    monkeypatch.setattr(control, "owner_of", lambda row: None)
    spec, commands, _ = ssh_target
    grant = await broker.create("chat", spec)
    tools = {t.name: t for t in recorded_sdk_server_tools(sdk.create_chat_access_server("chat"))}
    task = asyncio.create_task(tools["ChatRunCommand"].handler({"grant_id": grant["id"], "command": "sdk"}))
    action = await pending(broker)
    assert commands == []
    await broker.decide("chat", action.id, True)
    response = await task
    assert "remote:sdk" in response["content"][0]["text"]
    other = {t.name: t for t in recorded_sdk_server_tools(sdk.create_chat_access_server("other"))}
    response = await other["ChatRunCommand"].handler({"grant_id": grant["id"], "command": "escape"})
    assert response["isError"] and commands == ["sdk"]
    broker.revoke("chat", grant["id"])


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
    await broker.fulfill("chat", request.id, GrantSubmission(connection=spec, save_connection=True))
    handle = await first
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
async def test_voice_bridge_private_request_and_command_wait_for_card_even_in_bypass(broker, ssh_target, monkeypatch):
    from app.core.voice_bridge import VoiceBridge
    from app.mcp import chat_access_sdk as sdk
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.core.chat_access import GrantSubmission
    monkeypatch.setattr(sdk, "broker", broker)
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
    action = await pending(broker)
    assert not task.done() and not commands
    await broker.decide("voice", action.id, False)
    assert "denied" in str(await task)
    assert not commands
    broker.revoke("voice", grant_id)
