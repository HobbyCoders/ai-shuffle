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

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

import httpx

from fluid.providers._trace import trace_request

from fluid.providers._retry import RETRIABLE_STATUSES, RetryPolicy
from fluid.providers._sse import sse_events
from fluid.providers._transport import (
    DEFAULT_STREAM_DEADLINE_S,
    StreamDeadlineExceeded,
    deadline_error_payload,
    is_closed_client_error,
    is_usable_client,
    with_deadline,
)
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk

log = logging.getLogger(__name__)
from fluid.providers.openai_responses.pricing import estimate_cost as _estimate_cost
from fluid.providers.openai_responses.serialize import (
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
        stream_deadline: float | None = DEFAULT_STREAM_DEADLINE_S,
        http_client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url
        self._auth = auth
        self._timeout = timeout
        # Wall clock for one whole model call, above the per-read ``timeout``.
        # None disables it. See fluid.providers._transport.
        self._stream_deadline = stream_deadline
        self._http = http_client
        self._retry = retry or RetryPolicy()

    def estimate_cost(self, model: str, usage: dict) -> float | None:
        """Optional engine cost hook: list-price estimate for one model call."""
        return _estimate_cost(model, usage)

    def _is_codex_backend(self) -> bool:
        """True when this client is pointed at the ChatGPT/Codex responses URL.

        Codex-only headers (``chatgpt-account-id``, ``OpenAI-Beta``,
        ``originator``) must not leak to other Responses endpoints such as
        ``api.x.ai``.
        """
        return (
            self._base_url == CODEX_RESPONSES_URL
            or "chatgpt.com" in (self._base_url or "")
        )

    async def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if self._auth is not None:
            token = await self._auth.get_access_token()
            headers["Authorization"] = f"Bearer {token}"
            if self._is_codex_backend():
                account_id = getattr(self._auth, "account_id", None)
                if account_id:
                    headers["chatgpt-account-id"] = account_id
                headers["OpenAI-Beta"] = "responses=experimental"
                headers["originator"] = "codex_cli_rs"
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _pick_client(
        self, owned: httpx.AsyncClient | None
    ) -> tuple[httpx.AsyncClient, httpx.AsyncClient | None]:
        """The client for the next attempt, plus the one this call owns.

        An injected client belongs to the host, which may have closed it
        between calls — a parked session woken after its pool entry was
        evicted is the shape that keeps happening. A closed client can never
        send again, so it is dropped and this call falls back to one of its
        own rather than raising a non-retriable ``RuntimeError``.
        """
        if is_usable_client(self._http):
            assert self._http is not None
            return self._http, owned
        if self._http is not None:
            log.warning(
                "injected http client is closed; falling back to a per-call "
                "client for openai_responses"
            )
            self._http = None
        if not is_usable_client(owned):
            owned = httpx.AsyncClient(timeout=self._timeout)
        assert owned is not None
        return owned, owned

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        body = build_request_body(request)
        owned: httpx.AsyncClient | None = None
        attempt = 0
        refreshed_401 = False
        try:
            while True:
                yielded = False
                client, owned = self._pick_client(owned)
                try:
                    headers = await self._headers()
                    trace_request("openai_responses", body)
                    async with client.stream(
                        "POST", self._base_url, json=body, headers=headers
                    ) as resp:
                        if resp.status_code == 200:
                            normalizer = ResponsesStreamNormalizer()
                            events = with_deadline(
                                sse_events(resp.aiter_lines()), self._stream_deadline
                            )
                            async for event_name, data in events:
                                for chunk in normalizer.consume(event_name, data):
                                    yielded = True
                                    yield chunk
                            return
                        text = (await resp.aread()).decode(errors="replace")
                        # One free retry: a 401 with the OAuth hook refreshes
                        # the rotated token (doesn't consume a backoff attempt).
                        if (
                            resp.status_code == 401
                            and self._auth is not None
                            and not refreshed_401
                        ):
                            await self._auth.on_401()
                            refreshed_401 = True
                            continue
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
                except StreamDeadlineExceeded as exc:
                    yield StreamChunk("error", deadline_error_payload(exc))
                    return
                except RuntimeError as exc:
                    # httpx answers a send on a closed client with a plain
                    # RuntimeError. Nothing was sent, so this is a pre-stream
                    # failure: _pick_client already dropped the dead injected
                    # client, and the next attempt builds a live one.
                    if not is_closed_client_error(exc):
                        raise
                    self._http = None
                    if not yielded and attempt < self._retry.transport_retries:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk(
                        "error",
                        {
                            "message": f"{type(exc).__name__}: {exc}",
                            "retriable": True,
                        },
                    )
                    return
                except httpx.TransportError as exc:
                    # Pre-stream connection failures retry; mid-stream ones
                    # don't (chunks were already delivered downstream).
                    if not yielded and attempt < self._retry.transport_retries:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk(
                        "error",
                        {"message": f"{type(exc).__name__}: {exc}", "retriable": False},
                    )
                    return
        finally:
            if owned is not None:
                await owned.aclose()
