"""OpenRouter chat/completions client (API-key auth).

httpx streaming POST, same shape as ``AnthropicClient``. OpenRouter speaks the
OpenAI chat/completions wire format at a custom ``base_url``; the api key comes
from the arg or ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

import httpx

from fluid.providers._retry import RETRIABLE_STATUSES, RetryPolicy
from fluid.providers._sse import sse_events
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.openrouter.serialize import (
    ChatCompletionsNormalizer,
    build_request_body,
)

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    name = "openrouter"
    # OpenAI-compatible: no explicit cache breakpoints, no stateful reasoning, no
    # signed thinking; parallel tool calls are supported.
    caps = ProviderCaps(parallel_tool_calls=True)

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = API_URL,
        timeout: float = 600.0,
        http_client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url
        self._timeout = timeout
        self._http = http_client
        self._retry = retry or RetryPolicy()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
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
                                {
                                    "status": resp.status_code,
                                    "message": text[:2000],
                                    "retriable": retriable,
                                },
                            )
                            return
                        normalizer = ChatCompletionsNormalizer()
                        async for event_name, data in sse_events(resp.aiter_lines()):
                            for chunk in normalizer.consume(event_name, data):
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
