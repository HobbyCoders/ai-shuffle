"""Screen pixels to JPEG, and adb's raw screencap format. CPU work: call in a thread."""

from __future__ import annotations

import io
import struct
from typing import Tuple


def fit(width: int, height: int, long_edge: int) -> Tuple[int, int]:
    """``(w, h)`` scaled so the longer side is at most ``long_edge``."""
    longest = max(width, height)
    if longest <= long_edge or longest <= 0:
        return width, height
    factor = long_edge / longest
    return max(1, round(width * factor)), max(1, round(height * factor))


def rgb_to_jpeg(pixels: bytes, width: int, height: int, *, size: Tuple[int, int] = None,
                quality: int = 75) -> Tuple[bytes, int, int]:
    """RGB888 pixels → JPEG bytes, resized to ``size`` when given."""
    from PIL import Image

    image = Image.frombuffer("RGB", (width, height), pixels, "raw", "RGB", 0, 1)
    if size and size != (width, height):
        image = image.resize(size, Image.BILINEAR)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, optimize=False)
    return out.getvalue(), image.width, image.height


def raw_screencap_to_rgb(data: bytes) -> Tuple[bytes, int, int]:
    """``screencap`` (no -p) output → RGB888. The header is w, h, format[, colorspace]."""
    from PIL import Image

    if len(data) < 12:
        raise ValueError("screencap returned no image")
    width, height, _fmt = struct.unpack_from("<III", data, 0)
    size = width * height * 4
    for header in (16, 12):
        if len(data) - header >= size:
            break
    else:
        raise ValueError("screencap output is shorter than its header says")
    image = Image.frombuffer("RGBA", (width, height), data[header:header + size], "raw", "RGBA", 0, 1)
    return image.convert("RGB").tobytes(), width, height
