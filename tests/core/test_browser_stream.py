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
    for sid in ("hub-start", "hub-ack", "hub-stable", "hub-reattach", "hub-death"):
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


async def test_upstream_death_broadcasts_no_page(monkeypatch):
    sid = "hub-death"
    registry.register(sid, "target-x", None, "http://a/")
    fake = FakeUpstream()

    async def opener(uri):
        return fake

    monkeypatch.setattr(browser_stream, "_open_upstream", opener)

    ws = FakeViewerWS()
    hub, viewer = await browser_stream.attach_viewer(sid, ws)
    await _wait_for(lambda: "Page.startScreencast" in fake.methods())

    fake.feed_close()  # upstream dies
    await _wait_for(
        lambda: any(
            f.get("type") == "status" and f.get("state") == "no_page"
            for f in ws.sent
        )
    )
    await browser_stream.detach_viewer(sid, viewer)
