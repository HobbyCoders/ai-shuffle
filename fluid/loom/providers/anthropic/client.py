"""Anthropic Messages API client (API-key auth only).

Subscription OAuth reuse is deliberately unsupported: Anthropic bans and
technically blocks third-party use of Pro/Max OAuth tokens. Claude
subscription traffic belongs to the delegated engine (official ``claude`` CLI
subprocess), not this client.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import httpx

from loom.providers._sse import sse_events
from loom.providers.anthropic.serialize import build_request_body, sse_to_chunk
from loom.providers.base import ProviderCaps, ProviderRequest, StreamChunk

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
    ) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._base_url = base_url
        self._timeout = timeout
        self._http = http_client

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
        try:
            async with client.stream("POST", self._base_url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode(errors="replace")
                    yield StreamChunk(
                        "error",
                        {"status": resp.status_code, "message": text[:2000],
                         "retriable": resp.status_code in (429, 500, 502, 503, 529)},
                    )
                    return
                async for event_name, data in sse_events(resp.aiter_lines()):
                    chunk = sse_to_chunk(event_name, data)
                    if chunk is not None:
                        yield chunk
        finally:
            if owns_client:
                await client.aclose()
