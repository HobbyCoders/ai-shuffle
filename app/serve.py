"""Run local and managed-remote ingress with one application lifespan.

The remote socket is always loopback-only and never grants local authority.
HTTP headers cannot select an ingress: the ASGI server supplies the actual
listening socket address, and proxy-header rewriting is disabled.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
import sys
from contextlib import ExitStack


def configured_remote_port(env):
    """Only an explicit isolated desktop may move the protected listener."""
    if env.get('SHUFFLE_DESKTOP_ISOLATED') != '1':
        return 8001
    try:
        port = int(env['SHUFFLE_PROTECTED_PORT'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('An isolated desktop requires a valid protected port') from exc
    if not 1024 <= port <= 65535 or port in (8000, 8001):
        raise ValueError('An isolated protected port cannot use the normal worker ports')
    return port


REMOTE_PORT = configured_remote_port(os.environ)


def _loopback_endpoint(endpoint):
    try:
        return bool(endpoint) and ipaddress.ip_address(endpoint[0]).is_loopback
    except (ValueError, TypeError, IndexError):
        return False


def configured_preview_port(env, local_port: int, remote_port: int):
    """The agent-preview listener's port, or None when it is switched off.

    ``SHUFFLE_PREVIEW_PORT`` names it; ``0`` or ``off`` disables the listener.
    The default sits two above the local port (8002 beside 8000/8001, 18082
    beside an isolated desktop's 18080/18081) so parallel installs do not meet.
    """
    raw = str(env.get("SHUFFLE_PREVIEW_PORT", "")).strip().lower()
    if raw in ("0", "off", "false", "no"):
        return None
    if raw:
        try:
            port = int(raw)
        except ValueError as exc:
            raise ValueError("SHUFFLE_PREVIEW_PORT must be a port number, 0 or off") from exc
    else:
        port = local_port + 2
        if port == remote_port:
            port += 1
    if not 1 <= port <= 65535 or port in (local_port, remote_port):
        raise ValueError("The preview listener needs a valid port of its own")
    return port


def _cookie(scope, name: str):
    for key, value in scope.get("headers", []):
        if key.lower() == b"cookie":
            for part in value.decode("latin-1").split(";"):
                found, _, content = part.strip().partition("=")
                if found == name:
                    return content
    return None


class IngressApplication:
    """Marks each request with the listener it arrived on.

    ``local`` is the owner's LAN listener, ``remote`` the protected one behind
    the tunnel, and ``preview`` the agent browser's own door: it never confers
    local-owner trust, and nothing passes it without the session's preview
    grant (:mod:`app.core.preview_access`). The agent browser is refused the
    other two ports outright, so this is the only way it sees the running UI.
    """

    def __init__(self, app, local_port: int, remote_port: int = REMOTE_PORT, preview_port=None):
        if local_port == remote_port:
            raise ValueError("Local and remote listener ports must be different")
        if preview_port is not None and preview_port in (local_port, remote_port):
            raise ValueError("The preview listener needs a port of its own")
        self.app = app
        self.local_port = local_port
        self.remote_port = remote_port
        self.preview_port = preview_port

    async def _preview_admitted(self, scope) -> bool:
        """Only the token redirect and requests carrying a live grant pass."""
        from app.core import preview_access

        if scope["type"] == "http" and scope.get("path") == preview_access.REDIRECT_PATH \
                and scope.get("method") == "GET":
            return True
        token = _cookie(scope, preview_access.COOKIE_NAME)
        if not token:
            return False
        try:
            from app.db.async_database import run_sync

            return await run_sync(preview_access.verify, token) is not None
        except Exception:  # noqa: BLE001 - an unreadable grant is a denial
            return False

    @staticmethod
    async def _refuse(scope, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        body = b'{"detail":"This port only serves an agent preview opened with its session link."}'
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            scope = dict(scope)
            server = scope.get("server")
            port = server[1] if server else None
            # Unknown sockets are deliberately treated as remote.
            if self.preview_port is not None and port == self.preview_port:
                scope["shuffle.ingress"] = "preview"
                scope["shuffle.forwarded_https"] = False
                if not await self._preview_admitted(scope):
                    await self._refuse(scope, send)
                    return
                await self.app(scope, receive, send)
                return
            scope["shuffle.ingress"] = "local" if port == self.local_port else "remote"
            # TLS terminates at the managed tunnel (or the owner's HTTPS
            # proxy). Only its dedicated loopback listener may attest HTTPS.
            # Keep the real scheme/peer and owner authority unchanged; headers
            # on the LAN listener or unknown sockets cannot confer this flag.
            scope["shuffle.forwarded_https"] = (
                port == self.remote_port
                and _loopback_endpoint(server)
                and _loopback_endpoint(scope.get("client"))
                and [value for name, value in scope.get("headers", [])
                     if name.lower() == b"x-forwarded-proto"] == [b"https"]
            )
        await self.app(scope, receive, send)


def bind_listener(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sock.family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        sock.set_inheritable(True)
        return sock
    except BaseException:
        sock.close()
        raise


def run(app, settings) -> None:
    import uvicorn

    remote_port = int(os.environ.get("SHUFFLE_DESKTOP_PROTECTED_PORT", str(REMOTE_PORT)))
    if not 1 <= settings.port <= 65535 or not 1 <= remote_port <= 65535 or settings.port == remote_port:
        raise ValueError("Local and protected listener ports must be valid and different")
    # Bind both sockets before starting services. A collision cannot leave a
    # half-started worker exposing just one of the required ingress boundaries.
    with ExitStack() as stack:
        local = stack.enter_context(bind_listener(settings.host, settings.port))
        remote = stack.enter_context(bind_listener("127.0.0.1", remote_port))
        sockets = [local, remote]
        preview_port = None
        # The preview door is optional: a taken port costs agents their view of
        # the running UI, never the worker its two required boundaries.
        try:
            wanted = configured_preview_port(os.environ, settings.port, remote_port)
            if wanted is not None:
                preview = stack.enter_context(bind_listener(settings.host, wanted))
                preview_port = preview.getsockname()[1]
                sockets.append(preview)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "Agent preview listener is off: %s. Agents cannot open this worker's UI in their browser.", exc)
        from app.core import preview_access

        preview_access.set_listener_port(preview_port, settings.host)
        config = uvicorn.Config(
            IngressApplication(app, local.getsockname()[1], remote.getsockname()[1], preview_port),
            host=settings.host,
            port=settings.port,
            proxy_headers=False,
            log_level=settings.log_level.lower(),
            access_log=False,
            ws_max_size=10 * 1024 * 1024,
            ws_ping_interval=None,
            ws_ping_timeout=None,
        )
        uvicorn.Server(config).run(sockets=sockets)
