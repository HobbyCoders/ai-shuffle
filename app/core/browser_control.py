"""Ephemeral, bounded session-owned browser command broker (no CDP mutations)."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import time
import uuid
from collections import OrderedDict
from contextlib import suppress

TTL = 60
COMMAND_TTL = 30
MAX_PENDING = 128
MAX_BODY = 262144


def poll_response_size(commands):
    """Match the HTTP JSON envelope and the bridge's UTF-8 response ceiling."""
    return len(json.dumps({'commands': commands}, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).encode('utf-8'))


class ControlError(ValueError):
    pass


def identifier(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not all(c.isalnum() or c in "-_:." for c in value)
    ):
        raise ControlError("Invalid identifier")
    return value


def finite(value):
    if isinstance(value, dict):
        if len(value) > 128:
            raise ControlError("Too many fields")
        for v in value.values():
            finite(v)
    elif isinstance(value, list):
        if len(value) > 128:
            raise ControlError("Too many items")
        for v in value:
            finite(v)
    elif isinstance(value, (float, int)) and (
        abs(value) > 1e15 or not math.isfinite(value)
    ):
        raise ControlError("Invalid number")
    elif isinstance(value, str) and len(value) > 16384:
        raise ControlError("String too long")


def viewport(value):
    if not isinstance(value, dict) or value.get("mode") not in ("fit", "fixed"):
        raise ControlError("Invalid viewport")
    for key, maximum in (("width", 3840), ("height", 2160)):
        if type(value.get(key)) is not int or not 240 <= value[key] <= maximum:
            raise ControlError("Invalid viewport dimensions")


def url(value):
    from urllib.parse import urlsplit

    if not isinstance(value, str) or len(value) > 8192:
        raise ControlError("Invalid URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ControlError("Only http(s) URLs are supported")


def validate_state(state, runtime_id):
    if not isinstance(state, dict):
        raise ControlError("Invalid workspace")
    finite(state)
    if set(state) - {"editors"} != {
        "runtime_id",
        "revision",
        "available",
        "target_id",
        "active_tab_id",
        "tabs",
        "viewport",
        "control",
        "busy",
    }:
        raise ControlError("Invalid workspace fields")
    if (
        state.get("runtime_id") != runtime_id
        or type(state.get("revision")) is not int
        or state["revision"] < 0
    ):
        raise ControlError("Invalid workspace identity/revision")
    for key in ("available", "busy"):
        if type(state.get(key)) is not bool:
            raise ControlError("Invalid workspace flag")
    for key in ("target_id", "active_tab_id"):
        if state.get(key) is not None:
            identifier(state[key])
    editors = state.get("editors", [])
    if not isinstance(editors, list) or len(editors) > 32:
        raise ControlError("Invalid text fields")
    for field in editors:
        if not isinstance(field, dict) or set(field) != {"id", "type", "label", "value", "x", "y", "width", "height"}:
            raise ControlError("Invalid text field")
        identifier(field["id"])
        if field["type"] not in ("text", "search", "email", "url", "tel", "password", "textarea"):
            raise ControlError("Invalid text field type")
        if not isinstance(field["value"], str) or not isinstance(field["label"], str) or len(field["label"]) > 256:
            raise ControlError("Invalid text field content")
        if field["type"] == "password" and field["value"]:
            raise ControlError("Password contents must not be broadcast")
        if any(type(field[k]) not in (int, float) for k in ("x", "y", "width", "height")) or field["width"] <= 0 or field["height"] <= 0:
            raise ControlError("Invalid text field bounds")
    tabs = state.get("tabs")
    if not isinstance(tabs, list) or len(tabs) > 128:
        raise ControlError("Invalid tabs")
    ids = set()
    for tab in tabs:
        if not isinstance(tab, dict) or set(tab) != {"id", "url", "title", "current"}:
            raise ControlError("Invalid tab fields")
        identifier(tab["id"])
        if tab["id"] in ids or type(tab.get("current")) is not bool:
            raise ControlError("Invalid tab")
        ids.add(tab["id"])
        for key, limit in (("url", 8192), ("title", 4096)):
            if not isinstance(tab.get(key), str) or len(tab[key]) > limit:
                raise ControlError("Invalid tab text")
    if state.get("active_tab_id") is not None and state["active_tab_id"] not in ids:
        raise ControlError("Unknown active tab")
    active_tab = state.get("active_tab_id")
    if bool(tabs) != (active_tab is not None) or any(
        tab["current"] != (tab["id"] == active_tab) for tab in tabs
    ):
        raise ControlError("Invalid current tab")
    dimensions = state.get("viewport")
    if not isinstance(dimensions, dict) or dimensions.get("mode") not in (
        "fit",
        "fixed",
    ):
        raise ControlError("Invalid viewport")
    if any(
        type(dimensions.get(key)) is not int or not 1 <= dimensions[key] <= 4096
        for key in ("width", "height")
    ):
        raise ControlError("Invalid viewport dimensions")
    if set(state["viewport"]) != {"mode", "width", "height"}:
        raise ControlError("Invalid viewport fields")
    control = state.get("control")
    if not isinstance(control, dict) or control.get("owner") not in ("agent", "human"):
        raise ControlError("Invalid control")
    if set(control) != {"owner", "viewer_id", "pending_viewer_id"}:
        raise ControlError("Invalid control fields")
    if (control["owner"] == "human") != (control["viewer_id"] is not None):
        raise ControlError("Invalid control owner")
    for key in ("viewer_id", "pending_viewer_id"):
        if control.get(key) is not None:
            identifier(control[key])
    return copy.deepcopy(state)


def unavailable(runtime_id="unavailable"):
    return {
        "runtime_id": runtime_id,
        "revision": 0,
        "available": False,
        "target_id": None,
        "active_tab_id": None,
        "tabs": [],
        "viewport": {"mode": "fixed", "width": 1280, "height": 720},
        "control": {"owner": "agent", "viewer_id": None, "pending_viewer_id": None},
        "busy": False,
    }


class Broker:
    def __init__(self, session_id):
        self.session_id = session_id
        self.runtime_id = None
        self.retired = set()
        self.state = unavailable()
        self.seen = OrderedDict()
        self.pending = {}
        self.queue = []
        self.viewers = {}
        self.event = asyncio.Event()
        self.registration_lock = asyncio.Lock()
        self.last_poll = 0
        self.polling = None
        self.timer = None
        self.closed = False
        self.reserved_viewer = None

    def start(self):
        if self.timer is None:
            self.timer = asyncio.create_task(self._maintain())

    async def send(self, viewer_id, message):
        callback = self.viewers.get(viewer_id)
        if callback:
            with suppress(Exception):
                await asyncio.wait_for(callback(message), 2)

    async def broadcast(self):
        await asyncio.gather(
            *(
                self.send(v, {"type": "workspace", "state": copy.deepcopy(self.state)})
                for v in list(self.viewers)
            )
        )

    async def settle(self, command_id, ok, error=None):
        command = self.pending.pop(command_id, None)
        if command:
            if command["op"] in ("takeover", "release"):
                self.reserved_viewer = None
            message = {"type": "command-result", "id": command_id, "ok": ok}
            if error:
                message["error"] = error[:2048]
            await self.send(command["viewer_id"], message)

    async def retire(self):
        if self.runtime_id:
            self.retired.add(self.runtime_id)
        self.runtime_id = None
        self.reserved_viewer = None
        previous_revision = self.state["revision"]
        self.state = unavailable(self.state["runtime_id"])
        self.state["revision"] = previous_revision + 1
        from app.core import browser_panel_registry as registry

        registry.register(self.session_id, None, None, None)
        self.queue.clear()
        self.event.set()
        await asyncio.gather(
            *(
                self.settle(
                    command_id,
                    False,
                    "Runtime unavailable; outcome unknown, not replayed",
                )
                for command_id in list(self.pending)
            )
        )
        await self.broadcast()

    async def register(self, runtime_id):
        async with self.registration_lock:
            await self._register(runtime_id)

    async def _register(self, runtime_id):
        identifier(runtime_id)
        if runtime_id in self.retired or self.closed:
            raise ControlError("Superseded runtime")
        if self.runtime_id == runtime_id:
            self.check(runtime_id)
            return
        # Never forget a retired ID and accidentally allow it to reclaim ownership.
        # A session with excessive restarts must be cleared/restarted explicitly.
        if len(self.retired) >= 256:
            raise ControlError("Runtime retirement limit reached")
        await self.retire()
        self.runtime_id = runtime_id
        self.state = unavailable(runtime_id)
        self.report_revision = -1
        self.last_poll = time.monotonic()
        self.start()

    def check(self, runtime_id):
        if (
            self.closed
            or runtime_id != self.runtime_id
            or time.monotonic() - self.last_poll > TTL
        ):
            raise ControlError("Superseded or expired runtime")

    def identity(self, data):
        self.check(data.get("runtime_id"))
        if not self.state["available"]:
            raise ControlError("Runtime unavailable")
        if "tab_id" not in data or data["tab_id"] != self.state["active_tab_id"]:
            raise ControlError("Stale tab identity")

    async def poll(self, runtime_id):
        self.check(runtime_id)
        if self.polling == runtime_id:
            raise ControlError("A poll is already active")
        self.polling = runtime_id
        self.last_poll = time.monotonic()
        try:
            if not self.queue:
                self.event.clear()
                try:
                    await asyncio.wait_for(self.event.wait(), 20)
                except asyncio.TimeoutError:
                    pass
            self.check(runtime_id)
            commands = []
            while self.queue and len(commands) < 16:
                candidate = self.queue[0]
                if candidate['id'] not in self.pending or candidate['expires_at'] <= time.time() * 1000:
                    self.queue.pop(0)
                    continue
                if poll_response_size([*commands, candidate]) > MAX_BODY:
                    break  # Leave undelivered commands queued in their original order.
                commands.append(self.queue.pop(0))
            return commands
        finally:
            if self.polling == runtime_id:
                self.polling = None

    async def report(self, runtime_id, state, results):
        self.check(runtime_id)
        state = validate_state(state, runtime_id)
        if not isinstance(results, list) or len(results) > 64:
            raise ControlError("Invalid results")
        for result in results:
            identifier(result["id"])
            if type(result.get("ok")) is not bool or not isinstance(
                result.get("error", ""), str
            ):
                raise ControlError("Invalid result")
        if state["revision"] > self.report_revision:
            self.report_revision = state["revision"]
            self.state = state
            from app.core import browser_panel_registry as registry

            active = next(
                (t for t in state["tabs"] if t["id"] == state["active_tab_id"]), {}
            )
            registry.register(
                self.session_id,
                state["target_id"],
                state["active_tab_id"],
                active.get("url"),
            )
            await self.broadcast()
        for result in results:
            await self.settle(result["id"], result["ok"], result.get("error"))

    async def submit(self, viewer_id, data, internal=False):
        self.identity(data)
        command_id = identifier(data.get("id"))
        if command_id in self.seen:
            raise ControlError("Command already submitted; not replayed")
        op, args = data.get("op"), data.get("args")
        if not isinstance(args, dict):
            raise ControlError("Invalid command arguments")
        finite(args)
        allowed = {
            "takeover": set(),
            "release": set(),
            "heartbeat": set(),
            "navigate": {"url"},
            "command": {"command"},
            "tabs": {"action", "tab_id", "url"},
            "viewport": {"mode", "width", "height"},
            "input": {"method", "params"},
            "edit": {"field", "action", "value", "start", "end"},
        }
        if op not in allowed or set(args) - allowed[op]:
            raise ControlError("Unsupported operation or arguments")
        control = self.state["control"]
        if op not in ("takeover", "release", "heartbeat") and (
            control["owner"] != "human" or control["viewer_id"] != viewer_id
        ):
            raise ControlError("Take control before changing the browser")
        if op in ("takeover", "release", "heartbeat"):
            if args or (op == "heartbeat" and not internal):
                raise ControlError("Invalid control command")
            owner = (
                self.reserved_viewer
                or control.get("pending_viewer_id")
                or control.get("viewer_id")
            )
            if owner is not None and owner != viewer_id:
                raise ControlError("Another viewer controls the browser")
        elif op == "navigate":
            url(args.get("url"))
        elif op == "viewport":
            viewport(args)
        elif op == "command":
            if args.get("command") not in ("back", "forward", "reload", "stop"):
                raise ControlError("Invalid navigation command")
        elif op == "tabs":
            if args.get("action") not in ("create", "select", "close"):
                raise ControlError("Invalid tab action")
            if args["action"] != "create":
                identifier(args.get("tab_id"))
            if "url" in args:
                if args["action"] != "create":
                    raise ControlError("URL only allowed for tab creation")
                url(args["url"])
        elif op == "edit":
            identifier(args.get("field"))
            if args.get("action") not in ("focus", "blur", "replace"):
                raise ControlError("Invalid text edit")
            if args["action"] == "replace":
                value = args.get("value")
                if not isinstance(value, str) or any(type(args.get(k)) is not int for k in ("start", "end")) or not 0 <= args["start"] <= args["end"] <= len(value.encode('utf-16-le')) // 2:
                    raise ControlError("Invalid text selection")
        elif op == "input" and internal:
            if args.get("method") not in (
                "Input.dispatchMouseEvent",
                "Input.dispatchKeyEvent",
                "Input.insertText",
            ):
                raise ControlError("Invalid input")
            if (
                args["method"] == "Input.insertText"
                and len(args.get("params", {}).get("text", "")) > 16384
            ):
                raise ControlError("Committed text exceeds 16384 characters")
        else:
            raise ControlError("Unsupported operation")
        if len(self.seen) >= 100000:
            raise ControlError("Command history full; restart session")
        if len(self.pending) >= MAX_PENDING:
            raise ControlError("Browser command queue full")
        command = {
            "id": command_id,
            "runtime_id": self.runtime_id,
            "tab_id": data["tab_id"],
            "viewer_id": viewer_id,
            "op": op,
            "args": copy.deepcopy(args),
            "expires_at": time.time() * 1000 + COMMAND_TTL * 1000,
        }
        if poll_response_size([command]) > MAX_BODY:
            raise ControlError('Browser command exceeds delivery byte limit')
        if op == "takeover":
            self.reserved_viewer = viewer_id
        self.pending[command_id] = command
        self.seen[command_id] = None
        self.queue.append(command)
        self.event.set()

    async def internal(self, viewer_id, op):
        try:
            await self.submit(
                viewer_id,
                {
                    "id": str(uuid.uuid4()),
                    "runtime_id": self.runtime_id,
                    "tab_id": self.state["active_tab_id"],
                    "op": op,
                    "args": {},
                },
                internal=True,
            )
        except ControlError:
            pass  # MCP lease is the bounded fallback when a runtime/queue is gone.

    async def detach(self, viewer_id):
        self.viewers.pop(viewer_id, None)
        for cid, command in list(self.pending.items()):
            if command["viewer_id"] == viewer_id:
                self.pending.pop(cid, None)
        self.queue = [c for c in self.queue if c["id"] in self.pending]
        await self.internal(viewer_id, "release")

    async def _maintain(self):
        tick = 0
        try:
            while True:
                await asyncio.sleep(1)
                if self.runtime_id and time.monotonic() - self.last_poll > TTL:
                    await self.retire()
                for cid, command in list(self.pending.items()):
                    if command["expires_at"] <= time.time() * 1000:
                        await self.settle(
                            cid,
                            False,
                            "Command timed out; outcome unknown, not replayed",
                        )
                self.queue = [c for c in self.queue if c["id"] in self.pending]
                tick += 1
                if tick % 20 == 0:
                    owner = (
                        self.reserved_viewer
                        or self.state["control"].get("pending_viewer_id")
                        or self.state["control"].get("viewer_id")
                    )
                    if owner in self.viewers:
                        await self.internal(owner, "heartbeat")
        except asyncio.CancelledError:
            pass

    def close(self):
        self.closed = True
        self.event.set()
        if self.timer:
            self.timer.cancel()
        self.pending.clear()
        self.queue.clear()
        self.viewers.clear()


_brokers = {}


def get(session_id):
    if session_id not in _brokers:
        _brokers[session_id] = Broker(session_id)
    return _brokers[session_id]


def clear(session_id):
    broker = _brokers.pop(session_id, None)
    if broker:
        broker.close()
