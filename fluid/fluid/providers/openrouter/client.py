"""OpenRouter chat/completions client (API-key auth).

httpx streaming POST, same shape as ``AnthropicClient``. OpenRouter speaks the
OpenAI chat/completions wire format at a custom ``base_url``; the api key comes
from the arg or ``OPENROUTER_API_KEY``. The streaming/retry loop is the shared
``stream_chat_completions`` from ``fluid.providers._chat_completions``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx

from fluid.providers._chat_completions import stream_chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.openrouter.serialize import build_request_body

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient:
    name = "openrouter"
    # OpenAI-compatible: no stateful reasoning, no signed thinking; parallel
    # tool calls are supported. Explicit cache breakpoints are advertised
    # because OpenRouter forwards Anthropic-style ``cache_control`` markers in
    # message content parts (limit 4); the serializer only places them for
    # models that honor them (``anthropic/`` prefix) and drops the budget
    # elsewhere, where providers cache automatically or not at all.
    caps = ProviderCaps(
        explicit_cache_breakpoints=True,
        max_cache_breakpoints=4,
        parallel_tool_calls=True,
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
        async for chunk in stream_chat_completions(
            self._base_url,
            body,
            headers,
            http_client=self._http,
            timeout=self._timeout,
            retry=self._retry,
        ):
            yield chunk
