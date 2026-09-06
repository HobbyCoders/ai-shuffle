"""On-screen indicator for which physical display the agent is controlling.

AI Shuffle is usually minimized while the agent drives the desktop, so the
"controlling your screen" indicator must live on the actual screen, not in the
web app. This draws a thin, always-on-top, **click-through** frame around the
active display plus a small badge, shown only while the agent is acting and
hidden when control stops or is paused.

Implementation notes:
- Pure stdlib (tkinter); no new dependencies.
- Windows click-through/no-activate so it never blocks or steals input.
- A fully transparent center (Windows ``-transparentcolor``) means only the
  border + badge are painted, so almost nothing on screen is obscured.
- Runs its own Tk mainloop on a daemon thread and draws current session state.
  No-op if tkinter or native transparent windows are unavailable.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time

logger = logging.getLogger(__name__)

_manager = None
_lock = threading.Lock()

# Magic colour treated as fully transparent + click-through on Windows.
_TRANSPARENT_KEY = "#010102"
_ACCENT = "#7c5cff"
_BADGE_BG = "#141417"
_TEXT = "#f4f4f5"
_BORDER_W = 4
_BADGE_H = 30


def status() -> dict:
    return {"available": bool(_manager and getattr(_manager, "ready", False)),
            "error": getattr(_manager, "error", None) if _manager else None}


def ensure_started() -> None:
    """Start the overlay thread once (idempotent). Safe to call repeatedly."""
    global _manager
    with _lock:
        if _manager is not None:
            return
        try:
            import tkinter  # noqa: F401  — probe availability before spawning
            _manager = _OverlayManager()
            _manager.start()
            logger.info("computer-use on-screen overlay started")
        except Exception:
            logger.debug("computer-use overlay unavailable; continuing without it", exc_info=True)
            _manager = _Noop()


class _Noop:
    def start(self) -> None:  # pragma: no cover - trivial
        pass


class _OverlayManager:
    POLL_MS = 33

    def __init__(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cu-overlay", daemon=True)
        self._root = None
        self._canvas = None
        self._visible = False
        self._frame = 0
        self.ready = False
        self.error = None
        self._geometry = None
        self._hwnd = None

    def start(self) -> None:
        self._thread.start()

    # ----- tk thread ---------------------------------------------------------

    def _run(self) -> None:
        try:
            self._mainloop()
        except Exception as e:
            self.error = str(e)
            logger.exception("Computer-use overlay failed")
        finally:
            self.ready = False

    def _mainloop(self) -> None:
        import tkinter as tk
        from app.mcp.computer_use.scaling import _ensure_dpi_awareness

        _ensure_dpi_awareness()
        # Never cover a desktop with an opaque, input-blocking fallback window.
        if sys.platform != "win32":
            self.error = "Native agent overlay currently requires Windows"
            return

        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", _TRANSPARENT_KEY)
        self._canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=_TRANSPARENT_KEY)
        self._canvas.pack(fill="both", expand=True)
        root.update_idletasks()
        self._apply_clickthrough()
        self.ready = True
        self._tick()
        root.mainloop()

    def _tick(self) -> None:
        try:
            self._update()
        except Exception:
            logger.debug("overlay tick failed", exc_info=True)
            self.ready = False
            self._root.withdraw()
            self._visible = False
        else:
            self.ready = True
        finally:
            if self._root is not None:
                self._root.after(self.POLL_MS, self._tick)

    def _update(self) -> None:
        from app.mcp.computer_use import activity

        snap = activity.snapshot()
        show = bool(snap["active"]) and snap["geometry"] is not None

        if not show:
            if self._visible:
                self._root.withdraw()
                self._visible = False
            return

        display = snap["display"]
        geo = snap["geometry"]
        geometry = (geo["left"], geo["top"], geo["width"], geo["height"])
        if not self._visible:
            self._root.deiconify()
            self._apply_clickthrough()
            self._visible = True
            self._geometry = None
        if geometry != self._geometry:
            self._position(*geometry)
            self._geometry = geometry
        self._draw(geo["width"], geo["height"], display, snap)

    def _position(self, left, top, width, height):
        # Tk negative offsets mean "from the right/bottom edge", not virtual
        # desktop coordinates. Win32 accepts actual signed physical positions.
        import ctypes
        from ctypes import wintypes
        position = ctypes.windll.user32.SetWindowPos
        position.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
        position.restype = wintypes.BOOL
        if not position(self._hwnd, -1, left, top, width, height, 0x0010 | 0x0040):
            raise RuntimeError("Could not position the computer-use overlay")

    def _draw(self, w: int, h: int, display: int, snap: dict) -> None:
        self._frame = (self._frame + 1) % 1000
        # Pulse the accent between dim and bright.
        t = (math.sin(self._frame / (1000 / self.POLL_MS / 1.2)) + 1) / 2
        glow = _blend(_ACCENT, _BADGE_BG, 0.25 + 0.55 * t)

        c = self._canvas
        c.configure(width=w, height=h, bg=_TRANSPARENT_KEY)
        c.delete("all")
        # Transparent interior, painted accent border.
        c.create_rectangle(
            _BORDER_W // 2, _BORDER_W // 2, w - _BORDER_W // 2, h - _BORDER_W // 2,
            outline=glow, width=_BORDER_W, fill=_TRANSPARENT_KEY,
        )
        # Top-center badge.
        action = snap.get("action", "")
        labels = {"screenshot": "Looking at screen", "zoom": "Reading detail",
                  "type": "Typing", "key": "Pressing keys", "hold_key": "Holding keys",
                  "mouse_move": "Moving", "left_click_drag": "Dragging", "scroll": "Scrolling",
                  "switch_display": "Screen selected", "wait": "Waiting"}
        detail = labels.get(action, "Clicking" if "click" in action else "Using computer")
        if not snap.get("busy") and time.monotonic() - snap["at"] > 2:
            detail = "Thinking · control active"
        from app.mcp.computer_use.safety import escape_available
        stop_hint = "Esc to stop" if escape_available() else "Stop in AI Shuffle"
        label = f"  ●  AI Shuffle · Display {display} · {detail} · {stop_hint}  "
        font = ("Segoe UI", 11, "bold")
        approx_w = max(280, int(len(label) * 7.2))
        bx0 = (w - approx_w) // 2
        bx1 = bx0 + approx_w
        c.create_rectangle(bx0, 0, bx1, _BADGE_H, fill=_BADGE_BG, outline=glow, width=2)
        c.create_text((bx0 + bx1) // 2, _BADGE_H // 2, text=label, fill=_TEXT, font=font)

        cursor = snap.get("cursor")
        geo = snap["geometry"]
        if cursor:
            x, y = cursor[0] - geo["left"], cursor[1] - geo["top"]
            if 0 <= x < w and 0 <= y < h:
                age = time.monotonic() - snap["at"]
                if ("click" in action or action == "left_mouse_down") and age < 0.8:
                    radius = 12 + age * 30
                    c.create_oval(x-radius, y-radius, x+radius, y+radius, outline=_ACCENT, width=3)
                c.create_polygon(x, y, x+5, y+27, x+11, y+19, x+21, y+18,
                                 fill=_ACCENT, outline="white", width=2)
                tx, ty = min(w - 80, x + 27), min(h - 25, y + 30)
                c.create_rectangle(tx-4, ty-11, tx+62, ty+11, fill=_BADGE_BG, outline=_ACCENT)
                c.create_text(tx+29, ty, text="Agent", fill=_TEXT, font=("Segoe UI", 10, "bold"))

    def _apply_clickthrough(self) -> None:
        """Make the window ignore mouse input so it never blocks user/agent."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            hwnd = user32.GetParent(self._root.winfo_id()) or self._root.winfo_id()
            self._hwnd = hwnd
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW = 0x00000080  # keep off the taskbar / alt-tab
            WS_EX_NOACTIVATE = 0x08000000
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            )
            # Keep the border/badge/agent marker out of the model's screenshots.
            # Earlier builds interpret 0x11 as a black capture, so never use it there.
            if sys.getwindowsversion().build >= 19041:
                user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
                if not user32.SetWindowDisplayAffinity(hwnd, 0x11):
                    logger.warning("Computer-use overlay could not be excluded from screenshots")
        except Exception:
            # Input transparency is required, not a cosmetic best effort.
            self._root.withdraw()
            raise


def _blend(hex_a: str, hex_b: str, t: float) -> str:
    """Linear blend of two #rrggbb colours; t=0 -> a, t=1 -> b."""
    t = max(0.0, min(1.0, t))
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    out = tuple(round(a[i] + (b[i] - a[i]) * (1 - t)) for i in range(3))
    return "#%02x%02x%02x" % out
