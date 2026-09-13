"""Transport guards shared by the native provider clients.

Two failures the per-operation httpx timeout cannot see:

* **A stream that never ends.** ``timeout=`` is per read: a provider that keeps
  emitting bytes — a degenerate generation, an upstream that streams padding —
  resets it on every chunk and the call runs until something else stops it. A
  whole-stream deadline bounds the call itself, not the gaps inside it.
* **A client that is already closed.** A host that injects its own
  ``httpx.AsyncClient`` owns its lifetime, and a client closed between calls
  (session eviction, a pool teardown) makes httpx raise a plain ``RuntimeError``
  from ``send`` — not a ``TransportError``, so every retry ladder written
  against transport failures misses it and the call dies non-retriably.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import httpx

# Generous by design: this is a runaway guard, not a latency budget. A long
# reasoning call on a slow provider must never trip it, so it sits far above
# any plausible honest call.
DEFAULT_STREAM_DEADLINE_S = 900.0

T = TypeVar("T")
log = logging.getLogger(__name__)


class StreamDiagnostics:
    """Bounded transport evidence; never retain body, URL path, or auth headers."""

    def __init__(self, provider: str, url: str, model: str, attempt: int) -> None:
        parsed = httpx.URL(url)
        self.identity = {
            "request_id": uuid.uuid4().hex,
            "provider": provider,
            "host": parsed.host,
            "port": parsed.port,
            "scheme": parsed.scheme,
            "model": model[:160],
            "attempt": attempt,
        }
        self.started = time.monotonic()
        self.response: httpx.Response | None = None
        self.headers_after: float | None = None
        self.last_line_at: float | None = None
        self.lines_received = 0

    def received_headers(self, response: httpx.Response) -> None:
        self.response = response
        self.headers_after = time.monotonic() - self.started

    async def lines(self) -> AsyncIterator[str]:
        assert self.response is not None
        async for line in self.response.aiter_lines():
            self.last_line_at = time.monotonic()
            self.lines_received += 1
            yield line

    def failure(
        self, exc: httpx.TransportError, *, yielded: bool, retrying: bool
    ) -> dict[str, Any]:
        now = time.monotonic()
        response = self.response
        evidence = {
            **self.identity,
            "exception_type": type(exc).__name__,
            "incomplete_chunked_read": "incomplete chunked read" in str(exc).lower(),
            "elapsed_seconds": round(now - self.started, 3),
            "headers_after_seconds": (
                round(self.headers_after, 3) if self.headers_after is not None else None
            ),
            "last_line_age_seconds": (
                round(now - self.last_line_at, 3) if self.last_line_at is not None else None
            ),
            "lines_received": self.lines_received,
            "chunks_delivered": yielded,
            "retrying": retrying,
            "status": response.status_code if response is not None else None,
            "http_version": response.http_version if response is not None else None,
            "bytes_received": response.num_bytes_downloaded if response is not None else 0,
        }
        # Correlation headers only. Reject arbitrary text and cap their length;
        # never include cookies, authorization, response bodies, or exception text.
        if response is not None:
            evidence["response_ids"] = {
                key: value for key in ("x-request-id", "request-id", "cf-ray", "x-amzn-trace-id")
                if (value := response.headers.get(key))
                and re.fullmatch(r"[A-Za-z0-9_.:;=/-]{1,160}", value)
            }
        chain = []
        current: BaseException | None = exc
        while current is not None and len(chain) < 5:
            chain.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        evidence["exception_chain"] = chain
        log.warning("Provider transport failure: %s", json.dumps(evidence, sort_keys=True))
        return evidence


class StreamDeadlineExceeded(Exception):
    """The whole-stream wall-clock budget for one model call ran out."""

    def __init__(self, budget: float) -> None:
        super().__init__(
            f"provider stream exceeded its {budget:g}s whole-stream deadline "
            "and was abandoned mid-response; the per-read timeout never fired "
            "because data kept arriving"
        )
        self.budget = budget


def is_closed_client_error(exc: BaseException) -> bool:
    """True for httpx's "the client has been closed" ``RuntimeError``.

    httpx raises a bare ``RuntimeError`` when a request is sent on a client
    that has been ``aclose()``d, and another when a stream is opened after its
    response closed. Neither is a ``TransportError``; both mean the transport
    is gone and a fresh one would work.
    """
    return isinstance(exc, RuntimeError) and "closed" in str(exc).lower()


def is_usable_client(client: Any) -> bool:
    """True when an httpx client can still send. Unknown objects count as usable
    (test doubles and wrappers need not implement ``is_closed``)."""
    return client is not None and not getattr(client, "is_closed", False)


async def with_deadline(
    source: AsyncIterator[T], budget: float | None
) -> AsyncIterator[T]:
    """Yield from ``source`` until ``budget`` seconds have elapsed in total.

    Raises :class:`StreamDeadlineExceeded` when the budget runs out, whether
    the stream was mid-read or between reads. ``None`` disables the guard.
    """
    if budget is None or budget <= 0:
        async for item in source:
            yield item
        return
    deadline = time.monotonic() + budget
    iterator = source.__aiter__()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StreamDeadlineExceeded(budget)
            try:
                item = await asyncio.wait_for(iterator.__anext__(), remaining)
            except StopAsyncIteration:
                return
            except (asyncio.TimeoutError, TimeoutError):
                raise StreamDeadlineExceeded(budget) from None
            yield item
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 — nothing useful to do while unwinding
                pass


def deadline_error_payload(exc: StreamDeadlineExceeded) -> dict[str, Any]:
    """The ``error`` StreamChunk payload for a blown deadline.

    Retriable: a stream that ran this long produced nothing usable, and the
    same request issued again normally completes. The engine decides whether
    to spend another call; nothing here loops on its own.
    """
    return {"message": str(exc), "retriable": True}
