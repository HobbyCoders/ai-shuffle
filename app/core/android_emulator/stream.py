"""The Phone panel's live screen: one capture loop per device, fanned out.

While a device runs and at least one panel watches it, frames come from the
emulator's gRPC ``streamScreenshot`` (a frame whenever the display changes),
falling back to polling ``adb screencap``. The newest frame is JPEG-encoded
on a worker thread at most ``MAX_FPS`` times a second and offered to every
viewer; a slow viewer only ever skips to the newest frame, it never holds
the others back. State changes are queued per viewer and never dropped.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import logging
import time
from typing import Any, Deque, Dict, Optional

from app.core.android_emulator import frames
from app.core.android_emulator.manager import Device, EmulatorManager

logger = logging.getLogger(__name__)

MAX_FPS = 12.0
#: Frames fit in this box (aspect kept); the message reports device pixels.
STREAM_LONG_EDGE = 1280
JPEG_QUALITY = 70
POLL_SECONDS = 0.2


class Viewer:
    def __init__(self) -> None:
        self._control: Deque[Dict[str, Any]] = collections.deque(maxlen=256)
        self._frame: Optional[Dict[str, Any]] = None
        self._event = asyncio.Event()

    def send(self, message: Dict[str, Any]) -> None:
        self._control.append(message)
        self._event.set()

    def offer_frame(self, message: Dict[str, Any]) -> None:
        self._frame = message  # replaces an unsent older frame
        self._event.set()

    async def next(self) -> Dict[str, Any]:
        while True:
            if self._control:
                return self._control.popleft()
            if self._frame is not None:
                frame, self._frame = self._frame, None
                return frame
            self._event.clear()
            await self._event.wait()


def state_message(dev: Device) -> Dict[str, Any]:
    return {"type": "state", "state": dev.state, "message": dev.message,
            "width": dev.width, "height": dev.height}


class StreamHub:
    def __init__(self, manager: EmulatorManager, dev: Device) -> None:
        self.manager = manager
        self.dev = dev
        self.viewers: set = set()
        self.seq = 0
        self.last_frame: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None
        self._warned = False
        dev.listeners.append(self._on_state)

    def attach(self) -> Viewer:
        viewer = Viewer()
        self.viewers.add(viewer)
        self.dev.viewers = len(self.viewers)
        self.dev.touch()
        viewer.send(state_message(self.dev))
        if self.last_frame is not None and self.dev.state == "running":
            viewer.offer_frame(self.last_frame)
        self._kick()
        return viewer

    def detach(self, viewer: Viewer) -> None:
        self.viewers.discard(viewer)
        self.dev.viewers = len(self.viewers)
        self.dev.touch()  # the idle clock starts when the last viewer leaves

    def _on_state(self, dev: Device) -> None:
        if dev.state != "running":
            self.last_frame = None
            self._warned = False
        message = state_message(dev)
        for viewer in list(self.viewers):
            viewer.send(message)
        self._kick()

    def _active(self) -> bool:
        return bool(self.viewers) and self.dev.state == "running"

    def _kick(self) -> None:
        if self._active() and (self._task is None or self._task.done()):
            try:
                self._task = asyncio.get_running_loop().create_task(
                    self._run(), name=f"emulator-stream-{self.dev.id}")
            except RuntimeError:  # no loop (a state change from a thread): next attach starts it
                pass

    async def _run(self) -> None:
        dev = self.dev
        latest: Dict[str, Any] = {}
        ready = asyncio.Event()

        async def produce() -> None:
            if dev.grpc is not None:
                try:
                    async for frame in dev.grpc.stream_screenshots(STREAM_LONG_EDGE, STREAM_LONG_EDGE):
                        if not self._active():
                            return
                        if not frame.width or not frame.height:
                            continue
                        latest["frame"] = (frame.pixels, frame.width, frame.height)
                        ready.set()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Loud once per device run: adb polling is ~2 fps, not 10+.
                    if not self._warned:
                        self._warned = True
                        logger.warning("Emulator %s: gRPC frame stream failed (%s); polling adb instead",
                                       dev.id, exc)
                    else:
                        logger.debug("gRPC frame stream failed again", exc_info=True)
            while self._active():
                try:
                    latest["frame"] = await self.manager.capture(dev, STREAM_LONG_EDGE)
                    ready.set()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.debug("emulator frame capture failed", exc_info=True)
                    await asyncio.sleep(1.0)
                await asyncio.sleep(POLL_SECONDS)

        producer = asyncio.create_task(produce())
        last_sent = 0.0
        try:
            while self._active():
                try:
                    await asyncio.wait_for(ready.wait(), 1.0)
                except asyncio.TimeoutError:
                    if producer.done():
                        break
                    continue
                ready.clear()
                wait = 1.0 / MAX_FPS - (time.monotonic() - last_sent)
                if wait > 0:
                    await asyncio.sleep(wait)
                pixels, width, height = latest["frame"]
                self.manager._note_orientation(dev, width, height)
                size = frames.fit(width, height, STREAM_LONG_EDGE)
                data, _w, _h = await asyncio.to_thread(
                    frames.rgb_to_jpeg, pixels, width, height, size=size, quality=JPEG_QUALITY)
                self.seq += 1
                message = {"type": "frame", "format": "jpeg",
                           "data": base64.b64encode(data).decode("ascii"),
                           "width": dev.width, "height": dev.height, "seq": self.seq}
                self.last_frame = message
                for viewer in list(self.viewers):
                    viewer.offer_frame(message)
                last_sent = time.monotonic()
        finally:
            producer.cancel()
            try:
                await producer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # A device that is still running and watched (the producer ended on
        # its own, e.g. gRPC went away) gets a fresh loop.
        if self._active():
            await asyncio.sleep(1.0)
            self._task = None
            self._kick()


_hubs: Dict[str, StreamHub] = {}


def hub_for(manager: EmulatorManager, dev: Device) -> StreamHub:
    hub = _hubs.get(dev.id)
    if hub is None or hub.dev is not dev:
        hub = _hubs[dev.id] = StreamHub(manager, dev)
    return hub
