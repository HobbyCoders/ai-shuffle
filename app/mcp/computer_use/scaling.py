"""Display enumeration, DPI handling, and virtual<->physical coordinate scaling.

The model is shown screenshots downscaled to a virtual resolution (~1280x800)
because vision models click far more accurately on smaller images. Every action
coordinate the model produces is therefore in *virtual* pixels of the currently
selected display, and must be scaled back up to the real (physical) pixel
position before we drive the mouse. This module owns that mapping.

Key facts that make the math correct:

* ``mss`` reports each monitor's geometry in **physical** pixels on the virtual
  desktop, where the origin is the primary monitor's top-left and secondary
  monitors can have **negative** ``left``/``top``.
* On Windows we make the process **per-monitor DPI aware** once at startup, so
  ``mss`` (capture) and ``pynput`` (input) share that same physical-pixel space.
  Without this, clicks land in the wrong place on any non-100%-scaled monitor.
  Once DPI-aware, DPI never enters the math again.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Default virtual screenshot target (width, height). Aspect ratio is preserved,
# so the actual image is letterboxed within this box, never stretched.
DEFAULT_VIRTUAL_SIZE = (1280, 800)

_dpi_lock = threading.Lock()
_dpi_done = False


def _ensure_dpi_awareness() -> None:
    """Make the process per-monitor DPI aware (Windows only, idempotent).

    Must be called before the first capture/input. Calling the underlying API
    twice raises, so this is guarded. No-op on macOS/Linux.
    """
    global _dpi_done
    if sys.platform != "win32":
        return
    # Tk and captures live on different threads. A process may already have
    # initialized DPI handling, so set each calling thread to physical pixels.
    try:
        import ctypes
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    with _dpi_lock:
        if _dpi_done:
            return
        try:
            import ctypes

            # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+).
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
            except Exception:
                # Fall back to system-DPI awareness on older Windows.
                ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
            logger.debug("Computer-use: set per-monitor DPI awareness")
        except Exception:
            logger.debug("Computer-use: could not set DPI awareness", exc_info=True)
        finally:
            # Mark done even on failure: retrying won't help and may raise.
            _dpi_done = True


def computer_use_available() -> tuple[bool, str]:
    """Return ``(available, reason)`` for whether host control is possible here.

    Disabled inside Docker/headless, when deps are missing, or when no usable
    display is found. ``reason`` is a short machine-friendly token.
    """
    # 1. Never works from inside the container (no host display).
    try:
        from app.core.platform import detect_deployment_mode, DeploymentMode

        if detect_deployment_mode() == DeploymentMode.DOCKER:
            return (False, "docker")
    except Exception:
        # If platform detection fails, fall through to the probes below.
        pass

    # 2. Explicit operator kill-switch.
    if os.environ.get("COMPUTER_USE_DISABLED", "").lower() in ("1", "true", "yes"):
        return (False, "env-disabled")

    _ensure_dpi_awareness()
    # 3. Required libraries present?
    try:
        import mss  # noqa: F401
        import pynput  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:
        return (False, "missing-deps")

    # 4. Per-OS display sanity check.
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return (False, "no-display")

    # 5. Ground-truth probe: can we actually enumerate monitors?
    try:
        import mss as _mss

        with _mss.mss() as sct:
            # monitors[0] is the union box; at least one real monitor => len >= 2.
            if len(sct.monitors) < 2:
                return (False, "no-monitors")
    except Exception:
        logger.debug("Computer-use: monitor probe failed", exc_info=True)
        return (False, "screenshot-probe-failed")

    return (True, "ok")


def enumerate_displays() -> "list[DisplayInfo]":
    """Enumerate physical monitors (mss indices 1..N), in physical pixels.

    Module-level so the status endpoint can read display geometry without
    constructing a full DisplayManager.
    """
    import mss as _mss

    _ensure_dpi_awareness()
    out: "list[DisplayInfo]" = []
    with _mss.mss() as sct:
        mons = sct.monitors  # [0]=union, [1..]=physical
        for i, m in enumerate(mons[1:], start=1):
            primary = m["left"] == 0 and m["top"] == 0
            out.append(DisplayInfo(
                index=i,
                left=int(m["left"]),
                top=int(m["top"]),
                width=int(m["width"]),
                height=int(m["height"]),
                primary=primary,
            ))
    return out


def foreground_display(displays: list["DisplayInfo"]) -> "DisplayInfo | None":
    """Keyboard input goes to the focused window, not the screenshot selection."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        rect = wintypes.RECT()
        if not user32.GetWindowRect(user32.GetForegroundWindow(), ctypes.byref(rect)):
            return None
        def overlap(d):
            return (max(0, min(rect.right, d.left + d.width) - max(rect.left, d.left)) *
                    max(0, min(rect.bottom, d.top + d.height) - max(rect.top, d.top)))
        best = max(displays, key=overlap, default=None)
        return best if best and overlap(best) > 0 else None
    except Exception:
        logger.debug("Could not determine the keyboard's display", exc_info=True)
        return None


@dataclass
class DisplayInfo:
    """Geometry of one physical monitor, in virtual-desktop physical pixels."""

    index: int          # mss monitor index (1-based)
    left: int
    top: int
    width: int
    height: int
    primary: bool

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "primary": self.primary,
        }


class DisplayManager:
    """Owns the selected display and the virtual<->physical coordinate mapping.

    One instance per computer-use server (closure-captured by the tools). Methods
    are synchronous/blocking — callers wrap them in ``asyncio.to_thread``.
    """

    def __init__(
        self,
        virtual_size: tuple[int, int] = DEFAULT_VIRTUAL_SIZE,
        target_display: Optional[int] = None,
    ) -> None:
        self.virtual_w, self.virtual_h = virtual_size
        if self.virtual_w <= 0 or self.virtual_h <= 0:
            raise ValueError("virtual screenshot dimensions must be positive")
        # Per-display scale map, filled on each screenshot, consumed by actions.
        # index -> {"scale","pw","ph","left","top","vw","vh"}
        self._map: dict[int, dict] = {}
        _ensure_dpi_awareness()
        displays = self.list_displays()
        if target_display is not None and any(d.index == target_display for d in displays):
            self.selected_index = target_display
        else:
            self.selected_index = self._default_index(displays)

    # ----- enumeration -------------------------------------------------------

    @staticmethod
    def _default_index(displays: list[DisplayInfo]) -> int:
        for d in displays:
            if d.primary:
                return d.index
        return displays[0].index if displays else 1

    def list_displays(self) -> list[DisplayInfo]:
        """Enumerate physical monitors (mss indices 1..N)."""
        return enumerate_displays()

    def select_display(self, index: int) -> DisplayInfo:
        """Set the active display. Raises ValueError if it doesn't exist."""
        displays = self.list_displays()
        match = next((d for d in displays if d.index == index), None)
        if match is None:
            valid = ", ".join(str(d.index) for d in displays)
            raise ValueError(f"display {index} not found; available: {valid}")
        self.selected_index = index
        # Geometry may have changed; force a fresh screenshot before acting.
        self._map.pop(index, None)
        return match

    def _selected_geometry(self) -> DisplayInfo:
        displays = self.list_displays()
        match = next((d for d in displays if d.index == self.selected_index), None)
        if match is None:
            # Selection became invalid (monitor unplugged) — fall back to default.
            self.selected_index = self._default_index(displays)
            match = next((d for d in displays if d.index == self.selected_index), None)
        if match is None:
            raise RuntimeError("no displays available")
        return match

    # ----- capture -----------------------------------------------------------

    def capture(self) -> tuple[bytes, dict]:
        """Capture the selected display, downscaled to the virtual size.

        Returns ``(png_bytes, info)`` where info has the virtual + physical size
        and the selected display index. Records the scale map so subsequent
        actions on this display map correctly.
        """
        import mss as _mss
        from PIL import Image

        geo = self._selected_geometry()
        region = {"left": geo.left, "top": geo.top, "width": geo.width, "height": geo.height}
        with _mss.mss() as sct:
            raw = sct.grab(region)
        pw, ph = raw.width, raw.height

        # Only ever downscale; never upscale a small display.
        scale = min(self.virtual_w / pw, self.virtual_h / ph, 1.0)
        vw, vh = max(1, round(pw * scale)), max(1, round(ph * scale))

        img = Image.frombytes("RGB", (pw, ph), raw.rgb)
        if (vw, vh) != (pw, ph):
            img = img.resize((vw, vh), Image.Resampling.LANCZOS)

        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()

        self._map[self.selected_index] = {
            "scale": scale, "pw": pw, "ph": ph,
            "left": geo.left, "top": geo.top, "vw": vw, "vh": vh,
        }
        info = {
            "display": self.selected_index,
            "virtual_width": vw, "virtual_height": vh,
            "physical_width": pw, "physical_height": ph,
            "geometry": geo.to_dict(),
        }
        return png, info

    def mapped_geometry(self) -> dict:
        """Reject input based on an image captured before a topology change."""
        m = self._map.get(self.selected_index)
        if m is None:
            raise ValueError("take a screenshot first so coordinates can be mapped")
        geo = next((d for d in self.list_displays() if d.index == self.selected_index), None)
        if geo is None or (geo.left, geo.top, geo.width, geo.height) != (
                m["left"], m["top"], m["pw"], m["ph"]):
            self._map.pop(self.selected_index, None)
            raise ValueError("display layout changed; take a fresh screenshot before acting")
        return m

    def capture_region(self, region: "list[float]", max_w: int = 1600) -> tuple[bytes, dict]:
        """Capture a sub-region of the selected display at higher detail (zoom).

        ``region`` is ``[x0, y0, x1, y1]`` in the selected display's most-recent
        screenshot (virtual) coordinates. Returns the cropped area at close to
        physical resolution (capped to ``max_w`` wide), so the model can read
        small text. Requires a prior ``capture()`` of this display.
        """
        import mss as _mss
        from PIL import Image

        m = self.mapped_geometry()
        if len(region) != 4 or not all(math.isfinite(float(v)) for v in region):
            raise ValueError("region must contain four finite coordinates")
        scale = m["scale"] or 1.0
        x0, y0, x1, y1 = region[0], region[1], region[2], region[3]
        # Intersect both corners; clamping only the origin enlarged clipped crops.
        x0, x1 = max(0, min(x0, x1)), min(m["vw"], max(x0, x1))
        y0, y1 = max(0, min(y0, y1)), min(m["vh"], max(y0, y1))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("zoom region does not intersect the screenshot")
        gx0, gy0 = m["left"] + int(x0 / scale), m["top"] + int(y0 / scale)
        gw = min(max(1, round((x1 - x0) / scale)), m["left"] + m["pw"] - gx0)
        gh = min(max(1, round((y1 - y0) / scale)), m["top"] + m["ph"] - gy0)

        with _mss.mss() as sct:
            raw = sct.grab({"left": gx0, "top": gy0, "width": gw, "height": gh})
        img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, max(1, round(img.height * ratio))), Image.Resampling.LANCZOS)
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), {"region": [gx0, gy0, gw, gh], "display": self.selected_index,
                                "image_width": img.width, "image_height": img.height,
                                "coordinate_space": "full_screenshot",
                                "screenshot_region": [x0, y0, x1, y1]}

    # ----- coordinate mapping ------------------------------------------------

    def to_physical(self, vx: float, vy: float) -> tuple[int, int]:
        """Map a virtual coordinate on the selected display to a global physical
        ``(x, y)`` for input. Requires a prior ``capture()`` of this display.
        """
        m = self.mapped_geometry()
        if not all(math.isfinite(float(v)) for v in (vx, vy)):
            raise ValueError("coordinates must be finite numbers")
        # Clamp the virtual coordinate into the captured image bounds.
        vx = min(max(0.0, float(vx)), m["vw"] - 1)
        vy = min(max(0.0, float(vy)), m["vh"] - 1)
        scale = m["scale"] or 1.0
        gx = m["left"] + vx / scale
        gy = m["top"] + vy / scale
        # Clamp to the physical monitor rectangle.
        gx = min(max(m["left"], gx), m["left"] + m["pw"] - 1)
        gy = min(max(m["top"], gy), m["top"] + m["ph"] - 1)
        return int(round(gx)), int(round(gy))
