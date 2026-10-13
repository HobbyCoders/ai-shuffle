"""Close an API key's or a paired device's open sockets once it is revoked.

Sockets authenticate at connect only. ``/ws/chat`` re-validates its principal
itself (``app.api.chat_ws_handler``); every other socket an API key or a paired
device (the Android app, the mobile shell) can open (session and global sync,
the browser panel, voice audio, streaming transcription) wraps its serving
loop in :func:`watch_api_key`, which re-validates the principal every
``RECHECK_SECONDS`` and at once when the key's admin routes call
:func:`recheck` or the device's unpair routes call
:func:`close_paired_client_sockets`, and closes the socket with 4001 when the
key is disabled, rotated or deleted, or the device is unpaired or its session
token replaced.
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
PAIRED_WATCH_PREFIX = "paired:"

_watches: Dict[str, "weakref.WeakSet[_KeyWatch]"] = {}


def watch_key(principal: object) -> Optional[str]:
    """The registry key of a revocable socket principal, None for the owner.

    An API key's is its ``api_user_id`` (what the key's routes signal); a
    paired device's is ``"paired:<client id>"``, the socket's ``user_id``.
    """
    if not isinstance(principal, dict) or not principal.get("principal_id"):
        return None
    if principal.get("kind") == "api_user":
        return principal["principal_id"]
    if principal.get("kind") == "paired":
        return PAIRED_WATCH_PREFIX + principal["principal_id"]
    return None


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
        from app.core.mobile_tool_identity import revalidate

        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=RECHECK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                await run_sync(revalidate, self.principal)
            except PermissionError as exc:
                await self._close(str(exc))
                return
            except Exception:  # noqa: BLE001 - a DB hiccup must not end a live socket
                logger.warning("Socket principal re-check failed; retrying", exc_info=True)

    async def _close(self, reason: str) -> None:
        self.revoked = True
        logger.info("Closing a socket of %s %s: %s", self.principal.get("kind"),
                    self.principal.get("principal_id"), reason)
        try:
            await self.websocket.close(code=REVOKED_CLOSE_CODE, reason=reason[:120])
        except Exception:  # noqa: BLE001 - already closing or closed
            pass
        if self.on_revoke is not None:
            try:
                await self.on_revoke()
            except Exception:  # noqa: BLE001
                logger.warning("Socket clean-up after revocation failed", exc_info=True)


def recheck(api_user_id: str) -> int:
    """Make every watched socket of ``api_user_id`` (a :func:`watch_key`) re-validate now."""
    watches = list(_watches.get(api_user_id, ()))
    for watch in watches:
        watch.signal()
    return len(watches)


def recheck_paired_client(client_id: str) -> int:
    """Make every watched socket of paired device ``client_id`` re-validate now."""
    return recheck(PAIRED_WATCH_PREFIX + client_id)


def close_paired_client_sockets(client_id: str) -> None:
    """After an unpair: every socket the device holds re-checks now and closes with 4001.

    The chat sockets (``chat_ws_handler``) and every watched socket. Without
    this an idle socket would only notice at its periodic re-check. Never
    raises: the unpair is committed and the periodic re-check still runs.
    """
    from app.api.chat_ws_handler import recheck_paired_client_sockets
    for signal in (recheck_paired_client_sockets, recheck_paired_client):
        try:
            signal(client_id)
        except Exception:  # noqa: BLE001
            logger.warning("Could not signal the paired device's open sockets", exc_info=True)


@contextlib.asynccontextmanager
async def watch_api_key(
    websocket: WebSocket,
    *,
    on_revoke: Optional[Callable[[], Awaitable[None]]] = None,
) -> AsyncIterator[None]:
    """Close ``websocket`` with 4001 once its API key or paired device is revoked.

    A no-op for the owner.

    ``on_revoke`` runs after the close, for sockets that hold more than the
    socket (an upstream provider connection).
    """
    principal = getattr(websocket.state, "mobile_tool_principal", None)
    key = watch_key(principal)
    if key is None:
        yield
        return
    watch = _KeyWatch(websocket, principal, on_revoke)
    _watches.setdefault(key, weakref.WeakSet()).add(watch)
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
        watches = _watches.get(key)
        if watches is not None:
            watches.discard(watch)
            if not watches:
                _watches.pop(key, None)
