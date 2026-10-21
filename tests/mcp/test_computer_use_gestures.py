"""Regression timelines for desktop gestures and guardrails; no real desktop I/O."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.mcp import computer_use_sdk as sdk
from app.mcp.computer_use import abort, safety, scaling, uia, windows
from tests.mcp import test_computer_use_tools as _base

desktop = _base.desktop
payload = _base.payload


@pytest.fixture
def gesture(desktop, monkeypatch):
    events = []
    original_move = safety.SafeInput.move

    def move(self, point):
        events.append(("move", tuple(point)))
        original_move(self, point)

    def sleep(self, seconds):
        self.check()
        events.append(("sleep", seconds))

    monkeypatch.setattr(safety.SafeInput, "move", move)
    monkeypatch.setattr(safety.SafeInput, "sleep", sleep)
    for name, device in (("mouse", desktop.mouse), ("key", desktop.keyboard)):
        device.press.side_effect = lambda value, name=name: events.append((name + "_down", value))
        device.release.side_effect = lambda value, name=name: events.append((name + "_up", value))
    desktop.keyboard.type.side_effect = lambda text: events.append(("type", text))
    desktop.mouse.scroll.side_effect = lambda x, y: events.append(("scroll", x, y))
    desktop.events = events
    return desktop


@pytest.mark.parametrize("action,args", [
    ("focus_window", {"window_id": 101}),
    ("window_control", {"window_id": 101, "operation": "maximize"}),
    ("open_application", {"app": "Notepad"}),
])
async def test_window_changes_invalidate_coordinates(gesture, monkeypatch, action, args):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    shot = payload(await t["screenshot"]({"window_id": 101}))["screenshot_id"]
    monkeypatch.setattr(sdk.sys, "platform", "win32")
    assert payload(await t[action](args))["success"]
    result = payload(await t["left_click"]({"coordinate": [20, 20], "screenshot_id": shot}))
    assert result["code"] == "STALE_OBSERVATION"
    gesture.mouse.press.assert_not_called()


@pytest.mark.parametrize("change", ["move", "resize", "gone"])
async def test_external_window_change_invalidates_mapping(gesture, change):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    shot = payload(await t["screenshot"]({"window_id": 101}))["screenshot_id"]
    if change == "gone":
        gesture.windows.pop(0)
    else:
        bounds = (20, 10, 100, 50) if change == "move" else (10, 10, 110, 50)
        gesture.windows[0] = replace(gesture.windows[0], bounds=bounds)
    result = payload(await t["left_click"]({"coordinate": [20, 20], "screenshot_id": shot}))
    assert result["code"] == "STALE_OBSERVATION"
    gesture.mouse.press.assert_not_called()


@pytest.mark.parametrize("action,args", [("type", {"text": "no"}), ("key", {"text": "a"}),
                                        ("key_down", {"text": "shift"})])
async def test_keyboard_refuses_unrelated_focus(gesture, action, args):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await t["screenshot"]({"window_id": 101})  # Edge still has focus.
    assert payload(await t[action](args))["code"] == "TARGET_CHANGED"
    assert not any(e[0] in ("type", "key_down") for e in gesture.events)


async def test_keyboard_and_pointer_allow_same_process(gesture):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    gesture.windows[1] = replace(gesture.windows[1], pid=1)
    await t["screenshot"]({"window_id": 101})
    gesture.uia.window_hit = 202
    assert payload(await t["type"]({"text": "ok"}))["success"]
    assert payload(await t["left_click"]({"coordinate": [20, 20]}))["success"]
    assert ("mouse_down", "left") in gesture.events


async def test_click_refuses_covering_unrelated_window(gesture):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await t["screenshot"]({"window_id": 101})
    gesture.uia.window_hit = 202
    assert payload(await t["left_click"]({"coordinate": [20, 20]}))["code"] == "TARGET_CHANGED"
    gesture.mouse.press.assert_not_called()


async def test_start_failure_never_types_name_or_enter(gesture, monkeypatch):
    t = gesture.tools
    await t["request_access"]({"apps": ["Paint"], "reason": "test"})
    monkeypatch.setattr(sdk.sys, "platform", "win32")
    monkeypatch.setattr(windows, "start_search_focused", lambda: False)
    assert payload(await t["open_application"]({"app": "Paint"}))["code"] == "FOCUS_BLOCKED"
    assert [e for e in gesture.events if e[0].startswith("key_")] == [
        ("key_down", "cmd"), ("key_up", "cmd")]
    gesture.keyboard.type.assert_not_called()


async def test_long_multiline_uses_enter_not_clipboard(gesture):
    t = gesture.tools
    await t["request_access"]({"apps": [], "reason": "test", "clipboardWrite": True})
    text = "a" * 45 + "\nb"
    assert payload(await t["type"]({"text": text}))["method"] == "paced"
    gesture.write.assert_not_called()
    events = [e for e in gesture.events if e[0] != "sleep"]
    assert events == [("type", "a")] * 45 + [("key_down", "enter"), ("key_up", "enter"), ("type", "b")]


@pytest.mark.parametrize("landed", [True, False])
async def test_paste_checks_readable_field_without_duplicate_fallback(gesture, landed):
    t = gesture.tools
    await t["request_access"]({"apps": [], "reason": "test", "clipboardWrite": True})
    text = "x" * 45
    reads = []
    def focused_value():
        reads.append(1)
        return ((1,), text if landed and len(reads) > 1 else "old")
    gesture.uia.focused_value = focused_value
    result = payload(await t["type"]({"text": text}))
    if landed:
        assert result["method"] == "clipboard"
    else:
        assert result["code"] == "EXECUTION_OUTCOME_UNKNOWN" and result["outcome"] == "unknown"
    assert [e for e in gesture.events if e[0].startswith("key_")] == [
        ("key_down", "ctrl"), ("key_down", "v"), ("key_up", "v"), ("key_up", "ctrl")]
    gesture.keyboard.type.assert_not_called()


async def test_window_screenshot_reports_z_order_occlusion(gesture):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    target, cover, behind = gesture.windows
    gesture.windows[:] = [replace(cover, bounds=(20, 20, 50, 50)), target,
                          replace(behind, bounds=(20, 20, 20, 20))]
    shot = payload(await t["screenshot"]({"window_id": 101}))
    assert shot["occluded_by"] == [202]
    assert "covering windows are included" in shot["note"]
    assert "Only this window's visible part" not in shot["note"]


@pytest.mark.parametrize("action,args", [
    ("left_click", {"coordinate": [20, 20]}),
    ("scroll", {"coordinate": [20, 20], "scroll_amount": 2, "scroll_direction": "down"}),
    ("left_click_drag", {"start_coordinate": [20, 20], "coordinate": [30, 30]}),
    ("mouse_down", {"coordinate": [20, 20]}),
])
async def test_elevated_pointer_target_refused_before_press(gesture, monkeypatch, action, args):
    await gesture.tools["screenshot"]({})
    monkeypatch.setattr(windows, "window_blocks_input", lambda hwnd: True)
    assert payload(await gesture.tools[action](args))["code"] == "PERMISSION_REQUIRED"
    gesture.mouse.press.assert_not_called()
    gesture.mouse.scroll.assert_not_called()


@pytest.mark.parametrize("action,count", [("double_click", 2), ("triple_click", 3)])
async def test_macos_native_click_count(gesture, monkeypatch, action, count):
    await gesture.tools["screenshot"]({})
    monkeypatch.setattr(sdk.sys, "platform", "darwin")
    assert payload(await gesture.tools[action]({"coordinate": [20, 20]}))["success"]
    gesture.mouse.click.assert_called_once_with("left", count)
    gesture.mouse.press.assert_not_called()


@pytest.mark.parametrize("text,keys", [
    ("ctrl++", ["ctrl", "+"]), ("plus", ["+"]), ("minus", ["-"]),
    ("Super_L+a", ["cmd", "a"]),
])
async def test_key_spellings_send_expected_chord(gesture, text, keys):
    await gesture.tools["request_access"]({"apps": [], "reason": "test", "systemKeyCombos": True})
    assert payload(await gesture.tools["key"]({"text": text}))["success"]
    assert [e for e in gesture.events if e[0].startswith("key_")] == [
        *[("key_down", k) for k in keys], *[("key_up", k) for k in reversed(keys)]]


async def test_drag_path_timing_button_modifiers_and_enumeration(gesture, monkeypatch):
    t = gesture.tools
    await t["screenshot"]({})
    enumerations = []
    original = scaling.DisplayManager.list_displays
    monkeypatch.setattr(scaling.DisplayManager, "list_displays",
                        lambda self: enumerations.append(len(gesture.events)) or original(self))
    result = await t["left_click_drag"]({"start_coordinate": [0, 0], "path": [[30, 10]],
        "coordinate": [60, 30], "duration": 0.8, "hold": 0.2, "drop_delay": 0.4,
        "button": "right", "text": "shift"})
    assert payload(result)["success"]
    e = gesture.events
    press, release = e.index(("mouse_down", "right")), e.index(("mouse_up", "right"))
    assert e[press - 1] == ("key_down", "shift")
    assert e[press + 1] == ("sleep", 0.2)
    assert e[release - 1] == ("sleep", 0.4)
    assert e[release + 1] == ("key_up", "shift")
    moves = [event[1] for event in e[press:release] if event[0] == "move"]
    assert (40, 13) in moves and moves[-1] == (80, 40)
    assert sum(event[1] for event in e[press + 2:release - 1] if event[0] == "sleep") == pytest.approx(0.8)
    # Enumeration is allowed for validation and final freshness, never each move.
    assert not any(press + 2 < index < release - 1 for index in enumerations)


@pytest.mark.parametrize("args", [{"path": [[999, 20]]}, {"duration": -1}, {"hold": float("nan")},
                                   {"button": "bad"}, {"path": "bad"}])
async def test_drag_validates_all_arguments_before_press(gesture, args):
    await gesture.tools["screenshot"]({})
    result = await gesture.tools["left_click_drag"]({"coordinate": [30, 30], **args})
    assert result["isError"]
    gesture.mouse.press.assert_not_called()


async def test_drag_checks_endpoint_before_releasing(gesture):
    await gesture.tools["screenshot"]({})
    # An external move after the last model move must not be silently accepted.
    original_release = gesture.mouse.release.side_effect
    def release(button):
        assert gesture.mouse.position == (150, 80)
        original_release(button)
    gesture.mouse.release.side_effect = release
    original_move = safety.SafeInput.move
    def move(self, point):
        original_move(self, point)
        if point == (80, 40):
            gesture.mouse.position = (150, 80)
    # monkeypatch fixture is not needed outside this test's bounded context.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safety.SafeInput, "move", move)
        result = payload(await gesture.tools["left_click_drag"]({"coordinate": [60, 30]}))
    assert result["code"] == "POINTER_MISMATCH" and result["outcome"] == "partial"
    gesture.mouse.release.assert_called_once_with("left")


async def test_mouse_down_up_coordinates_button_and_modifiers(gesture):
    await gesture.tools["screenshot"]({})
    t = gesture.tools
    assert payload(await t["mouse_down"]({"button": "middle", "coordinate": [30, 20], "text": "ctrl"}))["success"]
    assert gesture.mouse.position == (40, 27)
    assert not gesture.mouse.release.called and not gesture.keyboard.release.called
    assert payload(await t["mouse_up"]({"button": "middle"}))["success"]
    assert [e for e in gesture.events if e[0] in ("key_down", "key_up", "mouse_down", "mouse_up")] == [
        ("key_down", "ctrl"), ("mouse_down", "middle"), ("mouse_up", "middle"), ("key_up", "ctrl")]


async def test_mouse_up_accepts_coordinate_and_drop_modifiers(gesture):
    t = gesture.tools
    await t["screenshot"]({})
    await t["mouse_down"]({"button": "right"})
    gesture.events.clear()
    assert payload(await t["mouse_up"]({"button": "right", "coordinate": [30, 20], "text": "shift"}))["success"]
    assert [e for e in gesture.events if e[0] != "sleep"] == [
        ("key_down", "shift"), ("move", (40, 27)), ("mouse_up", "right"), ("key_up", "shift")]


async def test_held_key_survives_other_actions_and_is_released(gesture):
    t = gesture.tools
    await t["screenshot"]({})
    assert payload(await t["key_down"]({"text": "shift"}))["success"]
    assert payload(await t["left_click"]({"text": "shift"}))["success"]
    gesture.keyboard.release.assert_not_called()
    assert payload(await t["key_up"]({"text": "shift"}))["success"]
    gesture.keyboard.release.assert_called_once_with("shift")


@pytest.mark.parametrize("end", ["stop", "cancel", "complete"])
@pytest.mark.parametrize("cross_call", [False, True])
async def test_drag_interrupt_event_order(gesture, monkeypatch, end, cross_call):
    t = gesture.tools
    await t["screenshot"]({})

    def finish():
        if end == "stop":
            abort.request_stop("test", reason="user")
        elif end == "cancel":
            abort.cancel_inflight("test")

    if cross_call:
        await t["mouse_down"]({"button": "right"})
        await t["mouse_move"]({"coordinate": [30, 30]})
        finish()
        if end == "complete":
            assert payload(await t["mouse_up"]({"button": "right"}))["success"]
    else:
        original_sleep = safety.SafeInput.sleep

        def sleep(self, seconds):
            if (self.mouse, "right") in self.held:
                finish()
            original_sleep(self, seconds)

        monkeypatch.setattr(safety.SafeInput, "sleep", sleep)
        result = await t["left_click_drag"]({"coordinate": [30, 30], "button": "right"})
        assert bool(result.get("isError")) == (end != "complete")

    expected = [("mouse_down", "right")]
    if end != "complete":
        expected += [("key_down", "esc"), ("key_up", "esc")]
    expected += [("mouse_up", "right")]
    assert [e for e in gesture.events if e[0] in
            ("mouse_down", "mouse_up", "key_down", "key_up")] == expected


@pytest.mark.parametrize("failure", ["press", "release"])
async def test_stop_drag_escape_failure_still_releases_button(gesture, caplog, failure):
    await gesture.tools["mouse_down"]({})
    original = getattr(gesture.keyboard, failure).side_effect

    def fail(key):
        original(key)
        getattr(gesture.keyboard, failure).side_effect = original
        raise RuntimeError("Escape injection failed")

    getattr(gesture.keyboard, failure).side_effect = fail
    abort.request_stop("test", reason="user")
    expected = [("mouse_down", "left"), ("key_down", "esc"), ("key_up", "esc")]
    if failure == "release":
        expected += [("key_up", "esc")]  # emergency cleanup retries the failed release
    expected += [("mouse_up", "left")]
    assert [e for e in gesture.events if e[0] in
            ("mouse_down", "mouse_up", "key_down", "key_up")] == expected
    assert "Escape" in caplog.text
    gesture.mouse.release.assert_called_once_with("left")


async def test_cancelled_drag_task_sends_escape_before_mouse_up(desktop):
    # Exercise the real async cancellation -> cancel_inflight producer, not just
    # the cleanup consumer. All injected input still goes to fake controllers.
    events = []
    started = asyncio.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()
    original_sleep = safety.SafeInput.sleep

    def sleep(self, seconds):
        try:
            original_sleep(self, seconds)
        finally:
            if seconds == 60:
                exited.set()

    def down(button):
        events.append(("mouse_down", button))
        loop.call_soon_threadsafe(started.set)

    desktop.mouse.press.side_effect = down
    desktop.mouse.release.side_effect = lambda button: events.append(("mouse_up", button))
    desktop.keyboard.press.side_effect = lambda key: events.append(("key_down", key))
    desktop.keyboard.release.side_effect = lambda key: events.append(("key_up", key))
    await desktop.tools["screenshot"]({})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(safety.SafeInput, "sleep", sleep)
        running = asyncio.create_task(desktop.tools["left_click_drag"]({
            "coordinate": [30, 30], "hold": 60}))
        await asyncio.wait_for(started.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert await asyncio.to_thread(exited.wait, 1), "cancelled drag worker did not exit"
    assert not abort.is_stopped("test")
    assert events == [("mouse_down", "left"), ("key_down", "esc"),
                      ("key_up", "esc"), ("mouse_up", "left")]


@pytest.mark.parametrize("held_key", [False, True])
async def test_stop_without_drag_sends_no_escape(gesture, held_key):
    if held_key:
        await gesture.tools["key_down"]({"text": "shift"})
    abort.request_stop("test", reason="user")
    assert gesture.events == ([("key_down", "shift"), ("key_up", "shift")] if held_key else [])


@pytest.mark.parametrize("end", ["error", "stop", "completed", "release_control"])
async def test_cross_call_keys_and_buttons_cleanup(gesture, end):
    t = gesture.tools
    await t["key_down"]({"text": "ctrl"})
    await t["mouse_down"]({"button": "right"})
    if end == "error":
        assert (await t["key"]({"text": "not_a_key"}))["isError"]
    elif end == "release_control":
        await t[end]({})
    else:
        abort.request_stop("test", reason=end)
    assert [e for e in gesture.events if e[0] in
            ("mouse_down", "mouse_up", "key_down", "key_up")] == [
        ("key_down", "ctrl"), ("mouse_down", "right"),
        *([("key_down", "esc"), ("key_up", "esc")] if end in ("stop", "completed") else []),
        ("mouse_up", "right"), ("key_up", "ctrl")]


async def test_cross_call_system_key_grant_and_escape_refusals(gesture, monkeypatch):
    t = gesture.tools
    assert payload(await t["key_down"]({"text": "Super_L"}))["code"] == "PERMISSION_REQUIRED"
    await t["key_down"]({"text": "alt"})
    assert payload(await t["key_down"]({"text": "Tab"}))["code"] == "PERMISSION_REQUIRED"
    assert ("key_down", "tab") not in gesture.events
    await t["key_down"]({"text": "ctrl"})
    assert payload(await t["key_down"]({"text": "alt"}))["code"] == "PERMISSION_REQUIRED"
    monkeypatch.setattr(safety, "_x11", lambda: True)
    assert payload(await t["key_down"]({"text": "Escape"}))["code"] == "UNSUPPORTED_CAPABILITY"
    assert ("key_down", "esc") not in gesture.events


async def test_modifier_scroll_timeline(gesture):
    t = gesture.tools
    await t["screenshot"]({})
    assert payload(await t["scroll"]({"coordinate": [20, 20], "text": "ctrl+shift",
                                    "scroll_direction": "down", "scroll_amount": 2}))["success"]
    assert [e for e in gesture.events if e[0] != "move" and e[0] != "sleep"] == [
        ("key_down", "ctrl"), ("key_down", "shift"), ("scroll", 0, -1), ("scroll", 0, -1),
        ("key_up", "shift"), ("key_up", "ctrl")]


async def test_click_current_pointer(gesture):
    await gesture.tools["screenshot"]({})
    gesture.mouse.position = (45, 35)
    assert payload(await gesture.tools["left_click"]({}))["success"]
    assert gesture.mouse.position == (45, 35)
    gesture.mouse.press.assert_called_once_with("left")


def test_hold_timeout_releases_without_escape_and_old_timer_cannot_release_new_hold(monkeypatch):
    timers = []
    def timer(seconds, callback):
        instance = SimpleNamespace(seconds=seconds, callback=callback, daemon=False,
                                   start=MagicMock(), cancel=MagicMock())
        timers.append(instance)
        return instance
    monkeypatch.setattr(safety.threading, "Timer", timer)
    mouse, keyboard = MagicMock(), MagicMock()
    inputs = safety.SafeInput("timer", mouse, keyboard)
    inputs.press(keyboard, "shift")
    inputs.press(mouse, "left")
    inputs.expire_held_after()
    assert timers[0].seconds == 15 and timers[0].daemon
    timers[0].callback()
    assert inputs.held == []
    mouse.release.assert_called_once_with("left")
    keyboard.release.assert_called_once_with("shift")
    inputs.press(keyboard, "ctrl")
    inputs.expire_held_after()
    timers[0].callback()  # a cancelled callback waiting for the lock
    assert inputs.held == [(keyboard, "ctrl")]
    inputs.release_all()


def test_related_windows_allow_owned_popup_not_unrelated_process(monkeypatch):
    # Native relationship helper, with all Win32 calls replaced by plain fakes.
    api = SimpleNamespace(GetWindow=lambda hwnd, flag: {303: 101}.get(hwnd, 0))
    monkeypatch.setattr(windows.sys, "platform", "win32")
    monkeypatch.setattr(windows, "_api", lambda: (None, None, api))
    monkeypatch.setattr(windows, "_pid", lambda hwnd: {101: 1, 202: 2, 303: 3}[hwnd])
    monkeypatch.setattr(windows, "find_window", lambda hwnd, displays: SimpleNamespace(pid={101: 1, 202: 2, 303: 3}[hwnd]))
    assert windows.related_window(101, 303, [])
    assert not windows.related_window(101, 202, [])
    assert not windows.related_window(303, 101, [])


@pytest.mark.parametrize("process,expected", [("StartMenuExperienceHost.exe", True),
    ("SearchHost.exe", True), ("notepad.exe", False), ("explorer.exe", False)])
def test_start_search_detection_uses_foreground_process_not_title(monkeypatch, process, expected):
    monkeypatch.setattr(windows, "foreground_window", lambda: 99)
    monkeypatch.setattr(windows, "_pid", lambda hwnd: 1)
    monkeypatch.setattr(windows, "_exe_path", lambda pid: process)
    monkeypatch.setattr(windows, "_api", lambda: (None, None, SimpleNamespace(GetClassNameW=None)))
    monkeypatch.setattr(windows, "_text", lambda fn, hwnd, size: "CabinetWClass")
    assert windows.start_search_focused() is expected


@pytest.mark.parametrize("password,readable", [(False, True), (True, True), (False, False)])
def test_focused_value_producer_uses_unclipped_value_and_never_reads_password(password, readable):
    reads = []
    text = "x" * 5000
    def get(prop):
        reads.append(prop)
        return {uia._P_PASSWORD: password, uia._P_HAS_VALUE: readable,
                uia._P_RUNTIME_ID: [1, 2], uia._P_VALUE: text}[prop]
    provider = uia.ComProvider.__new__(uia.ComProvider)
    provider.auto = SimpleNamespace(GetFocusedElement=lambda: SimpleNamespace(GetCurrentPropertyValue=get))
    if not password and readable:
        assert provider.focused_value() == ((1, 2), text)
    else:
        assert provider.focused_value() is None
        assert uia._P_VALUE not in reads


async def test_native_click_failure_releases_button_and_modifiers(gesture, monkeypatch):
    await gesture.tools["screenshot"]({})
    monkeypatch.setattr(sdk.sys, "platform", "darwin")
    gesture.mouse.click.side_effect = RuntimeError("native click failed after press")
    assert (await gesture.tools["double_click"]({"coordinate": [20, 20], "text": "shift"}))["isError"]
    gesture.mouse.release.assert_called_once_with("left")
    gesture.keyboard.release.assert_called_once_with("shift")


@pytest.mark.parametrize("action,args", [("left_click", {}),
    ("left_click_drag", {"coordinate": [20, 20]})])
async def test_current_pointer_outside_screenshot_is_refused(gesture, action, args):
    await gesture.tools["screenshot"]({})
    gesture.mouse.position = (-200, 0)
    assert payload(await gesture.tools[action](args))["code"] == "INVALID_ARGUMENT"
    gesture.mouse.press.assert_not_called()


async def test_new_held_actions_work_in_batch(gesture):
    result = await gesture.tools["computer_batch"]({"actions": [
        {"action": "screenshot"}, {"action": "key_down", "text": "shift"},
        {"action": "mouse_down", "button": "right", "coordinate": [20, 20]},
        {"action": "mouse_move", "coordinate": [30, 30]},
        {"action": "mouse_up", "button": "right"}, {"action": "key_up", "text": "shift"}]})
    assert not result.get("isError")
    assert [e for e in gesture.events if e[0] not in ("move", "sleep")] == [
        ("key_down", "shift"), ("mouse_down", "right"),
        ("mouse_up", "right"), ("key_up", "shift")]


async def test_timeout_releases_cross_call_tools_without_next_call(gesture, monkeypatch):
    callbacks = []
    def timer(seconds, callback):
        callbacks.append(callback)
        return SimpleNamespace(daemon=False, start=lambda: None, cancel=lambda: None)
    monkeypatch.setattr(safety.threading, "Timer", timer)
    await gesture.tools["mouse_down"]({"button": "right", "text": "ctrl"})
    await gesture.tools["key_down"]({"text": "shift"})
    assert len(callbacks) == 1  # a later key-down cannot extend the first hold
    callbacks[0]()
    assert [e for e in gesture.events if e[0] not in ("move", "sleep")] == [
        ("key_down", "ctrl"), ("mouse_down", "right"), ("key_down", "shift"),
        ("key_up", "shift"), ("mouse_up", "right"), ("key_up", "ctrl")]
    # A mouse-up after expiry must not release modifiers owned by a fresh key-down.
    await gesture.tools["key_down"]({"text": "ctrl"})
    gesture.keyboard.release.reset_mock()
    await gesture.tools["mouse_up"]({"button": "right"})
    gesture.keyboard.release.assert_not_called()
    await gesture.tools["key_up"]({"text": "ctrl"})

# Review repros: each formerly unsafe timeline must now refuse or report honestly.
async def test_mouse_modifier_cannot_bypass_system_grant_into_win_r(gesture):
    t = gesture.tools
    await t["screenshot"]({})
    assert payload(await t["mouse_down"]({"text": "cmd"}))["code"] == "PERMISSION_REQUIRED"
    assert payload(await t["type"]({"text": "r"}))["success"]
    assert ("key_down", "cmd") not in gesture.events
    assert ("type", "r") in gesture.events
    gesture.mouse.press.assert_not_called()


@pytest.mark.parametrize("modifier,text", [("alt", "\t"), ("ctrl", "a"), ("cmd", "r")])
async def test_type_with_held_system_modifier_requires_grant(gesture, modifier, text):
    t = gesture.tools
    if modifier == "cmd":
        await t["request_access"]({"apps": [], "reason": "test", "systemKeyCombos": True})
    await t["key_down"]({"text": modifier})
    if modifier == "cmd":
        # Simulate an earlier grant no longer being available to this held input.
        from app.mcp.computer_use import access
        access.revoke("test")
        access.grant("test")
    result = payload(await t["type"]({"text": text}))
    assert result["code"] == "PERMISSION_REQUIRED" and result["outcome"] == "partial"
    assert not any(e[0] == "type" or e == ("key_down", "tab") for e in gesture.events)
    gesture.keyboard.release.assert_called_with(modifier)


@pytest.mark.parametrize("action,args", [
    ("left_mouse_down", {}), ("left_click", {"coordinate": [20, 20]}),
    ("right_click", {"coordinate": [20, 20]}), ("double_click", {"coordinate": [20, 20]}),
    ("left_click_drag", {"coordinate": [20, 20]}),
    ("scroll", {"coordinate": [20, 20], "scroll_direction": "down", "scroll_amount": 1})])
async def test_pointer_modifiers_enforce_system_grant(gesture, action, args):
    await gesture.tools["screenshot"]({})
    result = payload(await gesture.tools[action]({**args, "text": "Super_L"}))
    assert result["code"] == "PERMISSION_REQUIRED"
    gesture.mouse.press.assert_not_called()
    gesture.keyboard.press.assert_not_called()
    gesture.mouse.scroll.assert_not_called()


async def test_pointer_modifiers_combine_with_held_modifiers(gesture):
    await gesture.tools["screenshot"]({})
    await gesture.tools["key_down"]({"text": "ctrl"})
    result = payload(await gesture.tools["scroll"]({"coordinate": [20, 20], "text": "alt"}))
    assert result["code"] == "PERMISSION_REQUIRED"
    assert ("key_down", "alt") not in gesture.events
    gesture.mouse.scroll.assert_not_called()


async def test_window_moving_drag_succeeds_then_next_action_is_stale(gesture, monkeypatch):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    shot = payload(await t["screenshot"]({"window_id": 101}))["screenshot_id"]
    original = safety.SafeInput.press
    def press(self, device, key):
        original(self, device, key)
        if device is self.mouse:
            gesture.windows[0] = replace(gesture.windows[0], bounds=(30, 10, 100, 50))
    monkeypatch.setattr(safety.SafeInput, "press", press)
    result = payload(await t["left_click_drag"]({"start_coordinate": [5, 2],
                                               "coordinate": [20, 2], "screenshot_id": shot}))
    assert result["success"]
    next_action = payload(await t["left_click"]({"coordinate": [20, 2], "screenshot_id": shot}))
    assert next_action["code"] == "STALE_OBSERVATION" and next_action["outcome"] == "rejected"
    gesture.mouse.press.assert_called_once_with("left")


@pytest.mark.parametrize("action,args", [
    ("left_click_drag", {"coordinate": [30, 20]}),
    ("scroll", {"coordinate": [20, 20], "scroll_amount": 2, "scroll_direction": "down"})])
async def test_mid_gesture_target_change_reports_partial(gesture, monkeypatch, action, args):
    t = gesture.tools
    await t["screenshot"]({})
    if action == "left_click_drag":
        original = gesture.mouse.press.side_effect
        def press(button):
            original(button)
            monkeypatch.setattr(windows, "window_blocks_input", lambda hwnd: True)
        gesture.mouse.press.side_effect = press
    else:
        original = gesture.mouse.scroll.side_effect
        def scroll(x, y):
            original(x, y)
            monkeypatch.setattr(windows, "window_blocks_input", lambda hwnd: True)
        gesture.mouse.scroll.side_effect = scroll
    result = payload(await t[action](args))
    assert result["code"] == "PERMISSION_REQUIRED" and result["outcome"] == "partial"


async def test_mouse_up_validates_mapping_before_pressing_modifiers(gesture):
    t = gesture.tools
    await t["screenshot"]({})
    await t["mouse_down"]({})
    gesture.events.clear()
    result = payload(await t["mouse_up"]({"coordinate": [20, 20], "screenshot_id": "old", "text": "shift"}))
    assert result["code"] == "STALE_OBSERVATION" and result["outcome"] == "partial"
    assert ("key_down", "shift") not in gesture.events
    assert gesture.events == [("mouse_up", "left")]


@pytest.mark.parametrize("pin", ["ui_tree", "zoom", "click_element"])
async def test_full_display_mapping_allows_shell_click_after_pin(gesture, pin):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await t["screenshot"]({})
    if pin == "click_element":
        tree = payload(await t["ui_tree"]({"window_id": 101}))
        assert payload(await t[pin]({"element_id": tree["elements"][0]["element_id"]}))["success"]
    else:
        assert payload(await t[pin]({"window_id": 101}))["success"]
    gesture.uia.window_hit = 999
    assert payload(await t["left_click"]({"coordinate": [140, 70]}))["success"]
    # A full-display observation is also the documented way to clear keyboard pinning.
    assert payload(await t["type"]({"text": "no"}))["code"] == "TARGET_CHANGED"
    await t["screenshot"]({})
    assert payload(await t["type"]({"text": "ok"}))["success"]


@pytest.mark.parametrize("action,args", [("mouse_up", {"button": "left"}), ("key_up", {"text": "shift"})])
async def test_up_after_timeout_reports_no_release(gesture, monkeypatch, action, args):
    callbacks = []
    monkeypatch.setattr(safety.threading, "Timer", lambda seconds, callback:
                        callbacks.append(callback) or SimpleNamespace(daemon=False, start=lambda: None,
                                                                     cancel=lambda: None))
    await gesture.tools["mouse_down"]({})
    await gesture.tools["key_down"]({"text": "shift"})
    callbacks[0]()
    if action == "mouse_up":
        args = {**args, "coordinate": [20, 20], "screenshot_id": "expired", "text": "shift"}
    gesture.events.clear()
    result = payload(await gesture.tools[action](args))
    assert result["released"] is False and "screenshot" in result["note"]
    assert gesture.events == []


async def test_paste_waits_for_delayed_value_delivery(gesture):
    t = gesture.tools
    await t["request_access"]({"apps": [], "reason": "test", "clipboardWrite": True})
    reads = []
    text = "x" * 45
    def focused_value():
        reads.append(1)
        return ((1,), text if len(reads) >= 8 else "old")
    gesture.uia.focused_value = focused_value
    assert payload(await t["type"]({"text": text}))["method"] == "clipboard"
    assert len(reads) == 8
    gesture.keyboard.type.assert_not_called()


async def test_paste_polling_respects_elapsed_deadline(gesture, monkeypatch):
    await gesture.tools["request_access"]({"apps": [], "reason": "test", "clipboardWrite": True})
    times = iter([0.0, 0.0, 1.1])
    monkeypatch.setattr(sdk, "monotonic", lambda: next(times))
    probes = []
    gesture.uia.focused_value = lambda: probes.append(1) or ((1,), "old")
    result = payload(await gesture.tools["type"]({"text": "x" * 45}))
    assert result["outcome"] == "unknown"
    assert len(probes) == 2  # pre-paste plus one read that consumed the deadline


async def test_start_waits_for_cold_launcher_focus(gesture, monkeypatch):
    await gesture.tools["request_access"]({"apps": ["Paint"], "reason": "test"})
    monkeypatch.setattr(sdk.sys, "platform", "win32")
    probes = []
    def focused():
        probes.append(1)
        return len(probes) == 30
    monkeypatch.setattr(windows, "start_search_focused", focused)
    assert payload(await gesture.tools["open_application"]({"app": "Paint"}))["method"] == "start_menu"
    assert len(probes) == 30 and ("type", "P") in gesture.events


async def test_start_polling_respects_elapsed_deadline(gesture, monkeypatch):
    await gesture.tools["request_access"]({"apps": ["Paint"], "reason": "test"})
    monkeypatch.setattr(sdk.sys, "platform", "win32")
    times = iter([0.0, 0.0, 2.1])
    monkeypatch.setattr(sdk, "monotonic", lambda: next(times))
    probes = []
    monkeypatch.setattr(windows, "start_search_focused", lambda: probes.append(1) or False)
    result = payload(await gesture.tools["open_application"]({"app": "Paint"}))
    assert result["code"] == "FOCUS_BLOCKED" and result["outcome"] == "partial"
    assert len(probes) == 1
    gesture.keyboard.type.assert_not_called()


async def test_system_grant_allows_authorized_pointer_and_typed_chords(gesture):
    t = gesture.tools
    await t["request_access"]({"apps": [], "reason": "test", "systemKeyCombos": True})
    await t["screenshot"]({})
    assert payload(await t["mouse_down"]({"text": "cmd"}))["success"]
    assert payload(await t["type"]({"text": "r"}))["success"]
    assert ("key_down", "cmd") in gesture.events and ("type", "r") in gesture.events
    await t["mouse_up"]({})


@pytest.mark.parametrize("cls,title,expected", [("DV2ControlHost", "", True),
    ("SearchPane", "", True), ("Windows.UI.Core.CoreWindow", "Start", True),
    ("Windows.UI.Core.CoreWindow", "Search", True),
    ("Windows.UI.Core.CoreWindow", "Other", False), ("CabinetWClass", "Start", False)])
def test_explorer_start_search_host_detection(monkeypatch, cls, title, expected):
    api = SimpleNamespace(GetClassNameW="class", GetWindowTextW="title")
    monkeypatch.setattr(windows, "_api", lambda: (None, None, api))
    monkeypatch.setattr(windows, "foreground_window", lambda: 99)
    monkeypatch.setattr(windows, "_pid", lambda hwnd: 1)
    monkeypatch.setattr(windows, "_exe_path", lambda pid: "explorer.exe")
    monkeypatch.setattr(windows, "_text", lambda fn, hwnd, size: cls if fn == "class" else title)
    assert windows.start_search_focused() is expected


async def test_window_bounds_validation_cached_per_action_not_path_point(gesture, monkeypatch):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await t["screenshot"]({"window_id": 101})
    original, calls = windows.find_window, []
    def find(window_id, displays):
        calls.append(window_id)
        return original(window_id, displays)
    monkeypatch.setattr(windows, "find_window", find)
    assert payload(await t["left_click_drag"]({"start_coordinate": [5, 5],
        "coordinate": [30, 30], "path": [[10, 10], [15, 20], [20, 10]]}))["success"]
    assert calls == [101]
    assert payload(await t["left_click"]({"coordinate": [20, 20]}))["success"]
    assert calls == [101, 101]


@pytest.mark.parametrize("platform,add,subtract", [("win32", 0x6B, 0x6D),
    ("darwin", 0x45, 0x4E), ("linux", 0xFFAB, 0xFFAD)])
async def test_keypad_arithmetic_sends_native_key_codes(gesture, monkeypatch, platform, add, subtract):
    monkeypatch.setattr(sdk.sys, "platform", platform)
    assert payload(await gesture.tools["key"]({"text": "Control_L+KP_Add"}))["success"]
    assert payload(await gesture.tools["key"]({"text": "KP_Subtract"}))["success"]
    assert [e for e in gesture.events if e[0] != "sleep"] == [
        ("key_down", "ctrl"), ("key_down", ("vk", add)), ("key_up", ("vk", add)),
        ("key_up", "ctrl"), ("key_down", ("vk", subtract)), ("key_up", ("vk", subtract))]


@pytest.mark.parametrize("action", ["open_application", "focus_window", "click_element", "set_value"])
@pytest.mark.parametrize("held", ["alt", "ctrl+shift", "shift", "a", "cmd"])
@pytest.mark.parametrize("system_grant", [False, True])
async def test_implicit_input_actions_refuse_held_keys(gesture, monkeypatch, action, held, system_grant):
    from app.mcp.computer_use import access
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad", "Paint"], "reason": "test", "systemKeyCombos": True})
    await t["screenshot"]({})
    tree = payload(await t["ui_tree"]({"window_id": 101}))
    element = tree["elements"][0]["element_id"]  # Edit: click_element uses its mouse fallback
    monkeypatch.setattr(windows, "foreground_window", lambda: 101)
    assert payload(await t["key_down"]({"text": held}))["success"]
    if not system_grant:
        access.revoke("test")
        access.grant("test", apps=["Notepad", "Paint"])
    gesture.events.clear()
    gesture.native.nudge = True
    args = {"open_application": {"app": "Paint\t" if held == "alt" else "Paint"},
            "focus_window": {"window_id": 101}, "click_element": {"element_id": element},
            "set_value": {"element_id": element, "value": "no"}}[action]
    result = payload(await t[action](args))
    assert result["code"] == "PERMISSION_REQUIRED" and result["outcome"] == "partial"
    assert "release held keys or buttons first" in result["error"].lower()
    assert gesture.native.focused == [] and gesture.uia.performed == []
    gesture.mouse.press.assert_not_called()
    assert gesture.events == [("key_up", key) for key in reversed(held.split("+"))]


@pytest.mark.parametrize("control", ["\t", "\n", "\r", "\x00", "\x7f", "\x85"])
async def test_open_application_rejects_control_characters(gesture, control):
    t = gesture.tools
    await t["request_access"]({"apps": ["Paint"], "reason": "test"})
    result = payload(await t["open_application"]({"app": "Paint" + control}))
    assert result["code"] == "INVALID_ARGUMENT" and result["outcome"] == "rejected"
    assert "control characters" in result["error"]
    assert gesture.native.focused == [] and gesture.events == []


@pytest.mark.parametrize("down,up,button", [("mouse_down", "mouse_up", "left"),
    ("mouse_down", "mouse_up", "right"), ("mouse_down", "mouse_up", "middle"),
    ("left_mouse_down", "left_mouse_up", "left")])
async def test_paired_window_drag_defers_bounds_check_until_drop(gesture, down, up, button):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    shot = payload(await t["screenshot"]({"window_id": 101}))["screenshot_id"]
    assert payload(await t[down]({"coordinate": [5, 2], "button": button}))["success"]
    gesture.windows[0] = replace(gesture.windows[0], bounds=(30, 10, 100, 50))
    for point in ([20, 2], [25, 3]):
        assert payload(await t["mouse_move"]({"coordinate": point, "screenshot_id": shot}))["success"]
        assert ("mouse_up", button) not in gesture.events
    result = payload(await t[up]({"coordinate": [30, 3], "button": button, "screenshot_id": shot}))
    assert result["success"] and result["released"]
    assert gesture.events.count(("mouse_up", button)) == 1
    assert payload(await t["left_click"]({"coordinate": [20, 2]}))["code"] == "STALE_OBSERVATION"
    # Deferral ended: the old crop is rejected before any additional press.
    gesture.mouse.press.assert_called_once_with(button)


@pytest.mark.parametrize("action", ["mouse_move", "mouse_up"])
async def test_paired_drag_keeps_screenshot_id_checks_and_reports_cleanup(gesture, action):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await t["screenshot"]({"window_id": 101})
    await t["mouse_down"]({"coordinate": [5, 2]})
    gesture.windows[0] = replace(gesture.windows[0], bounds=(30, 10, 100, 50))
    gesture.events.clear()
    result = payload(await t[action]({"coordinate": [20, 2], "screenshot_id": "obsolete"}))
    assert result["code"] == "STALE_OBSERVATION" and result["outcome"] == "partial"
    assert gesture.events == [("mouse_up", "left")]


async def test_batch_first_step_failure_counts_release_of_prior_call_hold(gesture):
    await gesture.tools["mouse_down"]({})
    gesture.events.clear()
    result = await gesture.tools["computer_batch"]({"actions": [{"action": "invalid"}]})
    failure = payload(result)
    assert failure["outcome"] == "partial" and failure["completed_actions"] == []
    assert gesture.events == [("mouse_up", "left")]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("held", [None, "shift", "mouse"])
async def test_invalid_modifiers_keep_argument_error_after_cleanup(gesture, batch, held):
    t = gesture.tools
    await t["screenshot"]({})
    if held == "mouse":
        await t["mouse_down"]({})
    elif held:
        await t["key_down"]({"text": held})
    gesture.events.clear()
    args = {"coordinate": [20, 20], "text": "bogus"}
    result = payload(await (t["computer_batch"]({"actions": [{"action": "left_click", **args}]})
                            if batch else t["left_click"](args)))
    assert result["code"] == "INVALID_ARGUMENT"
    assert result["outcome"] == ("partial" if held else "rejected")
    assert gesture.events == ([("mouse_up", "left")] if held == "mouse" else
                              [("key_up", held)] if held else [])


@pytest.mark.parametrize("action", ["open_application", "focus_window", "click_element", "set_value"])
@pytest.mark.parametrize("button", ["left", "right", "middle"])
async def test_implicit_input_actions_refuse_held_buttons(gesture, action, button):
    t = gesture.tools
    await t["request_access"]({"apps": ["Notepad", "Paint"], "reason": "test"})
    await t["screenshot"]({})
    tree = payload(await t["ui_tree"]({"window_id": 101}))
    element = tree["elements"][0]["element_id"]
    assert payload(await t["mouse_down"]({"button": button}))["success"]
    gesture.events.clear()
    args = {"open_application": {"app": "Paint"}, "focus_window": {"window_id": 101},
            "click_element": {"element_id": element},
            "set_value": {"element_id": element, "value": "no"}}[action]
    result = payload(await t[action](args))
    assert result["code"] == "PERMISSION_REQUIRED" and result["outcome"] == "partial"
    assert "release held keys or buttons first" in result["error"].lower()
    assert gesture.native.focused == [] and gesture.uia.performed == []
    assert gesture.events == [("mouse_up", button)]
