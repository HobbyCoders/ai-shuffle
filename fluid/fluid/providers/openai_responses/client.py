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

Routing identity on the Codex backend is an experiment (E1), chosen with
``FLUID_RESPONSES_ROUTING_ARM`` or the ``routing_arm`` argument:

  * ``A`` (default): the body's ``prompt_cache_key`` only; requests are exactly
    what this client sent before the experiment existed.
  * ``B``: A plus the Codex identity headers. ``session-id`` is the
    ``prompt_cache_key`` (the fluid session id unless the host overrides it),
    ``thread-id`` is the loop's session id for its role, and
    ``x-client-request-id`` repeats ``thread-id``, as Codex sends it (its
    request id is the thread id). For the main role all three equal the
    ``prompt_cache_key``.
  * ``C``: B plus ``x-codex-turn-state``: the token the first response of a
    turn returns is replayed on every later request of that turn, retries
    included, and dropped when the turn changes. It lives in the request's
    ``WireState`` (one per loop and role), never on this client.
  * ``D``: B plus a prewarm at turn start. The documented prewarm,
    ``response.create`` with ``generate: false``, exists only in the Responses
    WebSocket mode; this client speaks HTTP SSE, so the hook logs and sends
    nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

import httpx

from fluid.providers._trace import trace_request

from fluid.providers._retry import RetryPolicy
from fluid.providers._sse import KEEPALIVE_EVENT, sse_events
from fluid.providers._transport import (
    DEFAULT_FIRST_BYTE_TIMEOUT_S,
    DEFAULT_STREAM_DEADLINE_S,
    FirstByteTimeout,
    StreamDiagnostics,
    StreamDeadlineExceeded,
    deadline_error_payload,
    first_byte_note,
    is_closed_client_error,
    is_usable_client,
    open_stream,
    refusal_payload,
    truncated_stream_payload,
    with_deadline,
)
from fluid.providers.base import (
    TURN_MARKER_KEY,
    ErrorClass,
    ProviderCaps,
    ProviderRequest,
    StreamChunk,
    WireState,
)
from fluid.providers.openai_responses.pricing import estimate_cost as _estimate_cost
from fluid.providers.openai_responses.serialize import (
    LONG_BACKOFF_FLOOR_S,
    ResponsesStreamNormalizer,
    build_request_body,
    classify_error as _classify_error,
)

API_URL = "https://api.openai.com/v1/responses"
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

ROUTING_ARM_ENV = "FLUID_RESPONSES_ROUTING_ARM"
ROUTING_ARMS = frozenset("ABCD")
TURN_STATE_HEADER = "x-codex-turn-state"
# Response headers that carry usage-limit and quota state: the Codex
# backend's x-codex-* family (plan, primary/secondary window use and reset,
# credits), which Codex parses into its rate-limit snapshot, and the public
# API's x-ratelimit-* family. Logged at debug level for experiment E1.
_QUOTA_HEADER_PREFIXES = ("x-codex-", "x-ratelimit-")

log = logging.getLogger(__name__)


def resolve_routing_arm(value: str | None) -> str:
    """The routing arm named by ``value``; ``A`` when unset or unknown."""
    arm = (value or "A").strip().upper()
    if arm not in ROUTING_ARMS:
        log.warning(
            "%s=%r is not one of A, B, C, D; using A", ROUTING_ARM_ENV, value
        )
        return "A"
    return arm


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
        first_byte_timeout: float | None = DEFAULT_FIRST_BYTE_TIMEOUT_S,
        event_silence_timeout: float | None = None,
        http_client: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
        routing_arm: str | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        # Read once: one client sends one arm for its whole life.
        self._routing_arm = resolve_routing_arm(
            routing_arm if routing_arm is not None else os.environ.get(ROUTING_ARM_ENV)
        )
        self._base_url = base_url
        self._auth = auth
        self._timeout = timeout
        # Wall clock for one whole model call, above the per-read ``timeout``.
        # None disables it. See fluid.providers._transport.
        self._stream_deadline = stream_deadline
        # Wall clock from sending the request to its first SSE frame (headers
        # included; a heartbeat counts). Headers that never came are retried
        # on the transport budget; a 200 followed by silence is reported as a
        # truncated stream. None or 0 disables it. See
        # fluid.providers._transport.FirstByteTimeout.
        self._first_byte_timeout = first_byte_timeout
        # Wall clock from the headers to the first real (data) event. A
        # heartbeat keeps the read timeout and the first-byte timer satisfied,
        # so a server that answers 200 and then sends only heartbeats would
        # otherwise be bounded by the stream deadline alone (or nothing when
        # that is off). Once heartbeats have been the only frames for this
        # long, the stream ends as truncated. After the first data event the
        # bound no longer applies: with encrypted-only reasoning, heartbeats
        # can be the only frames for minutes, and only the stream deadline
        # caps that. None means the per-read ``timeout``; 0 disables it.
        self._event_silence_timeout = (
            timeout if event_silence_timeout is None else event_silence_timeout
        )
        self._http = http_client
        self._retry = retry or RetryPolicy()

    def estimate_cost(self, model: str, usage: dict) -> float | None:
        """Optional engine cost hook: list-price estimate for one model call."""
        return _estimate_cost(model, usage)

    def classify_error(self, error: dict) -> ErrorClass:
        """Retry class of an in-stream error; never drops the affinity key."""
        return _classify_error(error)

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

    def routing_headers(self, request: ProviderRequest | None = None) -> dict[str, str]:
        """The headers that pick the backend route, without credentials.

        The engine's cache probe hashes these into the request identity: a
        cache lives on a route, so a header change can cost the cache even
        when the body is byte-identical. ``_headers`` sends all of these.
        Under arms B-D, ``request`` adds the session-stable identity headers
        (``session-id``, ``thread-id``). ``x-client-request-id`` repeats
        ``thread-id``, so hashing it adds nothing, and ``x-codex-turn-state``
        stays out because it changes on every turn by design: hashing it
        would read every turn start as a routing change.
        """
        headers: dict[str, str] = {}
        if self._auth is not None and self._is_codex_backend():
            account_id = getattr(self._auth, "account_id", None)
            if account_id:
                headers["chatgpt-account-id"] = account_id
            headers["OpenAI-Beta"] = "responses=experimental"
            headers["originator"] = "codex_cli_rs"
        if request is not None and self._routing_arm != "A" and self._is_codex_backend():
            headers.update(self._session_headers(request.extra, request.wire_state))
        return headers

    async def _headers(
        self,
        body: dict | None = None,
        wire: WireState | None = None,
        request_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        if self._auth is not None:
            token = await self._auth.get_access_token()
            headers["Authorization"] = f"Bearer {token}"
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # After the token: an auth that loads its account id lazily has it now.
        headers.update(self.routing_headers())
        if self._routing_arm != "A" and self._is_codex_backend():
            headers.update(self._identity_headers(body or {}, wire, request_id))
        return headers

    def _identity_headers(
        self, body: dict, wire: WireState | None, request_id: str | None
    ) -> dict[str, str]:
        """Codex routing identity for arms B-D (Codex backend only).

        ``session-id`` carries the cache key, as Codex sends it: "ChatGPT
        derives cache affinity from the Responses session-id header".
        """
        out = self._session_headers(body, wire)
        # Codex sends the thread id here, not a per-request id
        # (codex-api/src/endpoint/responses.rs). With no session identity at
        # all there is no thread id, and a fresh id stands in.
        out["x-client-request-id"] = out.get("thread-id") or request_id or uuid.uuid4().hex
        if self._routing_arm == "C" and wire is not None and wire.turn_state:
            out[TURN_STATE_HEADER] = wire.turn_state
        return out

    @staticmethod
    def _session_headers(body: dict, wire: WireState | None) -> dict[str, str]:
        """``session-id`` and ``thread-id``: the identity headers that stay
        the same for a whole session and role.

        ``body`` is the request body or the request's ``extra``; both carry
        the same ``prompt_cache_key``.
        """
        out: dict[str, str] = {}
        cache_key = body.get("prompt_cache_key")
        session = cache_key if isinstance(cache_key, str) and cache_key else None
        if session is None and wire is not None:
            session = wire.session_id
        if session is not None:
            out["session-id"] = session
            out["thread-id"] = wire.thread_id if wire is not None else session
        return out

    def _capture_turn_state(self, resp: httpx.Response, wire: WireState | None) -> None:
        """Arm C: keep the first turn-state token a turn's responses return."""
        if (
            self._routing_arm != "C"
            or wire is None
            or wire.turn_marker is None
            or wire.turn_state is not None
            or resp.status_code != 200
        ):
            return
        token = resp.headers.get(TURN_STATE_HEADER)
        if token:
            wire.turn_state = token

    def _log_quota_headers(self, resp: httpx.Response, model: str) -> None:
        """Debug-log the usage-limit headers of one response (experiment E1)."""
        if not log.isEnabledFor(logging.DEBUG):
            return
        quota = {
            key.lower(): value for key, value in resp.headers.items()
            if key.lower().startswith(_QUOTA_HEADER_PREFIXES)
            and key.lower() != TURN_STATE_HEADER
        }
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            quota["retry-after"] = retry_after
        if quota:
            log.debug(
                "openai_responses usage-limit headers (arm=%s status=%s model=%s): %s",
                self._routing_arm, resp.status_code, model,
                json.dumps(quota, sort_keys=True),
            )

    async def _prewarm(self, request: ProviderRequest, wire: WireState) -> None:
        """Arm D's turn-start prewarm: a hook that logs and sends nothing.

        The only documented prewarm is ``response.create`` with
        ``generate: false`` in the Responses WebSocket mode, and it primes
        that connection. This client speaks HTTP SSE, where no equivalent is
        documented, and an unknown field could start a billed generation.
        """
        log.info(
            "openai_responses routing arm D: turn-start prewarm skipped, no "
            "HTTP equivalent of generate:false (session=%s role=%s turn=%s model=%s)",
            wire.session_id, wire.role, wire.turn_marker, request.model,
        )

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
        if self._is_codex_backend():
            # The ChatGPT/Codex backend is not the public Responses API and
            # answers an output ceiling with HTTP 400 (the Codex CLI never
            # sends one). The model's own maximum applies instead.
            body.pop("max_output_tokens", None)
        # An uncertain media request can already have generated a billed image.
        # Keep an explicit auth-refresh retry, but never replay a native image
        # generation after a transport failure or an ambiguous server error.
        native_image = any(t.get("type") == "image_generation" for t in (body.get("tools") or ()) if isinstance(t, dict))
        transport_retries = 0 if native_image else self._retry.transport_retries
        # Routing identity (arms B-D; see the module docstring). The wire
        # state belongs to the calling loop and role; one-off callers pass
        # none and get no turn state.
        wire = request.wire_state
        request_id = uuid.uuid4().hex
        if wire is not None and self._routing_arm in ("C", "D") and self._is_codex_backend():
            new_turn = wire.begin(request.extra.get(TURN_MARKER_KEY))
            if new_turn and self._routing_arm == "D":
                await self._prewarm(request, wire)
        owned: httpx.AsyncClient | None = None
        attempt = 0
        refreshed_401 = False
        start_sent = False
        try:
            while True:
                yielded = False
                client, owned = self._pick_client(owned)
                diagnostics = StreamDiagnostics(
                    self.name, self._base_url, request.model, attempt + 1
                )
                try:
                    headers = await self._headers(body, wire, request_id)
                    trace_request("openai_responses", body)
                    first_byte = self._first_byte_timeout
                    sent_at = time.monotonic()
                    async with contextlib.AsyncExitStack() as scope:
                        resp = await open_stream(
                            scope, client, "POST", self._base_url,
                            first_byte_timeout=first_byte, json=body, headers=headers,
                        )
                        diagnostics.received_headers(resp)
                        self._log_quota_headers(resp, request.model)
                        self._capture_turn_state(resp, wire)
                        if resp.status_code == 200:
                            normalizer = ResponsesStreamNormalizer()
                            # Heartbeats become provider_alive chunks: with
                            # encrypted-only reasoning they can be the only
                            # frames for minutes once response.created has
                            # arrived. Before the first data event they count
                            # for at most the heartbeat-only bound below;
                            # after it, the stream deadline alone caps the
                            # call.
                            # The headers took part of the first-byte budget;
                            # the first frame gets the rest (never less than a
                            # sliver, so a slow header does not fail the read).
                            first_frame_wait = (
                                max(first_byte - (time.monotonic() - sent_at), 0.05)
                                if first_byte and first_byte > 0 else None
                            )
                            events = with_deadline(
                                sse_events(diagnostics.lines(), keepalives=True),
                                self._stream_deadline,
                                first_item_timeout=first_frame_wait,
                                first_byte_budget=first_byte,
                            )
                            # message_start (sent on response.created) is held
                            # until the next chunk, and an in-stream error that
                            # arrives before any content is retried when it is
                            # retriable. Exactly one message_start reaches the
                            # engine across attempts: once one has been
                            # released, a retry's own start is dropped.
                            pending_start: StreamChunk | None = None
                            content = False
                            # Set once the response's own end (usage +
                            # message_stop) or an error reached the engine; a
                            # body that ends before either was cut off.
                            terminal = False
                            retry_class: ErrorClass | None = None
                            # Heartbeat-only bound: applies only until the
                            # first data event (proof of life for the first
                            # frame, not a limit on later reasoning).
                            silence_limit = self._event_silence_timeout
                            headers_at = time.monotonic()
                            data_seen = False
                            heartbeats_only = False
                            async for event_name, data in events:
                                if event_name != KEEPALIVE_EVENT:
                                    data_seen = True
                                elif (
                                    not data_seen
                                    and silence_limit and silence_limit > 0
                                    and time.monotonic() - headers_at > silence_limit
                                ):
                                    heartbeats_only = True
                                    break
                                for chunk in normalizer.consume(event_name, data):
                                    if chunk.kind == "message_start":
                                        # Held, so not yet yielded: a
                                        # transport failure before the first
                                        # content item is replayed, as on the
                                        # Anthropic lane.
                                        if not start_sent:
                                            pending_start = chunk
                                        continue
                                    if chunk.kind == "error" and not content and not native_image:
                                        verdict = self.classify_error(chunk.payload)
                                        budget = self._retry.max_retries
                                        if verdict.max_retries is not None:
                                            budget = min(budget, verdict.max_retries)
                                        if verdict.retriable and attempt < budget:
                                            retry_class = verdict
                                            break
                                    if pending_start is not None:
                                        start_sent = True
                                        yield pending_start
                                        pending_start = None
                                    # A liveness signal is not delivered
                                    # content, so it leaves a pre-content
                                    # failure retriable.
                                    if chunk.kind != "provider_alive":
                                        yielded = True
                                        content = True
                                    if chunk.kind in ("message_stop", "error"):
                                        terminal = True
                                    yield chunk
                                if retry_class is not None:
                                    break
                            if retry_class is not None:
                                await events.aclose()
                                log.info(
                                    "Retrying openai_responses call after an "
                                    "in-stream error before content (code=%s, "
                                    "attempt %d)", retry_class.code, attempt + 1,
                                )
                                if retry_class.retry_after is not None:
                                    # The provider said when; never longer
                                    # than the long floor.
                                    wait = min(
                                        retry_class.retry_after, LONG_BACKOFF_FLOOR_S,
                                    )
                                else:
                                    wait = max(
                                        self._retry.delay(attempt),
                                        retry_class.backoff_floor,
                                    )
                                await asyncio.sleep(wait)
                                attempt += 1
                                continue
                            if heartbeats_only:
                                await events.aclose()
                            if pending_start is not None:
                                start_sent = True
                                yield pending_start
                            if not terminal:
                                payload = truncated_stream_payload("response.completed")
                                if heartbeats_only:
                                    payload["message"] += (
                                        f" (only heartbeats for {silence_limit:g}s)"
                                    )
                                yield StreamChunk("error", payload)
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
                        refusal = refusal_payload(resp.status_code, text)
                        if (
                            refusal["retriable"] and not native_image
                            and attempt < self._retry.max_retries
                        ):
                            await asyncio.sleep(self._retry.delay(
                                attempt, resp.headers.get("retry-after")))
                            attempt += 1
                            continue
                        yield StreamChunk("error", refusal)
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
                    if not yielded and attempt < transport_retries:
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
                    if isinstance(exc, FirstByteTimeout) and diagnostics.response is not None:
                        # A 200 and then silence: the call was accepted (and
                        # may be billed), so it is not re-sent here. Reported
                        # as the empty stream it is; the engine's
                        # empty-completion retry decides whether to re-send.
                        diagnostics.failure(exc, yielded=yielded, retrying=False)
                        payload = truncated_stream_payload("response.completed")
                        payload["message"] += first_byte_note(exc.budget)
                        yield StreamChunk("error", payload)
                        return
                    evidence = diagnostics.failure(
                        exc, yielded=yielded,
                        retrying=not yielded and attempt < transport_retries,
                    )
                    # Pre-stream connection failures retry; mid-stream ones
                    # don't (chunks were already delivered downstream).
                    if not yielded and attempt < transport_retries:
                        await asyncio.sleep(self._retry.delay(attempt))
                        attempt += 1
                        continue
                    yield StreamChunk(
                        "error",
                        {
                            "message": f"{type(exc).__name__}: {exc}",
                            "retriable": False,
                            "transport_failure": evidence,
                        },
                    )
                    return
        finally:
            if owned is not None:
                await owned.aclose()
