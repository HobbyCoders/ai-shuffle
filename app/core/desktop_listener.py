"""The desktop app's per-launch listener token.

The desktop worker listens on loopback, and every local process can reach
loopback: other apps, scripts, other OS users on a shared machine. The desktop
therefore starts each worker with a fresh random token in
``SHUFFLE_DESKTOP_LISTENER_TOKEN`` and sends it as ``X-Shuffle-Desktop-Listener``
on every request and WebSocket its window (and its own main process) makes to
the worker. When the worker was started with a token, loopback is no longer
proof of ownership: only a request carrying the token is the desktop.

The token is read once, when this module is imported, and removed from the
process environment at the same moment so no agent, tool, terminal or MCP
server the worker spawns can inherit it.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from typing import MutableMapping, Optional

logger = logging.getLogger(__name__)

ENV = "SHUFFLE_DESKTOP_LISTENER_TOKEN"
HEADER = "x-shuffle-desktop-listener"
_VALID = re.compile(r"[a-f0-9]{64}")

_configured = False
_token: Optional[str] = None


def load(environ: MutableMapping[str, str] = os.environ) -> None:
    """Take the token out of ``environ``. A malformed value still turns the
    gate on (it fails closed: nothing can match it)."""
    global _configured, _token
    value = environ.pop(ENV, None)
    if value is None:
        return
    _configured = True
    if _VALID.fullmatch(value):
        _token = value
    else:
        _token = None
        logger.error("Ignoring a malformed desktop listener token; only /health will answer on this listener")


def configure(token: Optional[str]) -> None:
    """Set or clear the token directly (tests, and nothing else)."""
    global _configured, _token
    _configured = token is not None
    _token = token if token is not None and _VALID.fullmatch(token) else None


def configured() -> bool:
    """Whether this worker was launched by the desktop with a listener token."""
    return _configured


def matches(value: Optional[str]) -> bool:
    """Constant-time check of a header value against the launch token."""
    if not _configured or _token is None or not isinstance(value, str):
        return False
    return secrets.compare_digest(value.encode(), _token.encode())


load()
