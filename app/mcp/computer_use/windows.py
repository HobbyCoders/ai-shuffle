"""Top-level window enumeration and control for computer use (Windows only).

Bounds are physical virtual-desktop pixels, the same space as ``mss`` monitor
geometry and ``list_displays`` (the process is per-monitor DPI aware, see
scaling.py). Only windows a user could Alt+Tab to are listed, and control is
limited to windows from that list: a stale or arbitrary handle is refused.

Uses a private ``WinDLL`` instance so the argtypes set here never leak into
other ctypes users of user32.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

WINDOWS_ONLY = "window tools are supported on Windows only"
_ELEVATED_HINT = (
    "Windows did not apply the change. The window may belong to an app running as "
    "administrator (or the UAC secure desktop), which this process cannot control; "
    "ask the user to do it."
)

# Shell surfaces that are not app windows.
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# Start-menu names whose executable and file description do not say the same.
_ALIASES = {
    "explorer": ("File Explorer",),
    "systemsettings": ("Settings",),
    "windowsterminal": ("Terminal", "Windows Terminal"),
    "cmd": ("Command Prompt",),
    "code": ("VS Code", "Visual Studio Code"),
}

_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x80
_WS_EX_APPWINDOW = 0x40000
_WS_EX_NOACTIVATE = 0x08000000
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_SW_MAXIMIZE, _SW_MINIMIZE, _SW_RESTORE = 3, 6, 9
_SWP_NOZORDER, _SWP_NOACTIVATE, _SWP_ASYNCWINDOWPOS = 0x4, 0x10, 0x4000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_MIN_SIZE = 64  # smallest width/height move_resize accepts
_SETTLE_S = 1.5  # how long to wait for another process to apply a change


@dataclass
class WindowInfo:
    id: int
    title: str
    app: str
    process: str
    pid: int
    state: str  # "normal" | "minimized" | "maximized"
    foreground: bool
    bounds: Optional[tuple[int, int, int, int]]  # x, y, width, height; None when minimized
    display: Optional[int]
    names: tuple[str, ...] = field(default=(), repr=False)  # identities for grant matching

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "app": self.app,
            "process": self.process, "pid": self.pid, "state": self.state,
            "foreground": self.foreground,
            "bounds": list(self.bounds) if self.bounds else None,
            "display": self.display,
        }


# ---------------------------------------------------------------------------
# Grant matching (pure; tested on every platform)
# ---------------------------------------------------------------------------

def _tokens(name: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return toks[:-1] if len(toks) > 1 and toks[-1] == "exe" else toks


def name_matches(app_name: str, names: Iterable[str]) -> bool:
    """True when ``app_name`` (a Start-menu style name) names one of ``names``.

    Equal ignoring case/punctuation/spacing ('Windows Terminal' ==
    'WindowsTerminal', 'notepad.exe' == 'Notepad'), or a trailing-word match
    ('Edge' names 'Microsoft Edge'). A leading vendor word alone does not
    match: 'Microsoft' does not grant 'Microsoft Edge'.
    """
    want = _tokens(app_name)
    if not want:
        return False
    for name in names:
        have = _tokens(name)
        if not have:
            continue
        if "".join(have) == "".join(want):
            return True
        if len(want) < len(have) and have[-len(want):] == want:
            return True
    return False


def is_granted(win: WindowInfo, apps: Iterable[str]) -> bool:
    return any(name_matches(a, win.names) for a in apps)


# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

def _require_windows() -> None:
    if sys.platform != "win32":
        raise ValueError(WINDOWS_ONLY)
    # Worker threads must read and set window rects in physical pixels too.
    from app.mcp.computer_use import scaling
    scaling._ensure_dpi_awareness()


@functools.lru_cache(maxsize=1)
def _api():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    version = ctypes.WinDLL("version")
    H, B, D = wintypes.HWND, wintypes.BOOL, wintypes.DWORD
    enum_proc = ctypes.WINFUNCTYPE(B, H, wintypes.LPARAM)
    sig = {
        (user32, "EnumWindows"): ([enum_proc, wintypes.LPARAM], B),
        (user32, "EnumChildWindows"): ([H, enum_proc, wintypes.LPARAM], B),
        (user32, "IsWindow"): ([H], B),
        (user32, "IsWindowVisible"): ([H], B),
        (user32, "IsIconic"): ([H], B),
        (user32, "IsZoomed"): ([H], B),
        (user32, "GetWindow"): ([H, ctypes.c_uint], H),
        (user32, "GetWindowLongW"): ([H, ctypes.c_int], ctypes.c_long),
        (user32, "GetWindowTextLengthW"): ([H], ctypes.c_int),
        (user32, "GetWindowTextW"): ([H, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user32, "GetClassNameW"): ([H, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user32, "GetWindowThreadProcessId"): ([H, ctypes.POINTER(D)], D),
        (user32, "GetWindowRect"): ([H, ctypes.POINTER(wintypes.RECT)], B),
        (user32, "GetForegroundWindow"): ([], H),
        (user32, "SetForegroundWindow"): ([H], B),
        (user32, "BringWindowToTop"): ([H], B),
        (user32, "ShowWindowAsync"): ([H, ctypes.c_int], B),
        (user32, "SetWindowPos"): ([H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_uint], B),
        (user32, "AttachThreadInput"): ([D, D, B], B),
        (kernel32, "GetCurrentThreadId"): ([], D),
        (kernel32, "OpenProcess"): ([D, B, D], wintypes.HANDLE),
        (kernel32, "CloseHandle"): ([wintypes.HANDLE], B),
        (kernel32, "QueryFullProcessImageNameW"): ([wintypes.HANDLE, D, wintypes.LPWSTR,
                                                    ctypes.POINTER(D)], B),
        (dwmapi, "DwmGetWindowAttribute"): ([H, D, ctypes.c_void_p, D], ctypes.c_long),
        (version, "GetFileVersionInfoSizeW"): ([wintypes.LPCWSTR, ctypes.POINTER(D)], D),
        (version, "GetFileVersionInfoW"): ([wintypes.LPCWSTR, D, D, ctypes.c_void_p], B),
        (version, "VerQueryValueW"): ([ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(ctypes.c_uint)], B),
    }
    for (dll, name), (args, res) in sig.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, res
    return ctypes, wintypes, user32, kernel32, dwmapi, version, enum_proc


def _text(fn, hwnd, size: int) -> str:
    ctypes = _api()[0]
    buf = ctypes.create_unicode_buffer(size)
    fn(hwnd, buf, size)
    return buf.value


def _pid(hwnd) -> int:
    ctypes, wintypes, user32 = _api()[:3]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _app_pid(hwnd, class_name: str) -> int:
    """The app's pid. UWP windows are hosted by ApplicationFrameHost; the app
    itself owns a child CoreWindow."""
    pid = _pid(hwnd)
    if class_name != "ApplicationFrameWindow":
        return pid
    _, _, user32, _, _, _, enum_proc = _api()
    found = []

    def child(h, _):
        cpid = _pid(h)
        if cpid != pid:
            found.append(cpid)
            return False
        return True
    user32.EnumChildWindows(hwnd, enum_proc(child), 0)
    return found[0] if found else pid


def _exe_path(pid: int) -> str:
    ctypes, wintypes, _, kernel32 = _api()[:4]
    h = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


@functools.lru_cache(maxsize=256)
def _version_strings(path: str) -> tuple[str, str]:
    """(FileDescription, ProductName) from the executable's version resource."""
    if not path:
        return "", ""
    ctypes, _, _, _, _, version, _ = _api()
    try:
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return "", ""
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, data):
            return "", ""
        ptr, length = ctypes.c_void_p(), ctypes.c_uint()
        langs = []
        if version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr),
                                  ctypes.byref(length)) and length.value >= 4:
            words = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16))
            langs = [(words[i], words[i + 1]) for i in range(0, length.value // 2, 2)]
        langs += [(0x0409, 0x04B0), (0x0409, 0x04E4)]
        out = []
        for key in ("FileDescription", "ProductName"):
            value = ""
            for lang, cp in langs:
                q = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
                if version.VerQueryValueW(data, q, ctypes.byref(ptr), ctypes.byref(length)) \
                        and length.value:
                    value = ctypes.wstring_at(ptr, length.value).rstrip("\x00").strip()
                    if value:
                        break
            out.append(value)
        return out[0], out[1]
    except Exception:
        logger.debug("version info lookup failed for %s", path, exc_info=True)
        return "", ""


def _frame_rect(hwnd) -> Optional[tuple[int, int, int, int]]:
    """Visible frame (left, top, right, bottom), without invisible resize borders."""
    ctypes, wintypes, user32, _, dwmapi = _api()[:5]
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect),
                                    ctypes.sizeof(rect)) != 0:
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    return rect.left, rect.top, rect.right, rect.bottom


def _window_rect(hwnd) -> Optional[tuple[int, int, int, int]]:
    ctypes, wintypes, user32 = _api()[:3]
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def _cloaked(hwnd) -> bool:
    ctypes, wintypes, _, _, dwmapi = _api()[:5]
    value = wintypes.DWORD()
    ok = dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(value),
                                      ctypes.sizeof(value)) == 0
    return ok and value.value != 0


def _listable(hwnd) -> bool:
    """Alt+Tab-style filter: visible, uncloaked, titled app windows."""
    user32 = _api()[2]
    if not user32.IsWindowVisible(hwnd):
        return False
    ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    app_window = bool(ex & _WS_EX_APPWINDOW)
    if not app_window and (ex & (_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE)
                           or user32.GetWindow(hwnd, _GW_OWNER)):
        return False
    if user32.GetWindowTextLengthW(hwnd) <= 0 or _cloaked(hwnd):
        return False
    return True


def _overlap_display(rect, displays) -> Optional[int]:
    left, top, right, bottom = rect

    def area(d):
        return (max(0, min(right, d.left + d.width) - max(left, d.left)) *
                max(0, min(bottom, d.top + d.height) - max(top, d.top)))
    best = max(displays, key=area, default=None)
    return best.index if best is not None and area(best) > 0 else None


def _describe(hwnd, displays, foreground) -> WindowInfo:
    user32 = _api()[2]
    title = _text(user32.GetWindowTextW, hwnd, min(user32.GetWindowTextLengthW(hwnd) + 1, 512))
    class_name = _text(user32.GetClassNameW, hwnd, 256)
    pid = _app_pid(hwnd, class_name)
    path = _exe_path(pid)
    exe = os.path.basename(path) if path else ""
    stem = os.path.splitext(exe)[0]
    desc, product = _version_strings(path)
    names = tuple(n for n in (stem, desc, product, *_ALIASES.get(stem.lower(), ())) if n)
    if user32.IsIconic(hwnd):
        state, bounds, display = "minimized", None, None
    else:
        state = "maximized" if user32.IsZoomed(hwnd) else "normal"
        rect = _frame_rect(hwnd)
        bounds = (rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]) if rect else None
        display = _overlap_display(rect, displays) if rect else None
    hv = int(hwnd or 0)
    # Some descriptions are just the file name ("Notepad.exe").
    app = product if desc.lower().endswith(".exe") and product else desc
    return WindowInfo(id=hv, title=title[:200], app=app or stem or "unknown", process=exe,
                      pid=pid, state=state, foreground=hv == int(foreground or 0),
                      bounds=bounds, display=display, names=names)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_windows(displays) -> list[WindowInfo]:
    """Open top-level app windows, front to back (Z order)."""
    _require_windows()
    _, _, user32, _, _, _, enum_proc = _api()
    handles: list = []

    def collect(hwnd, _):
        handles.append(hwnd)
        return True
    user32.EnumWindows(enum_proc(collect), 0)
    foreground = user32.GetForegroundWindow()
    own = os.getpid()
    out = []
    for hwnd in handles:
        try:
            if not _listable(hwnd):
                continue
            if _text(user32.GetClassNameW, hwnd, 256) in _SKIP_CLASSES or _pid(hwnd) == own:
                continue
            out.append(_describe(hwnd, displays, foreground))
        except Exception:
            logger.debug("skipping window %s", hwnd, exc_info=True)
    return out


def find_window(window_id, displays) -> WindowInfo:
    """The listed window with this id; refuses ids not in list_windows."""
    try:
        wid = int(window_id)
    except (TypeError, ValueError):
        raise ValueError("window_id must be an id from list_windows")
    for win in list_windows(displays):
        if win.id == wid:
            return win
    raise ValueError(f"window {wid} is not open (or not an app window); call list_windows again")


def _wait(check: Callable[[], bool], sleep: Callable[[float], None], timeout=_SETTLE_S) -> bool:
    end = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= end:
            return False
        sleep(0.05)


def _set_foreground(hwnd) -> None:
    _, _, user32, kernel32 = _api()[:4]
    # Windows only lets the foreground thread hand focus over; borrow its
    # input state for the call.
    current = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = bool(fg_thread and fg_thread != current
                    and user32.AttachThreadInput(current, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current, fg_thread, False)


def focus(window_id: int, sleep: Callable[[float], None],
          nudge: Optional[Callable[[], None]] = None) -> bool:
    """Restore if minimized and bring to the foreground. True when it is in front."""
    _require_windows()
    user32 = _api()[2]
    hwnd = int(window_id)
    if user32.IsIconic(hwnd):
        user32.ShowWindowAsync(hwnd, _SW_RESTORE)
        _wait(lambda: not user32.IsIconic(hwnd), sleep)

    def in_front() -> bool:
        return int(user32.GetForegroundWindow() or 0) == hwnd
    if in_front():
        return True
    _set_foreground(hwnd)
    if _wait(in_front, sleep, 0.4):
        return True
    if nudge is not None:
        # Foreground lock: a fresh keyboard event from this process lifts it.
        nudge()
        _set_foreground(hwnd)
    return _wait(in_front, sleep, 0.6)


def set_state(window_id: int, action: str, sleep: Callable[[float], None]) -> str:
    """maximize / minimize / restore. Returns the resulting state."""
    _require_windows()
    user32 = _api()[2]
    hwnd = int(window_id)
    steps = {
        "maximize": [(_SW_MAXIMIZE, lambda: bool(user32.IsZoomed(hwnd)))],
        "minimize": [(_SW_MINIMIZE, lambda: bool(user32.IsIconic(hwnd)))],
        # A minimized window restores to its previous state, which may be
        # maximized; the second step takes it back to normal.
        "restore": [(_SW_RESTORE, lambda: not user32.IsIconic(hwnd)),
                    (_SW_RESTORE, lambda: not user32.IsZoomed(hwnd))],
    }[action]
    for cmd, done in steps:
        if not done():
            user32.ShowWindowAsync(hwnd, cmd)
            if not _wait(done, sleep):
                raise ValueError(_ELEVATED_HINT)
    return {"maximize": "maximized", "minimize": "minimized"}.get(action, "normal")


def move_resize(window_id: int, bounds, displays,
                sleep: Callable[[float], None]) -> tuple[int, int, int, int]:
    """Place the window's visible frame at ``bounds`` [x, y, width, height]
    (physical desktop pixels). Restores a maximized/minimized window first.
    Returns the bounds Windows applied (apps may enforce a minimum size)."""
    _require_windows()
    try:
        x, y, w, h = (float(v) for v in bounds)
    except (TypeError, ValueError):
        raise ValueError("bounds must be [x, y, width, height]")
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        raise ValueError("bounds must be finite numbers")
    x, y, w, h = int(round(x)), int(round(y)), int(round(w)), int(round(h))
    if w < _MIN_SIZE or h < _MIN_SIZE:
        raise ValueError(f"width and height must be at least {_MIN_SIZE} pixels")
    # Keep the title bar reachable: the top strip must land on a display.
    if _overlap_display((x, y, x + w, y + min(h, 32)), displays) is None:
        raise ValueError("bounds would place the window's title bar off every display "
                         "(use list_displays geometry)")
    user32 = _api()[2]
    hwnd = int(window_id)
    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindowAsync(hwnd, _SW_RESTORE)
        if not _wait(lambda: not user32.IsIconic(hwnd) and not user32.IsZoomed(hwnd), sleep):
            raise ValueError(_ELEVATED_HINT)
    before = _frame_rect(hwnd)
    outer = _window_rect(hwnd)
    if not before or not outer:
        raise ValueError("could not read the window's position")
    # SetWindowPos takes the outer rect, which includes invisible borders.
    dl, dt = before[0] - outer[0], before[1] - outer[1]
    dr, db = outer[2] - before[2], outer[3] - before[3]
    target = (x, y, x + w, y + h)
    user32.SetWindowPos(hwnd, None, x - dl, y - dt, w + dl + dr, h + dt + db,
                        _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_ASYNCWINDOWPOS)

    def close() -> bool:
        r = _frame_rect(hwnd)
        return bool(r) and all(abs(a - b) <= 2 for a, b in zip(r, target))
    _wait(close, sleep)
    after = _frame_rect(hwnd)
    if not after or (after == before and before != target):
        raise ValueError(_ELEVATED_HINT)
    return after[0], after[1], after[2] - after[0], after[3] - after[1]
