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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.translator_gateway import routing, upstream
from app.core.translator_gateway.anthropic_to_responses import translate_request
from app.core.translator_gateway.events import (
    FinalReasoningItem,
    FinalTextItem,
    FinalToolCallItem,
    NormalizedResponse,
)
from app.core.translator_gateway.responses_to_anthropic import (
    ResponsesToAnthropicTranslator,
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


def _anthropic_error(status_code: int, err_type: str, message: str) -> JSONResponse:
    """An Anthropic-shaped error response (``{"type":"error","error":{...}}``)."""
    return JSONResponse(
        status_code=status_code,
        content={"type": "error", "error": {"type": err_type, "message": message}},
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
    the requested model → its provider + upstream id); then the MAIN leg (the
    binding's ``provider_id`` with the requested model passed through unchanged
    — byte-identical to pre-Chunk-F behavior for every non-leg session). The
    gateway matches byte-for-byte with no alias normalization; unknown models
    fall through to MAIN.

    Never logs a token; leg-resolution debug logs name provider/model ids only.
    """
    if requested_model:
        for upstream_model, provider_id, _account in binding.legs:
            if requested_model == upstream_model:
                logger.debug("gateway leg: model %s -> provider %s", upstream_model, provider_id)
                return LegRoute(provider_id=provider_id, upstream_model=upstream_model)
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
                parsed = json.loads(item.arguments) if item.arguments else {}
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
    if mode == "anthropic_passthrough":
        return await _handle_passthrough(leg.provider_id, body, stream)

    # ── Codex / OpenAI: translate Anthropic Messages → Responses and back.
    responses_body = translate_request(
        body,
        upstream_mode=("openai" if mode == "openai" else "codex"),
        target_model=leg.upstream_model,
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
