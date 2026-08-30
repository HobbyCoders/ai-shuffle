"""CDP screencast hub for the live browser panel.

One :class:`BrowserStreamHub` per session, created on the first viewer and torn
down after the last one leaves. The hub opens a single upstream CDP WebSocket to
the supervised Chromium page published in ``browser_panel_registry`` and drives
``Page.startScreencast``; each JPEG frame is fanned out to every connected
viewer through a per-viewer latest-frame-wins queue. Client input (mouse / key /
navigate / inspect / viewport) is translated — never forwarded raw — into CDP
``Input.*`` / ``Page.navigate`` / ``Runtime.evaluate`` /
``Emulation.setDeviceMetricsOverride`` commands on the same upstream socket.

State machine (per hub, run by :meth:`BrowserStreamHub._run`):
  * no target registered            -> broadcast ``no_page``, wait for change
  * target registered               -> broadcast ``connecting`` -> attach ->
                                        first frame -> ``live``
  * registry change while attached   -> tear down upstream, re-evaluate target
  * upstream dies while attached     -> broadcast ``no_page``, wait for change
  * last viewer leaves               -> stopScreencast, close upstream, drop hub
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlparse

import websockets
from fastapi import WebSocket
from pydantic import BaseModel

from app.core import native_chrome
from app.api.ws_utils import WSPayloadEncodeError, safe_ws_send
from app.core import browser_panel_registry as registry

logger = logging.getLogger(__name__)


# Screencast parameters — JPEG, moderate quality, capped dimensions, every frame.
_SCREENCAST_PARAMS = {
    "format": "jpeg",
    "quality": 60,
    "maxWidth": 1440,
    "maxHeight": 1440,
    "everyNthFrame": 1,
}

# Backoff schedule for re-attaching to a still-registered target after the
# upstream CDP socket dies. Roughly 17.5s of patience before falling back to
# no_page + wait-for-registry-change.
_REATTACH_DELAYS: Tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 5.0, 5.0)

# An attach that stayed up longer than this is considered to have been healthy:
# its eventual death starts a fresh retry budget instead of accumulating.
_ATTACH_HEALTHY_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Client input vocabulary + pure translation
# ---------------------------------------------------------------------------
class _MouseInput(BaseModel):
    """A client mouse event. Extra keys (``type``) are ignored."""

    kind: str
    event: str  # move | down | up | wheel
    x: float
    y: float
    button: str = "left"  # left | middle | right
    clickCount: int = 0
    deltaX: float = 0.0
    deltaY: float = 0.0
    modifiers: int = 0


class _KeyInput(BaseModel):
    """A client key event. Extra keys (``type``) are ignored."""

    kind: str
    event: str  # down | up
    key: str = ""
    code: str = ""
    keyCode: int = 0
    text: str = ""
    modifiers: int = 0


class _NavigateInput(BaseModel):
    url: str


class _CommandInput(BaseModel):
    """A toolbar command: history walk, reload, or stop loading."""

    command: str  # back | forward | reload | stop


class _ViewportInput(BaseModel):
    """Live-view CSS size. Extra keys (``type``) are ignored."""

    width: int
    height: int


# Floor matches a usable live-view; ceiling tracks the screencast cap on
# height and a 1080p-class desktop width so a dragged-wide panel can cross
# the app's 1080px mobile cutoff.
_VIEWPORT_MIN = 200
_VIEWPORT_MAX_WIDTH = 1920
_VIEWPORT_MAX_HEIGHT = 1440


class InspectInput(BaseModel):
    """A point-and-prompt hit-test request at page CSS-pixel coordinates."""

    x: float
    y: float
    mode: str  # hover | select


_MOUSE_CDP_TYPE = {
    "move": "mouseMoved",
    "down": "mousePressed",
    "up": "mouseReleased",
    "wheel": "mouseWheel",
}
_VALID_MOUSE_BUTTONS = {"left", "middle", "right"}


def _translate_mouse(m: _MouseInput, bounds: Optional[Tuple[int, int]]) -> dict:
    if m.event not in _MOUSE_CDP_TYPE:
        raise ValueError(f"invalid mouse event: {m.event!r}")
    if m.button not in _VALID_MOUSE_BUTTONS:
        raise ValueError(f"invalid mouse button: {m.button!r}")

    x, y = float(m.x), float(m.y)
    if bounds is not None:
        w, h = bounds
        x = min(max(x, 0.0), float(w))
        y = min(max(y, 0.0), float(h))

    params: dict = {
        "type": _MOUSE_CDP_TYPE[m.event],
        "x": x,
        "y": y,
        "button": m.button,
        "clickCount": m.clickCount,
        "modifiers": m.modifiers,
    }
    if m.event == "wheel":
        params["deltaX"] = m.deltaX
        params["deltaY"] = m.deltaY
    return {"method": "Input.dispatchMouseEvent", "params": params}


def _translate_key(k: _KeyInput) -> dict:
    if k.event not in ("down", "up"):
        raise ValueError(f"invalid key event: {k.event!r}")

    params: dict = {
        "type": "keyDown" if k.event == "down" else "keyUp",
        "key": k.key,
        "code": k.code,
        "windowsVirtualKeyCode": k.keyCode,
        "modifiers": k.modifiers,
    }
    if k.event == "down":
        text = k.text
        if k.key == "Enter":
            text = "\r"
        if text:
            params["text"] = text
    return {"method": "Input.dispatchKeyEvent", "params": params}


def _translate_navigate(msg: dict) -> dict:
    nav = _NavigateInput(**msg)
    scheme = urlparse(nav.url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"navigate url must be http(s): {nav.url!r}")
    return {"method": "Page.navigate", "params": {"url": nav.url}}


def _translate_command(msg: dict) -> dict:
    """Translate a toolbar command into its first CDP step.

    reload / stop map straight onto Page.* commands. back / forward need the
    page's navigation history first, so they return the ``getNavigationHistory``
    probe carrying a private ``__history_offset__`` marker; ``dispatch_input``
    consumes the marker and the pump loop completes the jump when the reply
    arrives (see :meth:`BrowserStreamHub._complete_history_jump`).
    """
    cmd = _CommandInput(**msg).command
    if cmd == "reload":
        return {"method": "Page.reload", "params": {"ignoreCache": False}}
    if cmd == "stop":
        return {"method": "Page.stopLoading"}
    if cmd in ("back", "forward"):
        return {
            "method": "Page.getNavigationHistory",
            "params": {},
            "__history_offset__": -1 if cmd == "back" else 1,
        }
    raise ValueError(f"invalid command: {cmd!r}")


def _translate_viewport(msg: dict) -> dict:
    """Map a panel-size update onto CDP device-metrics override.

    Headless Chrome's window does not follow the docked panel, so CSS
    ``@media (max-width: 1080px)`` would otherwise stay stuck at the
    launch viewport (or whatever the last agent ``browser_resize`` set).
    The live view's box is the viewport the user is looking at.
    """
    v = _ViewportInput(**msg)
    w, h = int(v.width), int(v.height)
    if w < _VIEWPORT_MIN or h < _VIEWPORT_MIN:
        raise ValueError(f"viewport too small: {w}x{h}")
    w = min(w, _VIEWPORT_MAX_WIDTH)
    h = min(h, _VIEWPORT_MAX_HEIGHT)
    return {
        "method": "Emulation.setDeviceMetricsOverride",
        "params": {
            "width": w,
            "height": h,
            "deviceScaleFactor": 1,
            "mobile": False,
        },
    }


def translate_input(msg: dict, bounds: Optional[Tuple[int, int]] = None) -> Optional[dict]:
    """Translate one client message into a CDP command dict (or None).

    Pure and side-effect free. ``bounds`` is the last frame's
    ``(deviceWidth, deviceHeight)`` used to clamp mouse coordinates. Raises
    ``ValueError``/``ValidationError`` on any invalid or unknown message — the
    caller treats that as a protocol violation.
    """
    mtype = msg.get("type")
    if mtype == "navigate":
        return _translate_navigate(msg)
    if mtype == "command":
        return _translate_command(msg)
    if mtype == "viewport":
        return _translate_viewport(msg)
    if mtype == "input":
        kind = msg.get("kind")
        if kind == "mouse":
            return _translate_mouse(_MouseInput(**msg), bounds)
        if kind == "key":
            return _translate_key(_KeyInput(**msg))
        raise ValueError(f"invalid input kind: {kind!r}")
    raise ValueError(f"unknown client message type: {mtype!r}")


# ---------------------------------------------------------------------------
# Inspect (point-and-prompt element hit-test)
# ---------------------------------------------------------------------------
_VALID_INSPECT_MODES = {"hover", "select"}

# Upper bound on the JSON-encoded element descriptor a page may hand back. The
# in-page script caps every field itself, but the page owns its own JS realm,
# so the server re-checks before fanning anything out to a viewer.
_MAX_INSPECT_RESULT_BYTES = 32_768

# Self-contained hit-test script evaluated in the page. Placeholders __X__ /
# __Y__ / __DETAIL__ are substituted with JSON-encoded values. Returns either
# null or a plain-JSON element descriptor (returnByValue serialisable).
_INSPECT_JS_TEMPLATE = """
(() => {
  const el = document.elementFromPoint(__X__, __Y__);
  if (!el || el.nodeType !== 1) return null;
  const cap = (s, n) =>
    typeof s === 'string' && s.length > n ? s.slice(0, n) + '…' : s;
  const path = [];
  let cur = el;
  while (cur && cur.nodeType === 1 && path.length < 8) {
    let seg = cur.tagName.toLowerCase();
    if (cur.id) {
      path.unshift(seg + '#' + CSS.escape(cur.id));
      break;
    }
    const parent = cur.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(
        (c) => c.tagName === cur.tagName,
      );
      if (same.length > 1) seg += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
    }
    path.unshift(seg);
    cur = parent;
  }
  const r = el.getBoundingClientRect();
  const out = {
    selector: path.join(' > '),
    tag: el.tagName.toLowerCase(),
    id: el.id || null,
    classes: Array.from(el.classList).slice(0, 12).map((c) => cap(c, 80)),
    rect: { x: r.x, y: r.y, width: r.width, height: r.height },
  };
  if (__DETAIL__) {
    out.role = el.getAttribute('role');
    out.text = cap(
      ((el.innerText || el.textContent || '') + '').trim().replace(/\\s+/g, ' '),
      200,
    );
    out.html = cap(el.outerHTML, 800);
    const attrs = {};
    for (const a of Array.from(el.attributes).slice(0, 12)) {
      attrs[cap(a.name, 60)] = cap(a.value, 160);
    }
    out.attrs = attrs;
  }
  return out;
})()
"""


def parse_inspect(msg: dict) -> InspectInput:
    """Validate one client ``inspect`` message. Raises on anything invalid."""
    inspect = InspectInput(**msg)
    if inspect.mode not in _VALID_INSPECT_MODES:
        raise ValueError(f"invalid inspect mode: {inspect.mode!r}")
    if not (
        _is_finite(inspect.x) and _is_finite(inspect.y)
    ):
        raise ValueError("inspect coordinates must be finite")
    return inspect


def _is_finite(v: float) -> bool:
    return v == v and v not in (float("inf"), float("-inf"))


def build_inspect_expression(
    x: float, y: float, detail: bool, bounds: Optional[Tuple[int, int]] = None
) -> str:
    """Build the page-side hit-test expression for one inspect request."""
    if bounds is not None:
        w, h = bounds
        x = min(max(x, 0.0), float(w))
        y = min(max(y, 0.0), float(h))
    return (
        _INSPECT_JS_TEMPLATE
        .replace("__X__", json.dumps(float(x)))
        .replace("__Y__", json.dumps(float(y)))
        .replace("__DETAIL__", "true" if detail else "false")
    )


def sanitize_inspect_result(value: object) -> Optional[dict]:
    """Server-side guard on the page-provided descriptor: dict + bounded size."""
    if not isinstance(value, dict):
        return None
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return None
    if len(encoded) > _MAX_INSPECT_RESULT_BYTES:
        return None
    return value


# ---------------------------------------------------------------------------
# Upstream connection seam (patched in tests)
# ---------------------------------------------------------------------------
async def _open_upstream(uri: str):
    """Open the upstream CDP WebSocket. Overridden in tests with a stub."""
    return await websockets.connect(uri, max_size=None, open_timeout=5)


# ---------------------------------------------------------------------------
# Internal control-flow signals
# ---------------------------------------------------------------------------
class _RegistryChanged(Exception):
    """Raised inside the pump loop when the registry target changed."""


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------
class _Viewer:
    """One connected panel WebSocket.

    Frames arrive on ``queue`` (maxsize 1, drop-oldest) and are drained by a
    dedicated sender task. ``send_lock`` serialises every write to the socket so
    the frame sender, status broadcasts and pings never interleave.
    """

    __slots__ = ("websocket", "queue", "sender_task", "send_lock")

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.sender_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
class BrowserStreamHub:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.viewers: Set[_Viewer] = set()
        self._task: Optional[asyncio.Task] = None
        self._upstream = None
        self._upstream_msg_id = 0
        self._last_metadata: Optional[dict] = None
        # Toolbar state tracked from CDP page events, mirrored to viewers as
        # ``page`` messages. msg-id -> history offset awaiting its reply.
        self._pending_jumps: Dict[int, int] = {}
        # msg-id -> (viewer, mode) awaiting a Runtime.evaluate hit-test reply.
        self._pending_inspects: Dict[int, Tuple["_Viewer", str]] = {}
        self.page_url: Optional[str] = None
        self.page_loading: bool = False
        # Monotonic time the current/last upstream attach opened, for the
        # healthy-attach check in the reattach backoff. None = never opened.
        self._attach_opened_at: Optional[float] = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for viewer in list(self.viewers):
            await self._cancel_sender(viewer)
        self.viewers.clear()

    def viewer_count(self) -> int:
        return len(self.viewers)

    # -- viewers -------------------------------------------------------------
    async def add_viewer(self, websocket: WebSocket) -> _Viewer:
        viewer = _Viewer(websocket)
        self.viewers.add(viewer)
        viewer.sender_task = asyncio.create_task(self._viewer_sender(viewer))
        return viewer

    async def remove_viewer(self, viewer: _Viewer) -> None:
        self.viewers.discard(viewer)
        await self._cancel_sender(viewer)

    async def _cancel_sender(self, viewer: _Viewer) -> None:
        if viewer.sender_task is not None:
            viewer.sender_task.cancel()
            with suppress(asyncio.CancelledError):
                await viewer.sender_task
            viewer.sender_task = None

    async def _viewer_sender(self, viewer: _Viewer) -> None:
        try:
            while True:
                msg = await viewer.queue.get()
                ok = await self.send_to_viewer(viewer, msg)
                if not ok:
                    break
        except asyncio.CancelledError:
            pass

    async def send_to_viewer(self, viewer: _Viewer, msg: dict) -> bool:
        """Serialised write to one viewer. Returns False on a dead connection."""
        async with viewer.send_lock:
            try:
                await safe_ws_send(viewer.websocket, msg)
                return True
            except WSPayloadEncodeError:
                return True  # bad single message, connection still healthy
            except Exception:
                return False

    async def _broadcast_status(self, state: str, url: Optional[str]) -> None:
        msg = {"type": "status", "state": state, "url": url}
        for viewer in list(self.viewers):
            await self.send_to_viewer(viewer, msg)

    def _broadcast_page(self) -> None:
        """Fan out toolbar page state (url + loading), latest-wins, never blocks."""
        msg = {"type": "page", "url": self.page_url, "loading": self.page_loading}
        for viewer in list(self.viewers):
            q = viewer.queue
            if q.full():
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with suppress(asyncio.QueueFull):
                q.put_nowait(msg)

    def _handle_page_event(self, msg: dict) -> bool:
        """Update tracked page state from a CDP event. True if state changed."""
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Page.frameStartedLoading":
            # Subframe loads count too — while any frame loads, show busy.
            self.page_loading = True
            return True
        if method == "Page.frameStoppedLoading":
            self.page_loading = False
            return True
        if method == "Page.frameNavigated":
            frame = params.get("frame", {})
            # Main frames carry no parentId (or an absent/null one).
            if frame.get("parentId") is not None:
                return False
            url = frame.get("url")
            if url is not None and url != self.page_url:
                self.page_url = url
                self.page_loading = False
                return True
            if self.page_loading:
                self.page_loading = False
                return True
        if method == "Page.navigatedWithinDocument":
            url = params.get("url")
            if url is not None and url != self.page_url:
                self.page_url = url
                return True
        return False

    def _fan_out_frame(self, data: str, metadata: dict) -> None:
        """Enqueue a frame to every viewer, latest-frame-wins. Never blocks."""
        msg = {"type": "frame", "data": data, "metadata": metadata}
        for viewer in list(self.viewers):
            q = viewer.queue
            if q.full():
                with suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with suppress(asyncio.QueueFull):
                q.put_nowait(msg)

    # -- input dispatch ------------------------------------------------------
    def last_bounds(self) -> Optional[Tuple[int, int]]:
        md = self._last_metadata
        if not md:
            return None
        w = md.get("deviceWidth")
        h = md.get("deviceHeight")
        if w and h:
            return (int(w), int(h))
        return None

    async def ensure_target(self, url: str) -> bool:
        """Open a CDP tab at ``url`` if this hub has no attached page.

        Returns True if a new tab was created (Chrome already navigated;
        the caller should not also send ``Page.navigate``). False if a page
        is already attached — caller should navigate as usual.
        """
        if self._upstream is not None:
            return False
        created = await native_chrome.create_page(url)
        if not created:
            return False
        registry.register(
            self.session_id,
            created.get("id"),
            None,
            created.get("url") or url,
        )
        return True

    async def dispatch_input(self, cdp_cmd: dict) -> None:
        ws = self._upstream
        if ws is None:
            return
        offset = cdp_cmd.pop("__history_offset__", None)
        if not await self._send_upstream(
            ws,
            cdp_cmd["method"],
            cdp_cmd.get("params"),
            response=offset is not None,
        ):
            return
        if offset is not None:
            # The pump loop pairs the reply (same id) back to this jump.
            self._pending_jumps[self._upstream_msg_id] = int(offset)

    async def dispatch_inspect(self, viewer: "_Viewer", inspect: InspectInput) -> None:
        """Run one point-and-prompt hit-test for ``viewer``.

        Sends ``Runtime.evaluate`` with the in-page hit-test script; the pump
        loop pairs the reply back through ``_pending_inspects`` and answers the
        requesting viewer with an ``inspect-hit`` frame. With no upstream page
        attached the viewer gets an immediate empty hit so its hover/selection
        state clears instead of hanging.
        """
        ws = self._upstream
        if ws is None:
            await self.send_to_viewer(
                viewer, {"type": "inspect-hit", "mode": inspect.mode, "element": None}
            )
            return
        expression = build_inspect_expression(
            inspect.x,
            inspect.y,
            detail=inspect.mode == "select",
            bounds=self.last_bounds(),
        )
        if not await self._send_upstream(
            ws,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            response=True,
        ):
            return
        self._pending_inspects[self._upstream_msg_id] = (viewer, inspect.mode)

    async def _complete_inspect(self, msg_id: int, reply: dict) -> None:
        """Answer the viewer that asked for the hit-test (if still connected)."""
        pending = self._pending_inspects.pop(msg_id, None)
        if pending is None:
            return
        viewer, mode = pending
        if viewer not in self.viewers:
            return
        value = reply.get("result", {}).get("value")
        element = sanitize_inspect_result(value)
        await self.send_to_viewer(
            viewer, {"type": "inspect-hit", "mode": mode, "element": element}
        )

    async def _complete_history_jump(self, msg_id: int, reply: dict) -> None:
        """Finish a back/forward command once getNavigationHistory replies.

        Offset 0 marks the attachment-time history probe: seed the tracked URL
        from the current entry instead of navigating.
        """
        offset = self._pending_jumps.pop(msg_id, None)
        if offset is None:
            return
        entries = reply.get("entries", [])
        index = reply.get("currentIndex", 0)
        if not 0 <= index < len(entries):
            return
        current_url = entries[index].get("url")
        if offset == 0:
            if current_url and current_url != self.page_url:
                self.page_url = current_url
                self._broadcast_page()
            return
        if self._upstream is not None and 0 <= index + offset < len(entries):
            await self._send_upstream(
                self._upstream,
                "Page.navigateToHistoryEntry",
                {"entryId": entries[index + offset]["id"]},
            )

    async def _send_upstream(
        self, ws, method: str, params: Optional[dict] = None, *, response: bool = False
    ) -> bool:
        """Send one CDP command. Returns False on a dead upstream socket."""
        try:
            self._upstream_msg_id += 1
            payload: dict = {"id": self._upstream_msg_id, "method": method}
            if params is not None:
                payload["params"] = params
            await ws.send(json.dumps(payload))
            return True
        except Exception:
            return False

    # -- state machine -------------------------------------------------------
    async def _run(self) -> None:
        try:
            failures = 0
            while True:
                target = registry.get_target(self.session_id)
                if target is None or not target.target_id:
                    await self._broadcast_status("no_page", None)
                    await registry.wait_for_change(self.session_id)
                    continue

                await self._broadcast_status("connecting", target.url)
                try:
                    await self._attach_and_pump(target)
                except _RegistryChanged:
                    failures = 0
                    continue
                except Exception:
                    logger.debug(
                        "browser panel upstream ended for %s",
                        self.session_id,
                        exc_info=True,
                    )
                    # An upstream hiccup while the registry STILL points at this
                    # target is not "no page": the agent's browser tools only
                    # re-report on tab create/close/switch, so waiting for a
                    # registry change here left the panel dead for the rest of
                    # the turn (the mid-stream "reconnecting forever" bug).
                    # Retry the same target with backoff, racing a registry
                    # change; only after the retry budget is spent do we fall
                    # back to no_page + wait. A connection that had been healthy
                    # for a while earns a fresh budget; an instant-death target
                    # accumulates failures until the budget runs out.
                    if self._attach_alive_for() > _ATTACH_HEALTHY_SECONDS:
                        failures = 1
                    else:
                        failures += 1
                    current = registry.get_target(self.session_id)
                    same_target = (
                        current is not None
                        and current.target_id == target.target_id
                    )
                    if same_target and failures <= len(_REATTACH_DELAYS):
                        delay = _REATTACH_DELAYS[failures - 1]
                        change = asyncio.create_task(
                            registry.wait_for_change(self.session_id)
                        )
                        done, _ = await asyncio.wait({change}, timeout=delay)
                        if not done:
                            change.cancel()
                            with suppress(asyncio.CancelledError, Exception):
                                await change
                        continue
                    failures = 0
                    await self._broadcast_status("no_page", None)
                    await registry.wait_for_change(self.session_id)
                    continue
        except asyncio.CancelledError:
            pass

    def _attach_alive_for(self) -> float:
        """Seconds the current/last attach has been open (0 if it never opened)."""
        if self._attach_opened_at is None:
            return 0.0
        return time.monotonic() - self._attach_opened_at

    async def _attach_and_pump(self, target: registry.PanelTarget) -> None:
        uri = (
            f"ws://127.0.0.1:{native_chrome._cdp_port()}"
            f"/devtools/page/{target.target_id}"
        )
        self._attach_opened_at = None
        ws = await _open_upstream(uri)
        self._attach_opened_at = time.monotonic()
        self._upstream = ws
        self._upstream_msg_id = 0
        self._pending_jumps.clear()
        self._pending_inspects.clear()
        self.page_url = target.url
        self.page_loading = False
        change_task: Optional[asyncio.Task] = None
        recv_task: Optional[asyncio.Task] = None
        try:
            await self._send_upstream(ws, "Page.enable")
            await self._send_upstream(ws, "Page.startScreencast", _SCREENCAST_PARAMS)

            # Ask for the current URL up front; the reply lands in the pump loop
            # below via the same pending-jumps channel (offset 0 = no jump).
            await self._send_upstream(ws, "Page.getNavigationHistory", response=True)
            self._pending_jumps[self._upstream_msg_id] = 0

            change_task = asyncio.create_task(registry.wait_for_change(self.session_id))
            recv_task = asyncio.create_task(ws.recv())
            live = False

            while True:
                done, _pending = await asyncio.wait(
                    {change_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if recv_task in done:
                    raw = recv_task.result()  # may raise on upstream close
                    recv_task = asyncio.create_task(ws.recv())
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    method = msg.get("method")
                    if "id" in msg and msg["id"] in self._pending_jumps:
                        is_attach_probe = self._pending_jumps.get(msg["id"]) == 0
                        await self._complete_history_jump(msg["id"], msg.get("result", {}))
                        if is_attach_probe and not live:
                            # The target answered the attach-time probe, so the
                            # attachment is healthy — declare live NOW instead
                            # of waiting for a first screencast frame. Chrome
                            # only paints frames for the FOREGROUND tab, so a
                            # backgrounded or static page may never produce one;
                            # gating "live" on frames kept the panel stuck on
                            # "connecting"/"Reconnecting…" through an entire
                            # streaming turn (the panel-flicker bug).
                            live = True
                            await self._broadcast_status(
                                "live", self.page_url or target.url
                            )
                        continue
                    if "id" in msg and msg["id"] in self._pending_inspects:
                        await self._complete_inspect(msg["id"], msg.get("result", {}))
                        continue
                    changed = False
                    if method in (
                        "Page.frameStartedLoading",
                        "Page.frameStoppedLoading",
                        "Page.frameNavigated",
                        "Page.navigatedWithinDocument",
                    ):
                        changed = self._handle_page_event(msg)
                    elif method is None:
                        continue
                    if changed:
                        self._broadcast_page()
                        continue
                    if method == "Page.screencastFrame":
                        params = msg.get("params", {})
                        metadata = params.get("metadata", {})
                        self._last_metadata = metadata
                        # Enqueue to all viewers (non-blocking), THEN ack — acks
                        # must never wait on a slow viewer.
                        self._fan_out_frame(params.get("data", ""), metadata)
                        with suppress(Exception):
                            await self._send_upstream(
                                ws,
                                "Page.screencastFrameAck",
                                {"sessionId": params.get("sessionId")},
                            )
                        if not live:
                            live = True
                            await self._broadcast_status("live", target.url)
                if change_task in done:
                    change_task.result()
                    current = registry.get_target(self.session_id)
                    # A URL/tab metadata update on the SAME CDP target does not
                    # require an upstream teardown.  Keep the canvas-producing
                    # screencast alive, publish the fresh URL, and arm the next
                    # change waiter.  Only attachment identity changes reattach.
                    if current is not None and current.target_id == target.target_id:
                        target = current
                        await self._broadcast_status(
                            "live" if live else "connecting", current.url
                        )
                        change_task = asyncio.create_task(
                            registry.wait_for_change(self.session_id)
                        )
                        continue
                    raise _RegistryChanged()
        finally:
            if change_task is not None:
                change_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await change_task
            if recv_task is not None:
                recv_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await recv_task
            with suppress(Exception):
                await self._send_upstream(ws, "Page.stopScreencast")
            with suppress(Exception):
                await ws.close()
            self._upstream = None


# ---------------------------------------------------------------------------
# Module-level hub registry
# ---------------------------------------------------------------------------
_hubs: Dict[str, BrowserStreamHub] = {}
_hubs_lock = asyncio.Lock()


async def attach_viewer(session_id: str, websocket: WebSocket) -> Tuple[BrowserStreamHub, _Viewer]:
    async with _hubs_lock:
        hub = _hubs.get(session_id)
        if hub is None:
            hub = BrowserStreamHub(session_id)
            _hubs[session_id] = hub
            hub.start()
        viewer = await hub.add_viewer(websocket)
        return hub, viewer


async def detach_viewer(session_id: str, viewer: _Viewer) -> None:
    async with _hubs_lock:
        hub = _hubs.get(session_id)
        if hub is None:
            return
        await hub.remove_viewer(viewer)
        if hub.viewer_count() == 0:
            await hub.stop()
            _hubs.pop(session_id, None)
