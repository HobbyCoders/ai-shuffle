"""Stop behavior and overlay geometry without sending any real host input."""

import ctypes
import threading
import time
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.mcp.computer_use import abort, activity, safety, overlay


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    monkeypatch.setattr(activity, "_sessions", {})
    monkeypatch.setattr(activity, "_last_stop", None)


def test_activity_persists_through_thinking_and_only_owner_stop_hides_it(monkeypatch):
    activity.record(2, session_id="active", cursor=(-50, -20))
    now = time.monotonic()
    monkeypatch.setattr(activity.time, "monotonic", lambda: now + 90)
    abort.request_stop("other")
    assert activity.snapshot()["active"]
    assert activity.snapshot()["cursor"] == [-50, -20]
    abort.request_stop("active")
    assert not activity.snapshot()["active"]


@pytest.mark.parametrize("flags,msg,stopped", [(0, 0x100, True), (0, 0x104, True),
                                               (0x10, 0x100, False), (0, 0x101, False)])
def test_physical_escape_only(flags, msg, stopped):
    activity.record(1, session_id="s")
    assert safety._win32_event_filter(msg, SimpleNamespace(vkCode=0x1B, flags=flags))
    assert abort.is_stopped("s") is stopped


def test_escape_while_idle_does_not_halt_future_control():
    safety._win32_event_filter(0x100, SimpleNamespace(vkCode=0x1B, flags=0))
    assert not abort.is_stopped("s")


def test_escape_listener_is_started_once_and_restarts_after_exit(monkeypatch):
    listeners = []
    def create(**kwargs):
        listener = MagicMock()
        listener.is_alive.return_value = True
        listeners.append(listener)
        return listener
    monkeypatch.setattr(safety, "_listener", None)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", SimpleNamespace(Listener=create, Key=SimpleNamespace(esc="esc")))
    assert safety.ensure_started()
    assert safety.ensure_started()
    assert len(listeners) == 1
    listeners[0].is_alive.return_value = False
    assert safety.ensure_started()
    assert len(listeners) == 2


def test_escape_listener_failure_is_reported_without_blocking_tools(monkeypatch):
    monkeypatch.setattr(safety, "_listener", None)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", SimpleNamespace(
        Listener=MagicMock(side_effect=RuntimeError("unavailable")), Key=SimpleNamespace(esc="esc")))
    assert not safety.ensure_started()
    assert not safety.escape_available()


def test_stop_releases_only_agent_held_keys_and_mouse_buttons():
    mouse, keyboard = MagicMock(), MagicMock()
    inputs = safety.SafeInput("s", mouse, keyboard)
    inputs.press(keyboard, "ctrl")
    inputs.press(mouse, "left")
    abort.request_stop("s")
    mouse.release.assert_called_once_with("left")
    keyboard.release.assert_called_once_with("ctrl")
    assert inputs.held == []


def test_old_worker_stays_stopped_after_new_turn_resumes():
    inputs = safety.SafeInput("s", MagicMock(), MagicMock())
    abort.request_stop("s")
    abort.resume("s")
    with pytest.raises(abort.Aborted):
        inputs.press(inputs.keyboard, "x")
    inputs.keyboard.press.assert_not_called()


def test_worker_created_during_stop_does_not_revive_with_new_turn():
    abort.request_stop("s")
    inputs = safety.SafeInput("s", MagicMock(), MagicMock())
    abort.resume("s")
    with pytest.raises(abort.Aborted):
        inputs.press(inputs.keyboard, "x")
    inputs.keyboard.press.assert_not_called()


def test_long_wait_is_interruptible():
    inputs = safety.SafeInput("s", MagicMock(), MagicMock())
    done = threading.Event()
    def worker():
        with pytest.raises(abort.Aborted):
            inputs.sleep(60)
        done.set()
    thread = threading.Thread(target=worker)
    thread.start()
    abort.request_stop("s")
    assert done.wait(0.5), "Stop must interrupt a 60-second wait promptly"
    thread.join()


def test_overlay_uses_action_geometry_on_negative_origin_monitor():
    manager = overlay._OverlayManager()
    manager._root = MagicMock()
    manager._apply_clickthrough = MagicMock()
    manager._position = MagicMock()
    manager._draw = MagicMock()
    geo = {"left": -2560, "top": -300, "width": 2560, "height": 1600}
    activity.record(2, session_id="s", geometry=geo, cursor=(-100, 200))
    abort.request_stop("unrelated")
    manager._update()
    manager._position.assert_called_once_with(-2560, -300, 2560, 1600)
    assert manager._draw.call_args.args[:2] == ((-2560, -300, 2560, 1600), "Display 2")
    abort.request_stop("s")
    manager._update()
    manager._root.withdraw.assert_called_once()


def test_overlay_position_passes_signed_coordinates_to_win32(monkeypatch):
    user32 = MagicMock()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    manager = overlay._OverlayManager()
    manager._hwnd = 123
    manager._position(-1920, -1080, 1920, 1080)
    user32.SetWindowPos.assert_called_once_with(123, -1, -1920, -1080, 1920, 1080, 0x50)


def test_agent_cursor_drawn_at_physical_point_relative_to_active_display():
    manager = overlay._OverlayManager()
    manager._canvas = MagicMock()
    geo = {"left": -1920, "top": -200, "width": 1920, "height": 1080}
    activity.record(2, session_id="s", action="left_click", geometry=geo, cursor=(-1820, -150))
    manager._draw((-1920, -200, 1920, 1080), "Display 2", activity.snapshot())
    assert manager._canvas.create_polygon.call_args.args[:2] == (100, 50)
    manager._canvas.create_oval.assert_called_once()


_FMODEL = {"id": 7, "title": "FModel - Archive", "app": "FModel", "display": 2,
           "left": -1500, "top": 100, "width": 800, "height": 600}
_DISPLAY_2 = {"left": -1920, "top": -200, "width": 1920, "height": 1080}


def test_activity_record_carries_the_pinned_window():
    activity.record(2, session_id="s", action="focus_window", geometry=_DISPLAY_2,
                    window=_FMODEL)
    assert activity.snapshot()["window"] == _FMODEL
    # A display-wide action records no window: the overlay frames the display.
    activity.record(2, session_id="s", action="screenshot", geometry=_DISPLAY_2)
    assert activity.snapshot()["window"] is None
    abort.request_stop("s")
    assert activity.snapshot()["window"] is None


@pytest.mark.parametrize("window,expected", [
    (None, ((-1920, -200, 1920, 1080), "Display 2")),
    (_FMODEL, ((-1500, 100, 800, 600), "FModel")),
    # Clipped to the display it is framed on.
    ({**_FMODEL, "left": -2000, "top": 700, "width": 500, "height": 400},
     ((-1920, 700, 420, 180), "FModel")),
    # No app name: the title.
    ({**_FMODEL, "app": ""}, ((-1500, 100, 800, 600), "FModel - Archive")),
    # Entirely off the display, or malformed: the display frame.
    ({**_FMODEL, "left": 500}, ((-1920, -200, 1920, 1080), "Display 2")),
    ({"app": "FModel"}, ((-1920, -200, 1920, 1080), "Display 2")),
])
def test_frame_for_picks_the_window_or_the_display(window, expected):
    snap = {"display": 2, "geometry": _DISPLAY_2, "window": window}
    assert overlay.frame_for(snap) == expected


def test_frame_for_without_geometry_frames_nothing():
    assert overlay.frame_for({"display": None, "geometry": None, "window": _FMODEL}) == (None, "")


def test_overlay_frames_the_pinned_window_and_names_its_app():
    manager = overlay._OverlayManager()
    manager._root = MagicMock()
    manager._canvas = MagicMock()
    manager._apply_clickthrough = MagicMock()
    manager._position = MagicMock()
    activity.record(2, session_id="s", action="left_click", geometry=_DISPLAY_2,
                    cursor=(-1400, 150), window=_FMODEL)
    manager._update()
    manager._position.assert_called_once_with(-1500, 100, 800, 600)
    labels = [c.kwargs.get("text", "") for c in manager._canvas.create_text.call_args_list]
    assert any("AI Shuffle · FModel · Clicking ·" in t for t in labels)
    # The agent cursor is drawn relative to the window frame.
    assert manager._canvas.create_polygon.call_args.args[:2] == (100, 50)
