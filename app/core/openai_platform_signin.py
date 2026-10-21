"""Owner-driven Platform login in an isolated, ephemeral Chrome context.

Uses the app's supervised egress-protected Chrome, not the agent MCP context.
Only JPEGs and fixed status messages leave this module. Never publish the target
in browser_panel_registry, persist cookies, forward network events, or log CDP.
Closing the viewer/disconnecting CDP disposes the incognito context.
"""
import asyncio
import base64
import json
from contextlib import suppress
from urllib.parse import urlsplit

import websockets

from app.core import native_chrome
from app.core.browser_stream import translate_input

LOGIN_URL = "https://platform.openai.com/settings/organization/billing/overview"


def dashboard_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "api.openai.com"
                and parsed.port in (None, 443) and not parsed.username and not parsed.password
                and parsed.path.startswith(("/v1/dashboard/", "/dashboard/")))
    except ValueError:
        return False


def session_key(headers):
    for name, value in headers.items():
        if name.lower() == "authorization" and isinstance(value, str) and value.startswith("Bearer sess-"):
            key = value[7:]
            if len(key) <= 4096 and not any(c.isspace() for c in key):
                return key
    return None


class Cdp:
    def __init__(self, socket):
        self.socket = socket
        self.serial = 0
        self.pending = {}
        # Critical network events must never block command replies. Chrome only
        # sends a few unacked screencast frames; retain at most one queued frame.
        self.events = asyncio.Queue()
        self.frame_queued = None

    async def call(self, method, params=None, session=None):
        self.serial += 1
        ident = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        message = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            await self.socket.send(json.dumps(message))
            async with asyncio.timeout(10):
                return await future
        finally:
            self.pending.pop(ident, None)

    async def receive(self):
        try:
            async for raw in self.socket:
                msg = json.loads(raw)
                future = self.pending.get(msg.get("id"))
                if future is not None and not future.done():
                    if "error" in msg:
                        future.set_exception(RuntimeError("Browser command failed"))
                    else:
                        future.set_result(msg.get("result", {}))
                elif msg.get("method") in {
                    "Page.screencastFrame", "Network.requestWillBeSent",
                    "Network.requestWillBeSentExtraInfo", "Network.loadingFinished",
                }:
                    if msg["method"] == "Page.screencastFrame":
                        if self.frame_queued is not None:
                            self.frame_queued["acks"].append(msg["params"]["sessionId"])
                            self.frame_queued["params"] = msg["params"]
                            continue
                        msg["acks"] = [msg["params"]["sessionId"]]
                        self.frame_queued = msg
                    self.events.put_nowait(msg)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Browser disconnected"))


async def capture(viewer, *, authorize):
    async with native_chrome.browser_in_use():
        return await _capture(viewer, authorize=authorize)


async def _capture(viewer, *, authorize):
    version = await native_chrome.sign_in_browser_version()
    # Do not trust a remote debugging address returned by an external listener.
    path = urlsplit(version["webSocketDebuggerUrl"]).path
    if not path.startswith("/devtools/browser/") or "?" in path:
        raise RuntimeError("Invalid browser endpoint")
    endpoint = f"ws://127.0.0.1:{native_chrome._cdp_port()}{path}"
    async with websockets.connect(endpoint, max_size=4 * 1024 * 1024) as socket:
        cdp = Cdp(socket)
        reader = asyncio.create_task(cdp.receive())
        input_task = None
        context = None
        try:
            context = (await cdp.call("Target.createBrowserContext", {"disposeOnDetach": True}))["browserContextId"]
            target = await cdp.call("Target.createTarget", {"url": "about:blank", "browserContextId": context})
            session = (await cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}))["sessionId"]
            await cdp.call("Page.enable", session=session)
            await cdp.call("Network.enable", session=session)
            await cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": 1000, "height": 750, "deviceScaleFactor": 1, "mobile": False,
            }, session)
            await cdp.call("Page.startScreencast", {
                "format": "jpeg", "quality": 65, "maxWidth": 1000, "maxHeight": 750,
            }, session)

            async def inputs():
                while True:
                    msg = await viewer.receive_json()
                    await authorize()
                    # No arbitrary navigation, JS execution, or raw CDP passthrough.
                    if msg.get("type") != "input":
                        raise ValueError("Invalid login input")
                    command = translate_input(msg, (1000, 750))
                    await cdp.call(command["method"], command["params"], session)

            input_task = asyncio.create_task(inputs())
            await cdp.call("Page.navigate", {"url": LOGIN_URL}, session)
            requests = {}
            while True:
                event_task = asyncio.create_task(cdp.events.get())
                done, _ = await asyncio.wait((event_task, input_task, reader), return_when=asyncio.FIRST_COMPLETED)
                if event_task not in done:
                    event_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await event_task
                    raise RuntimeError("Sign-in disconnected")
                event = event_task.result()
                if event.get("sessionId") != session:
                    continue
                method, params = event["method"], event.get("params", {})
                if method == "Page.screencastFrame":
                    cdp.frame_queued = None
                    await authorize()
                    await viewer.send_json({"type": "frame", "data": params["data"]})
                    for frame_id in event["acks"]:
                        await cdp.call("Page.screencastFrameAck", {"sessionId": frame_id}, session)
                elif method == "Network.requestWillBeSent":
                    request = params["request"]
                    if dashboard_url(request.get("url", "")):
                        requests[params["requestId"]] = request["url"]
                        key = session_key(request.get("headers", {}))
                        if key:
                            return key
                elif method == "Network.requestWillBeSentExtraInfo" and params["requestId"] in requests:
                    key = session_key(params.get("headers", {}))
                    if key:
                        return key
                elif method == "Network.loadingFinished":
                    url = requests.pop(params["requestId"], "")
                    if urlsplit(url).path in {"/dashboard/onboarding/login", "/v1/dashboard/onboarding/login"}:
                        result = await cdp.call("Network.getResponseBody", {"requestId": params["requestId"]}, session)
                        body = base64.b64decode(result["body"]).decode() if result.get("base64Encoded") else result["body"]
                        key = json.loads(body).get("sensitive_id")
                        if isinstance(key, str) and session_key({"Authorization": "Bearer " + key}):
                            return key
        finally:
            if input_task:
                input_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await input_task
            if context:
                with suppress(Exception):
                    await cdp.call("Target.disposeBrowserContext", {"browserContextId": context})
            reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader
