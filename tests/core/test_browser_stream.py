"""Tests for app.core.browser_stream.

Two groups:
  * translate_input — the PURE client->CDP translation (exact dicts, clamping,
    rejections).
  * BrowserStreamHub — the screencast state machine, driven with an in-process
    fake upstream (patching ``_open_upstream``).
"""

import asyncio
import json

import pytest

from app.core import browser_panel_registry as registry
from app.core import browser_stream


# ===========================================================================
# translate_input
# ===========================================================================
class TestTranslateInput:
    def test_mouse_move(self):
        cmd = browser_stream.translate_input(
            {"type": "input", "kind": "mouse", "event": "move", "x": 10, "y": 20}
        )
        assert cmd == {
            "method": "Input.dispatchMouseEvent",
            "params": {
                "type": "mouseMoved",
                "x": 10.0,
                "y": 20.0,
                "button": "left",
                "clickCount": 0,
                "modifiers": 0,
            },
        }

    def test_mouse_down_up(self):
        down = browser_stream.translate_input(
            {"type": "input", "kind": "mouse", "event": "down", "x": 1, "y": 2,
             "button": "right", "clickCount": 2, "modifiers": 4}
        )
        assert down["params"]["type"] == "mousePressed"
        assert down["params"]["button"] == "right"
        assert down["params"]["clickCount"] == 2
        assert down["params"]["modifiers"] == 4

        up = browser_stream.translate_input(
            {"type": "input", "kind": "mouse", "event": "up", "x": 1, "y": 2}
        )
        assert up["params"]["type"] == "mouseReleased"

    def test_mouse_wheel_carries_deltas(self):
        cmd = browser_stream.translate_input(
            {"type": "input", "kind": "mouse", "event": "wheel", "x": 5, "y": 6,
             "deltaX": 3, "deltaY": -7}
        )
        assert cmd["params"]["type"] == "mouseWheel"
        assert cmd["params"]["deltaX"] == 3.0
        assert cmd["params"]["deltaY"] == -7.0

    def test_mouse_coordinates_clamped_to_bounds(self):
        cmd = browser_stream.translate_input(
            {"type": "input", "kind": "mouse", "event": "move", "x": 9999, "y": -50},
            bounds=(800, 600),
        )
        assert cmd["params"]["x"] == 800.0
        assert cmd["params"]["y"] == 0.0

    def test_key_down_sets_text_and_vk(self):
        cmd = browser_stream.translate_input(
            {"type": "input", "kind": "key", "event": "down", "key": "a",
             "code": "KeyA", "keyCode": 65, "text": "a", "modifiers": 0}
        )
        assert cmd == {
            "method": "Input.dispatchKeyEvent",
            "params": {
                "type": "keyDown",
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "modifiers": 0,
                "text": "a",
            },
        }

    def test_key_up_omits_text(self):
        cmd = browser_stream.translate_input(
            {"type": "input", "kind": "key", "event": "up", "key": "a",
             "code": "KeyA", "keyCode": 65, "text": "a"}
        )
        assert cmd["params"]["type"] == "keyUp"
        assert "text" not in cmd["params"]

    def test_key_enter_special_cased_to_cr(self):
        cmd = browser_stream.translate_input(
            {"type": "input", "kind": "key", "event": "down", "key": "Enter",
             "code": "Enter", "keyCode": 13, "text": ""}
        )
        assert cmd["params"]["text"] == "\r"

    def test_navigate_http_and_https_ok(self):
        for url in ("http://example.com/", "https://example.com/path"):
            cmd = browser_stream.translate_input({"type": "navigate", "url": url})
            assert cmd == {"method": "Page.navigate", "params": {"url": url}}

    def test_navigate_rejects_non_http_scheme(self):
        for url in ("ftp://x/", "file:///etc/passwd", "javascript:alert(1)"):
            with pytest.raises(Exception):
                browser_stream.translate_input({"type": "navigate", "url": url})

    def test_unknown_type_rejected(self):
        with pytest.raises(Exception):
            browser_stream.translate_input({"type": "evil", "method": "Runtime.evaluate"})

    def test_unknown_input_kind_rejected(self):
        with pytest.raises(Exception):
            browser_stream.translate_input({"type": "input", "kind": "touch", "x": 1, "y": 1})

    def test_command_reload_and_stop_map_to_page_commands(self):
        assert browser_stream.translate_input({"type": "command", "command": "reload"}) == {
            "method": "Page.reload",
            "params": {"ignoreCache": False},
        }
        assert browser_stream.translate_input({"type": "command", "command": "stop"}) == {
            "method": "Page.stopLoading"
        }

    def test_command_back_forward_probe_history_first(self):
        for command, offset in (("back", -1), ("forward", 1)):
            cmd = browser_stream.translate_input({"type": "command", "command": command})
            assert cmd["method"] == "Page.getNavigationHistory"
            assert cmd["__history_offset__"] == offset

    def test_command_rejects_unknown_command(self):
        with pytest.raises(Exception):
            browser_stream.translate_input({"type": "command", "command": "minimize"})


# ===========================================================================
# Page-state tracking (toolbar url + loading) and history jumps
# ===========================================================================
def _page_event(method, **params):
    return {"method": method, "params": params}


class TestPageEventTracking:
    def test_main_frame_navigation_updates_url_and_clears_loading(self):
        hub = browser_stream.BrowserStreamHub("x")
        hub.page_loading = True
        assert hub._handle_page_event(
            _page_event(
                "Page.frameNavigated",
                frame={"frameId": "f1", "url": "https://a/", "parentId": None},
            )
        )
        assert hub.page_url == "https://a/"
        assert not hub.page_loading
        # Unchanged URL is a no-op.
        assert not hub._handle_page_event(
            _page_event("Page.frameNavigated", frame={"frameId": "f1", "url": "https://a/"})
        )

    def test_subframe_navigation_does_not_change_url(self):
        hub = browser_stream.BrowserStreamHub("x")
        hub.page_url = "https://a/"
        assert not hub._handle_page_event(
            _page_event(
                "Page.frameNavigated",
                frame={"frameId": "sub", "url": "https://iframe.example/", "parentId": "f1"},
            )
        )
        assert hub.page_url == "https://a/"

    def test_started_and_stopped_loading_flip_the_flag(self):
        hub = browser_stream.BrowserStreamHub("x")
        assert hub._handle_page_event(_page_event("Page.frameStartedLoading", frameId="f1"))
        assert hub.page_loading
        assert hub._handle_page_event(_page_event("Page.frameStoppedLoading", frameId="f1"))
        assert not hub.page_loading

    def test_navigated_within_document_updates_url_only(self):
        hub = browser_stream.BrowserStreamHub("x")
        hub.page_url = "https://a/"
        assert hub._handle_page_event(
            _page_event("Page.navigatedWithinDocument", frameId="f1", url="https://a/#sec2")
        )
        assert hub.page_url == "https://a/#sec2"
        assert not hub.page_loading


async def _hub_with_fake_upstream(monkeypatch, sid="hub-cmd"):
    # Attach one viewer over a FakeUpstream; returns (hub, viewer, fake).
    registry.register(sid, "t-cmd", None, "http://start/")
    fake = FakeUpstream()

    async def opener(uri):
        return fake

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)
    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: any(m["method"] == "Page.startScreencast" for m in fake.sent))
    return hub, viewer, fake


class TestToolbarCommands:
    async def test_reload_goes_straight_upstream(self, monkeypatch):
        hub, viewer, fake = await _hub_with_fake_upstream(monkeypatch)
        await hub.dispatch_input(
            browser_stream.translate_input({"type": "command", "command": "reload"})
        )
        reloads = [m for m in fake.sent if m["method"] == "Page.reload"]
        assert reloads and reloads[0]["params"] == {"ignoreCache": False}
        await browser_stream.detach_viewer("hub-cmd", viewer)

    async def test_back_walks_history(self, monkeypatch):
        hub, viewer, fake = await _hub_with_fake_upstream(monkeypatch)
        # Seed the attachment-time probe reply: index 1 of 3 entries.
        probe_id = next(
            m["id"] for m in fake.sent if m["method"] == "Page.getNavigationHistory"
        )
        await hub._complete_history_jump(
            probe_id,
            {
                "currentIndex": 1,
                "entries": [
                    {"id": 11, "url": "http://start/"},
                    {"id": 22, "url": "https://ex/one"},
                    {"id": 33, "url": "https://ex/two"},
                ],
            },
        )
        await hub.dispatch_input(
            browser_stream.translate_input({"type": "command", "command": "back"})
        )

        # The back command probed again; answer it to complete the jump.
        probes = [m for m in fake.sent if m["method"] == "Page.getNavigationHistory"]
        assert len(probes) >= 2
        await hub._complete_history_jump(
            probes[-1]["id"],
            {
                "currentIndex": 1,
                "entries": [
                    {"id": 11, "url": "http://start/"},
                    {"id": 22, "url": "https://ex/one"},
                    {"id": 33, "url": "https://ex/two"},
                ],
            },
        )
        jumps = [m for m in fake.sent if m["method"] == "Page.navigateToHistoryEntry"]
        assert jumps and jumps[-1]["params"] == {"entryId": 11}
        await browser_stream.detach_viewer("hub-cmd", viewer)

    async def test_forward_at_end_of_history_is_a_noop(self, monkeypatch):
        hub, viewer, fake = await _hub_with_fake_upstream(monkeypatch)
        await hub.dispatch_input(
            browser_stream.translate_input({"type": "command", "command": "forward"})
        )
        probes = [m for m in fake.sent if m["method"] == "Page.getNavigationHistory"]
        await hub._complete_history_jump(
            probes[-1]["id"], {"currentIndex": 0, "entries": [{"id": 11, "url": "u"}]}
        )
        assert not any(m["method"] == "Page.navigateToHistoryEntry" for m in fake.sent)
        await browser_stream.detach_viewer("hub-cmd", viewer)

    async def test_page_events_broadcast_to_viewers(self, monkeypatch):
        hub, viewer, fake = await _hub_with_fake_upstream(monkeypatch)
        fake._incoming.put_nowait(json.dumps(_page_event(
            "Page.frameNavigated",
            frame={"frameId": "f1", "url": "https://next/", "parentId": None},
        )))
        # The per-viewer sender task may drain the queue before we look, so
        # assert on what landed on the socket.
        await _wait_for(lambda: any(m.get("type") == "page" for m in viewer.websocket.sent))
        msg = next(m for m in viewer.websocket.sent if m.get("type") == "page")
        assert msg["url"] == "https://next/"
        assert msg["loading"] is False
        await browser_stream.detach_viewer("hub-cmd", viewer)


# ===========================================================================
# Inspect (point-and-prompt hit-test)
# ===========================================================================
class TestInspect:
    def test_parse_inspect_valid(self):
        inspect = browser_stream.parse_inspect(
            {"type": "inspect", "x": 12.5, "y": 40, "mode": "select"}
        )
        assert (inspect.x, inspect.y, inspect.mode) == (12.5, 40.0, "select")

    def test_parse_inspect_rejects_bad_mode_and_coords(self):
        with pytest.raises(Exception):
            browser_stream.parse_inspect({"type": "inspect", "x": 1, "y": 2, "mode": "drag"})
        with pytest.raises(Exception):
            browser_stream.parse_inspect({"type": "inspect", "x": 1, "mode": "hover"})
        with pytest.raises(Exception):
            browser_stream.parse_inspect(
                {"type": "inspect", "x": float("nan"), "y": 2, "mode": "hover"}
            )
        with pytest.raises(Exception):
            browser_stream.parse_inspect(
                {"type": "inspect", "x": float("inf"), "y": 2, "mode": "hover"}
            )

    def test_build_expression_substitutes_and_clamps(self):
        expr = browser_stream.build_inspect_expression(9999, -5, detail=True, bounds=(800, 600))
        assert "document.elementFromPoint(800.0, 0.0)" in expr
        assert "__X__" not in expr and "__Y__" not in expr and "__DETAIL__" not in expr
        assert "if (true)" in expr

        hover = browser_stream.build_inspect_expression(10, 20, detail=False)
        assert "document.elementFromPoint(10.0, 20.0)" in hover
        assert "if (false)" in hover

    def test_sanitize_inspect_result(self):
        good = {"selector": "#a", "tag": "div"}
        assert browser_stream.sanitize_inspect_result(good) == good
        assert browser_stream.sanitize_inspect_result(None) is None
        assert browser_stream.sanitize_inspect_result("nope") is None
        assert browser_stream.sanitize_inspect_result(["a"]) is None
        huge = {"html": "x" * 40_000}
        assert browser_stream.sanitize_inspect_result(huge) is None

    async def test_select_round_trip_answers_requesting_viewer(self, monkeypatch):
        hub, viewer, fake = await _hub_with_fake_upstream(monkeypatch)
        inspect = browser_stream.parse_inspect(
            {"type": "inspect", "x": 10, "y": 20, "mode": "select"}
        )
        await hub.dispatch_inspect(viewer, inspect)

        ev = next(m for m in fake.sent if m["method"] == "Runtime.evaluate")
        assert ev["params"]["returnByValue"] is True
        assert "document.elementFromPoint(10.0, 20.0)" in ev["params"]["expression"]

        descriptor = {"selector": "#cta", "tag": "button", "classes": ["btn"]}
        fake._incoming.put_nowait(json.dumps({
            "id": ev["id"],
            "result": {"result": {"type": "object", "value": descriptor}},
        }))
        await _wait_for(
            lambda: any(m.get("type") == "inspect-hit" for m in viewer.websocket.sent)
        )
        hit = next(m for m in viewer.websocket.sent if m.get("type") == "inspect-hit")
        assert hit["mode"] == "select"
        assert hit["element"] == descriptor
        await browser_stream.detach_viewer("hub-cmd", viewer)

    async def test_miss_returns_null_element(self, monkeypatch):
        hub, viewer, fake = await _hub_with_fake_upstream(monkeypatch)
        inspect = browser_stream.parse_inspect(
            {"type": "inspect", "x": 1, "y": 1, "mode": "hover"}
        )
        await hub.dispatch_inspect(viewer, inspect)
        ev = next(m for m in fake.sent if m["method"] == "Runtime.evaluate")
        fake._incoming.put_nowait(json.dumps({
            "id": ev["id"],
            "result": {"result": {"type": "object", "subtype": "null", "value": None}},
        }))
        await _wait_for(
            lambda: any(m.get("type") == "inspect-hit" for m in viewer.websocket.sent)
        )
        hit = next(m for m in viewer.websocket.sent if m.get("type") == "inspect-hit")
        assert hit["mode"] == "hover"
        assert hit["element"] is None
        await browser_stream.detach_viewer("hub-cmd", viewer)

    async def test_inspect_without_upstream_answers_immediately(self):
        hub = browser_stream.BrowserStreamHub("x")
        viewer = browser_stream._Viewer(FakeViewerWS())
        hub.viewers.add(viewer)
        inspect = browser_stream.parse_inspect(
            {"type": "inspect", "x": 5, "y": 5, "mode": "hover"}
        )
        await hub.dispatch_inspect(viewer, inspect)
        assert viewer.websocket.sent == [
            {"type": "inspect-hit", "mode": "hover", "element": None}
        ]


# ===========================================================================
# Hub state machine
# ===========================================================================
_CLOSE = object()


class FakeUpstream:
    """In-process stand-in for the CDP WebSocket connection."""

    def __init__(self, uri=""):
        self.uri = uri
        self.sent = []  # list of parsed JSON commands we sent upstream
        self.closed = False
        self._incoming: asyncio.Queue = asyncio.Queue()

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        item = await self._incoming.get()
        if item is _CLOSE:
            raise ConnectionError("upstream closed")
        return item

    async def close(self):
        self.closed = True

    def feed_frame(self, data, metadata, session_id="cast-1"):
        self._incoming.put_nowait(json.dumps({
            "method": "Page.screencastFrame",
            "params": {"sessionId": session_id, "data": data, "metadata": metadata},
        }))

    def feed_close(self):
        self._incoming.put_nowait(_CLOSE)

    def methods(self):
        return [m["method"] for m in self.sent]


class FakeViewerWS:
    """Stands in for a FastAPI WebSocket; records text frames as parsed JSON.

    When ``block_frames`` is set, ``frame`` sends await a gate (simulating a
    slow viewer) while status/ping messages pass through.
    """

    def __init__(self, block_frames=False):
        self.sent = []
        self._gate = asyncio.Event()
        if not block_frames:
            self._gate.set()

    async def send_text(self, text):
        obj = json.loads(text)
        if obj.get("type") == "frame":
            await self._gate.wait()
        self.sent.append(obj)

    def open_gate(self):
        self._gate.set()


async def _wait_for(cond, timeout=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")


@pytest.fixture(autouse=True)
def _reset_hubs():
    for sid in (
        "hub-start", "hub-ack", "hub-stable", "hub-reattach", "hub-death",
        "hub-cmd", "hub-probe", "hub-retry",
    ):
        registry.clear(sid)
    browser_stream._hubs.clear()
    yield
    browser_stream._hubs.clear()


def test_fan_out_latest_frame_wins():
    """Drop-oldest: a maxsize-1 queue keeps only the newest frame."""
    hub = browser_stream.BrowserStreamHub("x")
    viewer = browser_stream._Viewer(FakeViewerWS())
    hub.viewers.add(viewer)

    hub._fan_out_frame("frame-1", {"deviceWidth": 1, "deviceHeight": 1})
    hub._fan_out_frame("frame-2", {"deviceWidth": 1, "deviceHeight": 1})

    assert viewer.queue.qsize() == 1
    msg = viewer.queue.get_nowait()
    assert msg["type"] == "frame"
    assert msg["data"] == "frame-2"


async def test_hub_starts_screencast_and_stops_on_last_viewer(monkeypatch):
    sid = "hub-start"
    registry.register(sid, "target-abc", None, "http://ex/")
    fake = FakeUpstream()

    async def opener(uri):
        fake.uri = uri
        return fake

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)

    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)

    await _wait_for(lambda: "Page.startScreencast" in fake.methods())
    assert "target-abc" in fake.uri
    assert "Page.enable" in fake.methods()
    sc = next(m for m in fake.sent if m["method"] == "Page.startScreencast")
    assert sc["params"] == {
        "format": "jpeg", "quality": 60, "maxWidth": 1440,
        "maxHeight": 1440, "everyNthFrame": 1,
    }

    fake.feed_frame("AAAA", {"deviceWidth": 800, "deviceHeight": 600}, session_id="s9")
    await _wait_for(lambda: any(f.get("type") == "frame" for f in ws.sent))
    frame = next(f for f in ws.sent if f.get("type") == "frame")
    assert frame["data"] == "AAAA"
    assert frame["metadata"]["deviceWidth"] == 800

    # Ack carries the frame's sessionId.
    ack = next(m for m in fake.sent if m["method"] == "Page.screencastFrameAck")
    assert ack["params"]["sessionId"] == "s9"

    # Bounds now reflect the last frame.
    assert hub.last_bounds() == (800, 600)

    await browser_stream.detach_viewer(sid, viewer)
    assert fake.closed
    assert "Page.stopScreencast" in fake.methods()
    assert sid not in browser_stream._hubs


async def test_ack_not_gated_on_slow_viewer(monkeypatch):
    """The ack must be sent even when the viewer cannot receive the frame."""
    sid = "hub-ack"
    registry.register(sid, "t", None, None)
    fake = FakeUpstream()

    async def opener(uri):
        return fake

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)

    ws = FakeViewerWS(block_frames=True)  # frames block; status passes
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: "Page.startScreencast" in fake.methods())

    fake.feed_frame("D", {"deviceWidth": 10, "deviceHeight": 10})
    await _wait_for(lambda: "Page.screencastFrameAck" in fake.methods())

    # Ack sent, but the (blocked) viewer has not received the frame yet.
    assert not any(f.get("type") == "frame" for f in ws.sent)

    # Release the gate and let the sender drain + settle before teardown.
    ws.open_gate()
    await _wait_for(lambda: any(f.get("type") == "frame" for f in ws.sent))
    await asyncio.sleep(0.02)
    await browser_stream.detach_viewer(sid, viewer)


async def test_reattaches_on_registry_change(monkeypatch):
    sid = "hub-reattach"
    registry.register(sid, "target-1", None, "http://a/")
    fakes = []

    async def opener(uri):
        f = FakeUpstream(uri)
        fakes.append(f)
        return f

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)

    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: fakes and "Page.startScreencast" in fakes[0].methods())

    # Point the registry at a new target -> hub tears down + re-attaches.
    registry.register(sid, "target-2", None, "http://b/")
    await _wait_for(lambda: len(fakes) >= 2)
    assert "target-2" in fakes[1].uri
    await _wait_for(lambda: fakes[0].closed)
    assert "Page.stopScreencast" in fakes[0].methods()

    await browser_stream.detach_viewer(sid, viewer)


async def test_same_target_updates_do_not_restart_live_screencast(monkeypatch):
    """Incoming browser events may refresh URL metadata many times; the live
    canvas must keep the same upstream connection throughout."""
    sid = "hub-stable"
    registry.register(sid, "target-1", "tab-1", "http://a/")
    fakes = []

    async def opener(uri):
        fake = FakeUpstream(uri)
        fakes.append(fake)
        return fake

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)
    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: fakes and "Page.startScreencast" in fakes[0].methods())
    fakes[0].feed_frame("A", {"deviceWidth": 10, "deviceHeight": 10})
    await _wait_for(lambda: any(m.get("state") == "live" for m in ws.sent))

    # Exact repeats are ignored; URL/tab metadata changes are broadcast without
    # replacing the CDP attachment.
    registry.register(sid, "target-1", "tab-1", "http://a/")
    registry.register(sid, "target-1", "tab-1", "http://b/")
    await _wait_for(
        lambda: any(m.get("state") == "live" and m.get("url") == "http://b/" for m in ws.sent)
    )
    await asyncio.sleep(0.02)
    assert len(fakes) == 1
    assert not fakes[0].closed

    await browser_stream.detach_viewer(sid, viewer)


async def test_upstream_death_exhausts_retries_then_no_page(monkeypatch):
    """When the target stays registered but every re-attach fails, the hub
    spends its retry budget (broadcasting `connecting`, not `no_page`) and only
    then falls back to no_page + wait-for-change."""
    sid = "hub-death"
    registry.register(sid, "target-x", None, "http://a/")
    fake = FakeUpstream()
    opens = 0

    async def opener(uri):
        nonlocal opens
        opens += 1
        if opens == 1:
            return fake
        raise ConnectionError("cdp gone")

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)
    monkeypatch.setattr(browser_stream, "_REATTACH_DELAYS", (0.01,))

    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: "Page.startScreencast" in fake.methods())

    fake.feed_close()  # upstream dies; retry budget: one failed re-open
    await _wait_for(
        lambda: any(
            f.get("type") == "status" and f.get("state") == "no_page"
            for f in ws.sent
        )
    )
    assert opens >= 2  # the retry actually happened before giving up
    await browser_stream.detach_viewer(sid, viewer)


async def test_upstream_death_reattaches_without_registry_change(monkeypatch):
    """An upstream hiccup while the target is still registered self-heals: the
    hub re-attaches on its own instead of waiting for the next browser tool to
    re-report (which could be a whole streaming turn away)."""
    sid = "hub-retry"
    registry.register(sid, "target-r", None, "http://a/")
    fakes = []

    async def opener(uri):
        f = FakeUpstream(uri)
        fakes.append(f)
        return f

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)
    monkeypatch.setattr(browser_stream, "_REATTACH_DELAYS", (0.01, 0.01, 0.01))

    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: fakes and "Page.startScreencast" in fakes[0].methods())

    fakes[0].feed_close()  # transient upstream death, registry unchanged
    await _wait_for(lambda: len(fakes) >= 2)
    await _wait_for(lambda: "Page.startScreencast" in fakes[1].methods())

    # The gap was reported as connecting, never as no_page.
    assert not any(
        f.get("type") == "status" and f.get("state") == "no_page" for f in ws.sent
    )

    # The re-attached target answers the probe -> live again, no frame needed.
    probe = next(
        m for m in fakes[1].sent if m["method"] == "Page.getNavigationHistory"
    )
    fakes[1]._incoming.put_nowait(json.dumps({
        "id": probe["id"],
        "result": {"currentIndex": 0, "entries": [{"id": 1, "url": "http://a/"}]},
    }))
    await _wait_for(
        lambda: any(
            f.get("type") == "status" and f.get("state") == "live" for f in ws.sent
        )
    )
    await browser_stream.detach_viewer(sid, viewer)


async def test_probe_reply_declares_live_without_any_frame(monkeypatch):
    """Chrome only paints screencast frames for the foreground tab, so `live`
    must not be gated on a first frame: the attach-time history probe reply is
    the health signal."""
    sid = "hub-probe"
    registry.register(sid, "target-p", None, "http://p/")
    fake = FakeUpstream()

    async def opener(uri):
        return fake

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)

    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: "Page.getNavigationHistory" in fake.methods())
    probe = next(m for m in fake.sent if m["method"] == "Page.getNavigationHistory")

    fake._incoming.put_nowait(json.dumps({
        "id": probe["id"],
        "result": {
            "currentIndex": 0,
            "entries": [{"id": 1, "url": "http://p/actual"}],
        },
    }))
    await _wait_for(
        lambda: any(
            f.get("type") == "status" and f.get("state") == "live" for f in ws.sent
        )
    )
    # No frame was ever fed — live came from the probe round-trip alone.
    assert not any(f.get("type") == "frame" for f in ws.sent)
    await browser_stream.detach_viewer(sid, viewer)
