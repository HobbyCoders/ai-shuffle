"""Regression timelines for desktop gestures and guardrails; no real desktop I/O."""

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
    values = iter([((1,), "old"), ((1,), text if landed else "old")])
    gesture.uia.focused_value = lambda: next(values)
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
    ("Control_L+KP_Add", ["ctrl", "+"]), ("ctrl+KP_Subtract", ["ctrl", "-"]),
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
    gesture.mouse.release.assert_called_once_with("right")
    gesture.keyboard.release.assert_called_once_with("ctrl")


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
