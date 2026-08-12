"""OpenAI Responses API client (API key OR Codex-subscription OAuth).

httpx streaming POST, same shape as ``AnthropicClient``. Two auth modes:

  * ``api_key`` (arg or ``OPENAI_API_KEY``) → ``Authorization: Bearer`` against
    ``https://api.openai.com/v1/responses`` (the default ``base_url``).
  * an ``auth`` object (the Codex OAuth hook, :class:`codex_oauth.CodexAuth`)
    implementing ``async get_access_token()`` + ``async on_401()`` and optionally
    exposing ``account_id`` — used instead of ``api_key``, with a single retry on
    401 (``on_401`` refreshes the rotated token) and the ``chatgpt-account-id``
    header attached. Point ``base_url`` at the Codex backend
    (``https://chatgpt.com/backend-api/codex/responses``) for subscription auth.
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Protocol, runtime_checkable

import httpx

from omni.providers._sse import sse_events
from omni.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from omni.providers.openai_responses.serialize import (
    ResponsesStreamNormalizer,
    build_request_body,
)

API_URL = "https://api.openai.com/v1/responses"
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"


@runtime_checkable
class ResponsesAuth(Protocol):
    """The Codex OAuth hook the client accepts in place of an api key."""

    account_id: str | None

    async def get_access_token(self) -> str: ...
    async def on_401(self) -> None: ...


class OpenAIResponsesClient:
    name = "openai_responses"
    caps = ProviderCaps(
        stateful_reasoning=True,
        explicit_cache_breakpoints=False,
    )

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = API_URL,
        auth: ResponsesAuth | None = None,
        timeout: float = 600.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url
        self._auth = auth
        self._timeout = timeout
        self._http = http_client

    async def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if self._auth is not None:
            token = await self._auth.get_access_token()
            headers["Authorization"] = f"Bearer {token}"
            account_id = getattr(self._auth, "account_id", None)
            if account_id:
                headers["chatgpt-account-id"] = account_id
            headers["OpenAI-Beta"] = "responses=experimental"
            headers["originator"] = "codex_cli_rs"
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        body = build_request_body(request)
        client = self._http or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._http is None
        try:
            # One retry: on a 401 with the OAuth hook, refresh the rotated token
            # once and re-issue the request.
            for attempt in range(2):
                headers = await self._headers()
                async with client.stream(
                    "POST", self._base_url, json=body, headers=headers
                ) as resp:
                    if resp.status_code == 200:
                        normalizer = ResponsesStreamNormalizer()
                        async for event_name, data in sse_events(resp.aiter_lines()):
                            for chunk in normalizer.consume(event_name, data):
                                yield chunk
                        return
                    text = (await resp.aread()).decode(errors="replace")
                    if (
                        resp.status_code == 401
                        and self._auth is not None
                        and attempt == 0
                    ):
                        await self._auth.on_401()
                        continue
                    yield StreamChunk(
                        "error",
                        {
                            "status": resp.status_code,
                            "message": text[:2000],
                            "retriable": resp.status_code in (429, 500, 502, 503, 529),
                        },
                    )
                    return
        finally:
            if owns_client:
                await client.aclose()
