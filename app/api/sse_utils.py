"""Shared helpers for Server-Sent Events (SSE) streaming endpoints.

Currently home to the client-disconnect classifier used by every SSE generator
so a normal disconnect ends the stream cleanly instead of collapsing through the
``BaseHTTPMiddleware`` stack as an unhandled 500 (issue #627).
"""

from __future__ import annotations

import asyncio

import anyio
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Send


def is_client_disconnect(exc: BaseException) -> bool:
    """True when ``exc`` represents a normal SSE client disconnect.

    On client disconnect (tab close, navigation, network blip) Starlette
    cancels the streaming task, throwing ``CancelledError`` into this generator
    at its current ``await``. Because the app wraps every request in a stack of
    ``BaseHTTPMiddleware`` (CSRF, access-log, ...), a cancellation
    that escapes the generator collapses into an anyio ``ExceptionGroup`` that
    surfaces as an unhandled 500 instead of a clean connection close (issue
    #627). This recognizes both the plain form and that group-wrapped form so
    the caller can swallow it — while any group carrying a *real* error is left
    to propagate.
    """
    if isinstance(exc, (asyncio.CancelledError, ClientDisconnect)):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(
            is_client_disconnect(sub) for sub in exc.exceptions
        )
    return False


class ClosingStreamingResponse(StreamingResponse):
    """A stream that closes its body as soon as the response ends.

    A client that leaves while the response is blocked in ``send`` cancels the
    response between two frames, so nothing reaches the generator parked at its
    ``yield``. Left alone, its ``finally`` blocks wait for the garbage collector,
    and a chat turn holds its chat until they run. The close is shielded because
    the turn's cleanup awaits inside a scope that has already been cancelled.
    """

    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
