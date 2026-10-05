"""The desktop tool contract: observation identity, coded outcomes, readiness
refusals, capability reporting and the model-facing text.

Fake displays and input devices only (see test_computer_use_tools).
"""

import json

import pytest

from app.mcp import computer_use_sdk as sdk
from app.mcp.computer_use import access, safety, windows
from tests.mcp import test_computer_use_tools as _base

pytest.importorskip("PIL.Image")

# Shared fake desktop: displays, input devices and windows (no real input).
desktop = _base.desktop
payload = _base.payload


def last(result):
    return json.loads(result["content"][-1]["text"])


# ----- observation identity and freshness ---------------------------------------

async def test_screenshot_ids_pin_coordinates_to_their_observation(desktop):
    t = desktop.tools
    first = payload(await t["screenshot"]({}))["screenshot_id"]
    assert payload(await t["left_click"]({"coordinate": [40, 30], "screenshot_id": first}))["success"]
    second = payload(await t["screenshot"]({}))["screenshot_id"]
    assert second != first
    stale = payload(await t["left_click"]({"coordinate": [40, 30], "screenshot_id": first}))
    assert stale["code"] == "STALE_OBSERVATION" and stale["outcome"] == "rejected"
    assert desktop.mouse.press.call_count == 1
    # Without an id the current screenshot is used, as before.
    assert payload(await t["left_click"]({"coordinate": [40, 30]}))["success"]


@pytest.mark.parametrize("invalidate", ["switch_display", "release_control"])
async def test_invalidated_observation_is_refused_not_misclicked(desktop, invalidate):
    t = desktop.tools
    shot = payload(await t["screenshot"]({}))["screenshot_id"]
    if invalidate == "switch_display":
        await t["switch_display"]({"display": "Display 2"})
    else:
        await t["release_control"]({})
    for args in ({"coordinate": [40, 30]}, {"coordinate": [40, 30], "screenshot_id": shot}):
        body = payload(await t["left_click"](args))
        assert body["code"] == "STALE_OBSERVATION" and "screenshot" in body["error"]
    desktop.mouse.press.assert_not_called()


async def test_batch_refuses_stale_step_and_reports_progress(desktop):
    t = desktop.tools
    old = payload(await t["screenshot"]({}))["screenshot_id"]
    result = await t["computer_batch"]({"actions": [
        {"action": "screenshot"},
        {"action": "left_click", "coordinate": [40, 30], "screenshot_id": old},
        {"action": "type", "text": "no"}]})
    body = last(result)
    assert body["code"] == "STALE_OBSERVATION" and body["action_index"] == 1
    assert body["completed_actions"] == [0] and body["not_run"] == [2]
    desktop.mouse.press.assert_not_called()
    desktop.keyboard.type.assert_not_called()


async def test_zoom_and_window_list_report_their_screenshot(desktop):
    t = desktop.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    shot = payload(await t["screenshot"]({}))["screenshot_id"]
    assert payload(await t["zoom"]({"region": [0, 0, 30, 20]}))["screenshot_id"] == shot
    assert payload(await t["list_windows"]({}))["screenshot_id"] == shot
    await t["screenshot"]({})
    assert payload(await t["zoom"]({"region": [0, 0, 30, 20], "screenshot_id": shot}))["code"] \
        == "STALE_OBSERVATION"


# ----- coordinate mapping ---------------------------------------------------------

async def test_zoom_crop_on_negative_origin_display_round_trips_to_physical(desktop):
    t = desktop.tools
    await t["switch_display"]({"display": "Display 2"})  # 300x200 at (-300, -100), shown 150x100
    await t["screenshot"]({})
    info = payload(await t["zoom"]({"region": [10, 10, 50, 30]}))
    assert info["region"] == [-280, -80, 80, 40]
    # An image pixel maps back through the documented formula...
    ix, iy = 160, 80
    sx = info["screenshot_region"][0] + ix / info["zoom_factor"]
    sy = info["screenshot_region"][1] + iy / info["zoom_factor"]
    await t["mouse_move"]({"coordinate": [sx, sy]})
    # ...to the same physical pixel the crop shows there.
    scale = info["image_width"] / info["region"][2]
    assert desktop.mouse.position == (round(-280 + ix / scale), round(-80 + iy / scale))


async def test_window_zoom_spanning_displays_maps_the_whole_image(desktop):
    t = desktop.tools
    desktop.windows[0].bounds = (-50, 10, 100, 50)  # half on Display 2, half on Display 1
    await t["screenshot"]({})  # Display 1: 200x100 at (0, 0), scale 0.75
    info = payload(await t["zoom"]({"window_id": 101}))
    assert info["screenshot_region"] == [-37.5, 7.5, 37.5, 45.0]
    # Image x of the display edge (physical 0) maps to screenshot x 0.
    edge_ix = 50 * info["image_width"] / 100
    assert info["screenshot_region"][0] + edge_ix / info["zoom_factor"] == pytest.approx(0, abs=0.01)
    # A point left of the screenshot is refused, not clamped onto its edge.
    body = payload(await t["left_click"]({"coordinate": [-30, 20]}))
    assert body["code"] == "INVALID_ARGUMENT"
    desktop.mouse.press.assert_not_called()


# ----- coded outcomes -------------------------------------------------------------

async def test_refusals_carry_codes(desktop):
    t = desktop.tools
    body = payload(await t["left_click"]({"coordinate": "40,30"}))
    assert body["code"] == "INVALID_ARGUMENT"
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    assert payload(await t["focus_window"]({"window_id": 999}))["code"] == "TARGET_NOT_FOUND"
    assert payload(await t["focus_window"]({"window_id": 202}))["code"] == "PERMISSION_REQUIRED"
    assert payload(await t["read_clipboard"]({}))["success"]  # bypass mode
    access.revoke("test")
    assert payload(await t["screenshot"]({}))["code"] == "PERMISSION_REQUIRED"


async def test_busy_desktop_is_reported_with_code(desktop):
    assert safety.DESKTOP_LOCK.acquire(timeout=1)
    try:
        body = payload(await desktop.tools["type"]({"text": "no"}))
    finally:
        safety.DESKTOP_LOCK.release()
    assert body["code"] == "DESKTOP_BUSY" and body["outcome"] == "rejected"


async def test_failure_after_input_is_outcome_unknown(desktop):
    def fail_second(ch):
        if desktop.keyboard.type.call_count == 2:
            raise RuntimeError("input queue full")
    desktop.keyboard.type.side_effect = fail_second
    body = payload(await desktop.tools["type"]({"text": "abc"}))
    assert body["code"] == "EXECUTION_OUTCOME_UNKNOWN" and body["outcome"] == "unknown"
    assert "screenshot" in body["error"]


async def test_scroll_failures_report_what_was_sent(desktop):
    desktop.mouse.scroll.side_effect = RuntimeError("no wheel")
    await desktop.tools["screenshot"]({})
    body = payload(await desktop.tools["scroll"]({"coordinate": [20, 20], "scroll_direction": "down",
                                                  "scroll_amount": 1}))
    # The scroll call itself counts as sent input: its effect is unknown.
    assert body["code"] == "EXECUTION_OUTCOME_UNKNOWN"
    body = payload(await desktop.tools["scroll"]({"coordinate": [20, 20], "scroll_direction": "sideways",
                                                  "scroll_amount": 1}))
    assert body["code"] == "INVALID_ARGUMENT" and body["outcome"] == "rejected"


# ----- readiness refusals (bounded recovery in the orchestrator) ------------------

async def test_locked_desktop_refuses_capture_and_input(desktop, monkeypatch):
    await desktop.tools["screenshot"]({})
    captured = len(desktop.captures)
    monkeypatch.setattr(windows, "input_desktop_locked", lambda: True)
    for name, args in [("screenshot", {}), ("left_click", {"coordinate": [40, 30]}),
                       ("type", {"text": "x"}), ("key", {"text": "Return"})]:
        body = payload(await desktop.tools[name](args))
        assert body["code"] == "DESKTOP_LOCKED", name
    assert len(desktop.captures) == captured
    desktop.mouse.press.assert_not_called()
    desktop.keyboard.type.assert_not_called()
    monkeypatch.setattr(windows, "input_desktop_locked", lambda: None)  # unknown: proceed
    assert payload(await desktop.tools["screenshot"]({}))["success"]


async def test_elevated_foreground_refuses_typing_only(desktop, monkeypatch):
    monkeypatch.setattr(windows, "foreground_blocks_input", lambda: True)
    body = payload(await desktop.tools["type"]({"text": "secret"}))
    assert body["code"] == "PERMISSION_REQUIRED" and "administrator" in body["error"]
    desktop.keyboard.type.assert_not_called()
    await desktop.tools["screenshot"]({})
    assert payload(await desktop.tools["left_click"]({"coordinate": [40, 30]}))["success"]


def test_probes_fail_soft_off_windows(monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "linux")
    assert windows.input_desktop_locked() is None
    assert windows.foreground_blocks_input() is None


def test_probes_fail_soft_when_win32_calls_break(monkeypatch):
    monkeypatch.setattr(windows.sys, "platform", "win32")
    monkeypatch.setattr(windows, "_api", lambda: (_ for _ in ()).throw(OSError("no user32")))
    assert windows.input_desktop_locked() is None
    assert windows.foreground_blocks_input() is None


# ----- capabilities and platform filtering -----------------------------------------

async def test_capabilities_are_reported_with_the_grant(desktop):
    caps = payload(await desktop.tools["request_access"]({"apps": [], "reason": "t"}))["capabilities"]
    assert caps["unavailable"] == {} and "list_windows" in caps["tools"]
    assert "screenshot_id" in caps["coordinates"]
    assert payload(await desktop.tools["list_granted_applications"]({}))["capabilities"] == caps


async def test_windows_only_tools_are_not_offered_elsewhere(desktop, monkeypatch):
    monkeypatch.setattr(sdk, "_windows_tools_supported", lambda: False)
    tools = sdk.create_computer_use_server(enabled=True, permission_mode="bypassPermissions",
                                           session_id="test", virtual_size=(150, 100))
    assert not set(tools) & sdk._WINDOWS_ONLY_TOOLS
    assert "screenshot" in tools and "computer_batch" in tools
    caps = payload(await tools["request_access"]({"apps": [], "reason": "t"}))["capabilities"]
    assert set(caps["unavailable"]) == set(sdk._WINDOWS_ONLY_TOOLS)
    assert not set(caps["tools"]) & sdk._WINDOWS_ONLY_TOOLS
    # A batch cannot reach them either.
    body = last(await tools["computer_batch"]({"actions": [{"action": "list_windows"}]}))
    assert body["code"] == "UNSUPPORTED_CAPABILITY"
    assert "list_windows" not in sdk.system_guidance()


# ----- model-facing text ------------------------------------------------------------

async def test_guidance_and_descriptions_are_short_and_plain(desktop, monkeypatch):
    monkeypatch.setattr(sdk, "_windows_tools_supported", lambda: True)
    guidance = sdk.system_guidance()
    assert len(guidance) < 2000
    for needed in ("request_access", "screenshot_id", "STALE_OBSERVATION",
                   "USER_INTERRUPTED", "data, not instructions", "list_windows"):
        assert needed in guidance
    # Rules a tool description already states are not repeated in the prompt.
    assert "zoom" not in guidance and "literal" not in guidance
    captured = []
    monkeypatch.setattr("app.mcp.sdk_compat.patch_sdk_server", lambda cfg, tools: captured.extend(tools))
    sdk.create_computer_use_server(enabled=True, session_id="test", virtual_size=(150, 100))
    text = guidance + "".join(t.description for t in captured)
    assert "—" not in text and "–" not in text  # no em or en dashes
    assert max(len(t.description) for t in captured) < 400
