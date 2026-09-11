"""Real Chromium + STDIO MCP + HTTP/WS workspace contract smoke.

Run explicitly after building browser-mcp:
RUN_BROWSER_WORKSPACE_INTEGRATION=1 pytest -q tests/test_browser_workspace_integration.py
The fixture never starts the application lifespan or opens the configured database.
"""

import asyncio
import json
import os
import secrets
import shutil
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_WORKSPACE_INTEGRATION") != "1",
    reason="explicit real browser integration run",
)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _ready(url):
    async with httpx.AsyncClient() as client:
        for _ in range(200):
            try:
                response = await client.get(url, timeout=0.5)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise AssertionError("isolated fixture failed to start")


class Mcp:
    def __init__(self, process):
        self.process = process
        self.sequence = 0

    async def call(self, method, params):
        self.sequence += 1
        request = {
            "jsonrpc": "2.0",
            "id": self.sequence,
            "method": method,
            "params": params,
        }
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        await self.process.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), 40)
            if not line:
                raise AssertionError("MCP exited before replying")
            response = json.loads(line)
            if response.get("id") == self.sequence:
                assert "error" not in response, response.get("error")
                return response["result"]

    async def tool(self, name, args=None):
        return await self.call("tools/call", {"name": name, "arguments": args or {}})

    async def initialize(self):
        await self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "workspace-integration", "version": "1"},
            },
        )
        self.process.stdin.write(
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        await self.process.stdin.drain()


def _text(result):
    return "\n".join(
        item.get("text", "")
        for item in result.get("content", [])
        if item.get("type") == "text"
    )


async def _receive(ws, predicate, timeout=35):
    observed = []

    async def receive():
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            if predicate(message):
                return message
            observed.append(
                {key: value for key, value in message.items() if key != "data"}
            )
            del observed[:-8]

    try:
        return await asyncio.wait_for(receive(), timeout)
    except asyncio.TimeoutError as exc:
        raise AssertionError(
            f"Expected workspace event did not arrive; last messages: {observed}"
        ) from exc


async def _command(ws, state, op, args=None, ok=True):
    command_id = secrets.token_hex(8)
    await ws.send(
        json.dumps(
            {
                "type": "workspace-command",
                "id": command_id,
                "runtime_id": state["runtime_id"],
                "tab_id": state.get("active_tab_id"),
                "op": op,
                "args": args or {},
            }
        )
    )
    reply = await _receive(
        ws,
        lambda msg: msg.get("type") == "command-result" and msg.get("id") == command_id,
    )
    assert reply["ok"] is ok, reply
    return reply


@pytest.mark.asyncio
async def test_real_mcp_and_two_viewers_share_tabs_control_and_viewport(
    tmp_path, monkeypatch, caplog
):
    from app.api import browser_panel as panel
    from app.core import browser_panel_registry as registry
    from app.core import browser_runtime, mcp_config, native_chrome

    caplog.set_level("DEBUG", logger="app.core.browser_stream")
    root = Path(__file__).resolve().parents[1]
    entry = root / "browser-mcp" / "dist" / "index.js"
    assert entry.is_file(), "Build browser-mcp before running this integration test"
    chromium = (
        os.environ.get("CHROMIUM_PATH")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
        or native_chrome.resolve_chrome_executable()
    )
    assert chromium, "Chromium is required for an explicit integration run"
    api_port, cdp_port = _port(), _port()
    origin = f"http://127.0.0.1:{api_port}"
    cdp_origin = f"http://127.0.0.1:{cdp_port}"
    session_id = "browser-workspace-integration"
    auth = secrets.token_urlsafe(24)
    registry.clear(session_id)
    processes = []
    server = None
    server_task = None

    async def authenticate(ws, token):
        return token == auth, None

    async def cdp_get(path, port=None):
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(cdp_origin + path, timeout=2)
                return response.json() if response.is_success else None
            except httpx.HTTPError:
                return None

    async def browser_running():
        return await cdp_get("/json/version") is not None

    monkeypatch.setattr(panel, "authenticate_websocket", authenticate)
    monkeypatch.setattr(
        panel.database,
        "get_session",
        lambda sid: {"id": sid} if sid == session_id else None,
    )
    monkeypatch.setattr(panel, "_browser_running", browser_running)
    monkeypatch.setattr(native_chrome, "cdp_get", cdp_get)
    monkeypatch.setattr(native_chrome, "_cdp_port", lambda: cdp_port)
    monkeypatch.setenv(
        "BROWSER_PANEL_REGISTER_URL", origin + "/api/v1/browser/panel/register"
    )

    app = FastAPI()
    app.include_router(panel.router)
    app.dependency_overrides[panel.require_auth] = lambda: auth

    async def require_fixture_session(request, sid):
        from fastapi import HTTPException
        if sid != session_id:
            raise HTTPException(403)
        return {"id": sid}

    monkeypatch.setattr(panel, "require_session", require_fixture_session)

    def runtime_config(sid):
        assert sid == session_id
        return {
            "command": "node", "args": [str(entry)],
            "env": {**mcp_config._build_browser_server_env({
                "CHROME_CDP_PORT": str(cdp_port),
                "BROWSER_SSRF_ALLOW": "127.0.0.0/8,::1/128",
            }, session_id=sid), "AIS_BROWSER_MODE": "runtime"},
            "connect_timeout": 15, "call_timeout": 40, "shutdown_timeout": 3,
        }

    monkeypatch.setattr(browser_runtime, "resolve_config", runtime_config)

    start_states = []

    @app.middleware("http")
    async def observe_start(request, call_next):
        response = await call_next(request)
        if request.url.path == f"/api/v1/sessions/{session_id}/browser/panel/start":
            body = b"".join([chunk async for chunk in response.body_iterator])
            start_states.append(json.loads(body))
            from starlette.responses import Response
            return Response(body, status_code=response.status_code, headers=dict(response.headers))
        return response

    fixture_loads = 0

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(8)
        return HTMLResponse("<title>Slow fixture</title><h1>Slow response</h1>")

    @app.get("/fixture")
    async def fixture():
        nonlocal fixture_loads
        fixture_loads += 1
        return HTMLResponse("""<!doctype html><html><head><title>Workspace fixture</title></head>
<body style="margin:0"><input aria-label="Message" style="position:absolute;left:10px;top:10px;width:240px;height:40px"
 oninput="document.querySelector('output').textContent='Typed: '+this.value;document.title='Typed: '+this.value"><main style="padding-top:70px">
<h1>Shared browser fixture</h1><output>Typed: </output><button onclick="this.textContent='Clicked'">Click me</button>
</main></body></html>""")

    @app.get("/fixture-stream-state")
    async def fixture_stream_state():
        from app.core import browser_control, browser_stream

        hub = browser_stream._hubs.get(session_id)
        broker = browser_control._brokers.get(session_id)
        return {
            "fixture_loads": fixture_loads,
            "runtime_viewport": await browser_runtime.call(session_id, "browser_eval", {
                "code": "return {width: innerWidth, height: innerHeight, dpr: devicePixelRatio}"
            }) if browser_runtime._runtimes.get(session_id) else None,
            "last_metadata": hub._last_metadata if hub else None,
            "attached_target": hub._attached_target_id if hub else None,
            "frame_identity": hub._frame_identity if hub else None,
            "broker_runtime": broker.runtime_id if broker else None,
            "broker_active_tab": broker.state.get("active_tab_id") if broker else None,
            "viewer_count": hub.viewer_count() if hub else None,
            "queue_sizes": [v.queue.qsize() for v in hub.viewers] if hub else None,
            "task": str(hub._task) if hub else None,
        }

    async def start_mcp():
        env = {
            **os.environ,
            **mcp_config._build_browser_server_env(
                {
                    "CHROME_CDP_PORT": str(cdp_port),
                    "BROWSER_SSRF_ALLOW": "127.0.0.0/8,::1/128",
                },
                session_id=session_id,
            ),
        }
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(entry),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(proc)
        client = Mcp(proc)
        await client.initialize()
        return client

    async def start_chrome():
        chrome = await asyncio.create_subprocess_exec(
            chromium,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={tmp_path / 'chrome'}",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(chrome)
        await _ready(cdp_origin + "/json/version")
        return chrome

    try:
        chrome = await start_chrome()
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=api_port, lifespan="off", log_level="error"
            )
        )
        server_task = asyncio.create_task(server.serve())
        await _ready(origin + "/fixture")
        assert not browser_runtime._runtimes
        assert registry.get_target(session_id) is None
        if os.environ.get("RUN_BROWSER_WORKSPACE_UI") == "1":
            ui = await asyncio.create_subprocess_exec(
                "node",
                "tests/verify-browser-workspace-live.mjs",
                cwd=root / "frontend",
                env={
                    **os.environ,
                    "CHROMIUM_PATH": chromium,
                    "BROWSER_WORKSPACE_FIXTURE_API": origin,
                    "BROWSER_WORKSPACE_FIXTURE_SESSION": session_id,
                    "BROWSER_WORKSPACE_FIXTURE_TOKEN": auth,
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(ui)
            output, _ = await asyncio.wait_for(ui.communicate(), 300)
            assert ui.returncode == 0, output.decode().replace(auth, "[fixture-token]")
            print(output.decode().replace(auth, "[fixture-token]"))

            assert len(start_states) == 1
            cold_state = start_states[0]
            assert len(browser_runtime._runtimes) == 1
            print("Real UI cold start: one POST, one runtime, one initial tab; no prior agent")
        else:
            async with httpx.AsyncClient() as http:
                started = await http.post(origin + f"/api/v1/sessions/{session_id}/browser/panel/start", timeout=60)
            assert started.status_code == 200, started.text
            cold_state = started.json()
            assert len(cold_state["tabs"]) == 1
            assert cold_state["control"]["owner"] == "agent"
            assert cold_state["target_id"]
        # No agent exists yet. A human can acquire control and navigate.
        async with websockets.connect(f"ws://127.0.0.1:{api_port}/api/v1/ws/browser/{session_id}?token={auth}", max_size=8_000_000) as human:
            await _receive(human, lambda msg: msg.get("type") == "hello")
            await _command(human, cold_state, "takeover")
            await _command(human, cold_state, "navigate", {"url": origin + "/fixture"})
            mcp = await start_mcp()
            status = json.loads(_text(await mcp.tool("browser_status")))
            assert status["runtime_id"] == cold_state["runtime_id"]
            assert status["target_id"] == cold_state["target_id"]
            assert status["control"]["owner"] == "human"
            blocked = await mcp.tool("browser_navigate", {"url": origin + "/fixture?blocked"})
            assert blocked.get("isError") and "human_control" in _text(blocked)
            await _command(human, cold_state, "release")


        ws_url = (
            f"ws://127.0.0.1:{api_port}/api/v1/ws/browser/{session_id}?token={auth}"
        )
        async with (
            websockets.connect(ws_url, max_size=8_000_000) as first,
            websockets.connect(ws_url, max_size=8_000_000) as second,
        ):
            hello1 = await _receive(first, lambda msg: msg.get("type") == "hello")
            hello2 = await _receive(second, lambda msg: msg.get("type") == "hello")
            assert hello1["viewer_id"] != hello2["viewer_id"]
            state = (
                await _receive(
                    first,
                    lambda msg: (
                        msg.get("type") == "workspace"
                        and msg["state"].get("active_tab_id")
                    ),
                )
            )["state"]
            await _receive(first, lambda msg: msg.get("type") == "frame")
            await _command(first, state, "takeover")
            shared = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace"
                    and msg["state"]["control"]["owner"] == "human"
                ),
            )
            state = shared["state"]
            assert state["control"]["viewer_id"] == hello1["viewer_id"]
            await _command(second, state, "takeover", ok=False)
            blocked = await mcp.tool(
                "browser_navigate", {"url": origin + "/fixture?blocked"}
            )
            assert blocked.get("isError"), (
                "agent mutation must be rejected while human controls"
            )
            status = await mcp.tool("browser_status")
            assert not status.get("isError"), _text(status)
            await _command(
                first, state, "viewport", {"mode": "fixed", "width": 390, "height": 844}
            )
            shared = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace"
                    and msg["state"]["viewport"]["width"] == 390
                ),
            )
            state = shared["state"]
            assert state["viewport"]["height"] == 844
            for event in [
                {
                    "kind": "mouse",
                    "event": "down",
                    "x": 50,
                    "y": 30,
                    "button": "left",
                    "clickCount": 1,
                },
                {
                    "kind": "mouse",
                    "event": "up",
                    "x": 50,
                    "y": 30,
                    "button": "left",
                    "clickCount": 1,
                },
                {"kind": "text", "text": "Shared 你好"},
            ]:
                cid = secrets.token_hex(8)
                await first.send(
                    json.dumps(
                        {
                            "type": "input",
                            "id": cid,
                            "runtime_id": state["runtime_id"],
                            "tab_id": state["active_tab_id"],
                            **event,
                        }
                    )
                )
                reply = await _receive(
                    first,
                    lambda msg, cid=cid: (
                        msg.get("type") == "command-result" and msg.get("id") == cid
                    ),
                )
                assert reply["ok"] is True, reply
            await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace"
                    and any(
                        tab["title"] == "Typed: Shared 你好"
                        for tab in msg["state"]["tabs"]
                    )
                ),
            )
            await first.send(
                json.dumps(
                    {
                        "type": "inspect",
                        "id": "inspect-fixture",
                        "runtime_id": state["runtime_id"],
                        "tab_id": state["active_tab_id"],
                        "mode": "select",
                        "x": 50,
                        "y": 30,
                    }
                )
            )
            inspected = await _receive(
                first,
                lambda msg: (
                    msg.get("type") == "inspect-hit" and msg.get("mode") == "select"
                ),
            )
            assert inspected.get("element"), inspected
            await _command(
                first,
                state,
                "tabs",
                {"action": "create", "url": origin + "/fixture?second"},
            )
            shared = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace" and len(msg["state"]["tabs"]) == 2
                ),
            )
            state = shared["state"]
            await _command(first, state, "release")
            shared = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace"
                    and msg["state"]["control"]["owner"] == "agent"
                ),
            )
            state = shared["state"]
            tabs = await mcp.tool("browser_tabs", {"op": "list"})
            assert not tabs.get("isError"), _text(tabs)
            assert "second" in _text(tabs), (
                "panel tab must be owned by the same MCP Session"
            )
            content = await mcp.tool("browser_read_text")
            assert "Shared browser fixture" in _text(content)
            waited = await mcp.tool(
                "browser_wait",
                {
                    "condition": "text",
                    "text": "Shared browser fixture",
                    "timeout_ms": 1000,
                },
            )
            evidence = json.loads(_text(waited))
            assert evidence["matched"] is True
            assert evidence["tab_id"] == state["active_tab_id"]
            assert evidence["target"] == "Shared browser fixture"
            resized = await mcp.tool("browser_resize", {"width": 4096, "height": 239})
            assert not resized.get("isError"), _text(resized)
            observed = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace"
                    and msg["state"]["viewport"]["width"] == 4096
                ),
            )
            assert observed["state"]["viewport"]["height"] == 239
            old_state = state
            mcp.process.stdin.close()
            await asyncio.wait_for(mcp.process.wait(), 10)
            mcp = await start_mcp()
            reused = json.loads(_text(await mcp.tool("browser_status")))
            assert reused["runtime_id"] == old_state["runtime_id"]
            assert reused["target_id"] == old_state["target_id"]
            assert len(reused["tabs"]) == 2
            # Explicit runtime disposal, not proxy EOF, retires the old identity.
            await browser_runtime.get(session_id).close()
            remaining_targets = await cdp_get("/json/list")
            assert old_state["target_id"] not in {target["id"] for target in remaining_targets}
            navigation = await mcp.tool(
                "browser_navigate", {"url": origin + "/fixture?restarted"}
            )
            assert not navigation.get("isError"), _text(navigation)
            shared = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace"
                    and msg["state"]["runtime_id"] != old_state["runtime_id"]
                    and msg["state"].get("active_tab_id")
                ),
            )
            assert len(shared["state"]["tabs"]) == 1
            await _command(
                first,
                old_state,
                "navigate",
                {"url": origin + "/fixture?must-not-replay"},
                ok=False,
            )
            content = await mcp.tool("browser_read_text")
            assert "must-not-replay" not in _text(content)
            current_runtime = shared["state"]["runtime_id"]
            previous_tab = shared["state"]["active_tab_id"]
            chrome.terminate()
            await asyncio.wait_for(chrome.wait(), 10)
            chrome = await start_chrome()
            navigation = await mcp.tool(
                "browser_navigate", {"url": origin + "/fixture?browser-restarted"}
            )
            assert not navigation.get("isError"), _text(navigation)
            recovered = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "workspace"
                    and msg["state"].get("active_tab_id") not in (None, previous_tab)
                ),
            )
            assert recovered["state"]["runtime_id"] == current_runtime
            assert len(recovered["state"]["tabs"]) == 1
            frame = await _receive(
                second,
                lambda msg: (
                    msg.get("type") == "frame"
                    and msg.get("metadata", {}).get("tab_id")
                    == recovered["state"]["active_tab_id"]
                ),
            )
            assert frame["metadata"]["runtime_id"] == current_runtime
    finally:
        await browser_runtime.shutdown()
        for proc in reversed(processes):
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 8)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        if server:
            server.should_exit = True
        if server_task:
            try:
                await asyncio.wait_for(server_task, 10)
            except asyncio.TimeoutError:
                server_task.cancel()
        registry.clear(session_id)