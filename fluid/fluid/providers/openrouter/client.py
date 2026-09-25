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
from fluid.providers._transport import (
    DEFAULT_FIRST_BYTE_TIMEOUT_S,
    DEFAULT_STREAM_DEADLINE_S,
    is_usable_client,
)
from fluid.providers._retry import is_retriable_status
from fluid.providers.base import ErrorClass, ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.openrouter.serialize import build_request_body

API_URL = "https://openrouter.ai/api/v1/chat/completions"


def is_openrouter_url(url: str) -> bool:
    """True when ``url`` points at OpenRouter (``openrouter.ai`` or a subdomain)."""
    try:
        host = (httpx.URL(url).host or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001 — an unparseable URL is not OpenRouter
        return False
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


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
        stream_deadline: float | None = DEFAULT_STREAM_DEADLINE_S,
        first_byte_timeout: float | None = DEFAULT_FIRST_BYTE_TIMEOUT_S,
        http_client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
        pricing: dict[str, dict[str, float]] | None = None,
        allow_fallbacks: bool = False,
        openrouter: bool | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url
        self._timeout = timeout
        # Wall clock for one whole model call, above the per-read ``timeout``.
        self._stream_deadline = stream_deadline
        # Wall clock for the headers plus the first data event; None or 0
        # disables it. Headers that never came are a pre-content transport
        # failure (retried only on the transport budget, 0 by default here);
        # a 200 followed by silence is a truncated stream. See
        # fluid.providers._chat_completions.stream_chat_completions.
        self._first_byte_timeout = first_byte_timeout
        self._http = http_client
        # A refused request (429, 5xx, or an error chunk sent in place of
        # content) started no generation, so it retries with backoff. A
        # transport failure is ambiguous: OpenRouter may have started a
        # billable generation with no idempotency guarantee, so those do not
        # retry by default. Embedders may raise that budget explicitly.
        self._retry = retry or RetryPolicy(max_retries=3, max_transport_retries=0)
        self._pricing = dict(pricing or {})
        # Serialized as ``provider.allow_fallbacks``. Off by default so one
        # logical call stays bound to one upstream generation; set it when the
        # embedder prefers OpenRouter's silent failover to visible failures.
        self._allow_fallbacks = allow_fallbacks
        # Whether the endpoint is OpenRouter, which decides whether its body
        # extensions (``usage``, ``provider``) are sent. This client is also
        # the generic chat/completions client for custom OpenAI-format rows
        # (vLLM, Groq, xAI) whose strict servers reject unknown fields. None
        # infers it from the host; a row that is OpenRouter behind a proxy
        # says so explicitly.
        self._openrouter = (
            is_openrouter_url(base_url) if openrouter is None else bool(openrouter)
        )

    def classify_error(self, error: dict) -> ErrorClass:
        """Retry class of an error; an empty completion drops ``session_id``.

        ``session_id`` pins requests to one upstream replica. When that
        replica answers empty it stays dead for longer than one call, and one
        that hangs costs a full first-data timeout per probe, so the engine
        stops sending the key for the rest of the loop.
        """
        if error.get("kind") == "empty_completion":
            return ErrorClass(retriable=True, drop_sticky=True)
        return ErrorClass(
            retriable=bool(error.get("retriable"))
            or is_retriable_status(error.get("status")),
        )

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
        body = build_request_body(
            request, allow_fallbacks=self._allow_fallbacks, openrouter=self._openrouter,
        )
        if not is_usable_client(self._http):
            # A host-injected client its owner has closed can never send again;
            # drop it here so later calls do not re-check a corpse.
            self._http = None
        async for chunk in stream_chat_completions(
            self._base_url,
            body,
            headers,
            http_client=self._http,
            timeout=self._timeout,
            retry=self._retry,
            track_provider_requests=True,
            stream_deadline=self._stream_deadline,
            first_byte_timeout=self._first_byte_timeout,
        ):
            yield chunk
