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

Each screenshot is an observation with an id (``screenshot_id``). Coordinates
belong to one observation: input is mapped through the current screenshot of
the selected display, and an action that names an older screenshot, or comes
after the mapping was invalidated (display switch, layout change, release,
cancelled call, new owner or turn), is refused with STALE_OBSERVATION rather
than clicked somewhere else.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Default virtual screenshot target (width, height). Aspect ratio is preserved,
# so the actual image is letterboxed within this box, never stretched.
DEFAULT_VIRTUAL_SIZE = (1280, 800)

# Zoom images: native pixels, fit to ZOOM_MAX_SIDE, small crops upscaled (at
# most ZOOM_MAX_UPSCALE) so their long side reaches ZOOM_MIN_SIDE and text
# stays legible to the model.
ZOOM_MAX_SIDE = 1568
ZOOM_MIN_SIDE = 800
ZOOM_MAX_UPSCALE = 4.0

# What the model actually sees. Fluid's condition_image shrinks any tool image
# whose long edge exceeds 1568 px, and Anthropic's API also shrinks images over
# about 1.15 megapixels. A shrink after capture silently breaks the pixel
# mapping (a 2% shrink is ~25 px at the far edge), so every image is sized to
# fit both limits here, before its mapping is recorded.
MODEL_MAX_EDGE = 1568
MODEL_MAX_PIXELS = 1_150_000


def _model_fit(width: int, height: int) -> float:
    """Largest scale <= 1 that keeps a width x height image within the model's
    image budget (long edge and area)."""
    return min(1.0, MODEL_MAX_EDGE / max(width, height, 1),
               math.sqrt(MODEL_MAX_PIXELS / max(1, width * height)))

def fit_virtual_size(cfg: object) -> tuple[int, int]:
    """The screenshot target from a profile's computer-use config, within the
    model's image budget (the one clamp: capture applies ``_model_fit`` again).

    A missing, null or invalid side falls back to the default rather than
    failing the server build (which turned desktop control off for the
    session). An oversized target is shrunk, aspect ratio kept.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    sizes = []
    for key, default in zip(("virtual_width", "virtual_height"), DEFAULT_VIRTUAL_SIZE):
        try:
            value = int(cfg.get(key) or default)
        except (TypeError, ValueError, OverflowError):
            value = default
        sizes.append(value if value > 0 else default)
    width, height = sizes
    scale = _model_fit(width, height)
    if scale < 1.0:
        # The epsilon keeps float error from shaving a pixel off an exact fit.
        width = max(1, int(width * scale + 1e-9))
        height = max(1, int(height * scale + 1e-9))
    return width, height

# Process-wide, so an id from another session or server never matches.
_OBSERVATION_SEQ = itertools.count(1)
# How far (virtual pixels) a coordinate may sit outside the screenshot and
# still be clamped onto its edge. Further out is a wrong coordinate.
_EDGE_SLACK = 2.0

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


@dataclass(frozen=True)
class Availability:
    """Whether this host can run computer use, and if not, what to do about it.

    ``reason`` is a short machine token (``ok``, ``docker``, ``wayland``, ...);
    ``detail`` is a sentence for the settings page; ``missing`` names the
    packages to install when that is the cause.
    """

    available: bool
    reason: str
    detail: str = ""
    missing: tuple[str, ...] = ()


DOCKER_HELP = ("AI Shuffle is running in a container, which has no desktop to see or "
               "control. Computer use needs a native install on a computer with a desktop.")

_last: Optional[Availability] = None


def _in_docker() -> bool:
    try:
        from app.core.platform import detect_deployment_mode, DeploymentMode

        return detect_deployment_mode() == DeploymentMode.DOCKER
    except Exception:
        # If platform detection fails, fall through to the probes below.
        return False


def _env_disabled() -> bool:
    return os.environ.get("COMPUTER_USE_DISABLED", "").lower() in ("1", "true", "yes")


def _linux_preflight() -> Optional[Availability]:
    """Linux checks that need no X connection: session, Python packages."""
    from app.mcp.computer_use import linux

    session = linux.session_type()
    if session == linux.SESSION_WAYLAND:
        return Availability(False, "wayland", linux.WAYLAND_HELP)
    if session == linux.SESSION_NONE:
        return Availability(False, "no-display", linux.NO_DISPLAY_HELP)
    missing = linux.missing_python_packages()
    if missing:
        return Availability(False, "missing-deps", linux.missing_python_help(missing), tuple(missing))
    return None


def computer_use_quick_check() -> Availability:
    """Cheap availability for capability heartbeats: env and installed packages only.

    Never opens a display or spawns a process. ``available`` here means "no
    known blocker"; :func:`computer_use_availability` is the ground truth.
    """
    if _in_docker():
        return Availability(False, "docker", DOCKER_HELP)
    if _env_disabled():
        return Availability(False, "env-disabled")
    if sys.platform.startswith("linux"):
        blocked = _linux_preflight()
        if blocked is not None:
            return blocked
    else:
        import importlib.util
        missing = [p for m, p in (("mss", "mss"), ("pynput", "pynput"), ("PIL", "Pillow"))
                   if importlib.util.find_spec(m) is None]
        if missing:
            return Availability(False, "missing-deps", missing=tuple(missing))
    return Availability(True, "ok")


def computer_use_availability() -> Availability:
    """Full availability check, including a live display probe.

    Disabled inside Docker, when deps are missing, or when no usable display is
    found. On Linux it also tells Wayland and headless hosts apart, names any
    missing system library, and checks the X server accepts synthetic input.
    """
    global _last
    _last = result = _availability()
    return result


def _availability() -> Availability:
    # 1. Never works from inside the container (no host display).
    if _in_docker():
        return Availability(False, "docker", DOCKER_HELP)

    # 2. Explicit operator kill-switch.
    if _env_disabled():
        return Availability(False, "env-disabled")

    if sys.platform.startswith("linux"):
        # 3. Session type and Python packages, then the X libraries mss loads
        # and a live X connection (importing pynput needs one).
        from app.mcp.computer_use import linux

        blocked = _linux_preflight()
        if blocked is not None:
            return blocked
        libs = linux.missing_system_libraries()
        if libs:
            return Availability(False, "missing-system-packages",
                                linux.missing_libraries_help(libs), tuple(libs))
        reason, help_text = linux.probe_x11()
        if reason:
            return Availability(False, reason, help_text)
        try:
            import pynput  # noqa: F401
        except Exception as exc:
            return Availability(False, "x11-unreachable", f"Mouse and keyboard control could not start: {exc}")
    else:
        _ensure_dpi_awareness()
        # 3. Required libraries present?
        try:
            import mss  # noqa: F401
            import pynput  # noqa: F401
            from PIL import Image  # noqa: F401
        except Exception as exc:
            return Availability(False, "missing-deps", f"Could not load the computer-use packages: {exc}. "
                                "Install them with 'pip install -r requirements-native.txt'.")

    # 4. Ground-truth probe: can we actually enumerate monitors?
    try:
        import mss as _mss

        with _mss.mss() as sct:
            # monitors[0] is the union box; at least one real monitor => len >= 2.
            if len(sct.monitors) < 2:
                return Availability(False, "no-monitors")
    except Exception as exc:
        logger.debug("Computer-use: monitor probe failed", exc_info=True)
        return Availability(False, "screenshot-probe-failed", f"Screen capture failed: {exc}")

    return Availability(True, "ok")


def computer_use_available() -> tuple[bool, str]:
    """Return ``(available, reason)`` for whether host control is possible here."""
    result = computer_use_availability()
    return result.available, result.reason


def last_availability(reason: str) -> Optional[Availability]:
    """The most recent full check, if it ended with ``reason`` (for its detail)."""
    last = _last
    return last if last is not None and last.reason == reason else None


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


def window_crop(display: "DisplayInfo",
                bounds: "Optional[tuple[int, int, int, int]]") -> "Optional[tuple[int, int, int, int]]":
    """The part of a window's visible frame on ``display``, as physical
    ``(left, top, width, height)``. ``bounds`` is the window's ``(x, y, width,
    height)`` (list_windows). None when minimized or not on that display."""
    if not bounds:
        return None
    x, y, w, h = (int(v) for v in bounds)
    x0, y0 = max(x, display.left), max(y, display.top)
    x1 = min(x + w, display.left + display.width)
    y1 = min(y + h, display.top + display.height)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


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

    def capture(self, crop: "Optional[tuple[int, int, int, int]]" = None) -> tuple[bytes, dict]:
        """Capture the selected display, downscaled to the virtual size.

        Returns ``(png_bytes, info)`` where info has the virtual + physical size
        and the selected display index. Records the scale map so subsequent
        actions on this display map correctly.

        ``crop`` is a physical ``(left, top, width, height)`` on the selected
        display (a window's visible frame, see ``window_crop``). Then only that
        area is captured and the screenshot's mapping starts at the crop's
        origin, so coordinates in the image still map to the right pixels.
        """
        import mss as _mss
        from PIL import Image

        geo = self._selected_geometry()
        if crop is not None:
            cl, ct, cw, ch = crop
            if not (cw > 0 and ch > 0 and geo.left <= cl and geo.top <= ct
                    and cl + cw <= geo.left + geo.width and ct + ch <= geo.top + geo.height):
                raise ValueError("the crop is not inside the selected display")
            region = {"left": int(cl), "top": int(ct), "width": int(cw), "height": int(ch)}
        else:
            region = {"left": geo.left, "top": geo.top, "width": geo.width, "height": geo.height}
        with _mss.mss() as sct:
            raw = sct.grab(region)
        pw, ph = raw.width, raw.height

        # Only ever downscale; never upscale a small display. Never exceed what
        # the model receives unchanged, whatever virtual size was configured.
        scale = min(self.virtual_w / pw, self.virtual_h / ph, 1.0, _model_fit(pw, ph))
        vw, vh = max(1, round(pw * scale)), max(1, round(ph * scale))

        img = Image.frombytes("RGB", (pw, ph), raw.rgb)
        if (vw, vh) != (pw, ph):
            img = img.resize((vw, vh), Image.Resampling.LANCZOS)

        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()

        shot_id = f"shot-{next(_OBSERVATION_SEQ)}"
        # left/top/pw/ph describe the captured area (the whole display, or the
        # crop); display_rect is the monitor, for the layout-change check.
        self._map[self.selected_index] = {
            "scale": scale, "pw": pw, "ph": ph,
            "left": region["left"], "top": region["top"], "vw": vw, "vh": vh,
            "display_rect": (geo.left, geo.top, geo.width, geo.height),
            "id": shot_id, "at": time.time(),
        }
        info = {
            "screenshot_id": shot_id,
            "display": self.selected_index,
            "virtual_width": vw, "virtual_height": vh,
            "physical_width": pw, "physical_height": ph,
            "geometry": geo.to_dict(),
        }
        if crop is not None:
            info["region"] = [region["left"], region["top"], pw, ph]
        return png, info

    def current_observation(self) -> Optional[str]:
        """Id of the screenshot input is mapped through, or None."""
        m = self._map.get(self.selected_index)
        return m.get("id") if m else None

    def mapped_geometry(self, screenshot_id: Optional[str] = None) -> dict:
        """The current screenshot's mapping. Refuses input based on an image
        that is gone (never taken, invalidated, or captured before a topology
        change) or on a named screenshot that is not the current one."""
        from app.mcp.computer_use.errors import stale
        m = self._map.get(self.selected_index)
        if m is None:
            raise stale(f"No current screenshot of Display {self.selected_index}. Take a "
                        "screenshot first so coordinates can be mapped.")
        if screenshot_id is not None and str(screenshot_id) != m.get("id"):
            raise stale(f"Screenshot {screenshot_id} is not the current one ({m.get('id')}) for "
                        f"Display {self.selected_index}. Use the current screenshot's coordinates "
                        "or take a new screenshot.")
        geo = next((d for d in self.list_displays() if d.index == self.selected_index), None)
        expected = tuple(m.get("display_rect") or (m["left"], m["top"], m["pw"], m["ph"]))
        if geo is None or (geo.left, geo.top, geo.width, geo.height) != expected:
            self._map.pop(self.selected_index, None)
            raise stale("The display layout changed since the screenshot (layout changed). "
                        "Take a fresh screenshot before acting.")
        return m

    @staticmethod
    def _grab_zoomed(left: int, top: int, width: int, height: int):
        """Grab a physical rect and size it for reading: native pixels, scaled
        down to fit ZOOM_MAX_SIDE or up (at most ZOOM_MAX_UPSCALE) so small
        crops reach ZOOM_MIN_SIDE on their long side."""
        import mss as _mss
        from PIL import Image

        with _mss.mss() as sct:
            raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
        img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
        long_side = max(img.width, img.height)
        if long_side > ZOOM_MAX_SIDE:
            ratio = ZOOM_MAX_SIDE / long_side
        elif long_side < ZOOM_MIN_SIDE:
            ratio = min(ZOOM_MAX_UPSCALE, ZOOM_MIN_SIDE / long_side)
        else:
            ratio = 1.0
        # Then fit the model's image budget (area as well as long edge).
        ratio *= _model_fit(round(img.width * ratio), round(img.height * ratio))
        if ratio != 1.0:
            img = img.resize((max(1, round(img.width * ratio)), max(1, round(img.height * ratio))),
                             Image.Resampling.LANCZOS)
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), img.width, img.height

    def screenshot_rect(self, left: int, top: int, right: int, bottom: int) -> "Optional[list[int]]":
        """Map a physical rect into the last screenshot of the selected display
        as ``[x0, y0, x1, y1]`` (clipped). None without a current screenshot of
        that display or when the rect is not on it."""
        m = self._map.get(self.selected_index)
        if not m:
            return None
        s = m["scale"] or 1.0
        x0 = max(0, math.floor((left - m["left"]) * s))
        y0 = max(0, math.floor((top - m["top"]) * s))
        x1 = min(m["vw"], math.ceil((right - m["left"]) * s))
        y1 = min(m["vh"], math.ceil((bottom - m["top"]) * s))
        return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None

    def capture_rect(self, left: int, top: int, width: int, height: int) -> tuple[bytes, dict]:
        """Zoomed capture of a physical desktop rect (e.g. one window), on any
        display. Does not change the click mapping of the last screenshot."""
        displays = self.list_displays()
        if not displays:
            raise RuntimeError("no displays available")
        ux0 = min(d.left for d in displays)
        uy0 = min(d.top for d in displays)
        ux1 = max(d.left + d.width for d in displays)
        uy1 = max(d.top + d.height for d in displays)
        x0, y0 = max(int(left), ux0), max(int(top), uy0)
        x1, y1 = min(int(left + width), ux1), min(int(top + height), uy1)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("that area is not on any display")
        png, iw, ih = self._grab_zoomed(x0, y0, x1 - x0, y1 - y0)
        shot = self.screenshot_rect(x0, y0, x1, y1)
        m = self._map.get(self.selected_index)
        info = {"region": [x0, y0, x1 - x0, y1 - y0], "display": self.selected_index,
                "image_width": iw, "image_height": ih,
                "coordinate_space": "full_screenshot", "screenshot_region": shot}
        if shot and m:
            # Map the WHOLE image, not just its part on this display: for a
            # window that spans monitors, image (0, 0) is left of / above the
            # screenshot, and screenshot_region starts outside it (negative or
            # past the edge). Points off the screenshot are refused on input.
            s = m["scale"] or 1.0
            info["screenshot_region"] = [round((x0 - m["left"]) * s, 1), round((y0 - m["top"]) * s, 1),
                                         round((x1 - m["left"]) * s, 1), round((y1 - m["top"]) * s, 1)]
            info["zoom_factor"] = round(iw / ((x1 - x0) * s), 3)
            info["screenshot_id"] = m.get("id")
        return png, info

    def capture_region(self, region: "list[float]",
                       screenshot_id: Optional[str] = None) -> tuple[bytes, dict]:
        """Capture a sub-region of the selected display at higher detail (zoom).

        ``region`` is ``[x0, y0, x1, y1]`` in the selected display's most-recent
        screenshot (virtual) coordinates. Returns the cropped area at native
        resolution or better (see ``_grab_zoomed``), so the model can read
        small text. Requires a prior ``capture()`` of this display.
        """
        m = self.mapped_geometry(screenshot_id)
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

        png, iw, ih = self._grab_zoomed(gx0, gy0, gw, gh)
        return png, {"region": [gx0, gy0, gw, gh], "display": self.selected_index,
                     "image_width": iw, "image_height": ih,
                     "coordinate_space": "full_screenshot",
                     "screenshot_region": [x0, y0, x1, y1],
                     "zoom_factor": round(iw / (x1 - x0), 3), "screenshot_id": m.get("id")}

    # ----- coordinate mapping ------------------------------------------------

    def to_physical(self, vx: float, vy: float,
                    screenshot_id: Optional[str] = None) -> tuple[int, int]:
        """Map a virtual coordinate on the selected display to a global physical
        ``(x, y)`` for input. Requires a current ``capture()`` of this display
        (and, when ``screenshot_id`` is given, that it is that screenshot).
        """
        from app.mcp.computer_use.errors import ActionError
        m = self.mapped_geometry(screenshot_id)
        if not all(math.isfinite(float(v)) for v in (vx, vy)):
            raise ActionError("INVALID_ARGUMENT", "coordinates must be finite numbers")
        if not (-_EDGE_SLACK <= vx <= m["vw"] - 1 + _EDGE_SLACK
                and -_EDGE_SLACK <= vy <= m["vh"] - 1 + _EDGE_SLACK):
            # Clamping a far-off point onto the edge clicks something else
            # (often the taskbar). Refuse instead.
            raise ActionError("INVALID_ARGUMENT",
                              f"[{vx:g}, {vy:g}] is outside the screenshot ({m['vw']}x{m['vh']}). "
                              "Use pixels of the current screenshot.")
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
