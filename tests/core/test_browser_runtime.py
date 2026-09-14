"""Shared MCP process coalescing, transparent results and lifetime isolation."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import browser_control as control, browser_runtime as runtime
from app.core import browser_panel_registry as registry


@pytest.fixture
async def fake(monkeypatch):
    clients = []

    class Client:
        def __init__(self, name, config):
            self.closed = False
            self.runtime_id = config["env"]["_AIS_BROWSER_RUNTIME_ID"]
            self.tools = []
            self.fail = False
            clients.append(self)

        async def start(self):
            await asyncio.sleep(0)

        async def list_tools(self):
            return [SimpleNamespace(name=name) for name in runtime.HANDSHAKE_TOOLS]

        async def call_tool(self, tool, arguments):
            self.tools.append((tool, arguments))
            if self.fail:
                raise ConnectionError("secret must not appear in response")
            if tool == "browser_status" and len(self.tools) == 1:
                return {"content": [{"type": "text", "text": json.dumps({"runtime_id": self.runtime_id})}]}
            if tool == "browser_tabs":
                broker = control.get("runtime-test")
                rid = self.runtime_id
                await broker.register(rid)
                state = control.unavailable(rid)
                state.update(available=True, target_id="target", active_tab_id="tab", tabs=[{
                    "id": "tab", "url": "about:blank", "title": "", "current": True,
                }])
                await broker.report(rid, state, [])
            return {"content": [{"type": "image", "data": "YQ==", "mimeType": "image/png"}], "isError": True, "structuredContent": {"x": 1}}

        async def aclose(self):
            self.closed = True

    # Bootstrap succeeds, subsequent calls retain their error/image payload.
    original = Client.call_tool
    async def call_tool(self, tool, arguments):
        result = await original(self, tool, arguments)
        if tool == "browser_tabs":
            result["isError"] = False
        return result
    Client.call_tool = call_tool
    monkeypatch.setattr(runtime, "RawMcpClient", Client)
    monkeypatch.setattr(runtime, "resolve_config", lambda sid: {})
    from app.core import native_chrome
    monkeypatch.setattr(native_chrome, "ensure_started", AsyncMock())
    yield clients
    await runtime.shutdown()
    registry.clear("runtime-test")


@pytest.mark.asyncio
async def test_concurrent_start_and_agent_call_coalesce(fake):
    results = await asyncio.gather(*[runtime.start("runtime-test") for _ in range(8)], runtime.call("runtime-test", "browser_status", {}))
    assert len(fake) == 1
    assert [t for t, _ in fake[0].tools].count("browser_tabs") == 1
    assert all(s["runtime_id"] == fake[0].runtime_id for s in results[:-1])
    assert results[-1]["content"][0]["type"] == "image"
    assert results[-1]["isError"] is True
    assert not fake[0].closed
    from app.core import native_chrome
    native_chrome.ensure_started.assert_awaited_once()


@pytest.mark.asyncio
async def test_reuse_does_not_enter_agent_admission(fake):
    await runtime.start("runtime-test")
    broker = control.get("runtime-test")
    broker.state["control"].update(owner="human", viewer_id="viewer")
    state = await runtime.start("runtime-test")
    assert state["control"]["owner"] == "human"
    assert fake[0].tools == [("browser_status", {}), ("browser_tabs", {"op": "list"}), ("browser_status", {})]


@pytest.mark.asyncio
async def test_failure_not_replayed_then_recovers(fake):
    await runtime.start("runtime-test")
    fake[0].fail = True
    with pytest.raises(runtime.RuntimeUnavailable, match="outcome unknown"):
        await runtime.call("runtime-test", "browser_navigate", {"url": "https://example.test"})
    assert fake[0].closed
    assert len(fake[0].tools) == 3
    assert (await runtime.start("runtime-test"))["runtime_id"] == fake[1].runtime_id
    assert fake[1].runtime_id != fake[0].runtime_id


@pytest.mark.asyncio
async def test_delete_and_shutdown_close_actor_and_token(fake):
    await runtime.start("runtime-test")
    token = registry.issue_token("runtime-test")
    await runtime.close_session("runtime-test")
    assert fake[0].closed and not registry.verify_token("runtime-test", token)
    assert "runtime-test" not in runtime._runtimes
    await runtime.start("runtime-test")
    await runtime.shutdown()
    assert fake[1].closed
    from app.core import native_chrome
    assert native_chrome.ensure_started.await_count == 2


@pytest.mark.asyncio
async def test_idle_keeps_viewers_and_active_calls(fake, monkeypatch):
    await runtime.start("runtime-test")
    monkeypatch.setattr(runtime, "IDLE_SECONDS", 0.01)
    instance = runtime.get("runtime-test")
    # Start the short idle clock after initialization, not during thread startup.
    instance.idle.cancel()
    await asyncio.gather(instance.idle, return_exceptions=True)
    instance.idle = asyncio.create_task(instance._idle())
    broker = control.get("runtime-test")
    broker.viewers["viewer"] = AsyncMock()
    instance.touched = time.monotonic() - 1
    await asyncio.sleep(0.03)
    assert not fake[0].closed
    broker.viewers.clear()
    instance.active = 1
    await asyncio.sleep(0.03)
    assert not fake[0].closed
    instance.active = 0
    await asyncio.sleep(0.03)
    assert fake[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_returns", [False, True])
async def test_idle_grace_starts_after_last_viewer_and_reopens(fake, monkeypatch, agent_returns):
    """Drive attach/detach and actor retirement at the real 900s boundary.

    Only the monotonic clock and timer wait are virtual; the broker release,
    actor disposal, registry removal and next start run their production paths.
    """
    # Integer virtual timestamps make the exact boundary representable on all
    # hosts; adding offsets to a fractional uptime can subtract to 899.999999s.
    now = [1000.0]
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(control, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    await runtime.start("runtime-test")
    instance = runtime.get("runtime-test")
    instance.idle.cancel()
    await asyncio.gather(instance.idle, return_exceptions=True)
    broker = control.get("runtime-test")

    async def tick(at):
        # The actor keeps long-polling even without viewers. Feed that real
        # producer in <=20s increments so its separate broker TTL stays valid.
        while now[0] < at:
            now[0] = min(at, now[0] + 20)
            broker._viewers_changed = True
            await broker.poll(broker.runtime_id)
        waits = 0

        async def one_tick(delay):
            nonlocal waits
            assert delay == 30  # keep the production 900s retention setting
            waits += 1
            if waits > 1:
                raise asyncio.CancelledError

        with monkeypatch.context() as patch:
            patch.setattr(runtime, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": one_tick})))
            await instance._idle()

    started = now[0]
    broker.attach("first", AsyncMock())
    broker.attach("last", AsyncMock())
    await tick(started + 1800)
    assert not instance.closed  # a long-lived viewer is never idle
    await broker.detach("first")
    await tick(started + 2700)
    assert not instance.closed
    await broker.detach("last")
    detached = now[0]
    # Disconnect must retain its release command, not silently steal control.
    assert any(c["op"] == "release" for c in broker.queue)
    await tick(detached + 899)
    assert not instance.closed
    # A duplicate/unknown disconnect cannot extend the last real viewer lease.
    await broker.detach("last")
    if agent_returns:
        await runtime.call("runtime-test", "browser_status", {})
        await tick(detached + 900)
        assert not instance.closed  # real tool completion restarts the grace
        await tick(detached + 1799)
    else:
        await tick(detached + 900)
    assert instance.closed and fake[0].closed
    assert "runtime-test" not in runtime._runtimes
    assert not broker.state["available"]
    assert (await runtime.start("runtime-test"))["runtime_id"] == fake[1].runtime_id
    assert fake[1].runtime_id != fake[0].runtime_id


@pytest.mark.asyncio
async def test_invalid_tool_and_nonfinite_arguments_cannot_spawn(fake):
    for tool, arguments in [("shell", {}), ("browser_act", {"x": float("nan")})]:
        with pytest.raises(ValueError):
            await runtime.call("runtime-test", tool, arguments)
    assert not fake


@pytest.mark.asyncio
async def test_raw_client_preserves_mcp_schema_images_and_errors():
    from mcp.types import CallToolResult
    payload = {"content": [{"type": "image", "data": "YQ==", "mimeType": "image/png"}], "isError": True, "structuredContent": {"exact": [1, 2]}}
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult.model_validate(payload)
    client = runtime.RawMcpClient("browser", {})
    assert await client._do_call(session, "browser_screenshot", {}) == payload


def test_cold_config_uses_session_project_profile_policy_and_direct_mode(monkeypatch, tmp_path):
    from app.core import environment, mcp_config, profiles, project_resolver
    from app.db import database
    row = {"id": "config-session", "profile_id": "profile", "provider_id": "provider", "model": "model", "api_user_id": "user"}
    monkeypatch.setattr(database, "get_session", lambda sid: row)
    monkeypatch.setattr(profiles, "get_profile", lambda pid: {"config": {"env": {"BROWSER_SSRF_BLOCK": "10.0.0.0/8"}, "ai_tools": {}}})
    monkeypatch.setattr(project_resolver, "resolve_cwd", lambda session: str(tmp_path))
    def env(base, ai, user, provider):
        assert user == "user" and provider == "provider"
        return dict(base)
    monkeypatch.setattr(environment, "_build_env_with_ai_tools", env)
    def overlay(env, provider, model, session_id):
        assert (provider, model, session_id) == ("provider", "model", "config-session")
        env["BROWSER_UPLOAD_DIR"] = str(tmp_path)
    monkeypatch.setattr(environment, "apply_provider_env", overlay)
    def servers(project, env, sid):
        assert project == tmp_path and sid == "config-session"
        assert env["BROWSER_SSRF_BLOCK"] == "10.0.0.0/8"
        return {"browser": {"command": "custom-node", "args": ["custom-entry.js"], "env": {**env, "AIS_BROWSER_MODE": "proxy"}}}
    monkeypatch.setattr(mcp_config, "_build_mcp_servers_dict", servers)
    result = runtime.resolve_config("config-session")
    assert result["command"] == "custom-node"
    assert result["args"] == ["custom-entry.js"]
    assert result["env"]["AIS_BROWSER_MODE"] == "runtime"
    assert result["env"]["BROWSER_UPLOAD_DIR"] == str(tmp_path)
    assert result["cwd"] == str(tmp_path)


def test_proxy_mode_only_with_session_wiring():
    from app.core.mcp_config import _build_browser_server_env
    assert "AIS_BROWSER_MODE" not in _build_browser_server_env({}, None)
    assert _build_browser_server_env({}, "mode-test")["AIS_BROWSER_MODE"] == "proxy"
    registry.clear("mode-test")


@pytest.mark.asyncio
async def test_existing_foreign_runtime_is_not_superseded(fake):
    broker = control.get("runtime-test")
    await broker.register("existing")
    with pytest.raises(runtime.RuntimeUnavailable):
        await runtime.start("runtime-test")
    assert broker.runtime_id == "existing"
    assert not fake


@pytest.mark.asyncio
async def test_cancelled_proxy_request_does_not_cancel_admitted_call(fake):
    await runtime.start("runtime-test")
    instance = runtime.get("runtime-test")
    entered, release = asyncio.Event(), asyncio.Event()
    original = fake[0].call_tool
    async def slow(tool, args):
        entered.set()
        await release.wait()
        return await original(tool, args)
    fake[0].call_tool = slow
    request = asyncio.create_task(runtime.call("runtime-test", "browser_status", {}))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert instance.active == 1 and not fake[0].closed
    release.set()
    await asyncio.gather(*instance.calls)
    assert instance.active == 0 and not fake[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "status"])
async def test_registered_child_failure_allows_immediate_retry(fake, monkeypatch, phase):
    client_type = runtime.RawMcpClient
    original_start, original_call = client_type.start, client_type.call_tool

    async def start(self):
        await control.get("runtime-test").register(self.runtime_id)
        if len(fake) == 1 and phase == "start":
            raise ConnectionError("initialization failed")
        await original_start(self)

    async def call(self, tool, arguments):
        if len(fake) == 1 and phase == "status" and tool == "browser_status":
            raise ConnectionError("status failed")
        return await original_call(self, tool, arguments)

    monkeypatch.setattr(client_type, "start", start)
    monkeypatch.setattr(client_type, "call_tool", call)
    with pytest.raises(runtime.RuntimeUnavailable):
        await runtime.start("runtime-test")
    assert fake[0].closed
    broker = control.get("runtime-test")
    assert broker.runtime_id is None
    assert fake[0].runtime_id in broker.retired
    state = await runtime.start("runtime-test")
    assert state["runtime_id"] == fake[1].runtime_id
    assert fake[1].runtime_id != fake[0].runtime_id


@pytest.mark.asyncio
async def test_failed_child_cleanup_preserves_foreign_replacement(fake, monkeypatch):
    async def start(self):
        await control.get("runtime-test").register(self.runtime_id)
        raise ConnectionError("initialization failed")

    async def close(self):
        self.closed = True
        await control.get("runtime-test").register("foreign-replacement")

    monkeypatch.setattr(runtime.RawMcpClient, "start", start)
    monkeypatch.setattr(runtime.RawMcpClient, "aclose", close)
    with pytest.raises(runtime.RuntimeUnavailable):
        await runtime.start("runtime-test")
    broker = control.get("runtime-test")
    assert fake[0].closed
    assert broker.runtime_id == "foreign-replacement"
    assert "foreign-replacement" not in broker.retired


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"content": []},
    {"content": [{"type": "text", "text": '{"runtime_id":"foreign-replacement"}'}]},
    {"content": [{"type": "text", "text": "not json"}]},
])
async def test_invalid_status_never_adopts_foreign_identity(fake, monkeypatch, payload):
    async def start(self):
        await control.get("runtime-test").register(self.runtime_id)
        await control.get("runtime-test").register("foreign-replacement")

    async def call(self, tool, arguments):
        assert tool == "browser_status"
        return payload

    monkeypatch.setattr(runtime.RawMcpClient, "start", start)
    monkeypatch.setattr(runtime.RawMcpClient, "call_tool", call)
    with pytest.raises(runtime.RuntimeUnavailable):
        await runtime.start("runtime-test")
    broker = control.get("runtime-test")
    assert fake[0].closed
    assert broker.runtime_id == "foreign-replacement"
    assert "foreign-replacement" not in broker.retired


@pytest.mark.asyncio
async def test_shutdown_retires_outstanding_rpc_jobs(fake):
    await runtime.start("runtime-test")
    entered = asyncio.Event()
    async def hung(tool, args):
        entered.set()
        await asyncio.Event().wait()
    fake[0].call_tool = hung
    instance = runtime.get("runtime-test")
    request = asyncio.create_task(runtime.call("runtime-test", "browser_status", {}))
    await entered.wait()
    await runtime.shutdown()
    await asyncio.gather(request, return_exceptions=True)
    assert not instance.calls and instance.active == 0
    assert fake[0].closed




@pytest.mark.asyncio
async def test_stale_build_is_named_not_a_parse_crash(fake, monkeypatch):
    """A dist predating the workspace runtime has no browser_status.

    Production shipped `503 Browser runtime startup failed (KeyError)` because
    the handshake read `runtime_id` straight out of that server's
    `{"ok": false, "error": "unknown tool: browser_status"}` error result.
    """
    async def list_tools(self):
        return [
            SimpleNamespace(name=name)
            for name in ("browser_navigate", "browser_snapshot", "browser_tabs")
        ]

    async def call_tool(self, tool, arguments):
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({"ok": False, "error": f"unknown tool: {tool}"}),
            }],
            "isError": True,
        }

    monkeypatch.setattr(runtime.RawMcpClient, "list_tools", list_tools)
    monkeypatch.setattr(runtime.RawMcpClient, "call_tool", call_tool)
    with pytest.raises(runtime.RuntimeUnavailable) as failure:
        await runtime.start("runtime-test")
    assert "out of date" in str(failure.value)
    assert "npm --prefix browser-mcp run build" in str(failure.value)
    assert fake[0].closed


@pytest.mark.asyncio
async def test_status_error_result_never_raises_key_error(fake, monkeypatch):
    """Same payload, but the tool listing looks healthy: still no crash."""
    async def call_tool(self, tool, arguments):
        return {
            "content": [{"type": "text", "text": json.dumps({"ok": False, "error": "x"})}],
            "isError": True,
        }

    monkeypatch.setattr(runtime.RawMcpClient, "call_tool", call_tool)
    with pytest.raises(runtime.RuntimeUnavailable, match="expected identity"):
        await runtime.start("runtime-test")
    assert fake[0].closed


@pytest.mark.parametrize("status", [
    None,
    {},
    {"content": [{"type": "text", "text": '{"ok": false}'}]},
    {"content": [{"type": "text", "text": "not json"}]},
    {"content": [{"type": "text"}]},
    {"content": [{"type": "text", "text": '{"runtime_id": 5}'}]},
    {"content": [{"type": "text", "text": '{"runtime_id": "has spaces"}'}]},
    {"content": [{"type": "text", "text": '{"runtime_id": "ok"}'}], "isError": True},
    {"content": "not a list"},
])
def test_reported_runtime_id_absorbs_every_malformed_status(status):
    assert runtime._reported_runtime_id(status) is None


def test_reported_runtime_id_reads_a_healthy_status():
    status = {"content": [
        {"type": "image", "data": "YQ==", "mimeType": "image/png"},
        {"type": "text", "text": '{"runtime_id": "abc-123", "available": true}'},
    ]}
    assert runtime._reported_runtime_id(status) == "abc-123"


@pytest.mark.asyncio
async def test_hung_call_returns_a_deadline_error_without_replaying(fake, monkeypatch):
    await runtime.start("runtime-test")
    monkeypatch.setenv("BROWSER_TOOL_TIMEOUT_SECONDS", "0.05")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hung(tool, args):
        entered.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "{}"}]}

    fake[0].call_tool = hung
    instance = runtime.get("runtime-test")
    result = await runtime.call("runtime-test", "browser_act", {"ref": "e1"})
    assert entered.is_set()
    assert result["isError"] is True
    body = json.loads(result["content"][0]["text"])
    assert body["ok"] is False
    assert "did not finish within 0.05s" in body["error"]
    assert "NOT replayed" in body["error"]
    # The admitted call is still owned by the runtime, not cancelled behind it.
    assert instance.calls and instance.active == 1
    release.set()
    await asyncio.gather(*instance.calls, return_exceptions=True)


def test_call_timeout_defaults_and_rejects_nonsense(monkeypatch):
    monkeypatch.delenv("BROWSER_TOOL_TIMEOUT_SECONDS", raising=False)
    assert runtime.call_timeout() == runtime.DEFAULT_CALL_TIMEOUT
    for bad in ("", "abc", "0", "-3"):
        monkeypatch.setenv("BROWSER_TOOL_TIMEOUT_SECONDS", bad)
        assert runtime.call_timeout() == runtime.DEFAULT_CALL_TIMEOUT
    monkeypatch.setenv("BROWSER_TOOL_TIMEOUT_SECONDS", "12.5")
    assert runtime.call_timeout() == 12.5
