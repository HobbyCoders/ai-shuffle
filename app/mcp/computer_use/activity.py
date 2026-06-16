"""Tracks recent computer-use activity so the UI can show a live "AI Shuffle is
controlling your screen" indicator and which display.

Process-global, like the emergency stop. An action calls ``record(display)``
each time it touches the screen; the status endpoint reads ``snapshot()`` and
considers control "active" if something happened within ACTIVE_WINDOW_SECONDS.
"""

from __future__ import annotations

import time
from typing import Optional

# How long after the last action we still report control as "active". Long
# enough to span the model's think time between tool calls.
ACTIVE_WINDOW_SECONDS = 8.0

_last_at: float = 0.0
_last_display: Optional[int] = None


def record(display: Optional[int]) -> None:
    """Mark that a computer-use action just ran on ``display``."""
    global _last_at, _last_display
    _last_at = time.monotonic()
    _last_display = display


def snapshot() -> dict:
    """Return ``{active, display, seconds_since}`` for the status endpoint."""
    if _last_at <= 0:
        return {"active": False, "display": None, "seconds_since": None}
    since = time.monotonic() - _last_at
    active = since <= ACTIVE_WINDOW_SECONDS
    return {
        "active": active,
        "display": _last_display if active else None,
        "seconds_since": round(since, 1),
    }
