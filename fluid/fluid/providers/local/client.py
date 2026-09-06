"""Local OpenAI-compatible chat/completions client (llama.cpp, Ollama, …).

Tuned for local servers:

* ``api_key`` defaults to ``"local"`` — llama-server and Ollama ignore the
  Bearer header, but it is always sent so a llama-server started with
  ``--api-key`` (or any auth-fronted proxy) works without special-casing.
* ``base_url`` accepts a server origin (``http://127.0.0.1:8080``), a ``/v1``
  prefix, or the full ``…/chat/completions`` endpoint — see
  :func:`fluid.providers.local.serialize.normalize_base_url` for the rule.
* Long default timeout (600s): local prompt processing on big contexts can
  stall far longer than a hosted API would.
* ``extra`` — standing server-specific knobs (llama.cpp ``cache_prompt``,
  samplers, Ollama ``options``, …) merged into every request body; a
  per-request ``ProviderRequest.extra`` merges after it and wins. The config
  layer feeds ``[providers.<name>.extra]`` here.
* No cost estimation — local tokens are free.

Usage parsing (prompt_tokens / completion_tokens /
prompt_tokens_details.cached_tokens, all degrading to 0) rides the shared
chat/completions normalizer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from fluid.providers._chat_completions import stream_chat_completions
from fluid.providers._retry import RetryPolicy
from fluid.providers._transport import DEFAULT_STREAM_DEADLINE_S
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.local.serialize import build_request_body, normalize_base_url


class LocalClient:
    name = "local"
    # Plain OpenAI-compatible surface: no explicit cache breakpoints (llama.cpp
    # caches prompt prefixes automatically; the append-only prompt discipline is
    # what makes that work), no stateful reasoning, no signed thinking.
    caps = ProviderCaps(parallel_tool_calls=True)

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str,
        timeout: float = 600.0,
        stream_deadline: float | None = DEFAULT_STREAM_DEADLINE_S,
        http_client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._api_key = api_key or "local"
        self._url = normalize_base_url(base_url)
        self._timeout = timeout
        # Wall clock for one whole model call, above the per-read ``timeout``.
        self._stream_deadline = stream_deadline
        self._http = http_client
        self._retry = retry or RetryPolicy()
        self._extra = dict(extra) if extra else {}

    @property
    def url(self) -> str:
        """The resolved chat/completions endpoint (post-normalization)."""
        return self._url

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        body = build_request_body(request, extra=self._extra)
        async for chunk in stream_chat_completions(
            self._url,
            body,
            headers,
            http_client=self._http,
            timeout=self._timeout,
            retry=self._retry,
            stream_deadline=self._stream_deadline,
        ):
            yield chunk
