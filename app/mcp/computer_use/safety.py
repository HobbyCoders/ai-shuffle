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
# On macOS with pynput before 1.8 the listener cannot say whether a key was
# injected, so an Escape that arrives this soon after the agent pressed Escape
# is the agent's own. Windows (LLKHF_INJECTED) and macOS with pynput 1.8+
# (source process id) say reliably, so there a real Esc right after the
# agent's own is never ignored. Linux does not use this window: XTest input
# looks like hardware there, so linux.py excuses exactly one Escape press per
# Escape the agent sends instead (see _on_press).
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


def _x11() -> bool:
    return sys.platform.startswith("linux")


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
    if _x11():
        # X11 cannot mark XTEST input as injected; skip only the agent's own
        # announced Escape press (see linux.py). A person's is never announced.
        from app.mcp.computer_use import linux
        if linux.consume_agent_escape():
            logger.info("computer-use: ignoring the agent's own Escape press")
            return
    elif not _injected_flag_reliable() and _recent_agent_escape():
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
        self._hold_timer = None
        abort.register_controller(self)

    def check(self):
        with abort.input_guard(self.session_id, self.token):
            pass

    def call(self, fn, *args):
        with abort.input_guard(self.session_id, self.token):
            self.sent += 1
            return fn(*args)

    def click(self, button, count=1):
        """Native click-count path, with emergency release if the controller fails."""
        with abort.input_guard(self.session_id, self.token):
            pair = (self.mouse, button)
            if pair not in self.held:
                self.held.append(pair)
            self.sent += 1
            try:
                self.mouse.click(button, count)
            except BaseException:
                self.release(self.mouse, button)
                raise
            else:
                # The native click already released it; do not send another mouse-up.
                self.held.remove(pair)

    def move(self, point):
        """Place the pointer. Not counted as a side effect: nothing is clicked."""
        with abort.input_guard(self.session_id, self.token):
            self.mouse.position = point

    def press(self, device, key):
        with abort.input_guard(self.session_id, self.token):
            self._press(device, key)

    def _press(self, device, key):
        # Caller holds the input lock. Emergency Escape is allowed after Stop.
        if (device, key) not in self.held:
            self.held.append((device, key))
        announced = None
        if self.escape_key is not None and key == self.escape_key:
            if _x11():
                # One excuse per press, withdrawn if the press fails.
                from app.mcp.computer_use import linux
                announced = linux.expect_agent_escape()
            else:
                note_agent_escape()
        self.sent += 1
        try:
            device.press(key)
        except BaseException:
            if announced is not None:
                linux.withdraw_agent_escape(announced)
            raise

    def release(self, device, key):
        # Releases are allowed after abort and serialize with emergency cleanup.
        with abort._LOCK:
            if (device, key) in self.held:
                device.release(key)
                # Cleanup can drop a cross-call drag or end a held shortcut.
                # That is input too, even if this action failed before pressing.
                self.sent += 1
                self.held.remove((device, key))
            if not self.held and self._hold_timer is not None:
                self._hold_timer.cancel()
                self._hold_timer = None

    def expire_held_after(self, seconds=15.0):
        """Bound cross-call holds even while the model is thinking. No Escape is sent."""
        with abort._LOCK:
            if self.held and self._hold_timer is None:
                def expire():
                    with abort._LOCK:
                        if self._hold_timer is timer:
                            self.release_all()
                timer = threading.Timer(seconds, expire)
                timer.daemon = True
                self._hold_timer = timer
                timer.start()

    def release_all(self, *, cancel_drag=False):
        with abort._LOCK:
            # A held mouse button is a single-call or cross-call drag. Cancel it
            # in the target app before mouse-up, rather than dropping at Stop.
            if cancel_drag and self.escape_key is not None and any(
                    device is self.mouse for device, _ in self.held):
                try:
                    self._press(self.keyboard, self.escape_key)
                except Exception:
                    logger.exception("Could not press Escape to cancel computer-use drag")
                finally:
                    try:
                        self.release(self.keyboard, self.escape_key)
                    except Exception:
                        logger.exception("Could not release Escape while cancelling computer-use drag")
            if self._hold_timer is not None:
                self._hold_timer.cancel()
                self._hold_timer = None
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
