"""On-screen indicator for which physical display the agent is controlling.

AI Shuffle is usually minimized while the agent drives the desktop, so the
"controlling your screen" indicator must live on the actual screen, not in the
web app. This draws a thin, always-on-top, **click-through** frame around the
active display plus a small badge, shown only while the agent is acting and
hidden when control stops or is paused.

Implementation notes:
- Pure stdlib (tkinter); no new dependencies.
- Click-through so it never blocks the user *or* the agent's own input
  (Windows: WS_EX_TRANSPARENT; other OSes: best-effort, falls back to a plain
  topmost frame).
- A fully transparent center (Windows ``-transparentcolor``) means only the
  border + badge are painted, so almost nothing on screen is obscured.
- Runs its own Tk mainloop on a daemon thread and polls the shared activity /
  abort state every few hundred ms. No-op if tkinter is unavailable.
"""

from __future__ import annotations

import logging
import math
import sys
import threading

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
    POLL_MS = 250

    def __init__(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cu-overlay", daemon=True)
        self._root = None
        self._canvas = None
        self._visible = False
        self._displays: dict = {}
        self._frame = 0

    def start(self) -> None:
        self._thread.start()

    # ----- tk thread ---------------------------------------------------------

    def _run(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", _TRANSPARENT_KEY)
        except Exception:
            pass
        self._canvas = tk.Canvas(root, highlightthickness=0, bd=0, bg=_TRANSPARENT_KEY)
        self._canvas.pack(fill="both", expand=True)
        self._tick()
        root.mainloop()

    def _tick(self) -> None:
        try:
            self._update()
        except Exception:
            logger.debug("overlay tick failed", exc_info=True)
        finally:
            if self._root is not None:
                self._root.after(self.POLL_MS, self._tick)

    def _refresh_displays(self) -> None:
        try:
            from app.mcp.computer_use.scaling import enumerate_displays
            self._displays = {d.index: d for d in enumerate_displays()}
        except Exception:
            logger.debug("overlay could not enumerate displays", exc_info=True)

    def _update(self) -> None:
        from app.mcp.computer_use import activity, abort

        snap = activity.snapshot()
        show = bool(snap["active"]) and not abort.is_stopped() and snap["display"] is not None

        if not show:
            if self._visible:
                self._root.withdraw()
                self._visible = False
            return

        display = snap["display"]
        if display not in self._displays:
            self._refresh_displays()
        geo = self._displays.get(display)
        if geo is None:
            return

        self._root.geometry(f"{geo.width}x{geo.height}+{geo.left}+{geo.top}")
        if not self._visible:
            self._root.deiconify()
            self._apply_clickthrough()
            self._visible = True
        self._draw(geo.width, geo.height, display)

    def _draw(self, w: int, h: int, display: int) -> None:
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
        label = f"  ●  AI Shuffle is controlling this screen · Display {display}  "
        font = ("Segoe UI", 11, "bold")
        approx_w = max(280, int(len(label) * 7.2))
        bx0 = (w - approx_w) // 2
        bx1 = bx0 + approx_w
        c.create_rectangle(bx0, 0, bx1, _BADGE_H, fill=_BADGE_BG, outline=glow, width=2)
        c.create_text((bx0 + bx1) // 2, _BADGE_H // 2, text=label, fill=_TEXT, font=font)

    def _apply_clickthrough(self) -> None:
        """Make the window ignore mouse input so it never blocks user/agent."""
        if sys.platform != "win32":
            try:
                self._root.attributes("-alpha", 0.9)
            except Exception:
                pass
            return
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id()) or self._root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_TOOLWINDOW = 0x00000080  # keep off the taskbar / alt-tab
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW,
            )
        except Exception:
            logger.debug("overlay click-through could not be applied", exc_info=True)


def _blend(hex_a: str, hex_b: str, t: float) -> str:
    """Linear blend of two #rrggbb colours; t=0 -> a, t=1 -> b."""
    t = max(0.0, min(1.0, t))
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    out = tuple(round(a[i] + (b[i] - a[i]) * (1 - t)) for i in range(3))
    return "#%02x%02x%02x" % out
