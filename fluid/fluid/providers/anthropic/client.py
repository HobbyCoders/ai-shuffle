"""Anthropic Messages API client (API-key auth only).

Subscription OAuth reuse is deliberately unsupported: Anthropic bans and
technically blocks third-party use of Pro/Max OAuth tokens. Claude
subscription traffic belongs to the delegated engine (official ``claude`` CLI
subprocess), not this client.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx

from fluid.providers._trace import trace_request

from fluid.providers._retry import RETRIABLE_STATUSES, RetryPolicy
from fluid.providers._sse import sse_events
from fluid.providers.anthropic.pricing import estimate_cost as _estimate_cost
from fluid.providers.anthropic.serialize import build_request_body, sse_to_chunk
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


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
        http_client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._base_url = base_url
        self._timeout = timeout
        self._http = http_client
        self._retry = retry or RetryPolicy()

    def estimate_cost(self, model: str, usage: dict) -> float | None:
        """Optional engine cost hook: list-price estimate for one model call."""
        return _estimate_cost(model, usage)

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        body = build_request_body(request)
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._http is None
        attempt = 0
        try:
            while True:
                yielded = False
                try:
                    trace_request("anthropic", body)
                    async with client.stream(
                        "POST", self._base_url, json=body, headers=headers
                    ) as resp:
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
                        async for event_name, data in sse_events(resp.aiter_lines()):
                            chunk = sse_to_chunk(event_name, data)
                            if chunk is not None:
                                yielded = True
                                yield chunk
                        return
                except httpx.TransportError as exc:
                    # Pre-stream connection failures retry; mid-stream ones
                    # don't (chunks were already delivered downstream).
                    if not yielded and attempt < self._retry.max_retries:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk(
                        "error",
                        {"message": f"{type(exc).__name__}: {exc}", "retriable": False},
                    )
                    return
        finally:
            if owns_client:
                await client.aclose()
