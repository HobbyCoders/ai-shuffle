"""The emulator's gRPC control service, spoken without generated stubs.

``emulator -grpc <port>`` serves ``android.emulation.control.EmulatorController``
on loopback (the proto ships as ``emulator/lib/emulator_controller.proto``).
It streams screenshots at the size asked for and injects touches, keys and
text in a few milliseconds, where ``adb shell input`` starts a JVM per event
(300-500 ms). The handful of messages used here are encoded by hand, so no
protoc step or generated code is needed; field numbers follow the proto.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SERVICE = "/android.emulation.control.EmulatorController/"

# ImageFormat.ImgFormat
PNG, RGBA8888, RGB888 = 0, 1, 2
# KeyboardEvent.KeyEventType
KEYDOWN, KEYUP, KEYPRESS = 0, 1, 2

#: Panel/tool key names → the W3C key values the emulator's keyboard maps.
KEY_VALUES: Dict[str, str] = {
    "HOME": "GoHome",
    "BACK": "GoBack",
    "APP_SWITCH": "AppSwitch",
    "ENTER": "Enter",
    "DEL": "Backspace",
    "POWER": "Power",
    "VOLUME_UP": "AudioVolumeUp",
    "VOLUME_DOWN": "AudioVolumeDown",
    "TAB": "Tab",
    "ESCAPE": "Escape",
    "MENU": "ContextMenu",
}


# ── protobuf wire format ─────────────────────────────────────────────────────
def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1  # negative int32/int64 encode as ten bytes
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _int(field: int, value: int) -> bytes:
    return b"" if not value else _varint(field << 3) + _varint(value)


def _len(field: int, payload: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _str(field: int, value: str) -> bytes:
    return b"" if not value else _len(field, value.encode("utf-8"))


def decode(buf: bytes) -> Dict[int, List[object]]:
    """``{field: [values]}``; varints as ints, length-delimited as bytes."""
    fields: Dict[int, List[object]] = {}
    i, n = 0, len(buf)

    def varint() -> int:
        nonlocal i
        shift = result = 0
        while True:
            if i >= n:
                raise ValueError("truncated varint")
            byte = buf[i]
            i += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    while i < n:
        key = varint()
        number, wire = key >> 3, key & 7
        if wire == 0:
            value: object = varint()
        elif wire == 2:
            size = varint()
            value = bytes(buf[i:i + size])
            i += size
        elif wire == 1:
            value = bytes(buf[i:i + 8])
            i += 8
        elif wire == 5:
            value = bytes(buf[i:i + 4])
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _first(fields: Dict[int, List[object]], number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


# ── messages ─────────────────────────────────────────────────────────────────
def image_format(fmt: int = RGB888, width: int = 0, height: int = 0, display: int = 0) -> bytes:
    """ImageFormat{format=1, width=3, height=4, display=5}; 0 means native size."""
    return _int(1, fmt) + _int(3, width) + _int(4, height) + _int(5, display)


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    pixels: bytes
    seq: int
    fmt: int
    #: SkinRotation of the display when captured: 0 portrait, 1 landscape, 2/3 reversed.
    rotation: int = 0


def parse_image(payload: bytes) -> Frame:
    """Image{format=1 (ImageFormat), width=2, height=3, image=4, seq=5}."""
    fields = decode(payload)
    fmt_fields = decode(_first(fields, 1, b"") or b"")
    width = _first(fmt_fields, 3, 0) or _first(fields, 2, 0)
    height = _first(fmt_fields, 4, 0) or _first(fields, 3, 0)
    rotation_msg = decode(_first(fmt_fields, 2, b"") or b"")
    return Frame(width=int(width), height=int(height), pixels=_first(fields, 4, b"") or b"",
                 seq=int(_first(fields, 5, 0)), fmt=int(_first(fmt_fields, 1, 0)),
                 rotation=int(_first(rotation_msg, 1, 0)))


def touch_event(touches: List[Tuple[int, int, int, int]], display: int = 0) -> bytes:
    """TouchEvent{touches=1 [Touch{x=1, y=2, identifier=3, pressure=4}], display=2}."""
    body = b"".join(
        _len(1, _int(1, x) + _int(2, y) + _int(3, ident) + _int(4, pressure))
        for x, y, ident, pressure in touches
    )
    return body + _int(2, display)


def keyboard_event(*, key: str = "", text: str = "", event_type: int = KEYPRESS) -> bytes:
    """KeyboardEvent{codeType=1, eventType=2, keyCode=3, key=4, text=5}."""
    return _int(2, event_type) + _str(4, key) + _str(5, text)


# ── client ───────────────────────────────────────────────────────────────────
class GrpcControl:
    """A grpc.aio channel to one emulator. Every call raises on failure.

    The emulator runs with ``-grpc-use-token``: it then listens on loopback
    only (without it the port is open on every interface) and every call must
    carry its token as ``authorization: Bearer <token>``. The token the server
    expects is the ``grpc.token`` it writes to its discovery file (see
    ``layout.discovery_file``), falling back to the console token.

    The server binds IPv4 loopback only, so the target is always the literal
    ``127.0.0.1``: ``localhost`` can resolve to ``::1`` and be refused.
    """

    def __init__(self, port: int, token: str = "", host: str = "127.0.0.1") -> None:
        self.port, self.token = port, token
        self.target = f"{host}:{port}"
        self._metadata = (("authorization", f"Bearer {token}"),) if token else None
        self._channel = None

    def _chan(self):
        if self._channel is None:
            import grpc

            self._channel = grpc.aio.insecure_channel(self.target, options=[
                ("grpc.max_receive_message_length", 64 * 1024 * 1024),
                # Loopback only: an http(s)_proxy in the app's environment
                # must never carry the emulator's control channel.
                ("grpc.enable_http_proxy", 0),
            ])
        return self._channel

    async def _unary(self, method: str, request: bytes, timeout: float) -> bytes:
        call = self._chan().unary_unary(SERVICE + method)
        return await call(request, timeout=timeout, metadata=self._metadata)

    async def status(self, timeout: float = 3.0) -> Dict[int, List[object]]:
        """getStatus(Empty) → EmulatorStatus; proves the service answers."""
        return decode(await self._unary("getStatus", b"", timeout))

    async def screenshot(self, width: int = 0, height: int = 0, fmt: int = RGB888,
                         timeout: float = 10.0) -> Frame:
        return parse_image(await self._unary("getScreenshot", image_format(fmt, width, height), timeout))

    async def stream_screenshots(self, width: int = 0, height: int = 0,
                                 fmt: int = RGB888) -> AsyncIterator[Frame]:
        """A frame each time the display changes (the first one at once)."""
        call = self._chan().unary_stream(SERVICE + "streamScreenshot")(
            image_format(fmt, width, height), metadata=self._metadata)
        try:
            async for payload in call:
                yield parse_image(payload)
        finally:
            call.cancel()

    async def touch(self, x: int, y: int, pressure: int, ident: int = 0, timeout: float = 3.0) -> None:
        await self._unary("sendTouch", touch_event([(x, y, ident, pressure)]), timeout)

    async def key(self, key: str, timeout: float = 3.0) -> None:
        await self._unary("sendKey", keyboard_event(key=key), timeout)

    async def text(self, text: str, timeout: float = 5.0) -> None:
        await self._unary("sendKey", keyboard_event(text=text), timeout)

    async def close(self) -> None:
        channel, self._channel = self._channel, None
        if channel is not None:
            try:
                await channel.close()
            except Exception:  # noqa: BLE001
                logger.debug("gRPC channel close failed", exc_info=True)
