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
        pricing: dict[str, dict[str, float]] | None = None,
        allow_fallbacks: bool = False,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url
        self._timeout = timeout
        self._http = http_client
        # OpenRouter already performs provider fallback before the first token.
        # A client-side retry after an ambiguous transport failure can create a
        # second billable generation with no idempotency guarantee, so metered
        # requests do not retry by default. Tests/embedders may opt in explicitly.
        self._retry = retry or RetryPolicy(max_retries=0)
        self._pricing = dict(pricing or {})
        # Serialized as ``provider.allow_fallbacks``. Off by default so one
        # logical call stays bound to one upstream generation; set it when the
        # embedder prefers OpenRouter's silent failover to visible failures.
        self._allow_fallbacks = allow_fallbacks

    def estimate_input_cost(self, model: str, input_tokens: int) -> float | None:
        row = self._pricing.get(model)
        if not row or "prompt" not in row:
            return None
        return max(input_tokens, 0) * row["prompt"]

    def estimate_cost(self, model: str, usage: dict) -> float | None:
        row = self._pricing.get(model)
        if not row:
            return None
        prompt = float(row.get("prompt", 0.0))
        completion = float(row.get("completion", 0.0))
        cache_read = float(row.get("input_cache_read", prompt))
        cache_write = float(row.get("input_cache_write", prompt))
        return (
            float(usage.get("input_tokens", 0) or 0) * prompt
            + float(usage.get("cache_read_input_tokens", 0) or 0) * cache_read
            + float(usage.get("cache_creation_input_tokens", 0) or 0) * cache_write
            + float(usage.get("output_tokens", 0) or 0) * completion
        )

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        body = build_request_body(request, allow_fallbacks=self._allow_fallbacks)
        async for chunk in stream_chat_completions(
            self._base_url,
            body,
            headers,
            http_client=self._http,
            timeout=self._timeout,
            retry=self._retry,
            track_provider_requests=True,
        ):
            yield chunk
