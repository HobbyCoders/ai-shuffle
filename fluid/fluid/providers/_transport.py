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
import contextlib
import html
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import httpx

from fluid.providers._retry import (
    BILLING_STATUS,
    RETRIABLE_STATUSES,
    billing_error_code,
)

# Generous by design: this is a runaway guard, not a latency budget. A long
# reasoning call on a slow provider must never trip it, so it sits far above
# any plausible honest call.
DEFAULT_STREAM_DEADLINE_S = 900.0

# How long a call may go without its first byte (response headers, then the
# first SSE event) before it is abandoned (retried only when no headers came).
# The per-read ``timeout`` is sized for gaps inside a long generation, so a
# provider that accepts the connection and then says nothing would otherwise
# hold the turn for the full read timeout (600 s) before anything happened.
DEFAULT_FIRST_BYTE_TIMEOUT_S = 60.0

# Longest provider explanation carried on an error (see provider_error_summary).
PROVIDER_MESSAGE_LIMIT = 300

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


class FirstByteTimeout(httpx.ReadTimeout):
    """No byte of the response arrived within the first-byte budget.

    A ``ReadTimeout`` so every client's transport handler treats it as one: a
    failure before any content. It is retried on the transport budget only
    while the response headers have not arrived; after a 200 the provider has
    accepted (and may bill) the call, so the Anthropic client reports it.
    """

    def __init__(self, budget: float) -> None:
        super().__init__(
            f"provider sent no response within {budget:g}s (first-byte timeout)"
        )
        self.budget = budget


async def open_stream(
    scope: contextlib.AsyncExitStack,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    first_byte_timeout: float | None,
    **kwargs: Any,
) -> httpx.Response:
    """``client.stream(...)`` entered on ``scope``, with the header wait bounded.

    Raises :class:`FirstByteTimeout` when the response headers do not arrive
    within ``first_byte_timeout`` seconds (``None`` or 0 disables the bound).
    The body is read by the caller under ``scope``.
    """
    manager = client.stream(method, url, **kwargs)
    if not first_byte_timeout or first_byte_timeout <= 0:
        return await scope.enter_async_context(manager)
    try:
        async with asyncio.timeout(first_byte_timeout):
            return await scope.enter_async_context(manager)
    except TimeoutError as exc:
        raise FirstByteTimeout(first_byte_timeout) from exc


_HTML_DROP = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
_HTML_TAG = re.compile(r"(?s)<[^>]*>")


def _error_text(value: Any) -> str | None:
    """The human-readable message inside a provider's JSON error body."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list) and value:
        return _error_text(value[0])
    if not isinstance(value, dict):
        return None
    for key in ("message", "error_description", "detail", "error", "msg"):
        found = _error_text(value.get(key))
        if found:
            return found
    return None


def provider_error_summary(body: Any, limit: int = PROVIDER_MESSAGE_LIMIT) -> str | None:
    """A short, readable account of a provider error, or None when empty.

    ``body`` is a response body (text or parsed JSON) or an error object. A
    JSON body yields its ``error.message`` (or the nearest equivalent); an
    HTML page (a gateway's 502, a Cloudflare challenge) is reduced to its
    text; anything else is whitespace-collapsed. Capped at ``limit`` chars.
    """
    if body is None:
        return None
    text: str | None
    if isinstance(body, (dict, list)):
        text = _error_text(body)
    else:
        raw = str(body)
        text = None
        stripped = raw.strip()
        if stripped[:1] in ("{", "["):
            try:
                text = _error_text(json.loads(stripped))
            except ValueError:
                text = None
        if text is None:
            text = raw
    if not text:
        return None
    if "<" in text and ">" in text:
        text = _HTML_TAG.sub(" ", _HTML_DROP.sub(" ", text))
        text = html.unescape(text)
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def billing_summary(code: str | None, detail: str | None) -> str | None:
    """The short explanation carried on a billing refusal (see
    fluid.providers._retry.BILLING_ERROR_CODES): says it is about credit, not
    load, then the provider's own words."""
    label = f" ({code})" if code else ""
    return provider_error_summary(
        f"Out of credit or quota{label}: {detail or 'the provider gave no detail'}"
    )


def refusal_payload(status: int, text: str) -> dict[str, Any]:
    """The ``error`` chunk payload for a non-200 response.

    Whether it is retried follows the status, except for a billing refusal:
    an ``insufficient_quota`` 429 (or any 402) is reported as 402 and is never
    retried, because no wait clears it. Only a billing code in the body is
    called "out of credit"; a bare 402 carries the provider's explanation.
    """
    summary = provider_error_summary(text)
    code = billing_error_code(text)
    if code is None and status != BILLING_STATUS:
        return {
            "status": status,
            "message": text[:2000],
            "provider_message": summary,
            "retriable": status in RETRIABLE_STATUSES,
        }
    if code is None:
        # A bare 402 is not always about credit: OpenRouter sends one when the
        # request asks for more ``max_tokens`` than the balance covers, which
        # a smaller request fixes. Its own words say which.
        return {
            "status": BILLING_STATUS,
            "message": (
                "the provider refused the request with HTTP 402 (Payment "
                "Required), so it was not retried. " + text[:2000]
            ),
            "provider_message": summary,
            "retriable": False,
        }
    payload: dict[str, Any] = {
        "status": BILLING_STATUS,
        "message": (
            f"the provider refused the request with HTTP {status}: the account "
            "is out of credit or quota, so it was not retried. " + text[:2000]
        ),
        "provider_message": billing_summary(code, summary),
        "retriable": False,
    }
    if code is not None:
        payload["code"] = code
    if status != BILLING_STATUS:
        payload["http_status"] = status
    return payload


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
    source: AsyncIterator[T],
    budget: float | None,
    *,
    first_item_timeout: float | None = None,
    first_byte_budget: float | None = None,
) -> AsyncIterator[T]:
    """Yield from ``source`` until ``budget`` seconds have elapsed in total.

    Raises :class:`StreamDeadlineExceeded` when the budget runs out, whether
    the stream was mid-read or between reads. ``None`` disables the guard.

    ``first_item_timeout`` bounds the wait for the FIRST item only and raises
    :class:`FirstByteTimeout` (naming ``first_byte_budget``, the caller's whole
    first-byte allowance, when given) — the tail of a first-byte budget whose
    head was spent waiting for the response headers.
    """
    has_budget = budget is not None and budget > 0
    first_wait = first_item_timeout if first_item_timeout and first_item_timeout > 0 else None
    if not has_budget and first_wait is None:
        async for item in source:
            yield item
        return
    deadline = time.monotonic() + budget if has_budget else None
    iterator = source.__aiter__()
    try:
        while True:
            wait: float | None = None
            deadline_bound = False
            if deadline is not None:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise StreamDeadlineExceeded(budget)  # type: ignore[arg-type]
                deadline_bound = True
            if first_wait is not None and (wait is None or first_wait < wait):
                wait = first_wait
                deadline_bound = False
            try:
                if wait is None:
                    item = await iterator.__anext__()
                else:
                    item = await asyncio.wait_for(iterator.__anext__(), wait)
            except StopAsyncIteration:
                return
            except (asyncio.TimeoutError, TimeoutError):
                if deadline_bound:
                    raise StreamDeadlineExceeded(budget) from None  # type: ignore[arg-type]
                raise FirstByteTimeout(first_byte_budget or first_wait) from None  # type: ignore[arg-type]
            first_wait = None
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


def truncated_stream_payload(terminal: str) -> dict[str, Any]:
    """The ``error`` StreamChunk payload for a stream that ended without its
    terminal event.

    A socket closed cleanly mid-response (a proxy or gateway giving up on its
    upstream, an HTTP/1.0 hop) reads as an ordinary end of body: no transport
    error is raised, and a client that simply returned would hand the engine a
    cut-off reply as a finished one, or a tool call whose arguments never
    closed. The wording carries the same "terminal usage chunk" marker the
    chat/completions client uses, so the engine treats both alike: a stream
    that delivered nothing is retried as an empty completion, one that
    delivered part of a reply ends the turn on this error.
    """
    return {
        "message": (
            "stream ended before the provider sent a terminal usage chunk "
            f"({terminal} never arrived: empty or truncated response)"
        ),
        "retriable": False,
    }
