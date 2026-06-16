"""Per-session computer-use access grants.

Mirrors the official computer-use ``request_access`` model: the agent must be
granted access (the user approves which apps + extra capabilities) before it can
control the screen. The actual approval prompt is driven by permission mode in
the query callback (see query_engine ``can_use_tool_callback``); this module just
records what was granted, keyed by session, so the action tools can enforce it.

Approval policy by permission mode (decided in the callback, not here):
- bypassPermissions -> implicit access, no request needed
- plan              -> denied
- default/ask/auto/acceptEdits -> request_access surfaces an approve/deny prompt
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

_lock = threading.Lock()
# session_id -> grant state
_grants: Dict[str, dict] = {}


def grant(
    session_id: str,
    apps: Optional[List[str]] = None,
    *,
    clipboard_read: bool = False,
    clipboard_write: bool = False,
    system_keys: bool = False,
) -> dict:
    """Record (or extend) access for a session. Returns the current grant."""
    if not session_id:
        session_id = "_global"
    with _lock:
        g = _grants.setdefault(session_id, {
            "apps": set(), "clipboard_read": False,
            "clipboard_write": False, "system_keys": False,
        })
        if apps:
            g["apps"].update(a for a in apps if a)
        g["clipboard_read"] = g["clipboard_read"] or clipboard_read
        g["clipboard_write"] = g["clipboard_write"] or clipboard_write
        g["system_keys"] = g["system_keys"] or system_keys
        return _snapshot(g)


def is_granted(session_id: Optional[str]) -> bool:
    """True if the session has been granted access at least once."""
    with _lock:
        return (session_id or "_global") in _grants


def info(session_id: Optional[str]) -> dict:
    """Return the current grant for a session (empty grant if none)."""
    with _lock:
        g = _grants.get(session_id or "_global")
        return _snapshot(g) if g else {
            "apps": [], "clipboard_read": False,
            "clipboard_write": False, "system_keys": False,
        }


def revoke(session_id: Optional[str]) -> None:
    with _lock:
        _grants.pop(session_id or "_global", None)


def _snapshot(g: dict) -> dict:
    return {
        "apps": sorted(g["apps"]),
        "clipboard_read": g["clipboard_read"],
        "clipboard_write": g["clipboard_write"],
        "system_keys": g["system_keys"],
    }
