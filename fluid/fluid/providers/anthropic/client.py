"""Anthropic Messages API client (API-key auth only).

Subscription OAuth reuse is deliberately unsupported: Anthropic bans and
technically blocks third-party use of Pro/Max OAuth tokens. Claude
subscription traffic belongs to the delegated engine (official ``claude`` CLI
subprocess), not this client.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator

import httpx

from fluid.providers._trace import trace_request

from fluid.providers._retry import RETRIABLE_STATUSES, RetryPolicy
from fluid.providers._sse import sse_events
from fluid.providers._transport import (
    DEFAULT_STREAM_DEADLINE_S,
    StreamDiagnostics,
    StreamDeadlineExceeded,
    deadline_error_payload,
    is_closed_client_error,
    is_usable_client,
    truncated_stream_payload,
    with_deadline,
)
from fluid.providers.anthropic.pricing import estimate_cost as _estimate_cost
from fluid.providers.anthropic.serialize import build_request_body, sse_to_chunk
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

log = logging.getLogger(__name__)


class AnthropicClient:
    name = "anthropic"
    caps = ProviderCaps(
        explicit_cache_breakpoints=True,
        max_cache_breakpoints=4,
        signed_thinking=True,
        server_tools=True,
    )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = API_URL,
        timeout: float = 600.0,
        stream_deadline: float | None = DEFAULT_STREAM_DEADLINE_S,
        http_client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._base_url = base_url
        self._timeout = timeout
        # Wall clock for one whole model call, above the per-read ``timeout``.
        # None disables it. See fluid.providers._transport.
        self._stream_deadline = stream_deadline
        self._http = http_client
        self._retry = retry or RetryPolicy()

    def estimate_cost(self, model: str, usage: dict) -> float | None:
        """Optional engine cost hook: list-price estimate for one model call."""
        return _estimate_cost(model, usage)

    def _pick_client(
        self, owned: httpx.AsyncClient | None
    ) -> tuple[httpx.AsyncClient, httpx.AsyncClient | None]:
        """The client for the next attempt, plus the one this call owns.

        A host-injected client may have been closed between calls; a closed
        client can never send again, so it is dropped in favor of a per-call
        one (see OpenAIResponsesClient._pick_client).
        """
        if is_usable_client(self._http):
            assert self._http is not None
            return self._http, owned
        if self._http is not None:
            log.warning(
                "injected http client is closed; falling back to a per-call "
                "client for anthropic"
            )
            self._http = None
        if not is_usable_client(owned):
            owned = httpx.AsyncClient(timeout=self._timeout)
        assert owned is not None
        return owned, owned

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        body = build_request_body(request)
        owned: httpx.AsyncClient | None = None
        attempt = 0
        try:
            while True:
                yielded = False
                client, owned = self._pick_client(owned)
                diagnostics = StreamDiagnostics(
                    self.name, self._base_url, request.model, attempt + 1
                )
                try:
                    trace_request("anthropic", body)
                    async with client.stream(
                        "POST", self._base_url, json=body, headers=headers
                    ) as resp:
                        diagnostics.received_headers(resp)
                        if resp.status_code != 200:
                            text = (await resp.aread()).decode(errors="replace")
                            retriable = resp.status_code in RETRIABLE_STATUSES
                            if retriable and attempt < self._retry.max_retries:
                                await asyncio.sleep(self._retry.delay(
                                    attempt, resp.headers.get("retry-after")))
                                attempt += 1
                                continue
                            yield StreamChunk(
                                "error",
                                {"status": resp.status_code, "message": text[:2000],
                                 "retriable": retriable},
                            )
                            return
                        # message_start is held until the first chunk after
                        # it, so an overloaded_error sent in place of content
                        # can be retried before the engine sees a message
                        # begin. Once content has flowed, an error surfaces.
                        pending_start: StreamChunk | None = None
                        retry_error = False
                        # Set once message_stop or an error reached the
                        # engine; a body that ends before either was cut off.
                        terminal = False
                        events = with_deadline(
                            sse_events(diagnostics.lines()), self._stream_deadline
                        )
                        async for event_name, data in events:
                            chunk = sse_to_chunk(event_name, data)
                            if chunk is None:
                                continue
                            if chunk.kind == "message_start" and not yielded:
                                pending_start = chunk
                                continue
                            if (
                                chunk.kind == "error"
                                and not yielded
                                and chunk.payload.get("retriable")
                                and attempt < self._retry.max_retries
                            ):
                                retry_error = True
                                break
                            if pending_start is not None:
                                yielded = True
                                yield pending_start
                                pending_start = None
                            yielded = True
                            if chunk.kind in ("message_stop", "error"):
                                terminal = True
                            yield chunk
                        if retry_error:
                            await asyncio.sleep(self._retry.delay(attempt))
                            attempt += 1
                            continue
                        if pending_start is not None:
                            yielded = True
                            yield pending_start
                        if not terminal:
                            yield StreamChunk(
                                "error", truncated_stream_payload("message_stop")
                            )
                        return
                except StreamDeadlineExceeded as exc:
                    yield StreamChunk("error", deadline_error_payload(exc))
                    return
                except RuntimeError as exc:
                    # A send on a closed injected client: nothing left the
                    # process, so this is a pre-stream failure and the next
                    # attempt gets a live client from _pick_client.
                    if not is_closed_client_error(exc):
                        raise
                    self._http = None
                    if not yielded and attempt < self._retry.transport_retries:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk(
                        "error",
                        {"message": f"{type(exc).__name__}: {exc}", "retriable": True},
                    )
                    return
                except httpx.TransportError as exc:
                    evidence = diagnostics.failure(
                        exc, yielded=yielded,
                        retrying=not yielded and attempt < self._retry.transport_retries,
                    )
                    # Pre-stream connection failures retry; mid-stream ones
                    # don't (chunks were already delivered downstream).
                    if not yielded and attempt < self._retry.transport_retries:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk(
                        "error",
                        {
                            "message": f"{type(exc).__name__}: {exc}",
                            "retriable": False,
                            "transport_failure": evidence,
                        },
                    )
                    return
        finally:
            if owned is not None:
                await owned.aclose()
