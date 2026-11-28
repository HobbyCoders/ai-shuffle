"""The user's own Chrome or Edge, connected through the AI Shuffle browser extension.

The extension (``browser-extension/``) signs in with the owner's account,
enrolls with this worker like the phone does and keeps one WebSocket open to
``/api/v1/browser-extension/connect``. A chat that picks that browser starts
its browser runtime (``browser-mcp``) against a loopback CDP endpoint served
here (``/api/v1/browser-extension/cdp/<token>``). :class:`Relay` answers the
browser-level CDP that Playwright sends (targets, auto-attach, version) and
forwards everything addressed to a page to the extension, which runs it with
``chrome.debugger`` on that chat's tabs. Each chat's tabs live in one Chrome
tab group named after the chat, and nothing outside that group is attached.

Extension protocol (JSON text frames):

* worker -> extension: ``{"id", "method", "params"}``. Methods: ``group.attach``
  ``{group, title}`` -> ``{tabs: [{tabId, targetInfo}]}``; ``tab.create``
  ``{group, title, url}`` -> ``{tabId, targetInfo}``; ``tab.close`` /
  ``tab.activate`` ``{tabId}``; ``cdp`` ``{tabId, sessionId?, method, params}``
  -> the CDP result; ``group.release`` ``{group}`` (detach, keep the tabs).
* extension -> worker: ``{"id", "result"}`` or ``{"id", "error"}``; events
  ``{"event": "hello", browser_id, name, product, version}`` (first frame),
  ``{"event": "cdp", tabId, sessionId?, method, params}``,
  ``{"event": "detached", tabId, reason}``,
  ``{"event": "tab_created", group, tabId, targetInfo}`` and ``{"event": "ping"}``.

Choices are kept in memory: a worker restart returns every chat to the
built-in browser, and a chat whose browser disconnects gets a clear error.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import re
import secrets
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")

# Builds of the AI Shuffle extension allowed to frame the phone UI in their
# side panel: the unpacked developer build (fixed by browser-extension/
# dev-key.txt). Store builds are added here when they are published;
# AI_SHUFFLE_BROWSER_EXTENSION_IDS (comma-separated) adds more.
EXTENSION_IDS = ("fnjpjlkegjhjepkdnnignfokaffgdmak",)


def extension_ids() -> list:
    import os
    extra = [part.strip() for part in os.environ.get("AI_SHUFFLE_BROWSER_EXTENSION_IDS", "").split(",")]
    return list(dict.fromkeys(i for i in (*EXTENSION_IDS, *extra) if _EXTENSION_ID.match(i)))


def extension_frame_ancestors() -> str:
    return " ".join(f"chrome-extension://{i}" for i in extension_ids())


class ExtensionError(RuntimeError):
    pass


def clean_text(value: Any, limit: int = 80) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:limit]


class ExtensionBrowser:
    """One connected extension (one Chrome or Edge profile)."""

    def __init__(self, websocket, hello: dict):
        browser_id = str(hello.get("browser_id") or "")
        if not _ID.match(browser_id):
            raise ExtensionError("Invalid browser id")
        self.websocket = websocket
        self.id = browser_id
        self.name = clean_text(hello.get("name")) or "Browser"
        self.product = clean_text(hello.get("product"), 40) or "Chrome"
        self.version = clean_text(hello.get("version"), 40)
        # The Shuffle account the extension signed in with (None for a browser
        # paired straight to this worker). Only that account's controllers, and
        # the worker's own login, may hand this browser to a chat.
        self.account_id = hello.get("account_id") or None
        # The extension's own paired device: its side panel may always use it.
        self.paired_client_id = hello.get("paired_client_id") or None
        self._ids = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.relays: Dict[str, "Relay"] = {}
        self.closed = False

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "product": self.product, "version": self.version}

    async def send(self, message: dict) -> None:
        async with self._send_lock:
            await self.websocket.send_text(json.dumps(message))

    async def request(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT) -> Any:
        if self.closed:
            raise ExtensionError("The browser disconnected")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise ExtensionError(f"The browser did not answer {method}") from None
        finally:
            self._pending.pop(request_id, None)

    async def dispatch(self, message: dict) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.get(message.get("id"))
            if future and not future.done():
                if message.get("error") is not None:
                    future.set_exception(ExtensionError(clean_text(message["error"], 300) or "Browser error"))
                else:
                    future.set_result(message.get("result"))
            return
        event = message.get("event")
        if event == "ping":
            await self.send({"event": "pong"})
            return
        if event in ("cdp", "detached", "tab_created"):
            for relay in list(self.relays.values()):
                await relay.on_extension_event(event, message)

    def close(self) -> None:
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ExtensionError("The browser disconnected"))
        self._pending.clear()
        for relay in list(self.relays.values()):
            relay.browser_gone()


_browsers: Dict[str, ExtensionBrowser] = {}
# chat session id -> browser id; None once the chat explicitly picked the
# built-in browser (a chat missing here has not picked any browser yet).
_targets: Dict[str, Optional[str]] = {}
# Runtime relay token -> (session id, browser id, runtime id). One use: the
# relay consumes it on connect, and a runtime that stops drops its own.
_relay_tokens: Dict[str, tuple] = {}

# Whether a caller may use a connected browser. Called with None for a browser
# that is not connected right now: only a caller who may use every browser
# (the worker's own login) answers True then.
MayUse = Callable[[Optional["ExtensionBrowser"]], bool]


def browsers(may_use: Optional[MayUse] = None) -> list:
    return [b.public() for b in _browsers.values() if not b.closed and (may_use is None or may_use(b))]


def get_browser(browser_id: str) -> Optional[ExtensionBrowser]:
    browser = _browsers.get(browser_id)
    return browser if browser and not browser.closed else None


def register(browser: ExtensionBrowser) -> None:
    previous = _browsers.get(browser.id)
    _browsers[browser.id] = browser
    if previous and previous is not browser:
        previous.close()
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(previous.websocket.close(code=4009, reason="Replaced"))


def unregister(browser: ExtensionBrowser) -> None:
    if _browsers.get(browser.id) is browser:
        del _browsers[browser.id]
    browser.close()


def session_target(session_id: str) -> Optional[str]:
    return _targets.get(session_id)


def session_assigned(session_id: str) -> bool:
    """Whether someone picked a browser (built-in or not) for this chat."""
    return session_id in _targets


def may_use_chat_browser(session_id: str, may_use: MayUse) -> bool:
    """Whether a caller may watch, drive or switch away from this chat's browser.

    Always for the built-in browser. For the person's own browser, only a caller
    that may use that browser: access to the chat alone is not enough.
    """
    browser_id = _targets.get(session_id)
    if not browser_id:
        return True
    return may_use(_browsers.get(browser_id))


# chat session id -> open Browser panel viewers: (may_use, close callback).
_panel_viewers: Dict[str, Dict[int, tuple]] = {}


@contextlib.contextmanager
def panel_viewer(session_id: str, may_use: MayUse, close: Callable[[], None]) -> Iterator[None]:
    """Hold an open Browser panel viewer; it is closed if the chat moves to a
    browser this viewer may not use."""
    key = object()
    _panel_viewers.setdefault(session_id, {})[id(key)] = (may_use, close)
    try:
        yield
    finally:
        viewers = _panel_viewers.get(session_id, {})
        viewers.pop(id(key), None)
        if not viewers:
            _panel_viewers.pop(session_id, None)


def _close_refused_viewers(session_id: str) -> None:
    for may_use, close in list(_panel_viewers.get(session_id, {}).values()):
        if not may_use_chat_browser(session_id, may_use):
            close()


def set_session_target(session_id: str, browser_id: Optional[str], may_use: Optional[MayUse] = None) -> None:
    if may_use is not None and not may_use_chat_browser(session_id, may_use):
        raise ExtensionError("Only this browser's owner can change the browser this chat uses")
    if browser_id:
        browser = get_browser(browser_id)
        if not browser or (may_use is not None and not may_use(browser)):
            raise ExtensionError("That browser is not connected")
    _targets[session_id] = browser_id or None
    _close_refused_viewers(session_id)
    for token, entry in list(_relay_tokens.items()):
        if entry[0] == session_id:
            del _relay_tokens[token]


def forget_runtime(runtime_id: Optional[str]) -> None:
    """A browser runtime stopped: its unused relay token stops working."""
    if not runtime_id:
        return
    for token, entry in list(_relay_tokens.items()):
        if entry[2] == runtime_id:
            del _relay_tokens[token]


def runtime_env(session_id: str, register_url: Optional[str], runtime_id: Optional[str] = None) -> dict:
    """Env that points one session's browser runtime at its chosen extension browser.

    The relay is served on the same loopback listener as the panel register URL.
    """
    browser_id = _targets.get(session_id)
    if not browser_id:
        return {}
    if not get_browser(browser_id):
        from app.core.browser_runtime import RuntimeUnavailable
        raise RuntimeUnavailable(
            "The browser this chat uses is not connected. Open the AI Shuffle extension in that "
            "browser, or switch the chat back to the built-in browser.")
    for stale, entry in list(_relay_tokens.items()):
        if entry[0] == session_id:
            del _relay_tokens[stale]
    token = secrets.token_urlsafe(32)
    _relay_tokens[token] = (session_id, browser_id, runtime_id)
    return {
        "AIS_BROWSER_TARGET": "extension",
        "AIS_BROWSER_CDP_ENDPOINT": f"ws://{_loopback_netloc(register_url)}/api/v1/browser-extension/cdp/{token}",
    }


def _loopback_netloc(register_url: Optional[str]) -> str:
    from urllib.parse import urlsplit

    from app.core.config import settings
    parts = urlsplit(register_url or "")
    if parts.hostname and parts.port:
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        return f"{host}:{parts.port}"
    return f"127.0.0.1:{settings.port}"


def claim_relay(token: str) -> Optional[tuple]:
    entry = _relay_tokens.pop(token, None)
    if not entry or _targets.get(entry[0]) != entry[1]:
        return None
    browser = get_browser(entry[1])
    return (entry[0], browser) if browser else None


def _session_title(session_id: str) -> str:
    from app.db import database
    with contextlib.suppress(Exception):
        row = database.get_session(session_id) or {}
        title = clean_text(row.get("title"), 60)
        if title:
            return title
    return "AI Shuffle"


class Relay:
    """One runtime's CDP connection, emulating a browser over one tab group."""

    def __init__(self, websocket, session_id: str, browser: ExtensionBrowser, title: str):
        self.websocket = websocket
        self.session_id = session_id
        self.browser = browser
        self.group = session_id
        self.title = title
        self.key = secrets.token_hex(8)
        self._sids = itertools.count(1)
        self.sessions: Dict[str, int] = {}  # relay CDP session id -> tab id
        self.children: Dict[str, int] = {}  # child (iframe/worker) session id -> tab id
        self.targets: Dict[int, dict] = {}  # tab id -> targetInfo
        self.browser_sessions: set = set()  # virtual browser-target sessions (Target.attachToBrowserTarget)
        self.auto_attach = False
        self._send_lock = asyncio.Lock()
        self.done = asyncio.Event()

    async def _emit(self, message: dict) -> None:
        async with self._send_lock:
            with contextlib.suppress(Exception):
                await self.websocket.send_text(json.dumps(message))

    def _target_info(self, tab_id: int, info: dict) -> dict:
        info = dict(info or {})
        info.setdefault("type", "page")
        info["attached"] = True
        info.setdefault("canAccessOpener", False)
        self.targets[tab_id] = info
        return info

    def _primary(self, tab_id: int) -> Optional[str]:
        return next((sid for sid, tab in self.sessions.items() if tab == tab_id), None)

    async def _announce(self, tab_id: int, info: dict) -> str:
        sid = f"ext-{self.key}-{next(self._sids)}"
        self.sessions[sid] = tab_id
        await self._emit({"method": "Target.attachedToTarget", "params": {
            "sessionId": sid, "targetInfo": self._target_info(tab_id, info), "waitingForDebugger": False}})
        return sid

    def _tab_for_target(self, target_id: str) -> Optional[int]:
        return next((tab for tab, info in self.targets.items() if info.get("targetId") == target_id), None)

    async def handle(self, message: dict) -> None:
        request_id = message.get("id")
        session_id = message.get("sessionId")
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        try:
            if session_id and session_id not in self.browser_sessions:
                result = await self._page_command(session_id, method, params)
            else:
                result = await self._browser_command(method, params)
            reply = {"id": request_id, "result": result or {}}
        except ExtensionError as exc:
            reply = {"id": request_id, "error": {"code": -32000, "message": str(exc)}}
        if session_id:
            reply["sessionId"] = session_id
        await self._emit(reply)

    async def _page_command(self, session_id: str, method: str, params: dict) -> Any:
        tab_id = self.sessions.get(session_id)
        if tab_id is not None:
            return await self.browser.request("cdp", {"tabId": tab_id, "method": method, "params": params})
        tab_id = self.children.get(session_id)
        if tab_id is None:
            raise ExtensionError(f"Session {session_id} is not attached")
        return await self.browser.request(
            "cdp", {"tabId": tab_id, "sessionId": session_id, "method": method, "params": params})

    async def _browser_command(self, method: str, params: dict) -> Any:
        if method == "Browser.getVersion":
            product = f"{self.browser.product}/{self.browser.version or '0'}"
            return {"protocolVersion": "1.3", "product": product, "revision": "",
                    "userAgent": product, "jsVersion": ""}
        if method in ("Browser.setDownloadBehavior", "Target.setDiscoverTargets"):
            return {}
        if method == "Target.attachToBrowserTarget":
            sid = f"ext-{self.key}-browser-{next(self._sids)}"
            self.browser_sessions.add(sid)
            return {"sessionId": sid}
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": []}
        if method == "Target.setAutoAttach":
            if not self.auto_attach:
                self.auto_attach = True
                attached = await self.browser.request("group.attach", {"group": self.group, "title": self.title})
                for tab in (attached or {}).get("tabs", []):
                    if tab.get("tabId") not in self.targets:
                        await self._announce(int(tab["tabId"]), tab.get("targetInfo") or {})
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets.values())}
        if method == "Target.getTargetInfo":
            tab_id = self._tab_for_target(str(params.get("targetId") or ""))
            if tab_id is not None:
                return {"targetInfo": self.targets[tab_id]}
            return {"targetInfo": {"targetId": "browser", "type": "browser", "title": "", "url": "",
                                   "attached": True, "canAccessOpener": False}}
        if method == "Target.createTarget":
            created = await self.browser.request("tab.create", {
                "group": self.group, "title": self.title, "url": params.get("url") or "about:blank"})
            tab_id = int(created["tabId"])
            if tab_id not in self.targets:
                await self._announce(tab_id, created.get("targetInfo") or {})
            return {"targetId": self.targets[tab_id].get("targetId")}
        if method == "Target.attachToTarget":
            tab_id = self._tab_for_target(str(params.get("targetId") or ""))
            if tab_id is None:
                raise ExtensionError("No such target in this chat's tab group")
            sid = f"ext-{self.key}-{next(self._sids)}"
            self.sessions[sid] = tab_id
            return {"sessionId": sid}
        if method == "Target.detachFromTarget":
            sid = params.get("sessionId")
            if sid in self.browser_sessions:
                self.browser_sessions.discard(sid)
                return {}
            tab_id = self.sessions.get(sid)
            if tab_id is not None and self._primary(tab_id) != sid:
                del self.sessions[sid]
            return {}
        if method in ("Target.closeTarget", "Target.activateTarget"):
            tab_id = self._tab_for_target(str(params.get("targetId") or ""))
            if tab_id is None:
                raise ExtensionError("No such target in this chat's tab group")
            await self.browser.request("tab.close" if method == "Target.closeTarget" else "tab.activate",
                                       {"tabId": tab_id})
            return {"success": True} if method == "Target.closeTarget" else {}
        raise ExtensionError(f"{method} is not available in a browser shared through the extension")

    async def on_extension_event(self, event: str, message: dict) -> None:
        tab_id = message.get("tabId")
        if event == "tab_created":
            if message.get("group") == self.group and self.auto_attach and tab_id not in self.targets:
                await self._announce(int(tab_id), message.get("targetInfo") or {})
            return
        if tab_id not in self.targets:
            return
        if event == "detached":
            await self._drop_tab(tab_id)
            return
        method, params = message.get("method"), message.get("params") or {}
        child = message.get("sessionId")
        if child:
            if child not in self.children:
                return
            if method == "Target.detachedFromTarget" and params.get("sessionId") in self.children:
                self.children.pop(params["sessionId"], None)
            elif method == "Target.attachedToTarget" and params.get("sessionId"):
                self.children[params["sessionId"]] = tab_id
            await self._emit({"method": method, "params": params, "sessionId": child})
            return
        if method == "Target.attachedToTarget" and params.get("sessionId"):
            self.children[params["sessionId"]] = tab_id
        elif method == "Target.detachedFromTarget" and params.get("sessionId"):
            self.children.pop(params["sessionId"], None)
        for sid, tab in list(self.sessions.items()):
            if tab == tab_id:
                await self._emit({"method": method, "params": params, "sessionId": sid})

    async def _drop_tab(self, tab_id: int) -> None:
        info = self.targets.pop(tab_id, {})
        for child, tab in list(self.children.items()):
            if tab == tab_id:
                del self.children[child]
        for sid, tab in list(self.sessions.items()):
            if tab == tab_id:
                del self.sessions[sid]
                await self._emit({"method": "Target.detachedFromTarget",
                                  "params": {"sessionId": sid, "targetId": info.get("targetId")}})

    def browser_gone(self) -> None:
        self.done.set()


async def serve_relay(websocket, token: str) -> None:
    """Serve one runtime's CDP connection until it or the browser goes away."""
    from app.db.async_database import run_sync

    claimed = claim_relay(token)
    if not claimed:
        await websocket.close(code=4004, reason="Unknown relay")
        return
    session_id, browser = claimed
    await websocket.accept()
    title = await run_sync(_session_title, session_id)
    relay = Relay(websocket, session_id, browser, title)
    browser.relays[relay.key] = relay
    tasks: set = set()

    async def receive() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                task = asyncio.create_task(relay.handle(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    receiver = asyncio.create_task(receive())
    gone = asyncio.create_task(relay.done.wait())
    try:
        await asyncio.wait({receiver, gone}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (receiver, gone, *tasks):
            task.cancel()
        browser.relays.pop(relay.key, None)
        if relay.done.is_set():
            # The browser went away under a running runtime. Its relay token
            # was single use, so stop that runtime: the next browser call
            # starts a fresh one with a new token once the browser is back.
            _stop_runtime_later(session_id)
        if not browser.closed and not any(r.group == relay.group for r in browser.relays.values()):
            with contextlib.suppress(Exception):
                await browser.request("group.release", {"group": relay.group}, timeout=5)
        with contextlib.suppress(Exception):
            await websocket.close()


def _stop_runtime_later(session_id: str) -> None:
    from app.core import browser_runtime
    runtime = browser_runtime._runtimes.get(session_id)
    if runtime is not None:
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(runtime.close())
            _background.add(task)
            task.add_done_callback(_background.discard)


_background: set = set()


# -- Browser panel live view -------------------------------------------------
# The Browser panel streams the agent's page over a page-level CDP socket
# (app/core/browser_stream.py). For a chat in the owner's own browser that
# socket comes from here: plain CDP (no session ids) for one tab of the chat's
# group, carried over the extension's chrome.debugger attachment.

_page_tokens: Dict[str, tuple] = {}  # token -> (session id, browser id, target id)


def _relay_tab(session_id: str, browser: ExtensionBrowser, target_id: str) -> Optional[int]:
    for relay in list(browser.relays.values()):
        if isinstance(relay, Relay) and relay.session_id == session_id:
            tab_id = relay._tab_for_target(target_id)
            if tab_id is not None:
                return tab_id
    return None


def page_endpoint(session_id: str, target_id: str) -> Optional[str]:
    """A loopback CDP URL for one of this chat's tabs in its extension browser.

    None when the chat uses the built-in browser (the caller connects to the
    built-in Chrome as before) or the tab is not in the chat's group. Served on
    the same loopback listener as the runtime's relay.
    """
    import os

    browser_id = _targets.get(session_id)
    browser = get_browser(browser_id) if browser_id else None
    if not browser or _relay_tab(session_id, browser, target_id) is None:
        return None
    for stale, entry in list(_page_tokens.items()):
        if entry[0] == session_id:
            del _page_tokens[stale]
    token = secrets.token_urlsafe(32)
    _page_tokens[token] = (session_id, browser.id, target_id)
    netloc = _loopback_netloc(os.environ.get("BROWSER_PANEL_REGISTER_URL"))
    return f"ws://{netloc}/api/v1/browser-extension/page/{token}"


class PageTap:
    """One page-level CDP socket onto a tab of a chat's group."""

    group = None  # never keeps a tab group alive on its own

    def __init__(self, websocket, browser: ExtensionBrowser, tab_id: int):
        self.websocket = websocket
        self.browser = browser
        self.tab_id = tab_id
        self.key = "page-" + secrets.token_hex(8)
        self._send_lock = asyncio.Lock()
        self.done = asyncio.Event()

    async def _emit(self, message: dict) -> None:
        async with self._send_lock:
            with contextlib.suppress(Exception):
                await self.websocket.send_text(json.dumps(message))

    async def handle(self, message: dict) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        try:
            if method == "Page.bringToFront":
                # Chrome paints (and screencasts) only a window's active tab.
                # Show the tab in its window without raising the window over
                # whatever the person is doing.
                await self.browser.request("tab.activate", {"tabId": self.tab_id})
                result = {}
            else:
                result = await self.browser.request("cdp", {"tabId": self.tab_id, "method": method, "params": params})
            reply = {"id": request_id, "result": result or {}}
        except ExtensionError as exc:
            reply = {"id": request_id, "error": {"code": -32000, "message": str(exc)}}
        await self._emit(reply)

    async def on_extension_event(self, event: str, message: dict) -> None:
        if message.get("tabId") != self.tab_id:
            return
        if event == "detached":
            self.done.set()
        elif event == "cdp" and not message.get("sessionId"):
            await self._emit({"method": message.get("method"), "params": message.get("params") or {}})

    def browser_gone(self) -> None:
        self.done.set()


async def serve_page(websocket, token: str) -> None:
    entry = _page_tokens.pop(token, None)
    browser = get_browser(entry[1]) if entry and _targets.get(entry[0]) == entry[1] else None
    tab_id = _relay_tab(entry[0], browser, entry[2]) if browser else None
    if tab_id is None:
        await websocket.close(code=4004, reason="Unknown page")
        return
    await websocket.accept()
    tap = PageTap(websocket, browser, tab_id)
    browser.relays[tap.key] = tap
    tasks: set = set()

    async def receive() -> None:
        while True:
            try:
                message = json.loads(await websocket.receive_text())
            except ValueError:
                continue
            if isinstance(message, dict):
                task = asyncio.create_task(tap.handle(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    receiver = asyncio.create_task(receive())
    gone = asyncio.create_task(tap.done.wait())
    try:
        await asyncio.wait({receiver, gone}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (receiver, gone, *tasks):
            task.cancel()
        browser.relays.pop(tap.key, None)
        with contextlib.suppress(Exception):
            await websocket.close()
