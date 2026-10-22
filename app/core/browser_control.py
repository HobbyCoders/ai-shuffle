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
COPY_LIMIT = 16384  # longest page selection a viewer can copy at once
# A select's options for the panel's own list (matches the runtime's bounds).
MAX_CHOICES = 1000
CHOICE_LABEL = 200
CHOICE_TEXT_BUDGET = 24000
TEXT_FIELDS = ("text", "search", "email", "url", "tel", "password", "textarea", "file")
# Controls whose popup Chrome draws outside the screencast: the panel shows
# its own list or calendar for these.
PICKER_FIELDS = ("select", "date", "time", "datetime-local", "month", "week")
MAX_BODY = 262144


def poll_response_size(commands):
    """Bound the HTTP envelope, reserving space for the live viewer count."""
    return len(json.dumps({'commands': commands, 'viewer_count': (1 << 63) - 1}, ensure_ascii=False,
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


def validate_choices(value):
    """A select's options, sent only to the viewer that opened the list.

    Labels, groups and disabled state; never option values (the panel picks
    by index).
    """
    if not isinstance(value, dict) or set(value) - {"truncated"} != {"selected", "groups", "options"}:
        raise ControlError("Invalid options")
    groups, options = value["groups"], value["options"]
    if not isinstance(groups, list) or not isinstance(options, list) or len(options) > MAX_CHOICES or len(groups) > len(options) + 1:
        raise ControlError("Invalid options")
    if "truncated" in value and type(value["truncated"]) is not bool:
        raise ControlError("Invalid options")
    budget = CHOICE_TEXT_BUDGET
    for label in groups:
        if not isinstance(label, str) or len(label) > CHOICE_LABEL:
            raise ControlError("Invalid option group")
        budget -= len(label)
    for option in options:
        if not isinstance(option, dict) or set(option) - {"group"} != {"label", "disabled"}:
            raise ControlError("Invalid option")
        if not isinstance(option["label"], str) or len(option["label"]) > CHOICE_LABEL or type(option["disabled"]) is not bool:
            raise ControlError("Invalid option")
        if "group" in option and (type(option["group"]) is not int or not 0 <= option["group"] < len(groups)):
            raise ControlError("Invalid option group")
        budget -= len(option["label"])
    if budget < 0:
        raise ControlError("Options too long")
    if type(value["selected"]) is not int or not -1 <= value["selected"] < len(options):
        raise ControlError("Invalid selected option")
    return value


def finite_report(body):
    """finite() for a runtime payload whose results may carry a long option list.

    Option lists are bounded by validate_choices() instead of the generic
    128-item list cap; everything else gets the usual checks.
    """
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or not any(isinstance(r, dict) and "choices" in r for r in results):
        finite(body)
        return
    if len(results) > 64:
        raise ControlError("Invalid results")
    for result in results:
        if isinstance(result, dict) and "choices" in result:
            validate_choices(result["choices"])
            for label in (*result["choices"]["groups"], *(o["label"] for o in result["choices"]["options"])):
                finite(label)
    finite({**body, "results": [
        {k: v for k, v in r.items() if k != "choices"} if isinstance(r, dict) else r for r in results
    ]})


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
    if set(state) - {"editors", "notice"} != {
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
    # A plain viewer notice, e.g. a captcha the site withheld (#2378).
    if "notice" in state and (not isinstance(state["notice"], str) or len(state["notice"]) > 512):
        raise ControlError("Invalid workspace notice")
    editors = state.get("editors", [])
    if not isinstance(editors, list) or len(editors) > 32:
        raise ControlError("Invalid text fields")
    for field in editors:
        if not isinstance(field, dict) or set(field) - {"accept", "multiple", "chooser", "keys", "style", "min", "max", "step", "base", "zone", "focused"} != {"id", "type", "label", "value", "x", "y", "width", "height"}:
            raise ControlError("Invalid text field")
        identifier(field["id"])
        if field["type"] not in TEXT_FIELDS + PICKER_FIELDS:
            raise ControlError("Invalid text field type")
        if field["type"] not in PICKER_FIELDS and {"min", "max", "step", "base", "zone", "focused"} & set(field):
            raise ControlError("Picker details on a text field")
        # base: the page's value attribute, which steps count from without a
        # min; zone: the browser's IANA time zone, for the panel's Today/Now.
        for name in ("min", "max", "step", "base", "zone"):
            if name in field and (not isinstance(field[name], str) or len(field[name]) > 64):
                raise ControlError("Invalid picker limit")
        if "focused" in field and type(field["focused"]) is not bool:
            raise ControlError("Invalid picker focus flag")
        if not isinstance(field["value"], str) or not isinstance(field["label"], str) or len(field["label"]) > 256:
            raise ControlError("Invalid text field content")
        if "style" in field:
            style = field["style"]
            if not isinstance(style, dict) or set(style) != {"fontSize", "lineHeight", "paddingLeft", "paddingTop", "borderLeft", "borderTop", "fontFamily"}:
                raise ControlError("Invalid field style")
            if not isinstance(style["fontFamily"], str) or len(style["fontFamily"]) > 200:
                raise ControlError("Invalid field font")
            for name in ("fontSize", "lineHeight", "paddingLeft", "paddingTop", "borderLeft", "borderTop"):
                if name == "lineHeight" and style[name] is None:
                    continue
                if type(style[name]) not in (int, float) or not 0 <= style[name] <= 1000:
                    raise ControlError("Invalid field typography")
        if "accept" in field and (not isinstance(field["accept"], str) or len(field["accept"]) > 1024):
            raise ControlError("Invalid file accept filter")
        if "chooser" in field and type(field["chooser"]) is not bool:
            raise ControlError("Invalid file chooser flag")
        if "keys" in field and type(field["keys"]) is not bool:
            raise ControlError("Invalid keystroke flag")
        if "multiple" in field and type(field["multiple"]) is not bool:
            raise ControlError("Invalid multiple-file flag")
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
        self.result_waiters = {}
        self.queue = []
        self.viewers = {}
        self.last_viewer_detached = 0.0
        self._viewers_changed = False
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

    def attach(self, viewer_id, callback):
        self.viewers[viewer_id] = callback
        self._viewers_changed = True
        self.event.set()

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

    async def settle(self, command_id, ok, error=None, text=None, choices=None):
        command = self.pending.pop(command_id, None)
        if command:
            if command["op"] in ("takeover", "release"):
                self.reserved_viewer = None
            message = {"type": "command-result", "id": command_id, "ok": ok}
            if error:
                message["error"] = error[:2048]
            # A copy goes only to the viewer that asked for it; never logged,
            # broadcast or kept in workspace state.
            if text is not None and command["op"] == "copy":
                message["text"] = text
            # A select's options likewise go only to the viewer that opened it.
            if choices is not None and command["op"] == "options":
                message["choices"] = choices
            waiter = self.result_waiters.get(command_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(message)
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
            if not self.queue and not self._viewers_changed:
                self.event.clear()
                try:
                    await asyncio.wait_for(self.event.wait(), 20)
                except asyncio.TimeoutError:
                    pass
            self.check(runtime_id)
            self._viewers_changed = False
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
            copied = result.get("text")
            if copied is not None and (
                not isinstance(copied, str) or len(copied) > COPY_LIMIT
            ):
                raise ControlError("Invalid copied text")
            if "choices" in result:
                validate_choices(result["choices"])
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
            await self.settle(
                result["id"], result["ok"], result.get("error"), result.get("text"), result.get("choices")
            )

    async def submit(self, viewer_id, data, internal=False):
        from app.core.native_chrome import wait_for_recovery

        await wait_for_recovery()
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
            "copy": set(),
            "navigate": {"url"},
            "command": {"command"},
            "tabs": {"action", "tab_id", "url"},
            "viewport": {"mode", "width", "height"},
            "input": {"method", "params"},
            "edit": {"field", "action", "value", "start", "end", "index", "label"},
            "upload": {"field", "paths"},
            "options": {"field"},
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
        elif op == "upload" and internal:
            identifier(args.get("field"))
            paths = args.get("paths")
            if not isinstance(paths, list) or not 1 <= len(paths) <= 10 or any(not isinstance(p, str) or not p for p in paths):
                raise ControlError("Invalid upload files")
        elif op == "copy":
            if args:
                raise ControlError("Invalid copy")
        elif op == "options":
            identifier(args.get("field"))
        elif op == "edit":
            identifier(args.get("field"))
            if args.get("action") not in ("focus", "blur", "replace", "set"):
                raise ControlError("Invalid text edit")
            if ("index" in args or "label" in args) and args["action"] != "set":
                raise ControlError("Invalid text edit")
            if args["action"] == "set":
                # A picker choice: a select option by index with the label the
                # panel showed (a refilled list is refused by the runtime), or a
                # date/time value.
                if "start" in args or "end" in args:
                    raise ControlError("Invalid choice")
                if "index" in args:
                    if "value" in args or type(args["index"]) is not int or not 0 <= args["index"] <= 9999:
                        raise ControlError("Invalid choice")
                    if not isinstance(args.get("label"), str) or len(args["label"]) > CHOICE_LABEL:
                        raise ControlError("Invalid choice")
                elif "label" in args or not isinstance(args.get("value"), str) or len(args["value"]) > 64:
                    raise ControlError("Invalid choice")
            elif args["action"] == "replace":
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

    async def submit_and_wait(self, viewer_id, data):
        """HTTP-owned upload: keep staged files alive until the runtime settles."""
        command_id = identifier(data.get("id"))
        future = asyncio.get_running_loop().create_future()
        if command_id in self.result_waiters:
            raise ControlError("Command already waiting")
        self.result_waiters[command_id] = future
        try:
            await self.submit(viewer_id, data, internal=True)
            return await asyncio.wait_for(future, COMMAND_TTL + 5)
        finally:
            self.result_waiters.pop(command_id, None)

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
        if viewer_id in self.viewers:
            self.viewers.pop(viewer_id)
            if not self.viewers:
                self.last_viewer_detached = time.monotonic()
        self._viewers_changed = True
        self.event.set()
        for cid, command in list(self.pending.items()):
            if command["viewer_id"] == viewer_id:
                await self.settle(cid, False, "Viewer disconnected; outcome unknown")
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


def recovery_blocked():
    """Do not repair shared Chrome while a viewer or queued command owns it."""
    return any(
        broker.pending or broker.reserved_viewer or broker.state.get("busy")
        or broker.state["control"].get("viewer_id")
        or broker.state["control"].get("pending_viewer_id")
        or broker.state["control"].get("owner") == "human"
        for broker in _brokers.values()
    )


def get(session_id):
    if session_id not in _brokers:
        _brokers[session_id] = Broker(session_id)
    return _brokers[session_id]


def clear(session_id):
    broker = _brokers.pop(session_id, None)
    if broker:
        broker.close()
