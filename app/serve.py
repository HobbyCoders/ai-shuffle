"""Run local and managed-remote ingress with one application lifespan.

The remote socket is always loopback-only and never grants local authority.
HTTP headers cannot select an ingress: the ASGI server supplies the actual
listening socket address, and proxy-header rewriting is disabled.
"""
from __future__ import annotations

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


class IngressApplication:
    def __init__(self, app, local_port: int, remote_port: int = REMOTE_PORT):
        if local_port == remote_port:
            raise ValueError("Local and remote listener ports must be different")
        self.app = app
        self.local_port = local_port
        self.remote_port = remote_port

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"}:
            scope = dict(scope)
            server = scope.get("server")
            port = server[1] if server else None
            # Unknown sockets are deliberately treated as remote.
            scope["shuffle.ingress"] = "local" if port == self.local_port else "remote"
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
        config = uvicorn.Config(
            IngressApplication(app, local.getsockname()[1], remote.getsockname()[1]),
            host=settings.host,
            port=settings.port,
            proxy_headers=False,
            log_level=settings.log_level.lower(),
            access_log=False,
            ws_max_size=10 * 1024 * 1024,
            ws_ping_interval=None,
            ws_ping_timeout=None,
        )
        uvicorn.Server(config).run(sockets=[local, remote])
