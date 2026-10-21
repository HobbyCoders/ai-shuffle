"""Opt-in private Chromium regressions; never connect to the worker browser."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import native_chrome as chrome, browser_runtime as runtime, browser_control as control

pytestmark = pytest.mark.skipif(not os.environ.get("CHROMIUM_PATH"), reason="requires opt-in private Chromium")


@pytest.fixture
async def private_browser(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    proc = await asyncio.create_subprocess_exec(
        os.environ["CHROMIUM_PATH"], "--headless=new", "--no-sandbox", "--remote-debugging-port=0",
        f"--user-data-dir={profile}", "about:blank", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    client = None
    try:
        async with asyncio.timeout(15):
            while not (profile / "DevToolsActivePort").exists():
                await asyncio.sleep(.05)
            port = int((profile / "DevToolsActivePort").read_text().splitlines()[0])
            while not await chrome.cdp_get("/json/version", port):
                await asyncio.sleep(.05)
        root = Path(__file__).resolve().parents[2]
        client = runtime.RawMcpClient("private-browser", {
            "command": "node", "args": [str(root / "browser-mcp/dist/index.js")], "cwd": str(root),
            "env": {"CHROME_CDP_PORT": str(port), "AIS_BROWSER_MODE": "runtime", "AIS_SESSION_ID": "",
                    "AIS_PANEL_TOKEN": "", "AIS_PANEL_REGISTER_URL": ""},
            "connect_timeout": 10, "call_timeout": 25, "shutdown_timeout": 2,
        })
        await client.start()
        await client.call_tool("browser_tabs", {"action": "list"})
        initialized = await client.call_tool("browser_eval", {"code": "1"})
        assert not initialized.get("isError")
        monkeypatch.setattr(runtime, "_runtimes", {"private": SimpleNamespace(active=0, client=client)})
        monkeypatch.setattr(control, "_brokers", {})
        monkeypatch.setattr(chrome, "_health_states", {})
        monkeypatch.setattr(chrome, "_protected_pages", set())
        monkeypatch.setattr(chrome, "_external_users", 0)
        stopper = AsyncMock()
        monkeypatch.setattr(chrome, "_stop_process", stopper)
        yield port, client, stopper
    finally:
        if client:
            await client.aclose()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()


@pytest.mark.asyncio
async def test_pending_confirm_survives_health_checks_and_can_be_accepted(private_browser, monkeypatch):
    port, client, stopper = private_browser
    before = {t["id"] for t in await chrome.cdp_get("/json/list", port)}
    scheduled = await client.call_tool("browser_eval", {"code": "setTimeout(() => confirm('keep this tab'), 50); 'scheduled'"})
    assert not scheduled.get("isError")
    async with asyncio.timeout(3):
        while not await runtime.dialogs_block_recovery():
            await asyncio.sleep(.05)
    probe = AsyncMock(side_effect=AssertionError("must not probe a page with a pending dialog"))
    monkeypatch.setattr(chrome, "_probe_pages", probe)
    for _ in range(4):
        await chrome._check_health(port)
    probe.assert_not_awaited()
    stopper.assert_not_awaited()
    result = await client.call_tool("browser_act", {"kind": "accept_dialog"})
    assert not result.get("isError")
    assert not await runtime.dialogs_block_recovery()
    assert {t["id"] for t in await chrome.cdp_get("/json/list", port)} == before


@pytest.mark.asyncio
async def test_twelve_second_script_is_not_a_wedged_tab(private_browser):
    port, client, stopper = private_browser
    before = {t["id"] for t in await chrome.cdp_get("/json/list", port)}
    import websockets
    import time

    target = next(t for t in await chrome.cdp_get("/json/list", port) if t["type"] == "page")
    async with websockets.connect(target["webSocketDebuggerUrl"]) as owner:
        await owner.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        while json.loads(await asyncio.wait_for(owner.recv(), 5)).get("id") != 1:
            pass
        await owner.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
            "expression": "console.log('health-busy-start'); { const end = Date.now()+12000; while(Date.now()<end) {} }",
        }}))
        while True:
            message = json.loads(await asyncio.wait_for(owner.recv(), 5))
            if message.get("method") == "Runtime.consoleAPICalled" and any(
                arg.get("value") == "health-busy-start" for arg in message["params"]["args"]
            ):
                break
        started = time.monotonic()
        check = asyncio.create_task(chrome._probe_pages(port))
        await asyncio.sleep(.1)
        assert chrome._recovery_done is None  # Probing does not block new calls.
        await asyncio.wait_for(chrome.wait_for_recovery(), .1)
        assert await asyncio.wait_for(check, 25) is None
        assert time.monotonic() - started >= 10  # The producer really ran a long script.
        stopper.assert_not_awaited()
        assert not chrome._health_state(port)["failures"]
        assert {t["id"] for t in await chrome.cdp_get("/json/list", port)} == before
