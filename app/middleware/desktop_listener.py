"""The worker side of the desktop app's per-launch listener token.

When the desktop app started this worker with a listener token
(``app.core.desktop_listener``), every HTTP request and WebSocket on the local
listener must carry it in ``X-Shuffle-Desktop-Listener``. Other local
programs, and other users of a shared machine, reach loopback too; without the
token they get a 403 for everything except:

- ``/health`` (and ``/health/...``, ``/api/v1/health``), which the desktop
  probes before it knows whether the worker is its own;
- routes that check their own one-time or per-session token: the desktop's
  browser handoff, the agent browser's preview door, and the agent browser's
  panel callbacks and RPC (validated by their per-session panel token);
- requests carrying a credential that stands on its own: an owner session
  (the browser opened through the handoff), a paired device session, an API
  key, or the agent browser's preview cookie. Such a request is then judged by
  that credential alone; plain loopback confers nothing without the token.

The protected remote listener is not affected. Workers started without a token
(Docker, a manual run) are not affected either.
"""
from __future__ import annotations

import hashlib
import json
import logging

from starlette.requests import HTTPConnection

from app.core import desktop_listener

logger = logging.getLogger(__name__)

_OPEN_PATHS = frozenset({
    "/health", "/api/v1/health",
    "/api/v1/owner/handoff",
    "/api/v1/auth/preview-access",
    "/api/v1/browser/panel/register",
    # The agent proxy has only a session panel token, never the desktop token.
    # This exact route checks that token and session before allocating a runtime.
    "/api/v1/browser/panel/rpc",
})
_OPEN_PREFIXES = ("/health/", "/api/v1/browser/panel/control/")


def _open_path(path: str) -> bool:
    return path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)


def _standalone_credential(conn: HTTPConnection) -> bool:
    """A credential that authorizes on its own, validated, never loopback."""
    from app.core import owner_auth, preview_access
    from app.db import database
    from app.api.auth import get_paired_client_from_token
    if preview_access.grant(conn) is not None:
        return True
    candidates = [conn.cookies.get("session")]
    authorization = conn.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        candidates.append(authorization[7:])
    if conn.scope.get("type") == "websocket":
        candidates.append(conn.query_params.get("token"))
    for token in candidates:
        if not token:
            continue
        if owner_auth.local_session(token) is not None:
            return True
        if get_paired_client_from_token(token, touch=False):
            return True
        user = database.get_api_user_by_key_hash(hashlib.sha256(token.encode()).hexdigest())
        if user and user.get("is_active", True):
            return True
    return False


class DesktopListenerGate:
    """Pure ASGI, so it covers WebSocket handshakes as well as HTTP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") not in ("http", "websocket") or not desktop_listener.configured()
                or scope.get("shuffle.ingress") != "local"):
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        if desktop_listener.matches(conn.headers.get(desktop_listener.HEADER)) or _open_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        from app.db.async_database import run_sync
        try:
            allowed = await run_sync(_standalone_credential, conn)
        except Exception:
            logger.warning("Desktop listener gate could not check a credential", exc_info=True)
            allowed = False
        if allowed:
            await self.app(scope, receive, send)
            return
        await _deny(scope, receive, send)


async def _deny(scope, receive, send):
    if scope["type"] == "websocket":
        # Closing before accept makes the server answer the handshake with 403.
        await receive()
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": "Desktop listener token required"}).encode()
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
