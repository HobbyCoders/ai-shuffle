"""Session activity retained until control is released, with physical geometry.

The agent marker retains its last input position when the human moves the mouse.
"""

from __future__ import annotations

import time
import threading

_LOCK = threading.RLock()
_sessions: dict[str, dict] = {}
_last_stop: str | None = None


def record(display: int | None, *, session_id: str | None = None,
           action: str = "screenshot", geometry: dict | None = None,
           cursor: tuple[int, int] | None = None, window: dict | None = None) -> None:
    """Record the session's latest action for the overlay and the status API.

    ``window`` is the granted app window the action works in, as
    ``{"id", "title", "app", "display", "left", "top", "width", "height"}``
    (physical pixels), or None for a display-wide action. The overlay frames
    it instead of the whole display.
    """
    global _last_stop
    key = session_id or "_global"
    with _LOCK:
        previous = _sessions.get(key, {})
        same_display = previous.get("display") == display
        _sessions[key] = {
            "session_id": key, "display": display, "action": action,
            "busy": True,
            "geometry": geometry or (previous.get("geometry") if same_display else None),
            "cursor": list(cursor) if cursor is not None else (
                previous.get("cursor") if same_display else None),
            "window": dict(window) if window else None,
            "at": time.monotonic(),
        }
        _last_stop = None


def idle(session_id: str | None) -> None:
    with _LOCK:
        state = _sessions.get(session_id or "_global")
        if state:
            state["busy"] = False


def finish(session_id: str | None = None, *, reason: str | None = None) -> None:
    global _last_stop
    with _LOCK:
        removed = _sessions.pop(session_id or "_global", None)
        if removed and reason:
            _last_stop = reason


def active_sessions() -> list[str]:
    with _LOCK:
        return list(_sessions)


def snapshot() -> dict:
    with _LOCK:
        latest = max(_sessions.values(), key=lambda s: s["at"], default=None)
        if latest is None:
            return {"active": False, "display": None, "seconds_since": None,
                    "busy": False,
                    "session_id": None, "action": None, "geometry": None,
                    "cursor": None, "window": None, "stop_reason": _last_stop}
        return {**latest, "active": True, "stop_reason": None,
                "seconds_since": round(time.monotonic() - latest["at"], 1)}
