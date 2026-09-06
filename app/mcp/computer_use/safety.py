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


def _win32_event_filter(msg, data):
    # LLKHF_INJECTED: an agent's key('Escape') must still close menus/dialogs.
    if msg in (0x100, 0x104) and data.vkCode == 0x1B and not (data.flags & 0x10):
        abort.stop_active("escape")
    return True  # Observe; never swallow the user's keys.


def ensure_started() -> bool:
    global _listener
    with _listener_lock:
        if _listener is not None and _listener.is_alive():
            return True
        try:
            from pynput.keyboard import Listener, Key

            if sys.platform == "win32":
                _listener = Listener(win32_event_filter=_win32_event_filter)
            else:
                def on_press(key, injected=False):
                    if key == Key.esc and not injected:
                        abort.stop_active("escape")
                _listener = Listener(on_press=on_press)
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

    def __init__(self, session_id, mouse, keyboard):
        self.session_id = session_id
        self.mouse = mouse
        self.keyboard = keyboard
        self.token = abort.generation(session_id)
        self.held: list[tuple[object, object]] = []
        abort.register_controller(self)

    def check(self):
        with abort.input_guard(self.session_id, self.token):
            pass

    def call(self, fn, *args):
        with abort.input_guard(self.session_id, self.token):
            return fn(*args)

    def press(self, device, key):
        with abort.input_guard(self.session_id, self.token):
            if (device, key) not in self.held:
                self.held.append((device, key))
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
