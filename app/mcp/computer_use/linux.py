"""Linux specifics for computer use: which session this is and what it needs.

Screen capture (``mss``) and input (``pynput``'s xorg backend) both talk to an
X11 server, so computer use on Linux works in an X11 session (a desktop login,
or any X server such as Xvfb/Xephyr that DISPLAY points at) and not in a native
Wayland session, which does not let one app read the screen or inject input
into others. Everything here only inspects the environment and the libraries
on disk, except :func:`probe_x11`, which opens (and closes) one X connection.
"""

from __future__ import annotations

import ctypes.util
import importlib.metadata
import logging
import os
import shutil
import threading
import time
from collections.abc import Mapping

logger = logging.getLogger(__name__)

SESSION_X11 = "x11"
SESSION_WAYLAND = "wayland"
SESSION_NONE = "none"

# Shared-library names (as ``ctypes.util.find_library`` takes them) that mss
# loads on Linux, each with the Debian/Ubuntu package that ships it. mss 10+
# captures through XCB; older releases went through Xlib.
_MSS_XCB_LIBS = (
    ("xcb", "libxcb1"),
    ("xcb-randr", "libxcb-randr0"),
    ("xcb-render", "libxcb-render0"),
    ("xcb-shm", "libxcb-shm0"),
    ("xcb-xfixes", "libxcb-xfixes0"),
)
_MSS_XLIB_LIBS = (
    ("X11", "libx11-6"),
    ("Xrandr", "libxrandr2"),
)

# Python modules computer use imports on Linux, with the pip package for each.
# ``Xlib`` is python-xlib, pynput's X11 backend.
PYTHON_MODULES = (
    ("mss", "mss"),
    ("pynput", "pynput"),
    ("PIL", "Pillow"),
    ("Xlib", "python-xlib"),
)

WAYLAND_HELP = (
    "This is a Wayland session. Computer use captures the screen and sends input "
    "through X11, and Wayland does not let one app see or control the others. Log "
    "out, pick an X11 session on the login screen (for example 'GNOME on Xorg' or "
    "'Plasma (X11)'), and start AI Shuffle again."
)
XWAYLAND_HELP = (
    "DISPLAY points at XWayland, so this desktop is a Wayland session. XWayland only "
    "shows X11 apps: screenshots would miss native windows and input would not reach "
    "them. Log in with an X11 session (for example 'GNOME on Xorg' or 'Plasma (X11)') "
    "and start AI Shuffle again."
)
NO_DISPLAY_HELP = (
    "No desktop session: DISPLAY is not set for AI Shuffle, so there is no screen "
    "to see or control. This is normal for a headless server. To use computer use, "
    "run AI Shuffle from an X11 desktop session, or set DISPLAY (and XAUTHORITY) to "
    "one before starting it."
)


def session_type(environ: Mapping[str, str] | None = None) -> str:
    """Classify the graphical session AI Shuffle would control.

    ``wayland`` when the process belongs to a Wayland session (even with
    XWayland's DISPLAY also set: XWayland only shows X11 apps, so screenshots
    come out blank and input misses native windows). ``x11`` when DISPLAY names
    an X server. ``none`` when there is no display at all (headless).
    """
    env = os.environ if environ is None else environ
    if env.get("WAYLAND_DISPLAY") or env.get("XDG_SESSION_TYPE", "").strip().lower() == "wayland":
        return SESSION_WAYLAND
    if env.get("DISPLAY"):
        return SESSION_X11
    return SESSION_NONE


def missing_python_packages(find_spec=None) -> list[str]:
    """pip names of the computer-use modules that are not installed.

    Uses ``find_spec`` rather than importing: importing pynput on Linux opens
    an X connection, so a failed import would hide the real cause.
    """
    import importlib
    import importlib.util

    # Pick up packages installed since startup.
    importlib.invalidate_caches()
    find = find_spec or importlib.util.find_spec
    missing = []
    for module, package in PYTHON_MODULES:
        try:
            if find(module) is None:
                missing.append(package)
        except (ImportError, ValueError):
            missing.append(package)
    return missing


def _mss_libraries() -> tuple[tuple[str, str], ...]:
    try:
        major = int(importlib.metadata.version("mss").split(".")[0])
    except Exception:
        major = 10
    return _MSS_XCB_LIBS if major >= 10 else _MSS_XLIB_LIBS


_found_libraries: set[str] = set()


def missing_system_libraries(find_library=None) -> list[str]:
    """Debian/Ubuntu package names of the X libraries mss needs but cannot find.

    A library once found is remembered (``find_library`` runs ldconfig, and the
    status poll calls this every few seconds); a missing one is looked up
    again, so installing it shows up without a restart.
    """
    find = find_library or ctypes.util.find_library
    missing = []
    for name, package in _mss_libraries():
        if name in _found_libraries:
            continue
        if find(name):
            _found_libraries.add(name)
        else:
            missing.append(package)
    return missing


def missing_python_help(missing: list[str]) -> str:
    return (
        f"Missing Python packages: {', '.join(missing)}. Install them into AI Shuffle's "
        "Python environment with 'pip install -r requirements-native.txt'. On Linux, "
        "pynput's evdev dependency compiles from source and needs a C compiler and kernel "
        "headers (Debian/Ubuntu: sudo apt install build-essential python3-dev linux-libc-dev)."
    )


def missing_libraries_help(missing: list[str]) -> str:
    return (
        f"Missing system libraries for screen capture: {', '.join(missing)}. "
        f"Debian/Ubuntu: sudo apt install {' '.join(missing)}. "
        "Fedora: sudo dnf install libxcb. Arch: sudo pacman -S libxcb."
    )


def probe_x11(display_factory=None) -> tuple[str | None, str]:
    """Open the X display once and check what input injection needs.

    Returns ``(None, "")`` when usable, else ``(reason, help)`` with reason
    ``x11-unreachable`` (cannot connect: wrong DISPLAY, no permission to the
    session's X server), ``wayland`` (the server is XWayland) or ``x11-no-xtest`` (the server lacks the XTEST
    extension pynput uses to move the mouse and press keys).
    """
    name = os.environ.get("DISPLAY", "")
    try:
        if display_factory is None:
            from Xlib import display as xdisplay
            display_factory = xdisplay.Display
        conn = display_factory()
    except Exception as exc:
        return ("x11-unreachable",
                (f"Could not connect to the X display {name!r}: {exc}. Run AI Shuffle as the "
                 "user signed in to that desktop, or set DISPLAY and XAUTHORITY to that "
                 "session's values (see 'echo $DISPLAY $XAUTHORITY' in its terminal)."))
    try:
        # XWayland advertises an XWAYLAND extension. Catches a Wayland desktop
        # whose WAYLAND_DISPLAY/XDG_SESSION_TYPE did not reach this process
        # (a systemd user service, a shell that only imported DISPLAY).
        if conn.has_extension("XWAYLAND"):
            return ("wayland", XWAYLAND_HELP)
        if not conn.has_extension("XTEST"):
            return ("x11-no-xtest",
                    (f"The X server {name!r} has no XTEST extension, which mouse and keyboard "
                     "control needs. Use a standard Xorg session or an X server with XTEST enabled."))
    except Exception:
        logger.debug("computer-use: XTEST query failed", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            logger.debug("computer-use: closing the X probe connection failed", exc_info=True)
    return (None, "")


# ---------------------------------------------------------------------------
# Escape listener: telling the agent's Escape from the person's
# ---------------------------------------------------------------------------
#
# On Windows and macOS pynput marks synthetic key events as injected, so the
# physical-Escape listener ignores the agent's own key('Escape'). On X11, XTEST
# events look exactly like hardware events (pynput reports injected=False), so
# an agent closing a menu would stop its own turn. The input layer announces
# each Escape press it sends; the listener consumes one announcement per
# Escape press it sees, within a short window. A person's Escape is never
# announced, so it always stops the turn: if it arrives first it consumes the
# agent's announcement and the agent's own press then stops the turn instead.
#
# Nothing here suppresses Escape for as long as the agent holds the key:
# auto-repeat cannot be told from a person pressing Escape, so holding Escape
# is refused on X11 (see computer_use_sdk) rather than silencing the stop key.

_ECHO_WINDOW = 0.5
_echo_lock = threading.Lock()
_pending_echoes: list[float] = []


def _prune(t: float) -> None:
    _pending_echoes[:] = [p for p in _pending_echoes if t - p <= _ECHO_WINDOW]


def expect_agent_escape(now: float | None = None) -> float:
    """Record that the agent is about to press Escape (call before sending it).

    Returns a token for :func:`withdraw_agent_escape` if the press fails.
    """
    t = time.monotonic() if now is None else now
    with _echo_lock:
        _prune(t)
        _pending_echoes.append(t)
    return t


def withdraw_agent_escape(token: float) -> None:
    """Forget an announcement whose press was never sent."""
    with _echo_lock:
        try:
            _pending_echoes.remove(token)
        except ValueError:
            pass


def consume_agent_escape(now: float | None = None) -> bool:
    """True when an Escape press just seen is the agent's own."""
    t = time.monotonic() if now is None else now
    with _echo_lock:
        _prune(t)
        if _pending_echoes:
            _pending_echoes.pop(0)
            return True
        return False


def reset_agent_escapes() -> None:
    with _echo_lock:
        _pending_echoes.clear()


# ---------------------------------------------------------------------------
# Clipboard (xclip or xsel)
# ---------------------------------------------------------------------------

CLIPBOARD_HELP = ("Clipboard access on Linux needs xclip or xsel "
                  "(Debian/Ubuntu: sudo apt install xclip).")


def clipboard_command(write: bool, which=None) -> list[str]:
    """argv that reads (or, with ``write``, sets from stdin) the X clipboard."""
    find = which or shutil.which
    if find("xclip"):
        return ["xclip", "-selection", "clipboard", "-in" if write else "-out"]
    if find("xsel"):
        return ["xsel", "--clipboard", "--input" if write else "--output"]
    raise RuntimeError(CLIPBOARD_HELP)
