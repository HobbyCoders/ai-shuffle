"""Exercise the CDP producer; no real browser, account, or provider traffic."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.core import openai_platform_signin as signin


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_from", ["header", "login_response"])
async def test_isolated_capture_never_forwards_secret_and_disposes_context(monkeypatch, capture_from):
    class Socket:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.commands = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            return json.dumps(await self.queue.get())
        async def send(self, text):
            msg = json.loads(text)
            self.commands.append(msg)
            result = {
                "Target.createBrowserContext": {"browserContextId": "isolated"},
                "Target.createTarget": {"targetId": "private-target"},
                "Target.attachToTarget": {"sessionId": "private-session"},
                "Network.getResponseBody": {"body": json.dumps({"sensitive_id": "sess-captured-test"})},
            }.get(msg["method"], {})
            await self.queue.put({"id": msg["id"], "result": result})
            if msg["method"] == "Page.navigate":
                async def event(method, params):
                    await self.queue.put({"sessionId": "private-session", "method": method, "params": params})
                await event("Page.screencastFrame", {"data": "fake-jpeg", "sessionId": 1})
                await event("Network.requestWillBeSent", {"requestId": "evil", "request": {
                    "url": "https://evil.test/dashboard/", "headers": {"Authorization": "Bearer sess-evil"}}})
                await event("Network.requestWillBeSent", {"requestId": "login", "request": {
                    "url": "https://api.openai.com/dashboard/onboarding/login", "headers": {}}})
                if capture_from == "header":
                    await event("Network.requestWillBeSentExtraInfo", {"requestId": "login", "headers": {
                        "Authorization": "Bearer sess-captured-test"}})
                else:
                    await event("Network.loadingFinished", {"requestId": "login"})
    socket = Socket()
    monkeypatch.setattr(signin.websockets, "connect", lambda *a, **k: socket)
    monkeypatch.setattr(signin.native_chrome, "ensure_started", AsyncMock())
    monkeypatch.setattr(signin.native_chrome, "cdp_get", AsyncMock(return_value={
        "webSocketDebuggerUrl": "ws://localhost:9221/devtools/browser/test"}))
    async def never_input():
        await asyncio.Future()
    viewer = type("Viewer", (), {"receive_json": staticmethod(never_input), "send_json": AsyncMock()})()
    authorize = AsyncMock()
    key = await signin.capture(viewer, authorize=authorize)
    assert key == "sess-captured-test"
    assert "sess-" not in repr(viewer.send_json.call_args_list)
    assert viewer.send_json.call_args_list[0].args[0] == {"type": "frame", "data": "fake-jpeg"}
    create = next(c for c in socket.commands if c["method"] == "Target.createBrowserContext")
    assert create["params"]["disposeOnDetach"] is True
    target = next(c for c in socket.commands if c["method"] == "Target.createTarget")
    assert target["params"]["browserContextId"] == "isolated"
    assert any(c["method"] == "Target.disposeBrowserContext" for c in socket.commands)
    authorize.assert_awaited()
