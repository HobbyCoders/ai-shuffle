"""Translator gateway HTTP endpoints (Chunk A, stage 2).

Serves the Anthropic Messages surface the bundled Claude Code CLI talks to when a
session is bound to a Codex/OpenAI-subscription (or Anthropic-passthrough)
provider. The CLI is spawned with ``ANTHROPIC_BASE_URL`` pointing at the loopback
gateway and ``ANTHROPIC_AUTH_TOKEN`` = an opaque per-session routing token (G4);
that token arrives here as ``Authorization: Bearer`` and is resolved to a
provider by :mod:`app.core.translator_gateway.routing`.

Endpoints:
  * ``POST /v1/messages``               — ``stream:true`` → incremental Anthropic
    SSE; ``stream:false`` → a complete buffered Anthropic Messages response.
  * ``POST /v1/messages/count_tokens``  — ``{"input_tokens": N}``.

The request ``model`` passes through as the upstream model id (the CLI is spawned
with real upstream ids). :func:`resolve_leg_model` is the single seam a future
per-model leg map (Chunk F) extends without touching the endpoints.

Security: unknown/missing token → 401 in the Anthropic error shape. No token,
key, or account id is ever logged.

Rate limits: ``POST /v1/messages`` pre-flights the per-provider circuit breaker
(:mod:`app.core.translator_gateway.circuit_breaker`) before dispatching. Once an
upstream 429 has tripped a provider, every gateway request routed to it is
refused install-wide, without a DB read or an upstream call, until the cooldown
elapses. The breaker only ever REFUSES — it never reroutes (ruling P10).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.translator_gateway import circuit_breaker, routing, upstream
from app.core.translator_gateway.anthropic_to_responses import translate_request
from app.core.translator_gateway.events import (
    FinalReasoningItem,
    FinalTextItem,
    FinalToolCallItem,
    NormalizedResponse,
)
from app.core.translator_gateway.responses_to_anthropic import (
    ResponsesToAnthropicTranslator,
    strip_null_values,
)
from app.core.translator_gateway.token_estimate import count_tokens

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Auth: opaque routing token → provider binding ───────────────────────────

def _bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip() or None
    return None


def _anthropic_error(
    status_code: int,
    err_type: str,
    message: str,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """An Anthropic-shaped error response (``{"type":"error","error":{...}}``).

    ``headers`` is additive and optional — every existing call site is
    byte-identical without it.
    """
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": err_type, "message": message}},
        headers=headers or None,
    )


def _resolve_binding(request: Request) -> Optional[routing.RouteBinding]:
    return routing.resolve(_bearer_token(request))


# ─── Chunk F seam: requested model → served leg (provider + upstream model) ──

@dataclass(frozen=True)
class LegRoute:
    """Which leg serves a gateway request: the provider row to load + the
    upstream model id to send. For a non-leg session this is just the binding's
    main provider and the passed-through requested model (today's behavior)."""

    provider_id: str
    upstream_model: Optional[str]


def resolve_leg_model(
    binding: routing.RouteBinding,
    requested_model: Optional[str],
) -> LegRoute:
    """Decide which leg serves this request (Chunk F/S3, ruling A2).

    Resolution order: the N-leg ``legs`` map is checked FIRST (exact match on
    the requested model → its provider + upstream id); then, when the MAIN
    provider is TRANSLATED, a stray ``claude-*`` / bare-alias id that matched no
    leg is REMAPPED onto the main leg's real upstream model (a translated
    upstream cannot serve a ``claude-*`` id — this is defense-in-depth alongside
    the S8-R1 PreToolUse hook for paths a hook can't cover, e.g. nested
    subagents); then the MAIN leg (the binding's ``provider_id`` with the
    requested model passed through unchanged — byte-identical to pre-Chunk-F
    behavior for every non-leg, non-translated session). The gateway matches
    byte-for-byte with no alias normalization; unknown models fall through to
    MAIN.

    Never logs a token; leg-resolution debug logs name provider/model ids only.
    """
    if requested_model:
        for upstream_model, provider_id, _account in binding.legs:
            if requested_model == upstream_model:
                logger.debug("gateway leg: model %s -> provider %s", upstream_model, provider_id)
                return LegRoute(provider_id=provider_id, upstream_model=upstream_model)
        # Remap a stray claude-*/bare-alias id (matched no leg) onto the main
        # leg's real model when the MAIN provider is translated and can't serve
        # it. main_upstream_model is set only for a translated main (see
        # environment.apply_provider_env), so an anthropic-passthrough main is
        # untouched and claude-* ids pass through verbatim.
        if (
            binding.main_upstream_model
            and requested_model != binding.main_upstream_model
            and (
                requested_model.startswith("claude-")
                or requested_model in {"haiku", "sonnet", "opus", "fable"}
            )
        ):
            logger.debug(
                "gateway remap: unsupported model %s -> main model %s on provider %s",
                requested_model,
                binding.main_upstream_model,
                binding.provider_id,
            )
            return LegRoute(
                provider_id=binding.provider_id,
                upstream_model=binding.main_upstream_model,
            )
    return LegRoute(provider_id=binding.provider_id, upstream_model=requested_model)


# ─── SSE serialization (module-level generator — testable w/o ASGITransport) ─
#
# CRITICAL (S7): httpx ASGITransport / TestClient BUFFER the whole response body,
# so an infinite SSE stream deadlocks pytest. Keep the translation loop as this
# module-level async generator and test it directly against a stubbed
# ``(name, payload)`` event source — never through the ASGI app.

def _sse_frame(frame: Dict[str, Any]) -> bytes:
    """Encode one Anthropic SSE frame (``event:``+``data:``) as bytes."""
    return (
        f"event: {frame.get('type', 'message')}\n"
        f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


async def anthropic_sse_from_events(
    event_source: AsyncIterator[Tuple[str, Dict[str, Any]]],
    *,
    request_model: Optional[str],
) -> AsyncIterator[bytes]:
    """Translate an upstream Responses ``(name, payload)`` stream into Anthropic
    SSE bytes. This is the streaming heart of ``POST /v1/messages`` and the unit
    under test for the streaming path."""
    translator = ResponsesToAnthropicTranslator(request_model=request_model)
    try:
        async for name, payload in event_source:
            for frame in translator.push(name, payload):
                yield _sse_frame(frame)
    except upstream.UpstreamError as exc:
        # The stream may already have started; emit a terminal Anthropic error
        # event so the CLI sees a clean failure rather than a truncated stream.
        yield _sse_frame(
            {
                "type": "error",
                "error": {"type": _err_type(exc.status_code), "message": exc.message},
            }
        )


def _err_type(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 429:
        return "rate_limit_error"
    if 400 <= status_code < 500:
        return "invalid_request_error"
    return "api_error"


# ─── Buffered (non-streaming) assembly: NormalizedResponse → Messages body ───

def _summary_text(summary: List[Any]) -> str:
    parts: List[str] = []
    for part in summary or []:
        if isinstance(part, dict) and part.get("type") == "summary_text":
            parts.append(str(part.get("text", "")))
    return "".join(parts)


def _messages_response_body(
    final: NormalizedResponse, request_model: Optional[str]
) -> Dict[str, Any]:
    """Render a buffered :class:`NormalizedResponse` as a complete Anthropic
    Messages response object."""
    content: List[Dict[str, Any]] = []
    for item in final.items:
        if isinstance(item, FinalReasoningItem):
            content.append(
                {
                    "type": "thinking",
                    "thinking": _summary_text(item.summary),
                    "signature": item.signature,
                }
            )
        elif isinstance(item, FinalTextItem):
            content.append({"type": "text", "text": item.text})
        elif isinstance(item, FinalToolCallItem):
            try:
                parsed = strip_null_values(json.loads(item.arguments)) if item.arguments else {}
            except (json.JSONDecodeError, ValueError):
                parsed = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": item.call_id,
                    "name": item.name,
                    "input": parsed,
                }
            )
    usage = final.usage
    return {
        "id": final.message_id or "msg_unknown",
        "type": "message",
        "role": "assistant",
        "model": final.model or request_model,
        "content": content,
        "stop_reason": final.stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        },
    }


# ─── Endpoints ───────────────────────────────────────────────────────────────

async def _load_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    from app.db import database as db
    from app.db.async_database import run_sync

    return await run_sync(db.get_model_provider, provider_id, True)


@router.post("/v1/messages")
async def create_message(request: Request):
    binding = _resolve_binding(request)
    if binding is None:
        return _anthropic_error(
            401, "authentication_error", "Invalid or missing gateway routing token."
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _anthropic_error(400, "invalid_request_error", "Request body must be an object.")

    stream = bool(body.get("stream"))
    requested_model = body.get("model")

    # Chunk F: resolve which leg (provider + upstream model) serves this request,
    # then load THAT leg's provider row and dispatch with the same provider_mode
    # machinery. For a non-leg session the leg IS the main provider + the
    # requested model, so every step below is byte-identical to pre-Chunk-F.
    leg = resolve_leg_model(binding, requested_model)

    # ── Circuit-breaker pre-flight (install-wide, per provider). Placed here,
    # after leg resolution (so it names the provider that would actually serve
    # this request) and BEFORE _load_provider, so a refusal costs neither a DB
    # read, a credential decrypt, nor an upstream socket.
    refusal = _circuit_breaker_refusal(leg.provider_id, stream)
    if refusal is not None:
        return refusal

    provider = await _load_provider(leg.provider_id)
    if not provider:
        return _anthropic_error(
            404, "not_found_error", "The provider for this session no longer exists."
        )

    try:
        mode = upstream.provider_mode(provider)
    except upstream.UpstreamError as exc:
        return _anthropic_error(exc.status_code, _err_type(exc.status_code), exc.message)

    # ── Anthropic passthrough (A.7 / G3 / F1): forward VERBATIM, no translation.
    # Deliberately NOT oversize-guarded below: the real Anthropic API enforces its
    # own context limits and returns a good actionable error, so we do not
    # second-guess it here.
    if mode == "anthropic_passthrough":
        return await _handle_passthrough(leg.provider_id, body, stream)

    # ── Oversize guard (translated modes only): a translated upstream rejects an
    # over-window request with an opaque error. When the provider row declares a
    # context_window, estimate the request locally and fail early with an
    # actionable Anthropic-shaped 400 so the CLI can surface it. No window set →
    # skip the guard entirely (no guessing).
    window = provider.get("context_window")
    if window:
        est = count_tokens(body)
        if est > int(window):
            return _anthropic_error(
                400,
                "invalid_request_error",
                f"This request is too large for the selected model: the gateway "
                f"estimates ~{est} tokens, but the model's context window is "
                f"{int(window)} tokens. Compact or shorten the conversation and retry.",
            )

    # ── Codex / OpenAI: translate Anthropic Messages → Responses and back.
    responses_body = translate_request(
        body,
        upstream_mode=("openai" if mode == "openai" else "codex"),
        target_model=leg.upstream_model,
        # Native array-form tool_result images only for the api.openai.com
        # upstream; the Codex backend stays on the fallback path until B3
        # live-validates that it accepts array-form function outputs.
        images_native=(mode == "openai"),
    )

    if stream:
        event_source = upstream.stream_responses_events(
            provider_id=leg.provider_id, mode=mode, responses_body=responses_body
        )
        return StreamingResponse(
            anthropic_sse_from_events(event_source, request_model=requested_model),
            media_type="text/event-stream",
        )

    # Buffered: drain the upstream stream, assemble the authoritative final turn.
    try:
        translator = ResponsesToAnthropicTranslator(request_model=requested_model)
        async for name, payload in upstream.stream_responses_events(
            provider_id=leg.provider_id, mode=mode, responses_body=responses_body
        ):
            translator.push(name, payload)
    except upstream.UpstreamError as exc:
        return _anthropic_error(exc.status_code, _err_type(exc.status_code), exc.message)
    final = translator.final_response()
    return JSONResponse(content=_messages_response_body(final, requested_model))


async def _handle_passthrough(provider_id: str, body: Dict[str, Any], stream: bool):
    if stream:
        async def _proxy() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.anthropic_passthrough_stream(
                    provider_id=provider_id, anthropic_body=body
                ):
                    yield chunk
            except upstream.UpstreamError as exc:
                yield upstream_error_sse(exc)

        return StreamingResponse(_proxy(), media_type="text/event-stream")
    try:
        payload = await upstream.anthropic_passthrough_json(
            provider_id=provider_id, anthropic_body=body
        )
    except upstream.UpstreamError as exc:
        return _anthropic_error(exc.status_code, _err_type(exc.status_code), exc.message)
    return JSONResponse(content=payload)


def upstream_error_sse(exc: "upstream.UpstreamError") -> bytes:
    return _sse_frame(
        {"type": "error", "error": {"type": _err_type(exc.status_code), "message": exc.message}}
    )


async def _single_error_sse(exc: "upstream.UpstreamError") -> AsyncIterator[bytes]:
    """A one-frame SSE body carrying ``exc`` — the streaming rendering of an
    upstream error that happened before any bytes were produced."""
    yield upstream_error_sse(exc)


def _circuit_breaker_refusal(provider_id: str, stream: bool):
    """Refuse this request if ``provider_id``'s circuit breaker is open.

    Returns a response when the breaker is OPEN, else ``None`` (proceed).

    This is the pre-flight the 2026-07-22 rate-limit storm needed: the retry loop
    lives inside the CLI processes, so the only place that can stop the traffic is
    here, BEFORE any credential is decrypted or any socket is opened. The
    refusal is a normal :class:`upstream.UpstreamError` (429) rendered through
    the SAME two paths a real upstream 429 takes — an Anthropic-shaped JSON error
    for a buffered request, a terminal SSE ``error`` frame for a streaming one —
    so nothing downstream needs a new error channel.

    The buffered refusal carries a real ``Retry-After`` (the cooldown remaining,
    ceil'd to whole seconds, floor 1) so a client that honours the header backs
    off for exactly as long as the breaker is actually open instead of retrying
    straight back into it — the retry loop lives in the CLI, so this header is
    the only thing this response can say to it. The STREAMING refusal is
    deliberately header-less: it is an HTTP **200** carrying an SSE error frame
    (that is how a real upstream 429 renders once a stream has been opened), and
    a ``Retry-After`` on a 200 is meaningless to a client.
    """
    trip = circuit_breaker.check(provider_id)
    if trip is None:
        return None
    exc = upstream.UpstreamError(429, circuit_breaker.refusal_message(trip))
    logger.warning(
        "gateway refusing request: provider %s circuit breaker open for another %.0fs",
        provider_id,
        trip.remaining_seconds(),
    )
    if stream:
        return StreamingResponse(
            _single_error_sse(exc), media_type="text/event-stream"
        )
    retry_after = max(1, math.ceil(trip.remaining_seconds()))
    return _anthropic_error(
        exc.status_code,
        _err_type(exc.status_code),
        exc.message,
        headers={"Retry-After": str(retry_after)},
    )


@router.post("/v1/messages/count_tokens")
async def count_message_tokens(request: Request):
    binding = _resolve_binding(request)
    if binding is None:
        return _anthropic_error(
            401, "authentication_error", "Invalid or missing gateway routing token."
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _anthropic_error(400, "invalid_request_error", "Request body is not valid JSON.")
    if not isinstance(body, dict):
        return _anthropic_error(400, "invalid_request_error", "Request body must be an object.")

    # Chunk F: honor the resolved leg here too — a count_tokens for the subagent
    # model counts against the subagent leg's provider, other models against the
    # main provider. The count itself is a local estimate (there is no upstream
    # count-tokens call), so a missing leg provider row is non-fatal.
    leg = resolve_leg_model(binding, body.get("model"))
    await _load_provider(leg.provider_id)
    return JSONResponse(content={"input_tokens": count_tokens(body)})
