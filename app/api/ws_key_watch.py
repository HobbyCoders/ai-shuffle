"""Close an API key's open sockets once the key is disabled, rotated or deleted.

Sockets authenticate at connect only. ``/ws/chat`` re-validates its key itself
(``app.api.chat_ws_handler``); every other socket a key can open (session and
global sync, the browser panel, voice audio, streaming transcription) wraps its
serving loop in :func:`watch_api_key`, which re-validates the key every
``RECHECK_SECONDS`` and at once when the key's admin routes call
:func:`recheck`, and closes the socket with 4001 when the key is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from typing import AsyncIterator, Awaitable, Callable, Dict, Optional

from fastapi import WebSocket

from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

RECHECK_SECONDS = 30.0
REVOKED_CLOSE_CODE = 4001

_watches: Dict[str, "weakref.WeakSet[_KeyWatch]"] = {}


class _KeyWatch:
    def __init__(self, websocket: WebSocket, principal: dict,
                 on_revoke: Optional[Callable[[], Awaitable[None]]]) -> None:
        self.websocket = websocket
        self.principal = principal
        self.on_revoke = on_revoke
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.revoked = False

    def signal(self) -> None:
        """Wake the watchdog now; safe from any thread."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self.wake.set()
        elif not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.wake.set)

    async def run(self) -> None:
        from app.core.mobile_tool_identity import validate

        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=RECHECK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                await run_sync(validate, self.principal)
            except PermissionError as exc:
                await self._close(str(exc))
                return
            except Exception:  # noqa: BLE001 - a DB hiccup must not end a live socket
                logger.warning("API key re-check failed; retrying", exc_info=True)

    async def _close(self, reason: str) -> None:
        self.revoked = True
        logger.info("Closing a socket of API user %s: %s", self.principal.get("principal_id"), reason)
        try:
            await self.websocket.close(code=REVOKED_CLOSE_CODE, reason=reason[:120])
        except Exception:  # noqa: BLE001 - already closing or closed
            pass
        if self.on_revoke is not None:
            try:
                await self.on_revoke()
            except Exception:  # noqa: BLE001
                logger.warning("Socket clean-up after key revocation failed", exc_info=True)


def recheck(api_user_id: str) -> int:
    """Make every watched socket of ``api_user_id`` re-validate its key now."""
    watches = list(_watches.get(api_user_id, ()))
    for watch in watches:
        watch.signal()
    return len(watches)


@contextlib.asynccontextmanager
async def watch_api_key(
    websocket: WebSocket,
    *,
    on_revoke: Optional[Callable[[], Awaitable[None]]] = None,
) -> AsyncIterator[None]:
    """Close ``websocket`` with 4001 once its API key is revoked (no-op for other principals).

    ``on_revoke`` runs after the close, for sockets that hold more than the
    socket (an upstream provider connection).
    """
    principal = getattr(websocket.state, "mobile_tool_principal", None)
    if not isinstance(principal, dict) or principal.get("kind") != "api_user":
        yield
        return
    api_user_id = principal.get("principal_id")
    watch = _KeyWatch(websocket, principal, on_revoke)
    _watches.setdefault(api_user_id, weakref.WeakSet()).add(watch)
    task = asyncio.create_task(watch.run())
    try:
        yield
    finally:
        if watch.revoked and not task.done():
            # Let a revocation's clean-up finish (bounded), not cut it short.
            await asyncio.wait({task}, timeout=5.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        watches = _watches.get(api_user_id)
        if watches is not None:
            watches.discard(watch)
            if not watches:
                _watches.pop(api_user_id, None)
