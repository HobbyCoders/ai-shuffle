"""Exercise every registered desktop tool against fake displays/input devices.

Capture and PNG scaling are real; the screenshot source is synthetic. These
tests never move a physical cursor, type into apps, or access the clipboard.
"""

import asyncio
import base64
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.mcp import computer_use_sdk as sdk
from app.mcp.computer_use import abort, access, activity, scaling, safety


def payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    monkeypatch.setattr(activity, "_sessions", {})
    monkeypatch.setattr(activity, "_last_stop", None)
    access.revoke("test")
    mouse, keyboard = MagicMock(), MagicMock()
    mouse.position = (20, 20)
    keys = SimpleNamespace(**{name: name for name in ["ctrl", "shift", "alt", "cmd", "enter", "esc", "tab"]})
    monkeypatch.setitem(sys.modules, "pynput.mouse", SimpleNamespace(
        Controller=lambda: mouse, Button=SimpleNamespace(left="left", right="right", middle="middle")))
    monkeypatch.setitem(sys.modules, "pynput.keyboard", SimpleNamespace(Controller=lambda: keyboard, Key=keys))
    monkeypatch.setattr(sdk, "computer_use_available", lambda: (True, "ok"))
    monkeypatch.setattr(sdk, "foreground_display", lambda displays: None)
    monkeypatch.setattr("app.api.computer_use.get_computer_use_enabled", lambda: True)
    monkeypatch.setattr("app.mcp.computer_use.overlay.ensure_started", lambda: None)
    monkeypatch.setattr(safety, "ensure_started", lambda: True)
    monkeypatch.setattr(scaling, "_ensure_dpi_awareness", lambda: None)
    monitors = [{"left": -300, "top": -100, "width": 500, "height": 200},
                {"left": 0, "top": 0, "width": 200, "height": 100},
                {"left": -300, "top": -100, "width": 300, "height": 200}]
    captures = []
    class Capture:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        @property
        def monitors(self):
            return monitors
        def grab(self, region):
            captures.append(region)
            img = Image.new("RGB", (region["width"], region["height"]), (40, 90, 150))
            return SimpleNamespace(width=img.width, height=img.height, rgb=img.tobytes())
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Capture))
    monkeypatch.setattr("app.mcp.sdk_compat.patch_sdk_server", lambda cfg, tools: {t.name: t.handler for t in tools})
    read, write = MagicMock(return_value="clipboard text"), MagicMock()
    monkeypatch.setattr(sdk, "_clipboard_read", read)
    monkeypatch.setattr(sdk, "_clipboard_write", write)
    tools = sdk.create_computer_use_server(enabled=True, permission_mode="bypassPermissions",
                                           session_id="test", virtual_size=(150, 100))
    yield SimpleNamespace(tools=tools, mouse=mouse, keyboard=keyboard, captures=captures,
                          monitors=monitors, read=read, write=write)
    abort.request_stop("test")
    assert safety.DESKTOP_LOCK.acquire(timeout=1), "worker did not exit after Stop"
    safety.DESKTOP_LOCK.release()
    access.revoke("test")


async def test_all_advertised_tools_are_registered(desktop):
    assert set(desktop.tools) == set(sdk._TOOL_NAMES)


async def test_screenshot_png_dimensions_mapping_and_display_switch(desktop):
    tools = desktop.tools
    displays = payload(await tools["list_displays"]({}))
    assert [d["primary"] for d in displays["displays"]] == [True, False]
    assert payload(await tools["switch_display"]({"display": "Display 2"}))["selected"] == 2
    assert activity.snapshot()["geometry"]["left"] == -300
    assert (await tools["left_click"]({"coordinate": [75, 50]}))["isError"]
    result = await tools["screenshot"]({})
    info = payload(result)
    assert info["display"] == 2
    image = Image.open(io.BytesIO(base64.b64decode(result["content"][1]["data"])))
    assert image.size == (150, 100)
    await tools["left_click"]({"coordinate": [75, 50]})
    assert desktop.mouse.position == (-150, 0)
    assert activity.snapshot()["cursor"] == [-150, 0]
    await tools["switch_display"]({"display": "auto"})
    assert activity.snapshot()["display"] == 1
    assert activity.snapshot()["cursor"] is None


async def test_zoom_intersects_both_corners_and_preserves_full_image_coordinates(desktop):
    t = desktop.tools
    await t["switch_display"]({"display": "2"})
    await t["screenshot"]({})
    result = await t["zoom"]({"region": [-10, -10, 50, 30]})
    assert payload(result)["region"] == [-300, -100, 100, 60]
    assert payload(result)["coordinate_space"] == "full_screenshot"
    await t["mouse_move"]({"coordinate": [75, 50]})
    assert desktop.mouse.position == (-150, 0)
    assert (await t["zoom"]({"region": [200, 200, 250, 250]}))["isError"]


@pytest.mark.parametrize("name,button,count", [("left_click", "left", 1), ("right_click", "right", 1),
    ("middle_click", "middle", 1), ("double_click", "left", 2), ("triple_click", "left", 3)])
async def test_click_tools_release_buttons_and_modifiers(desktop, name, button, count):
    await desktop.tools["screenshot"]({})
    result = await desktop.tools[name]({"coordinate": [30, 20], "text": "ctrl+shift"})
    assert payload(result)["success"]
    assert desktop.mouse.press.call_count == count
    assert desktop.mouse.release.call_count == count
    desktop.mouse.release.assert_called_with(button)
    assert desktop.keyboard.release.call_count == 2


async def test_drag_and_manual_mouse_buttons(desktop):
    await desktop.tools["screenshot"]({})
    assert payload(await desktop.tools["left_click_drag"]({"start_coordinate": [0, 0], "coordinate": [60, 30]}))["success"]
    assert desktop.mouse.position == (80, 40)
    desktop.mouse.release.assert_called_with("left")
    await desktop.tools["left_mouse_down"]({})
    await desktop.tools["left_mouse_up"]({})
    assert desktop.mouse.press.call_count == desktop.mouse.release.call_count


async def test_keyboard_type_repeat_hold_and_wait(desktop):
    for name, args in [("key", {"text": "ctrl+c", "repeat": 3}),
                       ("hold_key", {"text": "shift", "duration": 0.01}),
                       ("type", {"text": "Hi\n\t!"}), ("wait", {"duration": 0.01})]:
        assert payload(await desktop.tools[name](args))["success"]
    assert desktop.keyboard.type.call_count == 3
    assert desktop.keyboard.press.call_count == desktop.keyboard.release.call_count


@pytest.mark.parametrize("direction,delta", [("up", (0, 1)), ("down", (0, -1)),
                                            ("left", (-1, 0)), ("right", (1, 0))])
async def test_scroll_directions(desktop, direction, delta):
    await desktop.tools["screenshot"]({})
    await desktop.tools["scroll"]({"coordinate": [20, 20], "scroll_direction": direction, "scroll_amount": 2})
    assert desktop.mouse.scroll.call_count == 2
    desktop.mouse.scroll.assert_called_with(*delta)


async def test_clipboard_grants_and_open_application(desktop, monkeypatch):
    await desktop.tools["request_access"]({"apps": ["Notepad"], "reason": "test", "clipboardWrite": True})
    assert payload(await desktop.tools["list_granted_applications"]({}))["apps"] == ["Notepad"]
    assert payload(await desktop.tools["read_clipboard"]({}))["text"] == "clipboard text"
    await desktop.tools["write_clipboard"]({"text": "text"})
    desktop.write.assert_called_with("text")
    assert payload(await desktop.tools["type"]({"text": "x" * 50}))["method"] == "clipboard"
    monkeypatch.setattr(safety.SafeInput, "sleep", lambda self, seconds: self.check())
    monkeypatch.setattr(sdk.sys, "platform", "win32")
    assert payload(await desktop.tools["open_application"]({"app": "Notepad"}))["success"]


async def test_cursor_reports_actual_display_without_fabricated_coordinates(desktop):
    await desktop.tools["screenshot"]({})
    desktop.mouse.position = (-250, -50)
    result = payload(await desktop.tools["cursor_position"]({}))
    assert result["display"] == 2
    assert not result["on_selected_display"]
    assert "x" not in result


async def test_stop_mid_typing_prevents_remaining_characters_and_batch_actions(desktop):
    def on_character(ch):
        if desktop.keyboard.type.call_count == 3:
            abort.stop_active("escape")
    desktop.keyboard.type.side_effect = on_character
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "type", "text": "abcdefgh"}, {"action": "key", "text": "Return"}]})
    assert result["isError"]
    assert desktop.keyboard.type.call_count == 3
    desktop.keyboard.press.assert_not_called()
    assert json.loads(result["content"][-1]["text"])["action_index"] == 0
    assert (await desktop.tools["write_clipboard"]({"text": "no"}))["isError"]
    desktop.write.assert_not_called()
    assert (await desktop.tools["switch_display"]({"display": "2"}))["isError"]
    assert (await desktop.tools["request_access"]({"apps": ["Notepad"]}))["isError"]


async def test_stop_during_key_hold_releases_key(desktop):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    desktop.keyboard.press.side_effect = lambda key: loop.call_soon_threadsafe(started.set)
    running = asyncio.create_task(desktop.tools["hold_key"]({"text": "ctrl", "duration": 60}))
    await asyncio.wait_for(started.wait(), timeout=1)
    abort.stop_active("escape")
    result = await asyncio.wait_for(running, timeout=0.5)
    assert result["isError"]
    desktop.keyboard.release.assert_called_once_with("ctrl")


async def test_cancelled_task_invalidates_the_worker(desktop):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    desktop.keyboard.press.side_effect = lambda key: loop.call_soon_threadsafe(started.set)
    running = asyncio.create_task(desktop.tools["hold_key"]({"text": "ctrl", "duration": 60}))
    await asyncio.wait_for(started.wait(), timeout=1)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert abort.is_stopped("test")
    desktop.keyboard.release.assert_called_once_with("ctrl")


async def test_batch_stops_at_invalid_action_and_releases_held_button(desktop):
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "left_mouse_down"}, {"action": "invalid"}, {"action": "type", "text": "no"}]})
    assert result["isError"]
    desktop.mouse.release.assert_called_once_with("left")
    desktop.keyboard.type.assert_not_called()


async def test_release_control_hides_activity_and_releases_held_input(desktop):
    await desktop.tools["left_mouse_down"]({})
    assert activity.snapshot()["active"]
    assert payload(await desktop.tools["release_control"]({}))["success"]
    assert not activity.snapshot()["active"]
    desktop.mouse.release.assert_called_once_with("left")


async def test_second_chat_cannot_interleave_with_desktop_owner(desktop):
    activity.record(2, session_id="other")
    result = await desktop.tools["type"]({"text": "no"})
    assert result["isError"]
    assert "Another chat" in payload(result)["error"]
    desktop.keyboard.type.assert_not_called()


async def test_concurrent_action_is_rejected_instead_of_queued_behind_stop(desktop):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    desktop.keyboard.press.side_effect = lambda key: loop.call_soon_threadsafe(started.set)
    running = asyncio.create_task(desktop.tools["hold_key"]({"text": "ctrl", "duration": 60}))
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        result = await asyncio.wait_for(desktop.tools["type"]({"text": "no"}), timeout=0.5)
        assert result["isError"]
        assert "busy" in payload(result)["error"]
    finally:
        abort.stop_active("escape")
        await asyncio.wait_for(running, timeout=0.5)
    desktop.keyboard.type.assert_not_called()


async def test_keyboard_overlay_tracks_focused_window_display(desktop, monkeypatch):
    monkeypatch.setattr(sdk, "foreground_display", lambda ds: ds[1])
    await desktop.tools["type"]({"text": "abc"})
    assert activity.snapshot()["display"] == 2
    assert activity.snapshot()["geometry"]["left"] == -300


async def test_manual_mouse_movement_cancels_click(desktop, monkeypatch):
    await desktop.tools["screenshot"]({})
    def move_during_settle(self, seconds):
        desktop.mouse.position = (1, 1)
    monkeypatch.setattr(safety.SafeInput, "sleep", move_during_settle)
    result = await desktop.tools["left_click"]({"coordinate": [40, 30]})
    assert result["isError"]
    desktop.mouse.press.assert_not_called()
    assert activity.snapshot()["stop_reason"] == "pointer_moved"
