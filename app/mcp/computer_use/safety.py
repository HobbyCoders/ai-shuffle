"""Interruptible input and a host-wide physical Escape listener."""

from __future__ import annotations

import logging
import sys
import threading
import time

from app.mcp.computer_use import abort

logger = logging.getLogger(__name__)
DESKTOP_LOCK = threading.Lock()
_listener = None
_listener_lock = threading.Lock()

# LLKHF_INJECTED: Windows sets it on every SendInput key, including pynput's.
# (LLKHF_LOWER_IL_INJECTED, 0x02, is only ever set together with it.)
_LLKHF_INJECTED = 0x10
# Where the platform cannot say whether a key was injected, an Escape that
# arrives this soon after the agent pressed Escape is the agent's own. That is
# Linux (pynput's Xorg listener reads XTest input as not injected; uinput
# cannot tell) and pynput before 1.8 anywhere. Windows (LLKHF_INJECTED) and
# macOS with pynput 1.8+ (source process id) say reliably, so there a real
# Esc right after the agent's own is never ignored.
_AGENT_ESCAPE_WINDOW_S = 0.5
_agent_escape_at = 0.0


def note_agent_escape() -> None:
    """Record that the agent itself just sent Escape (see on_press below)."""
    global _agent_escape_at
    _agent_escape_at = time.monotonic()


def _recent_agent_escape() -> bool:
    return time.monotonic() - _agent_escape_at <= _AGENT_ESCAPE_WINDOW_S


def _injected_flag_reliable() -> bool:
    """Whether the non-Windows listener's ``injected`` flag can be trusted."""
    if sys.platform != "darwin":
        return False
    try:
        from pynput._info import __version__
        return tuple(__version__[:2]) >= (1, 8)
    except (ImportError, AttributeError, TypeError):
        return False


def _win32_event_filter(msg, data):
    # Physical Escape only: the agent's key('Escape') must still close
    # menus/dialogs. The injected flag is authoritative on Windows.
    if msg in (0x100, 0x104) and data.vkCode == 0x1B and not (data.flags & _LLKHF_INJECTED):
        abort.stop_active("escape", detail=(
            f"physical Esc key-down (msg=0x{msg:x}, flags=0x{int(data.flags):x}, "
            f"scan={getattr(data, 'scanCode', '?')})"))
    return True  # Observe; never swallow the user's keys.


def _on_press(key, injected=False):
    """Non-Windows listener. ``injected`` is only passed by pynput >= 1.8."""
    from pynput.keyboard import Key
    if key != Key.esc or injected:
        return
    if not _injected_flag_reliable() and _recent_agent_escape():
        logger.info("computer-use: ignoring Escape right after the agent's own Escape")
        return
    abort.stop_active("escape", detail="physical Esc key-down (non-Windows listener)")


def ensure_started() -> bool:
    global _listener
    with _listener_lock:
        if _listener is not None and _listener.is_alive():
            return True
        try:
            from pynput.keyboard import Listener

            if sys.platform == "win32":
                _listener = Listener(win32_event_filter=_win32_event_filter)
            else:
                _listener = Listener(on_press=_on_press)
            _listener.start()
            listener = _listener
            ready = threading.Event()
            def wait_ready():
                listener.wait()
                ready.set()
            threading.Thread(target=wait_ready, name="cu-escape-ready", daemon=True).start()
            if not ready.wait(2) or not listener.is_alive():
                listener.stop()
                raise RuntimeError("Escape listener did not become ready")
            return _listener.is_alive()
        except Exception:
            logger.exception("Computer-use Escape listener could not start")
            _listener = None
            return False


def escape_available() -> bool:
    return _listener is not None and _listener.is_alive()


class SafeInput:
    """Track only agent-held inputs; release them even between tool calls."""

    def __init__(self, session_id, mouse, keyboard, escape_key=None):
        self.session_id = session_id
        self.mouse = mouse
        self.keyboard = keyboard
        self.escape_key = escape_key
        # Count of input events sent; tells "nothing happened" from "partial".
        self.sent = 0
        self.token = abort.generation(session_id)
        self.held: list[tuple[object, object]] = []
        abort.register_controller(self)

    def check(self):
        with abort.input_guard(self.session_id, self.token):
            pass

    def call(self, fn, *args):
        with abort.input_guard(self.session_id, self.token):
            self.sent += 1
            return fn(*args)

    def move(self, point):
        """Place the pointer. Not counted as a side effect: nothing is clicked."""
        with abort.input_guard(self.session_id, self.token):
            self.mouse.position = point

    def press(self, device, key):
        with abort.input_guard(self.session_id, self.token):
            if self.escape_key is not None and key == self.escape_key:
                note_agent_escape()
            if (device, key) not in self.held:
                self.held.append((device, key))
            self.sent += 1
            device.press(key)

    def release(self, device, key):
        # Releases are allowed after abort and serialize with emergency cleanup.
        with abort._LOCK:
            if (device, key) in self.held:
                device.release(key)
                self.held.remove((device, key))

    def release_all(self):
        with abort._LOCK:
            for device, key in list(reversed(self.held)):
                try:
                    self.release(device, key)
                except Exception:
                    logger.exception("Could not release computer-use input")

    def sleep(self, seconds):
        end = time.monotonic() + max(0, seconds)
        while True:
            self.check()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.02, remaining))
