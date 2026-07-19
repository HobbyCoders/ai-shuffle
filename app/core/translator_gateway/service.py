"""The translator gateway as a standalone loopback HTTP service (Chunk A, S1).

The gateway runs as a SEPARATE uvicorn server — NOT mounted on the main app —
bound to ``127.0.0.1`` ONLY, on ``settings.translator_gateway_port`` (env
``TRANSLATOR_GATEWAY_PORT``, default 8788). It is started and stopped as an
asyncio task in the main app's lifespan (see ``app/main.py``).

Loopback-only is a hard invariant: the host is hardcoded ``127.0.0.1`` here and
nowhere is it configurable, so the gateway can never bind a non-loopback
interface. It carries real upstream credentials in-process; it must never be
reachable off-host.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI

from app.core.config import settings
from app.core.translator_gateway.router import router as gateway_router

logger = logging.getLogger(__name__)

# Hard invariant — the gateway carries real upstream credentials and must never
# be reachable off-host. Do NOT make this configurable.
GATEWAY_HOST = "127.0.0.1"


def build_gateway_app() -> FastAPI:
    """Construct the standalone gateway FastAPI app (no docs, no middleware)."""
    app = FastAPI(
        title="AI Shuffle Translator Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(gateway_router)
    return app


class GatewayServer:
    """Owns the loopback uvicorn server and its serve task."""

    def __init__(self, port: Optional[int] = None) -> None:
        self._port = int(port if port is not None else settings.translator_gateway_port)
        self._server = None  # type: ignore[assignment]
        self._task: Optional[asyncio.Task] = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        """Start the loopback uvicorn server as a background asyncio task."""
        if self._task is not None:
            return
        import uvicorn

        config = uvicorn.Config(
            build_gateway_app(),
            host=GATEWAY_HOST,  # loopback only — never configurable
            port=self._port,
            log_level="warning",
            access_log=False,
            # We manage the process lifecycle ourselves via the main app lifespan.
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        # uvicorn installs signal handlers by default, which is invalid off the
        # main thread and would clobber the host app's handlers; disable them.
        self._server.install_signal_handlers = lambda: None  # type: ignore[assignment]
        self._task = asyncio.create_task(
            self._server.serve(), name="translator-gateway"
        )
        logger.info(
            "translator gateway listening on http://%s:%d", GATEWAY_HOST, self._port
        )

    async def stop(self) -> None:
        """Ask the server to exit and await the serve task."""
        if self._server is not None:
            self._server.should_exit = True
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self._server = None


# Process-wide handle so the lifespan can start/stop exactly one instance.
_gateway: Optional[GatewayServer] = None


async def start_gateway() -> Optional[GatewayServer]:
    """Start the singleton gateway server (idempotent). Never raises — a gateway
    failure must not block app boot; provider spawns will surface the error."""
    global _gateway
    if _gateway is not None:
        return _gateway
    server = GatewayServer()
    try:
        await server.start()
    except Exception:  # noqa: BLE001 — non-fatal to app boot
        logger.exception("translator gateway failed to start")
        return None
    _gateway = server
    return _gateway


async def stop_gateway() -> None:
    """Stop the singleton gateway server if running (idempotent)."""
    global _gateway
    if _gateway is None:
        return
    try:
        await _gateway.stop()
    except Exception:  # noqa: BLE001
        logger.exception("translator gateway failed to stop cleanly")
    finally:
        _gateway = None
