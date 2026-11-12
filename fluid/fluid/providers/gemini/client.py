"""Gemini Interactions API client (Google AI Studio API key).

One stateless ``POST /v1beta/interactions`` per model call with ``store:
false`` and ``stream: true`` (see serialize.py for why this surface and not
``generateContent`` or Google's OpenAI-compatible endpoint). Authenticated by
the ``x-goog-api-key`` header only; the key never appears in the URL, so it
cannot leak into a logged request line.

Retries follow the shared policy and happen only before the first content
chunk: a refused request (429, 408, 5xx) or an in-stream transient error that
arrives before anything was yielded. A daily-quota refusal
(``quota_exceeded``) is never retried. Timeouts are the same three bounds as
the other native clients (per read, first byte, whole stream).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from fluid.providers._retry import RetryPolicy
from fluid.providers._sse import sse_events
from fluid.providers._trace import trace_request
from fluid.providers._transport import (
    DEFAULT_FIRST_BYTE_TIMEOUT_S,
    DEFAULT_STREAM_DEADLINE_S,
    FirstByteTimeout,
    StreamDeadlineExceeded,
    StreamDiagnostics,
    deadline_error_payload,
    is_closed_client_error,
    is_usable_client,
    open_stream,
    refusal_payload,
    truncated_stream_payload,
    with_deadline,
)
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.gemini.pricing import estimate_cost as _estimate_cost
from fluid.providers.gemini.serialize import (
    API_REVISION,
    QUOTA_CODE,
    StreamNormalizer,
    build_request_body,
    error_fields,
    error_payload,
)

API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

log = logging.getLogger(__name__)


def _redact(value: Any, key: str) -> Any:
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, dict):
        return {k: _redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key) for v in value]
    return value


def http_refusal(status: int, text: str, retry_after: str | None) -> dict[str, Any]:
    """The error chunk for a non-200 Interactions response."""
    payload = refusal_payload(status, text, retry_after)
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code, _status, _message = error_fields(body)
        if code is not None:
            payload["code"] = code
        if code == QUOTA_CODE:
            detailed = error_payload(body)
            payload.update(
                retriable=False, usage_limit=True,
                provider_message=detailed.get("provider_message"),
            )
    return payload


class GeminiClient:
    name = "gemini"
    # Parallel function calls arrive as separate function_call steps. No
    # explicit cache markers: these models cache implicitly.
    caps = ProviderCaps(parallel_tool_calls=True)
    pricing_is_authoritative = True

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
        user_agent: str | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self._base_url = base_url
        self._timeout = timeout
        self._stream_deadline = stream_deadline
        self._first_byte_timeout = first_byte_timeout
        self._http = http_client
        self._retry = retry or RetryPolicy()
        self._user_agent = user_agent

    def estimate_cost(self, model: str, usage: dict) -> float | None:
        """Optional engine cost hook: list-price estimate for one model call."""
        return _estimate_cost(model, usage)

    def _pick_client(
        self, owned: httpx.AsyncClient | None
    ) -> tuple[httpx.AsyncClient, httpx.AsyncClient | None]:
        if is_usable_client(self._http):
            assert self._http is not None
            return self._http, owned
        if self._http is not None:
            log.warning("injected http client is closed; falling back to a per-call client for gemini")
            self._http = None
        if not is_usable_client(owned):
            owned = httpx.AsyncClient(timeout=self._timeout)
        assert owned is not None
        return owned, owned

    def _headers(self) -> dict[str, str]:
        headers = {
            "x-goog-api-key": self._api_key,
            "Api-Revision": API_REVISION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if self._user_agent:
            headers["User-Agent"] = self._user_agent
        return headers

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        headers = self._headers()
        body = await asyncio.to_thread(build_request_body, request)
        owned: httpx.AsyncClient | None = None
        attempt = 0
        try:
            while True:
                yielded = False
                client, owned = self._pick_client(owned)
                diagnostics = StreamDiagnostics(self.name, self._base_url, request.model, attempt + 1)
                try:
                    trace_request("gemini", body)
                    first_byte = self._first_byte_timeout
                    sent_at = time.monotonic()
                    async with contextlib.AsyncExitStack() as scope:
                        resp = await open_stream(
                            scope, client, "POST", self._base_url,
                            first_byte_timeout=first_byte, json=body, headers=headers,
                            follow_redirects=False,
                        )
                        diagnostics.received_headers(resp)
                        if resp.status_code != 200:
                            text = (await resp.aread()).decode(errors="replace")
                            text = _redact(text, self._api_key)
                            refusal = http_refusal(resp.status_code, text, resp.headers.get("retry-after"))
                            if refusal["retriable"] and attempt < self._retry.max_retries:
                                await asyncio.sleep(self._retry.delay(attempt, resp.headers.get("retry-after")))
                                attempt += 1
                                continue
                            yield StreamChunk("error", refusal)
                            return
                        normalizer = StreamNormalizer()
                        # message_start is held until real content follows,
                        # so a transient error sent in place of content can
                        # be retried before the engine sees a message begin.
                        pending_start: StreamChunk | None = None
                        retry_error = False
                        first_event_wait = (
                            max(first_byte - (time.monotonic() - sent_at), 0.05)
                            if first_byte and first_byte > 0 else None
                        )
                        events = with_deadline(
                            sse_events(diagnostics.lines()), self._stream_deadline,
                            first_item_timeout=first_event_wait,
                            first_byte_budget=first_byte,
                        )
                        async for event_name, data in events:
                            for chunk in normalizer.consume(event_name, data):
                                if chunk.kind == "error":
                                    chunk = StreamChunk("error", _redact(chunk.payload, self._api_key))
                                if chunk.kind == "message_start" and not yielded:
                                    pending_start = chunk
                                    continue
                                if chunk.kind == "provider_alive" and not yielded:
                                    continue
                                if (
                                    chunk.kind == "error" and not yielded
                                    and chunk.payload.get("retriable")
                                    and attempt < self._retry.max_retries
                                ):
                                    retry_error = True
                                    break
                                if pending_start is not None:
                                    yield pending_start
                                    pending_start = None
                                yielded = True
                                yield chunk
                            if retry_error:
                                break
                        if retry_error:
                            await asyncio.sleep(self._retry.delay(attempt))
                            attempt += 1
                            continue
                        if pending_start is not None:
                            yield pending_start
                        if not normalizer.terminal:
                            yield StreamChunk("error", truncated_stream_payload("interaction.completed"))
                        return
                except StreamDeadlineExceeded as exc:
                    yield StreamChunk("error", deadline_error_payload(exc))
                    return
                except RuntimeError as exc:
                    if not is_closed_client_error(exc):
                        raise
                    self._http = None
                    if not yielded and attempt < self._retry.transport_retries:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk("error", {"message": f"{type(exc).__name__}: {exc}", "retriable": True})
                    return
                except httpx.TransportError as exc:
                    # Pre-stream connection failures retry; once content has
                    # flowed, or the 200 headers arrived (Google may bill the
                    # call), the failure is reported instead.
                    retrying = (
                        not yielded
                        and attempt < self._retry.transport_retries
                        and not (isinstance(exc, FirstByteTimeout) and diagnostics.response is not None)
                    )
                    evidence = diagnostics.failure(exc, yielded=yielded, retrying=retrying)
                    if retrying:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk("error", {
                        "message": f"{type(exc).__name__}: {exc}",
                        "retriable": False,
                        "transport_failure": evidence,
                    })
                    return
        finally:
            if owned is not None:
                await owned.aclose()
