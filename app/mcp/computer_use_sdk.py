"""Computer-use SDK MCP server — drive the host desktop.

Exposes `mcp__computer-use__*` tools whose **names and schemas mirror Anthropic's
official computer-use MCP** (coordinate arrays, scroll direction/amount, zoom,
open_application, request_access, computer_batch, …) so the model uses them with
its trained conventions instead of a bespoke interface.

This controls the REAL machine the backend runs on (like Claude Desktop): one
shared cursor. Native mode only — auto-disabled in Docker/headless.

Permission model (faithful to the official `request_access` flow, keyed to the
session's permission mode — enforced in query_engine's can_use_tool callback):
- bypassPermissions -> implicit access, no request needed
- default/ask/auto/acceptEdits -> request_access surfaces an approve/deny prompt
Action tools refuse until access is granted (see app/mcp/computer_use/access.py),
and always check their session's halt flag first (the chat Stop button
sets it; the session's next turn clears it).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import sys
from typing import Any, List, Optional

from app.mcp.computer_use import access, activity, abort, safety
from app.mcp.computer_use.abort import is_stopped, Aborted as _Aborted
from app.mcp.computer_use.scaling import DisplayManager, computer_use_available, foreground_display

logger = logging.getLogger(__name__)


_MOVE_SETTLE = 0.04
_MAX_WAIT = 60.0

# Per-character delay for typing. pynput fires synthetic key events with no
# pacing; on Windows a fast burst drops key-downs and lets the Shift state bleed
# into adjacent keys (capitals come out as their Shift+digit symbols). A small
# inter-key delay serializes the events and fixes it. Tunable via env.
try:
    import os as _os
    _TYPE_DELAY = max(0.0, float(_os.environ.get("COMPUTER_USE_TYPE_DELAY", "0.012")))
except Exception:
    _TYPE_DELAY = 0.012

# Above this length (or for multiline text) we prefer the clipboard paste fast
# path when clipboard-write has been granted — instant and immune to key timing.
_TYPE_CLIPBOARD_THRESHOLD = 40

# Tool names mirror the official server. (list_displays is an extra convenience.)
_TOOL_NAMES = [
    "request_access", "list_granted_applications",
    "screenshot", "zoom", "cursor_position", "list_displays", "switch_display",
    "mouse_move", "left_click", "right_click", "middle_click",
    "double_click", "triple_click", "left_click_drag",
    "left_mouse_down", "left_mouse_up",
    "key", "hold_key", "type", "scroll", "wait",
    "open_application", "read_clipboard", "write_clipboard",
    "computer_batch",
    "release_control",
]


def get_computer_use_tool_names() -> List[str]:
    return [f"mcp__computer-use__{t}" for t in _TOOL_NAMES]


# ---------------------------------------------------------------------------
# Content / result helpers
# ---------------------------------------------------------------------------

def _text_item(payload: dict) -> dict:
    return {"type": "text", "text": json.dumps(payload)}


def _img_item(png: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"}


def _ok(payload: dict) -> dict:
    return {"content": [_text_item({"success": True, **payload})]}


def _err(message: str, **extra: Any) -> dict:
    return {"isError": True, "content": [_text_item({"success": False, "error": message, **extra})]}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_computer_use_server(
    *,
    enabled: bool = False,
    target_display: Optional[int] = None,
    virtual_size: tuple[int, int] = (1280, 800),
    permission_mode: Optional[str] = None,
    session_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    require_confirmation: bool = False,  # accepted for back-compat; unused
) -> Optional[Any]:
    """Build the `computer-use` SDK MCP server, or None if unavailable/disabled."""
    if not enabled:
        return None
    available, reason = computer_use_available()
    if not available:
        logger.info("computer-use disabled (%s) — server not created", reason)
        return None
    try:
        from app.mcp.inproc import tool, create_tool_server
        from mcp.types import ToolAnnotations
    except ImportError:
        return None
    try:
        from pynput.mouse import Controller as MouseController, Button
        from pynput.keyboard import Controller as KeyboardController, Key
    except Exception:
        logger.info("pynput unavailable — computer-use disabled")
        return None

    from app.mcp.sdk_compat import patch_sdk_server
    from app.mcp.computer_use.keymap import parse_chord, parse_modifiers
    from app.mcp.computer_use import overlay

    mgr = DisplayManager(virtual_size=virtual_size, target_display=target_display)
    overlay.ensure_started()
    safety.ensure_started()
    mouse = MouseController()
    keyboard = KeyboardController()
    inputs = safety.SafeInput(session_id, mouse, keyboard)
    big = ToolAnnotations(maxResultSizeChars=8_000_000)

    _CLICKS = {
        "left_click": (Button.left, 1), "right_click": (Button.right, 1),
        "middle_click": (Button.middle, 1), "double_click": (Button.left, 2),
        "triple_click": (Button.left, 3),
    }

    # ----- access / abort gating --------------------------------------------

    def _access_error() -> Optional[dict]:
        if permission_mode == "bypassPermissions":
            return None
        if not access.is_granted(session_id):
            return _err(
                "No access yet. Call request_access(apps=[...], reason='...') first; "
                "the user must approve before you can see or control the screen."
            )
        return None

    def _guard() -> Optional[dict]:
        if is_stopped(session_id):
            return _err("Computer-use was stopped for this session. Start a new turn to continue.")
        from app.api.computer_use import get_computer_use_enabled
        if not get_computer_use_enabled():
            return _err("Computer use is disabled in Settings.")
        return _access_error()

    # ----- the synchronous action executor (shared by tools + batch) --------

    def _record(action: str, cursor=None, geo=None):
        if cursor is not None:
            geo = next((d for d in mgr.list_displays() if
                        d.left <= cursor[0] < d.left + d.width and
                        d.top <= cursor[1] < d.top + d.height), geo)
        elif action in ("key", "hold_key", "type", "open_application"):
            geo = foreground_display(mgr.list_displays())
        geo = geo or mgr._selected_geometry()
        with abort.input_guard(session_id, inputs.token):
            activity.record(geo.index, session_id=session_id, action=action,
                            geometry=geo.to_dict(), cursor=cursor)

    def _move(point, action):
        inputs.call(setattr, mouse, "position", point)
        _record(action, cursor=point)

    def _check_pointer(point):
        inputs.check()
        if tuple(mouse.position) != tuple(point):
            # If the user moved it during the settle delay, don't click there.
            abort.request_stop(session_id, reason="pointer_moved")
            raise _Aborted()

    def _chord(mods, main, duration=0):
        try:
            for key in [*mods, main]:
                inputs.press(keyboard, key)
            inputs.sleep(duration)
        finally:
            for key in reversed([*mods, main]):
                inputs.release(keyboard, key)

    def _type_text(text: str) -> str:
        """Type text reliably. Returns the method used ('paced' or 'clipboard').

        Long/multiline text uses a clipboard paste when clipboardWrite is granted
        (instant, immune to key timing). Otherwise types character-by-character
        with a small delay so the OS input queue keeps up and Shift toggles
        cleanly between keys — fixing dropped/garbled characters in long strings.
        """
        if not text:
            return "noop"
        long_or_multiline = len(text) >= _TYPE_CLIPBOARD_THRESHOLD or "\n" in text
        if long_or_multiline and access.info(session_id).get("clipboard_write"):
            try:
                inputs.check()
                _clipboard_write(text)
                inputs.sleep(0.05)
                _chord([Key.ctrl], "v")
                inputs.sleep(0.05)
                return "clipboard"
            except _Aborted:
                raise
            except Exception:
                logger.debug("clipboard type fast-path failed; falling back to paced", exc_info=True)
        for ch in text:
            if ch == "\n":
                _chord([], Key.enter)
            elif ch == "\t":
                _chord([], Key.tab)
            elif ch != "\r":
                inputs.call(keyboard.type, ch)
            if _TYPE_DELAY:
                inputs.sleep(_TYPE_DELAY)
        return "paced"

    def _execute(action: str, p: dict) -> list:
        inputs.check()
        _record(action)

        if action in ("read_clipboard", "write_clipboard"):
            flag = "clipboard_read" if action == "read_clipboard" else "clipboard_write"
            if permission_mode != "bypassPermissions" and not access.info(session_id).get(flag):
                raise ValueError(f"{flag} is not granted; call request_access first")
            if action == "read_clipboard":
                text = _clipboard_read()
                inputs.check()
                return [_text_item({"success": True, "text": text})]
            _clipboard_write(str(p.get("text") or ""))
            inputs.check()
            return [_text_item({"success": True, "action": action})]

        if action == "screenshot":
            png, info = mgr.capture()
            _record(action)
            inputs.check()
            return [_text_item({"success": True, **info, **_display_note(),
                                "cursor": _virtual_cursor(*mouse.position)}), _img_item(png)]
        if action == "zoom":
            png, info = mgr.capture_region(p["region"])
            inputs.check()
            return [_text_item({"success": True, **info}), _img_item(png)]
        if action == "cursor_position":
            gx, gy = mouse.position
            return [_text_item({"success": True, **_virtual_cursor(gx, gy)})]
        if action == "mouse_move":
            gx, gy = mgr.to_physical(*p["coordinate"])
            _move((gx, gy), action)
            return [_text_item({"success": True, "action": action, "x": gx, "y": gy})]
        if action in _CLICKS:
            button, count = _CLICKS[action]
            gx, gy = mgr.to_physical(*p["coordinate"])
            mods = parse_modifiers(p.get("text", ""))
            _move((gx, gy), action)
            inputs.sleep(_MOVE_SETTLE)
            _check_pointer((gx, gy))
            try:
                for m in mods:
                    inputs.press(keyboard, m)
                for i in range(count):
                    inputs.press(mouse, button)
                    inputs.release(mouse, button)
                    if i < count - 1:
                        inputs.sleep(0.05)
            finally:
                inputs.release(mouse, button)
                for m in reversed(mods):
                    inputs.release(keyboard, m)
            return [_text_item({"success": True, "action": action, "x": gx, "y": gy})]
        if action == "left_click_drag":
            # Validate both endpoints before pressing any button.
            ex, ey = mgr.to_physical(*p["coordinate"])
            if p.get("start_coordinate"):
                sx, sy = mgr.to_physical(*p["start_coordinate"])
                _move((sx, sy), action)
                inputs.sleep(_MOVE_SETTLE)
                _check_pointer((sx, sy))
            sx, sy = mouse.position
            try:
                inputs.press(mouse, Button.left)
                for step in range(1, 21):
                    _move((round(sx + (ex - sx) * step / 20),
                           round(sy + (ey - sy) * step / 20)), action)
                    inputs.sleep(0.015)
            finally:
                inputs.release(mouse, Button.left)
            return [_text_item({"success": True, "action": action, "x": ex, "y": ey})]
        if action == "left_mouse_down":
            inputs.press(mouse, Button.left)
            _record(action, cursor=mouse.position)
            return [_text_item({"success": True, "action": action})]
        if action == "left_mouse_up":
            inputs.release(mouse, Button.left)
            return [_text_item({"success": True, "action": action})]
        if action == "type":
            method = _type_text(str(p.get("text") or ""))
            return [_text_item({"success": True, "action": action, "method": method})]
        if action == "key":
            mods, main = parse_chord(str(p.get("text") or ""))
            repeat = max(1, min(100, int(p.get("repeat", 1) or 1)))
            for _ in range(repeat):
                _chord(mods, main)
            return [_text_item({"success": True, "action": action})]
        if action == "hold_key":
            mods, main = parse_chord(str(p.get("text") or ""))
            dur = max(0.0, min(100.0, float(p.get("duration", 0) or 0)))
            _chord(mods, main, dur)
            return [_text_item({"success": True, "action": action})]
        if action == "scroll":
            gx, gy = mgr.to_physical(*p["coordinate"])
            amt = int(p.get("scroll_amount", 3))
            if amt < 0 or amt > 100:
                raise ValueError("scroll_amount must be between 0 and 100")
            direction = p.get("scroll_direction", "down")
            if direction not in ("up", "down", "left", "right"):
                raise ValueError("scroll_direction must be up, down, left, or right")
            _move((gx, gy), action)
            inputs.sleep(_MOVE_SETTLE)
            _check_pointer((gx, gy))
            dx, dy = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}[direction]
            for _ in range(amt):
                inputs.call(mouse.scroll, dx, dy)
                inputs.sleep(0.01)
            return [_text_item({"success": True, "action": action, "direction": direction})]
        if action == "wait":
            inputs.sleep(max(0.0, min(_MAX_WAIT, float(p.get("duration", 0) or 0))))
            return [_text_item({"success": True, "action": action})]
        if action == "open_application":
            _open_app(str(p.get("app") or ""))
            return [_text_item({"success": True, "action": action, "app": p.get("app")})]
        raise ValueError(f"unknown action: {action}")

    def _open_app(name: str) -> None:
        # Launch/foreground via the Start menu: tap Win, type, Enter. Robust
        # across installed apps without resolving executables.
        if not name.strip():
            raise ValueError("application name cannot be empty")
        if sys.platform != "win32":
            raise ValueError("open_application is supported on Windows only")
        _chord([], Key.cmd)
        inputs.sleep(0.5)
        _type_text(name)
        inputs.sleep(0.6)
        _chord([], Key.enter)
        inputs.sleep(0.8)

    def _virtual_cursor(gx: float, gy: float) -> dict:
        actual = next((d for d in mgr.list_displays()
                       if d.left <= gx < d.left + d.width and d.top <= gy < d.top + d.height), None)
        on_selected = actual is not None and actual.index == mgr.selected_index
        out = {"physical_x": int(gx), "physical_y": int(gy),
               "display": actual.index if actual else None,
               "selected_display": mgr.selected_index, "on_selected_display": on_selected}
        m = mgr._map.get(mgr.selected_index)
        if m and on_selected:
            out["x"] = int(round((gx - m["left"]) * m["scale"]))
            out["y"] = int(round((gy - m["top"]) * m["scale"]))
        return out

    def _display_note() -> dict:
        try:
            ds = mgr.list_displays()
            return {
                "current_display": f"Display {mgr.selected_index}",
                "available_displays": [f"Display {d.index}" for d in ds],
            }
        except Exception:
            return {}

    # ----- async wrapper -----------------------------------------------------

    def _serialized(fn, token):
        # Multiple calls and sessions share one desktop. Never interleave a
        # click/type batch, a display switch, and another action's coordinates.
        # Don't queue stale actions behind an owner: Escape must not hand the
        # desktop straight to a previously blocked call from another chat.
        if not safety.DESKTOP_LOCK.acquire(blocking=False):
            raise ValueError("Computer use is busy with another action. Retry after it finishes.")
        try:
            if inputs.token != token:
                mgr._map.clear()
            inputs.token = token
            inputs.check()
            others = set(activity.active_sessions()) - {session_id or "_global"}
            if others:
                raise ValueError("Another chat is using the desktop. Wait for it to finish.")
            try:
                return fn()
            except BaseException:
                inputs.release_all()
                raise
        finally:
            activity.idle(session_id)
            safety.DESKTOP_LOCK.release()

    async def _run(action: str, params: dict) -> dict:
        err = _guard()
        if err:
            return err
        try:
            token = abort.generation(session_id)
            content = await asyncio.to_thread(_serialized, lambda: _execute(action, params), token)
            return {"content": content}
        except _Aborted:
            return _err("aborted — computer-use was stopped for this session")
        except asyncio.CancelledError:
            abort.request_stop(session_id)
            raise
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            logger.exception("computer-use %s failed", action)
            return _err(f"{action} failed: {e}")

    tools: list = []

    # ----- schemas -----------------------------------------------------------
    COORD = {"coordinate": {"type": "array", "items": {"type": "number"},
                            "description": "[x, y] in pixels of the most recent screenshot."}}
    MODS = {"text": {"type": "string", "description": "Modifier keys to hold, e.g. 'shift', 'ctrl+shift'."}}

    # ----- access tools ------------------------------------------------------
    @tool(
        "request_access",
        "Request permission to control a set of applications for this session. Call "
        "this FIRST, before any other tool. The user approves or denies. apps are "
        "Windows Start-menu display names (e.g. 'Notepad', 'File Explorer'). Returns the "
        "granted apps.",
        {
            "type": "object",
            "properties": {
                "apps": {"type": "array", "items": {"type": "string"},
                         "description": "App display names, e.g. ['Notepad','Microsoft Edge']."},
                "reason": {"type": "string", "description": "One sentence shown to the user explaining the task."},
                "clipboardRead": {"type": "boolean"},
                "clipboardWrite": {"type": "boolean"},
                "systemKeyCombos": {"type": "boolean"},
            },
            "required": ["apps", "reason"],
        },
    )
    async def request_access(args: dict) -> dict:
        if is_stopped(session_id):
            return _err("Computer-use was stopped. Start a new turn to request access again.")
        # Approval already enforced by the query callback (by permission mode);
        # if we got here, record the grant.
        granted = access.grant(
            session_id, args.get("apps") or [],
            clipboard_read=bool(args.get("clipboardRead")),
            clipboard_write=bool(args.get("clipboardWrite")),
            system_keys=bool(args.get("systemKeyCombos")),
        )
        return _ok({
            "granted_apps": granted["apps"], "denied_apps": [],
            "clipboard_read": granted["clipboard_read"],
            "clipboard_write": granted["clipboard_write"],
            "screenshot_filtering": False,
        })
    tools.append(request_access)

    @tool("list_granted_applications",
          "List the applications granted for this session, plus active grant flags.",
          {"type": "object", "properties": {}})
    async def list_granted_applications(args: dict) -> dict:
        return _ok(access.info(session_id))
    tools.append(list_granted_applications)

    # ----- screenshot / inspect ---------------------------------------------
    @tool("screenshot",
          "Take a screenshot of the selected display. The returned image is what "
          "subsequent click coordinates are relative to. Call before acting and to "
          "check results.",
          {"type": "object", "properties": {}},
          annotations=big)
    async def screenshot(args: dict) -> dict:
        return await _run("screenshot", args)
    tools.append(screenshot)

    @tool("zoom",
          "Higher-resolution screenshot of a sub-region of the last screenshot, to read "
          "small text or fine detail. Coordinates in later clicks still refer to the full "
          "screenshot, not the zoom.",
          {"type": "object",
           "properties": {"region": {"type": "array", "items": {"type": "number"},
                                     "description": "[x0,y0,x1,y1] in the last screenshot's pixels."}},
           "required": ["region"]},
          annotations=big)
    async def zoom(args: dict) -> dict:
        return await _run("zoom", args)
    tools.append(zoom)

    @tool("cursor_position", "Get the current mouse cursor position.",
          {"type": "object", "properties": {}})
    async def cursor_position(args: dict) -> dict:
        return await _run("cursor_position", args)
    tools.append(cursor_position)

    @tool("list_displays", "List connected monitors (index, resolution, position, selected).",
          {"type": "object", "properties": {}})
    async def list_displays(args: dict) -> dict:
        try:
            ds = await asyncio.to_thread(mgr.list_displays)
        except Exception as e:
            return _err(f"could not list displays: {e}")
        return _ok({"displays": [{**d.to_dict(), "name": f"Display {d.index}"} for d in ds],
                    "selected": mgr.selected_index})
    tools.append(list_displays)

    @tool("switch_display",
          "Switch which monitor screenshots and actions target. Pass a display name from "
          "list_displays / the screenshot note (e.g. 'Display 2') or 'auto'. Take a fresh "
          "screenshot afterwards.",
          {"type": "object", "properties": {"display": {"type": "string"}}, "required": ["display"]})
    async def switch_display(args: dict) -> dict:
        err = _guard()
        if err:
            return err
        s = str(args.get("display") or "").strip()
        def switch():
            displays = mgr.list_displays()
            if s.lower() == "auto":
                idx = mgr._default_index(displays)
            else:
                import re
                mnum = re.search(r"\d+", s)
                if not mnum:
                    raise ValueError(f"unrecognized display {s!r}; use a name like 'Display 2' or 'auto'")
                idx = int(mnum.group())
            info = mgr.select_display(idx)
            _record("switch_display", geo=info)
            return _ok({"selected": mgr.selected_index, "display": {**info.to_dict(), "name": f"Display {info.index}"}})
        try:
            return await asyncio.to_thread(_serialized, switch, abort.generation(session_id))
        except _Aborted:
            return _err("Computer-use was stopped for this session.")
        except asyncio.CancelledError:
            abort.request_stop(session_id)
            raise
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"switch failed: {e}")
    tools.append(switch_display)

    # ----- mouse -------------------------------------------------------------
    def _mouse_tool(name: str, desc: str, with_mods: bool = True):
        props = dict(COORD)
        if with_mods:
            props.update(MODS)

        @tool(name, desc, {"type": "object", "properties": props, "required": ["coordinate"]})
        async def _t(args: dict) -> dict:
            return await _run(name, args)
        return _t

    tools.append(_mouse_tool("mouse_move", "Move the mouse to coordinate without clicking.", with_mods=False))
    tools.append(_mouse_tool("left_click", "Left-click at coordinate."))
    tools.append(_mouse_tool("right_click", "Right-click at coordinate (context menu)."))
    tools.append(_mouse_tool("middle_click", "Middle-click (scroll-wheel) at coordinate."))
    tools.append(_mouse_tool("double_click", "Double-click at coordinate."))
    tools.append(_mouse_tool("triple_click", "Triple-click at coordinate (selects a line)."))

    @tool("left_click_drag",
          "Press at start_coordinate (or current cursor), drag to coordinate, release.",
          {"type": "object",
           "properties": {"coordinate": {"type": "array", "items": {"type": "number"}, "description": "[x,y] end point."},
                          "start_coordinate": {"type": "array", "items": {"type": "number"}, "description": "[x,y] start (optional)."}},
           "required": ["coordinate"]})
    async def left_click_drag(args: dict) -> dict:
        return await _run("left_click_drag", args)
    tools.append(left_click_drag)

    @tool("left_mouse_down", "Press and hold the left button at the current cursor position.",
          {"type": "object", "properties": {}})
    async def left_mouse_down(args: dict) -> dict:
        return await _run("left_mouse_down", args)
    tools.append(left_mouse_down)

    @tool("left_mouse_up", "Release the left mouse button.", {"type": "object", "properties": {}})
    async def left_mouse_up(args: dict) -> dict:
        return await _run("left_mouse_up", args)
    tools.append(left_mouse_up)

    # ----- keyboard ----------------------------------------------------------
    @tool("key",
          "Press a key or chord, e.g. 'Return', 'Escape', 'ctrl+c', 'alt+Tab'. Optional repeat.",
          {"type": "object",
           "properties": {"text": {"type": "string"}, "repeat": {"type": "number"}},
           "required": ["text"]})
    async def key(args: dict) -> dict:
        return await _run("key", args)
    tools.append(key)

    @tool("hold_key", "Hold a key/chord for `duration` seconds, then release.",
          {"type": "object",
           "properties": {"text": {"type": "string"}, "duration": {"type": "number"}},
           "required": ["text", "duration"]})
    async def hold_key(args: dict) -> dict:
        return await _run("hold_key", args)
    tools.append(hold_key)

    @tool("type", "Type text into whatever has keyboard focus. Newlines supported. Use `key` for shortcuts.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
    async def type_text(args: dict) -> dict:
        return await _run("type", args)
    tools.append(type_text)

    @tool("scroll", "Scroll at coordinate in a direction by scroll_amount ticks.",
          {"type": "object",
           "properties": {**COORD,
                          "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                          "scroll_amount": {"type": "number"}},
           "required": ["coordinate", "scroll_direction", "scroll_amount"]})
    async def scroll(args: dict) -> dict:
        return await _run("scroll", args)
    tools.append(scroll)

    @tool("wait", "Wait for a number of seconds (0-60).",
          {"type": "object", "properties": {"duration": {"type": "number"}}, "required": ["duration"]})
    async def wait(args: dict) -> dict:
        return await _run("wait", args)
    tools.append(wait)

    # ----- apps / clipboard --------------------------------------------------
    @tool("open_application",
          "Launch or foreground an app by its Start-menu name (e.g. 'Notepad'). Take a "
          "screenshot afterwards to confirm.",
          {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]})
    async def open_application(args: dict) -> dict:
        return await _run("open_application", args)
    tools.append(open_application)

    @tool("read_clipboard", "Read the clipboard as text. Requires the clipboardRead grant.",
          {"type": "object", "properties": {}})
    async def read_clipboard(args: dict) -> dict:
        return await _run("read_clipboard", args)
    tools.append(read_clipboard)

    @tool("write_clipboard", "Write text to the clipboard. Requires the clipboardWrite grant.",
          {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
    async def write_clipboard(args: dict) -> dict:
        return await _run("write_clipboard", args)
    tools.append(write_clipboard)

    @tool("release_control", "Release the desktop and hide the agent cursor when done using the computer.",
          {"type": "object", "properties": {}})
    async def release_control(args: dict) -> dict:
        def release():
            if not safety.DESKTOP_LOCK.acquire(blocking=False):
                raise ValueError("Wait for the current action to finish before releasing control.")
            try:
                inputs.release_all()
                activity.finish(session_id)
                mgr._map.clear()
            finally:
                safety.DESKTOP_LOCK.release()
        # Release never needs access and remains legal after Stop.
        try:
            await asyncio.to_thread(release)
        except ValueError as e:
            return _err(str(e))
        return _ok({"action": "release_control"})
    tools.append(release_control)

    # ----- batch -------------------------------------------------------------
    @tool("computer_batch",
          "Execute a sequence of actions in ONE call (e.g. click, type, key, screenshot). "
          "Each item: {action, coordinate, text, duration, repeat, scroll_direction, "
          "scroll_amount, region, start_coordinate}. Stops on the first error. Coordinates "
          "refer to the screenshot taken before this call.",
          {"type": "object",
           "properties": {"actions": {"type": "array", "items": {"type": "object"}}},
           "required": ["actions"]},
          annotations=big)
    async def computer_batch(args: dict) -> dict:
        err = _guard()
        if err:
            return err
        actions = args.get("actions") or []
        if not isinstance(actions, list) or len(actions) > 100:
            return _err("actions must be a list of at most 100 actions")

        def _run_all():
            out: list = []
            for i, a in enumerate(actions):
                try:
                    out.extend(_execute(a.get("action"), a))
                except _Aborted:
                    inputs.release_all()
                    out.append(_text_item({"success": False, "error": "aborted — computer-use was stopped for this session", "action_index": i}))
                    return {"content": out, "isError": True}
                except Exception as e:
                    inputs.release_all()
                    out.append(_text_item({"success": False, "error": str(e), "action_index": i}))
                    return {"content": out, "isError": True}
            return {"content": out}

        try:
            return await asyncio.to_thread(_serialized, _run_all, abort.generation(session_id))
        except _Aborted:
            return _err("Computer-use was stopped for this session.")
        except asyncio.CancelledError:
            abort.request_stop(session_id)
            raise
        except Exception as e:
            return _err(str(e))
    tools.append(computer_batch)

    logger.info(
        "Registering computer-use MCP server: %d tools (session=%s, mode=%s, display=%s)",
        len(tools), session_id, permission_mode, mgr.selected_index,
    )
    return patch_sdk_server(
        create_tool_server(name="computer-use", version="2.0.0", tools=tools),
        tools,
    )


# ---------------------------------------------------------------------------
# Clipboard (Windows via PowerShell; best-effort elsewhere)
# ---------------------------------------------------------------------------

def _clipboard_read() -> str:
    if sys.platform == "win32":
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
             "[Console]::Write((Get-Clipboard -Raw))"],
            capture_output=True, text=True, encoding="utf-8", timeout=5, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return out.stdout
    raise RuntimeError("clipboard read not supported on this platform")


def _clipboard_write(text: str) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new(); "
             "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            input=text, text=True, encoding="utf-8", capture_output=True, timeout=5, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    raise RuntimeError("clipboard write not supported on this platform")
