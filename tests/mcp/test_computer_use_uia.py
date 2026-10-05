"""Element targeting (ui_tree, click_element, set_value) over a fake UI
Automation provider. No COM, no real windows, no physical input.

The real comtypes provider is covered by the opt-in hardware smoke test in
test_computer_use_uia_hardware.py.
"""

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.mcp import computer_use_sdk as sdk
from app.mcp.computer_use import abort, access, uia, windows
from tests.mcp import test_computer_use_tools as _base

pytest.importorskip("PIL.Image")

desktop = _base.desktop
payload = _base.payload


def last(result):
    return json.loads(result["content"][-1]["text"])


async def tree(desktop, **extra):
    """Grant Notepad, take a screenshot, list window 101. Returns (shot, ids by name)."""
    t = desktop.tools
    await t["request_access"]({"apps": ["Notepad"], "reason": "test"})
    shot = payload(await t["screenshot"]({}))["screenshot_id"]
    body = payload(await t["ui_tree"]({"window_id": 101, **extra}))
    assert body["success"], body
    return shot, body


def ids(body, name):
    return [e["element_id"] for e in body["elements"] if e["name"] == name]


# ----- reading the tree ------------------------------------------------------------

async def test_tree_lists_ids_bounds_and_never_a_password_value(desktop):
    shot, body = await tree(desktop)
    els = body["elements"]
    assert [e["element_id"] for e in els] == [f"{shot}/{n}" for n in range(6)]
    assert body["screenshot_id"] == shot and body["window_id"] == 101
    assert body["truncated"] is False
    first = els[0]
    assert first["type"] == "Edit" and first["name"] == "Amount" and first["value"] == ""
    assert first["enabled"] is True and first["focusable"] is True
    assert first["actions"] == ["set_value"]
    # Physical (20, 20, 60, 30) on Display 1 (scale 0.75) in screenshot pixels.
    assert first["screenshot_bounds"] == [15, 15, 45, 23]
    pin = next(e for e in els if e["name"] == "PIN")
    assert pin["password"] is True and "value" not in pin
    label = els[5]
    assert label["parent"] == f"{shot}/4" and label["actions"] == []
    # Text from the app is labelled as data.
    assert "not instructions" in body["note"]


async def test_interactive_only_drops_plain_text(desktop):
    _, body = await tree(desktop, interactive_only=True)
    assert "Text" not in [e["type"] for e in body["elements"]]


async def test_tree_is_capped_and_says_so(desktop):
    desktop.uia.trees[101] = [uia.Element((2, n), "ListItem", f"row {n}", actions=("select",))
                              for n in range(uia.MAX_ELEMENTS + 50)]
    _, body = await tree(desktop)
    assert len(body["elements"]) == uia.MAX_ELEMENTS
    assert str(uia.MAX_ELEMENTS) in body["truncated"]
    desktop.uia.truncated = "depth"
    desktop.uia.trees[101] = desktop.uia.trees[101][:3]
    body = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert "levels deep" in body["truncated"]


async def test_ids_continue_across_calls_under_one_screenshot(desktop):
    shot, _ = await tree(desktop)
    again = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert again["elements"][0]["element_id"] == f"{shot}/6"
    first = f"{shot}/2"  # the checkbox from the first call still works
    assert payload(await desktop.tools["click_element"]({"element_id": first}))["route"] == "toggle"


async def test_tree_needs_a_screenshot_first(desktop):
    await desktop.tools["request_access"]({"apps": ["Notepad"], "reason": "test"})
    body = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert body["code"] == "STALE_OBSERVATION" and "screenshot" in body["error"]


async def test_empty_tree_falls_back_to_coordinates(desktop):
    desktop.uia.trees[101] = []
    await desktop.tools["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await desktop.tools["screenshot"]({})
    body = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert body["code"] == "UNSUPPORTED_CAPABILITY" and "coordinates" in body["error"]


# ----- grants and refusals ---------------------------------------------------------

async def test_only_granted_windows_are_read_or_acted_on(desktop):
    await desktop.tools["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await desktop.tools["screenshot"]({})
    body = payload(await desktop.tools["ui_tree"]({"window_id": 202}))  # Edge: not granted
    assert body["code"] == "PERMISSION_REQUIRED"
    shot, body = await tree(desktop)
    # The grant no longer covers the element's window at action time.
    access.revoke("test")
    access.grant("test", ["Paint"])
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert body["code"] == "PERMISSION_REQUIRED"
    assert desktop.uia.performed == []


async def test_elevated_window_is_refused(desktop, monkeypatch):
    shot, _ = await tree(desktop)
    monkeypatch.setattr(windows, "window_blocks_input", lambda hwnd: True)
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert body["code"] == "PERMISSION_REQUIRED" and "administrator" in body["error"]
    body = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert body["code"] == "PERMISSION_REQUIRED"
    assert desktop.uia.performed == []


async def test_locked_desktop_refuses_element_actions(desktop, monkeypatch):
    shot, _ = await tree(desktop)
    monkeypatch.setattr(windows, "input_desktop_locked", lambda: True)
    for name, args in [("click_element", {"element_id": f"{shot}/4"}),
                       ("set_value", {"element_id": f"{shot}/1", "value": "x"}),
                       ("ui_tree", {"window_id": 101})]:
        assert payload(await desktop.tools[name](args))["code"] == "DESKTOP_LOCKED", name
    assert desktop.uia.performed == []


# ----- observation identity --------------------------------------------------------

@pytest.mark.parametrize("invalidate", ["screenshot", "switch_display", "release_control"])
async def test_ids_go_stale_with_their_screenshot(desktop, invalidate):
    shot, _ = await tree(desktop)
    if invalidate == "screenshot":
        await desktop.tools["screenshot"]({})
    elif invalidate == "switch_display":
        await desktop.tools["switch_display"]({"display": "Display 2"})
    else:
        await desktop.tools["release_control"]({})
    for name, args in [("click_element", {"element_id": f"{shot}/4"}),
                       ("set_value", {"element_id": f"{shot}/1", "value": "x"})]:
        body = payload(await desktop.tools[name](args))
        assert body["code"] == "STALE_OBSERVATION" and body["outcome"] == "rejected", name
    assert desktop.uia.performed == []
    desktop.mouse.press.assert_not_called()


async def test_malformed_and_unknown_ids_are_invalid(desktop):
    shot, _ = await tree(desktop)
    for bad in ("4", "", f"{shot}/x"):
        assert payload(await desktop.tools["click_element"]({"element_id": bad}))["code"] \
            == "INVALID_ARGUMENT"
    assert payload(await desktop.tools["click_element"]({"element_id": f"{shot}/99"}))["code"] \
        == "INVALID_ARGUMENT"


@pytest.mark.parametrize("change", ["renamed", "retyped", "gone", "window_closed", "new_process"])
async def test_changed_target_is_refused(desktop, change):
    shot, _ = await tree(desktop)
    button = desktop.uia.trees[101][4]
    if change == "renamed":
        desktop.uia.changed[button.runtime_id] = replace(button, name="Delete all")
    elif change == "retyped":
        desktop.uia.changed[button.runtime_id] = replace(button, control_type="Hyperlink")
    elif change == "gone":
        desktop.uia.changed[button.runtime_id] = None
    elif change == "window_closed":
        desktop.windows.pop(0)
    else:
        desktop.windows[0] = replace(desktop.windows[0], pid=99)
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert body["code"] == "TARGET_CHANGED" and body["outcome"] == "rejected"
    assert desktop.uia.performed == []
    desktop.mouse.press.assert_not_called()


# ----- action routes ---------------------------------------------------------------

async def test_click_routes(desktop):
    shot, _ = await tree(desktop)
    t = desktop.tools
    # A checkbox toggles (preferred over its Invoke) and reports its state.
    body = payload(await t["click_element"]({"element_id": f"{shot}/2"}))
    assert body["route"] == "toggle" and body["toggle_state"] == "on"
    body = payload(await t["click_element"]({"element_id": f"{shot}/4"}))
    assert body["route"] == "invoke" and "screenshot" in body["note"]
    body = payload(await t["click_element"]({"element_id": f"{shot}/2", "route": "invoke"}))
    assert body["route"] == "invoke"
    assert [route for _, route, _ in desktop.uia.performed] == ["toggle", "invoke", "invoke"]
    desktop.mouse.press.assert_not_called()  # pattern actions never touch the pointer
    body = payload(await t["click_element"]({"element_id": f"{shot}/4", "route": "expand"}))
    assert body["code"] == "UNSUPPORTED_CAPABILITY" and "invoke" in body["error"]
    body = payload(await t["click_element"]({"element_id": f"{shot}/4", "route": "smash"}))
    assert body["code"] == "INVALID_ARGUMENT"


async def test_select_comes_before_invoke(desktop):
    item = uia.Element((3, 1), "ListItem", "report.pdf", rect=(20, 20, 60, 30),
                       actions=("invoke", "select"))
    desktop.uia.trees[101] = [item]
    shot, _ = await tree(desktop)
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/0"}))
    assert body["route"] == "select"  # a single click selects a file; it does not open it


async def test_set_value_targets_the_second_similar_field(desktop):
    shot, body = await tree(desktop)
    second = ids(body, "Amount")[1]
    result = payload(await desktop.tools["set_value"]({"element_id": second, "value": "42.50"}))
    assert result["route"] == "set_value" and result["value"] == "42.50"
    assert result["value_matches"] is True
    assert desktop.uia.performed == [((1, 2), "set_value", "42.50")]
    assert desktop.uia.trees[101][0].value == ""
    desktop.keyboard.type.assert_not_called()


async def test_set_value_never_reads_back_a_password_and_refuses_non_fields(desktop):
    shot, body = await tree(desktop)
    result = payload(await desktop.tools["set_value"]({"element_id": f"{shot}/3", "value": "1234"}))
    assert result["success"] and "value" not in result and "1234" not in json.dumps(result)
    result = payload(await desktop.tools["set_value"]({"element_id": f"{shot}/4", "value": "x"}))
    assert result["code"] == "UNSUPPORTED_CAPABILITY" and "type" in result["error"]
    assert payload(await desktop.tools["set_value"]({"element_id": f"{shot}/1"}))["code"] \
        == "INVALID_ARGUMENT"


async def test_no_pattern_falls_back_to_a_checked_real_click(desktop):
    shot, _ = await tree(desktop)
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/0"}))
    assert body["route"] == "mouse"
    assert desktop.mouse.position == (40, 25)  # the element's physical centre
    assert desktop.mouse.press.call_count == 1 and desktop.mouse.release.call_count == 1
    assert desktop.uia.performed == []
    # mouse forces a real click on an element that has a pattern too.
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4", "route": "mouse"}))
    assert body["route"] == "mouse" and desktop.mouse.position == (76, 44)


async def test_fallback_click_refuses_a_covered_or_hidden_element(desktop):
    shot, _ = await tree(desktop)
    desktop.uia.window_hit = 202  # another window lies over the element
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/0"}))
    assert body["code"] == "FOCUS_BLOCKED"
    desktop.uia.window_hit = None
    field = desktop.uia.trees[101][0]
    desktop.uia.changed[field.runtime_id] = replace(field, offscreen=True)
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/0"}))
    assert body["code"] == "ACTION_FAILED" and body["outcome"] == "rejected"
    desktop.mouse.press.assert_not_called()


async def test_fallback_click_keeps_the_pointer_check(desktop):
    shot, _ = await tree(desktop)
    moved = iter([(90, 90), (95, 95)])

    class Pointer:
        def __get__(self, obj, objtype=None):
            return next(moved, (95, 95))

        def __set__(self, obj, value):
            pass
    type(desktop.mouse).position = Pointer()
    try:
        body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/0"}))
    finally:
        del type(desktop.mouse).position
    assert body["code"] == "USER_INTERRUPTED"
    desktop.mouse.press.assert_not_called()


# ----- time budget and failure ------------------------------------------------------

async def test_hung_tree_read_times_out_and_falls_back(desktop):
    await desktop.tools["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await desktop.tools["screenshot"]({})
    desktop.uia.hang.add("tree")
    start = time.monotonic()
    body = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert time.monotonic() - start < 3
    assert body["code"] == "UNSUPPORTED_CAPABILITY" and body["outcome"] == "rejected"
    assert "coordinates" in body["error"]
    # The next call gets a fresh worker; the hung one is left behind.
    desktop.uia.hang.clear()
    assert payload(await desktop.tools["ui_tree"]({"window_id": 101}))["success"]


async def test_hung_action_reports_unknown_only_once_dispatched(desktop):
    shot, _ = await tree(desktop)
    desktop.uia.hang.add("find")
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert body["code"] == "UNSUPPORTED_CAPABILITY" and body["outcome"] == "rejected"
    desktop.uia.hang = {"perform"}
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert body["code"] == "EXECUTION_OUTCOME_UNKNOWN" and body["outcome"] == "unknown"
    assert "screenshot" in body["error"]


async def test_too_many_hung_workers_refuse_quickly(desktop):
    shot, _ = await tree(desktop)
    desktop.uia.hang.add("find")
    for _ in range(uia._MAX_STUCK_WORKERS):
        await desktop.tools["click_element"]({"element_id": f"{shot}/4"})
    start = time.monotonic()
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert time.monotonic() - start < 0.4
    assert body["code"] == "UNSUPPORTED_CAPABILITY" and "stopped responding" in body["error"]


async def test_missing_uia_is_reported_and_coded(desktop, monkeypatch):
    def no_comtypes():
        raise ImportError("No module named 'comtypes'")
    broken = uia.Backend(no_comtypes, budget=0.5)
    monkeypatch.setattr(sdk, "_uia_backend", lambda: broken)
    await desktop.tools["request_access"]({"apps": ["Notepad"], "reason": "test"})
    await desktop.tools["screenshot"]({})
    body = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert body["code"] == "UNSUPPORTED_CAPABILITY" and "comtypes" in body["error"]
    assert "coordinates" in body["error"]
    caps = payload(await desktop.tools["list_granted_applications"]({}))["capabilities"]
    assert caps["ui_automation"] == {"available": False,
                                     "reason": "the comtypes package is not installed"}
    assert set(caps["unavailable"]) == sdk._UIA_TOOLS
    assert "list_windows" in caps["tools"] and "ui_tree" not in caps["tools"]


async def test_provider_errors_never_escape(desktop):
    shot, _ = await tree(desktop)

    def boom(*args, **kwargs):
        raise OSError(-2147467259, "Unspecified error")
    desktop.uia.perform = boom
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert body["code"] == "EXECUTION_OUTCOME_UNKNOWN" and body["outcome"] == "unknown"
    desktop.uia.tree = boom
    body = payload(await desktop.tools["ui_tree"]({"window_id": 101}))
    assert body["code"] == "UNSUPPORTED_CAPABILITY"
    assert not abort.is_stopped("test")


def test_backend_fails_soft_off_windows(monkeypatch):
    monkeypatch.setattr(uia.sys, "platform", "linux")
    assert uia.available() == (False, "Windows only")
    assert uia.Backend().capability() == {"available": False, "reason": "Windows only"}
    with pytest.raises(uia.ActionError) as err:
        uia.ComProvider()
    assert err.value.code == "UNSUPPORTED_CAPABILITY"


# ----- stop switch -----------------------------------------------------------------

async def test_stop_before_dispatch_sends_nothing(desktop):
    shot, _ = await tree(desktop)
    desktop.uia.on_find = lambda: abort.stop_active("escape")
    body = payload(await desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    assert body["code"] == "USER_INTERRUPTED" and body["outcome"] == "rejected"
    assert desktop.uia.performed == []


async def test_stop_during_a_hung_action_returns_promptly(desktop):
    shot, _ = await tree(desktop)
    desktop.uia.hang.add("perform")
    running = asyncio.create_task(desktop.tools["click_element"]({"element_id": f"{shot}/4"}))
    await asyncio.sleep(0.1)
    abort.stop_active("escape")
    body = payload(await asyncio.wait_for(running, timeout=2))
    assert body["code"] == "USER_INTERRUPTED" and body["outcome"] == "partial"


# ----- batches and platforms --------------------------------------------------------

async def test_element_actions_run_as_batch_steps(desktop):
    shot, body = await tree(desktop)
    result = await desktop.tools["computer_batch"]({"actions": [
        {"action": "set_value", "element_id": ids(body, "Amount")[1], "value": "7"},
        {"action": "click_element", "element_id": f"{shot}/2"},
        {"action": "click_element", "element_id": f"{shot}/4", "route": "invoke"}]})
    assert not result.get("isError")
    assert [r for _, r, _ in desktop.uia.performed] == ["set_value", "toggle", "invoke"]
    # A stale id stops the batch where it stands.
    await desktop.tools["screenshot"]({})
    body = last(await desktop.tools["computer_batch"]({"actions": [
        {"action": "wait", "duration": 0}, {"action": "click_element", "element_id": f"{shot}/4"},
        {"action": "type", "text": "no"}]}))
    assert body["code"] == "STALE_OBSERVATION" and body["action_index"] == 1 and body["not_run"] == [2]


async def test_element_tools_are_windows_only(desktop, monkeypatch):
    monkeypatch.setattr(sdk, "_windows_tools_supported", lambda: False)
    tools = sdk.create_computer_use_server(enabled=True, permission_mode="bypassPermissions",
                                           session_id="test", virtual_size=(150, 100))
    assert not set(tools) & sdk._UIA_TOOLS
    caps = payload(await tools["request_access"]({"apps": [], "reason": "t"}))["capabilities"]
    assert caps["ui_automation"]["available"] is False
    body = last(await tools["computer_batch"]({"actions": [{"action": "ui_tree", "window_id": 101}]}))
    assert body["code"] == "UNSUPPORTED_CAPABILITY"
    assert "ui_tree" not in sdk.system_guidance()


async def test_capabilities_and_guidance_mention_elements(desktop, monkeypatch):
    caps = payload(await desktop.tools["request_access"]({"apps": [], "reason": "t"}))["capabilities"]
    assert caps["ui_automation"] == {"available": True, "reason": None}
    assert sdk._UIA_TOOLS <= set(caps["tools"])
    monkeypatch.setattr(sdk, "_windows_tools_supported", lambda: True)
    guidance = sdk.system_guidance()
    assert "ui_tree" in guidance and "element_id" in guidance


# ----- the real provider's walk, over fake COM nodes ------------------------------------

class _Node(SimpleNamespace):
    pass


def _com_provider(root):
    """A ComProvider whose COM objects are fakes: exercises its breadth-first
    walk and caps without COM. Nodes have .children; a null child is None."""
    provider = object.__new__(uia.ComProvider)

    def first(node, cache=None):
        return node.children[0] if node.children else None

    def nxt(node, cache=None):
        sibs = node.parent.children
        i = sibs.index(node)
        return sibs[i + 1] if i + 1 < len(sibs) else None
    provider.walker = SimpleNamespace(GetFirstChildElementBuildCache=first,
                                      GetNextSiblingElementBuildCache=nxt,
                                      GetFirstChildElement=first)
    provider.auto = SimpleNamespace(ElementFromHandleBuildCache=lambda hwnd, cache: root)
    provider.cache = None
    provider._element = lambda node, cached, depth=1, parent=None: uia.Element(
        (node.n,), "Button", f"n{node.n}", actions=("invoke",), depth=depth, parent=parent)
    return provider


def _build(fanout, depth):
    counter = iter(range(10**6))
    root = _Node(n=next(counter), children=[], parent=None)
    level = [root]
    for _ in range(depth):
        nxt = []
        for node in level:
            for _ in range(fanout):
                child = _Node(n=next(counter), children=[], parent=node)
                node.children.append(child)
                nxt.append(child)
        level = nxt
    return root


def test_walk_is_breadth_first_and_capped():
    root = _build(fanout=3, depth=6)  # 1092 nodes
    els, why = _com_provider(root).tree(1, max_elements=200, max_depth=8, max_children=50,
                                         interactive_only=False, deadline=time.monotonic() + 5)
    assert len(els) == 200 and why == "count"
    assert [e.depth for e in els] == sorted(e.depth for e in els)  # breadth first
    assert els[0].parent is None and els[3].parent == 0


def test_walk_stops_at_depth_children_and_time():
    chain = _build(fanout=1, depth=12)
    els, why = _com_provider(chain).tree(1, max_elements=200, max_depth=8, max_children=50,
                                          interactive_only=False, deadline=time.monotonic() + 5)
    assert len(els) == 8 and why == "depth"
    wide = _build(fanout=120, depth=1)
    els, why = _com_provider(wide).tree(1, max_elements=200, max_depth=8, max_children=50,
                                         interactive_only=False, deadline=time.monotonic() + 5)
    assert len(els) == 50 and why == "children"
    els, why = _com_provider(wide).tree(1, max_elements=200, max_depth=8, max_children=50,
                                         interactive_only=False, deadline=time.monotonic() - 1)
    assert els == [] and why == "time"


def test_unnamed_layout_panes_are_skipped_and_not_counted():
    """Web and Electron apps nest their controls a dozen unnamed panes deep."""
    chain = _build(fanout=1, depth=14)
    provider = _com_provider(chain)

    def element(node, cached, depth=1, parent=None):
        if node.children:  # every node but the leaf is an inert, unnamed wrapper
            return uia.Element((node.n,), "Pane", "", depth=depth, parent=parent)
        return uia.Element((node.n,), "Button", "Send", actions=("invoke",), depth=depth,
                           parent=parent)
    provider._element = element
    els, why = provider.tree(1, max_elements=200, max_depth=8, max_children=50,
                             interactive_only=False, deadline=time.monotonic() + 5)
    assert [(e.name, e.depth, e.parent) for e in els] == [("Send", 1, None)] and why is None
