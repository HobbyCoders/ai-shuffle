"""Voice-mode API routes (waves VW1 + VW3 of docs/VOICE_MODE_PLAN.md).

Deliberately small surface:

* ``GET  /api/v1/voice/providers`` — which ``model_providers`` rows can fund a
  realtime call. OpenAI Realtime is **API-key only**: a ChatGPT/Codex
  subscription row (ruling VM3) can never be used, so those are filtered out
  here rather than failing at mint time.
* ``POST /api/v1/voice/sessions`` — create the session row (ruling VM5: a voice
  session is a REAL ``sessions`` row, created eagerly, ``mode='voice'``) and
  mint a short-lived OpenAI client secret for the browser's WebRTC leg, so the
  provider's API key never leaves this process. A declared
  ``session_mode='command'`` (hands-free wake-word one-shots) creates the row
  list-hidden — fully functional, just not in "recent sessions".
* ``POST /api/v1/voice/sessions/{id}/secret`` — re-mint that secret for an
  EXISTING voice session (rejoin, or the FE's reconnect near OpenAI's session
  wall). Same flag / eligibility / decrypt path as creation.
* ``POST /api/v1/voice/sessions/{id}/call`` — the browser has finished its
  WebRTC handshake and hands us the ``call_id``; the backend joins the same
  call over its sideband WebSocket (:mod:`app.core.voice_bridge`). Posting a
  new ``call_id`` REPLACES any existing attachment.
* ``GET /api/v1/voice/sessions/{id}/config`` — the pinned voice engine and funding provider.
* ``WS /api/v1/voice/sessions/{id}/audio`` — server-owned PCM streaming for
  GPT Live client delegation and fal.ai PersonaPlex; no provider key reaches the browser.
* ``POST /api/v1/voice/sessions/{id}/hangup`` — stop either voice transport.
  Idempotent.
* ``GET  /api/v1/voice/sessions/{id}/state`` — is a bridge attached, and is its
  sideband still connected.
* ``GET  /api/v1/voice/agents`` — the agents a voice call can dispatch (those
  with a voice assignment under Agents → Provider models → Voice).
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.auth import require_auth
from app.db import database
from app.db.async_database import run_sync
from app.core.voice_models import (
    FAL_PROVIDER_ID, LIVE_MODEL, PERSONAPLEX_MODEL, session_config, transport_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["Voice"])

# OpenAI's ephemeral-credential endpoint. The returned secret is what the
# browser uses to open its WebRTC connection; the real API key stays here.
OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

# How long a minted secret stays usable (OpenAI default 600s, max 7200s). The
# secret is ALSO the sideband's primary credential (live-validated 2026-08-14:
# an API-key sideband handshake 404s on browser-created calls, the call's own
# secret connects), so it must outlive the whole call — cap is 60 minutes
# (voice_max_call_minutes) plus reconnect headroom, hence the maximum.
CLIENT_SECRET_TTL_SECONDS = 7200

# Input-transcription model for the user's side of the conversation. Kept here
# because VW1 mints with a MINIMAL session config — the VW3 sideband owns the
# full ``session.update`` (instructions, tools, VAD, retention).
INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

# Upstream mint is a single small request; fail fast rather than hanging the
# user's "start call" click.
MINT_TIMEOUT_SECONDS = 15.0

# Voice-preview synthesis (ruling VM21). One short sample per click, generated
# through OpenAI's speech endpoint with the same provider key that funds calls.
# ``gpt-4o-mini-tts`` is the cheapest model that speaks the realtime voice set.
OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech"
PREVIEW_TTS_MODEL = "gpt-4o-mini-tts"
PREVIEW_TIMEOUT_SECONDS = 20.0
PREVIEW_SAMPLE_TEXT = (
    "Hey! This is what your voice agent will sound like. Ready when you are."
)

# Most sessions that can have a live secret at once. A hard bound purely so a
# long-lived process can never accumulate credentials for sessions that were
# started and abandoned; expired entries are dropped first, then the oldest.
MAX_REMEMBERED_SECRETS = 64


# ============================================================================
# Provider eligibility
# ============================================================================

def _is_eligible_voice_provider(row: Dict[str, Any]) -> bool:
    """Whether a provider row can fund an OpenAI Realtime call.

    Requires an enabled ``provider_type='openai'`` row carrying a stored API
    key. ChatGPT/Codex subscription rows are excluded via the same discriminator
    the fluid credential layer uses — Realtime has no subscription-OAuth path
    (ruling VM3).
    """
    from app.core.provider.fluid_credentials import _is_codex_subscription

    if row.get("provider_type") != "openai":
        return False
    if not row.get("enabled"):
        return False
    if _is_codex_subscription(row):
        return False
    return bool(row.get("auth_token"))


def list_eligible_voice_providers() -> List[Dict[str, str]]:
    """``[{id, name}]`` for every provider row eligible to fund voice calls.

    Synchronous (call via ``run_sync`` from async code). Deliberately returns
    only id + name: the rows are read with ``include_token=True`` so eligibility
    can test for a stored key, and that blob must never travel outward.
    """
    rows = database.get_enabled_model_providers(include_token=True)
    providers = [
        {"id": row["id"], "name": row.get("name") or row["id"]}
        for row in rows
        if _is_eligible_voice_provider(row)
    ]
    if database.get_system_setting("fal_api_key"):
        providers.append({"id": FAL_PROVIDER_ID, "name": "fal.ai"})
    return providers


def _eligible_provider_row(provider_id: str) -> Optional[Dict[str, Any]]:
    """The full (token-carrying) provider row for ``provider_id``, if eligible."""
    if provider_id == FAL_PROVIDER_ID:
        if database.get_system_setting("fal_api_key"):
            return {"id": FAL_PROVIDER_ID, "name": "fal.ai", "provider_type": "fal"}
        return None
    row = database.get_model_provider(provider_id, include_token=True)
    if row and _is_eligible_voice_provider(row):
        return row
    return None


# ============================================================================
# Models
# ============================================================================

class VoiceProvider(BaseModel):
    """A provider row that can fund voice calls."""
    id: str
    name: str


class VoiceAgent(BaseModel):
    """An agent the voice agent may dispatch, and the leg its worker runs on."""
    id: str
    name: str
    description: str = ""
    model: str
    provider_id: Optional[str] = None
    provider_name: str


class VoiceSessionCreateRequest(BaseModel):
    """Start a voice session. Every field is optional.

    ``session_mode`` ("command" | "chat") declared at creation lets a
    wake-word "command" session be born hidden (``sessions.hidden``, v75) so
    the quick one-shot task never flashes into the sessions list. Chat and
    browser sessions (no mode) are created visible as before.
    """
    profile_id: Optional[str] = None
    project_id: Optional[str] = None
    project_v2_id: Optional[str] = None
    provider_id: Optional[str] = None
    # Voice is a lane, not a reduced session shape.  These are the same durable
    # bindings a normal code session carries; the bridge uses the owning session
    # when it dispatches a worker.
    worktree_id: Optional[str] = None
    worktree_branch: Optional[str] = None
    base_branch: Optional[str] = None
    execution_mode: Optional[str] = None
    target_instance_id: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_mode: Optional[str] = None
    context_window: Optional[int] = None
    fast: Optional[bool] = None
    session_mode: Optional[str] = None


class VoiceSessionResponse(BaseModel):
    """The new session plus the browser's short-lived realtime credential."""
    session_id: str
    client_secret: str
    expires_at: Optional[int] = None
    model: str
    options: dict = Field(default_factory=dict)


class VoiceSecretResponse(BaseModel):
    """A freshly minted realtime credential for an existing voice session."""
    client_secret: str
    expires_at: Optional[int] = None
    model: str
    options: dict = Field(default_factory=dict)


class VoiceCallRequest(BaseModel):
    """The ``call_id`` the client got back from OpenAI's WebRTC handshake.

    ``client_tools`` lets a native client (the Android app) declare extra
    realtime function tools it will service ITSELF over its own data channel —
    device-local actions like placing a call or sending a text. The sideband
    merges them into its ``session.update`` (so the model can see them) and
    skips servicing those names (so the client's answer is the only one).
    ``client_instructions`` is appended to the coordinator instructions to
    teach the model when/how to use those tools.

    ``session_mode`` ("command" | "chat") is how a native client reports what
    kind of session this is; the route composes the matching instructions
    appendix (Settings-overridable) into the sideband's system prompt, and
    "command" additionally hides the session from the sessions list (v75).
    Browser calls omit it and get the base prompt only.
    """
    call_id: str
    client_tools: Optional[List[Dict[str, Any]]] = None
    client_instructions: Optional[str] = None
    session_mode: Optional[str] = None


class VoicePreviewRequest(BaseModel):
    """Which voice to synthesise a short sample for (VM21)."""
    voice: str
    provider_id: Optional[str] = None


class VoiceOkResponse(BaseModel):
    """Acknowledgement for the call/hangup control endpoints."""
    ok: bool = True


class VoiceStateResponse(BaseModel):
    """Whether a sideband bridge is attached to this session, and its health.

    ``usage`` accumulates realtime token counters across the whole call (it
    survives the FE's reconnect), and ``estimated_cost_usd`` prices them from a
    static per-model table — an ESTIMATE, not billing: cached-input discounts
    are ignored and an unpriced model yields ``null``.
    """
    active: bool
    connected: bool
    started_at: Optional[float] = None
    model: Optional[str] = None
    usage: Dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: Optional[float] = None
    active_tasks: int = 0


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/providers", response_model=List[VoiceProvider])
async def get_voice_providers(token: str = Depends(require_auth)):
    """List configured OpenAI and fal.ai voice providers, without credentials."""
    return await run_sync(list_eligible_voice_providers)


def list_voice_agents() -> List[Dict[str, Any]]:
    """The voice-dispatchable roster with a display name for each leg.

    Synchronous (call via ``run_sync``). Read from the same resolver the bridge
    dispatches through, so what Settings shows is exactly what a call can use.
    """
    from app.core.agent_provider_models import available_voice_subagents

    names: Dict[str, str] = {}
    out: List[Dict[str, Any]] = []
    for target in available_voice_subagents():
        provider_id = target.provider_id
        if provider_id and provider_id not in names:
            try:
                row = database.get_model_provider(provider_id, include_token=False)
            except TypeError:
                row = database.get_model_provider(provider_id)
            except Exception:
                row = None
            names[provider_id] = (row or {}).get("name") or provider_id
        out.append({
            "id": target.id,
            "name": target.name,
            "description": target.description,
            "model": target.model,
            "provider_id": provider_id,
            "provider_name": names[provider_id] if provider_id else "Claude",
        })
    return out


@router.get("/agents", response_model=List[VoiceAgent])
async def get_voice_agents(token: str = Depends(require_auth)):
    """List the agents a voice call can dispatch work to.

    Empty is the Settings surface's cue to point at the Agents editor: an
    agent joins this list by getting a model under Provider models → Voice.
    """
    return await run_sync(list_voice_agents)


@router.post("/preview")
async def preview_voice(
    request: VoicePreviewRequest,
    token: str = Depends(require_auth),
):
    """Synthesise a short "this is what I sound like" sample for one voice.

    Settings-surface helper (ruling VM21): the audio comes back directly as
    ``audio/mpeg`` for the browser to play. Uses the same provider resolution
    as a call (explicit > pinned > single eligible), and deliberately does NOT
    require ``voice_mode_enabled`` — picking a voice is part of setting the
    feature up before switching it on.
    """
    from app.api.settings import VOICE_VOICES
    from app.core.http_client import get_http_client

    voice = (request.voice or "").strip()
    if voice not in VOICE_VOICES:
        raise HTTPException(
            status_code=400,
            detail=f"voice must be one of {list(VOICE_VOICES)}",
        )

    from app.api.settings import get_voice_settings

    voice_settings = await run_sync(get_voice_settings)
    provider_row = await run_sync(
        _resolve_provider_row, request.provider_id, voice_settings["voice_provider_id"]
    )
    api_key = await _provider_api_key(provider_row)

    try:
        response = await get_http_client().post(
            OPENAI_SPEECH_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": PREVIEW_TTS_MODEL,
                "voice": voice,
                "input": PREVIEW_SAMPLE_TEXT,
                "response_format": "mp3",
            },
            timeout=PREVIEW_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Voice: preview synthesis failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach OpenAI for the voice preview: {exc}",
        ) from exc

    if response.status_code >= 400:
        body = (response.text or "").strip()
        logger.warning(
            "Voice: OpenAI rejected the voice preview (%s): %s",
            response.status_code, body[:500],
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"OpenAI rejected the voice preview "
                f"(HTTP {response.status_code}): {body[:300]}"
            ),
        )

    return Response(content=response.content, media_type="audio/mpeg")


def _resolve_provider_row(
    requested_id: Optional[str], settings_provider_id: Optional[str]
) -> Dict[str, Any]:
    """Pick the provider row that funds this call, or raise a helpful 400.

    Precedence: explicit request value > the pinned ``voice_provider_id``
    setting > the single eligible row when exactly one exists.
    """
    requested = (requested_id or "").strip()
    if requested:
        row = _eligible_provider_row(requested)
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Provider {requested!r} cannot fund voice calls. Pick an "
                    "enabled OpenAI API-key provider or configure a fal.ai API key. "
                    "ChatGPT-subscription logins cannot fund voice calls."
                ),
            )
        return row

    pinned = (settings_provider_id or "").strip()
    if pinned:
        row = _eligible_provider_row(pinned)
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The provider configured for voice ({pinned!r}) is no longer "
                    "eligible. Check its API key or choose another voice provider "
                    "in Settings."
                ),
            )
        return row

    eligible = [p for p in list_eligible_voice_providers() if p["id"] != FAL_PROVIDER_ID]
    if not eligible:
        raise HTTPException(
            status_code=400,
            detail=(
                "No OpenAI provider with a stored API key is configured. Add one "
                "in Settings → Providers before starting a voice session "
                "(Realtime does not support ChatGPT-subscription logins)."
            ),
        )
    if len(eligible) > 1:
        names = ", ".join(f"{p['name']} ({p['id']})" for p in eligible)
        raise HTTPException(
            status_code=400,
            detail=(
                "More than one OpenAI provider can fund voice calls, so one must "
                f"be chosen — either in the voice settings or per call: {names}"
            ),
        )
    row = _eligible_provider_row(eligible[0]["id"])
    if row is None:  # pragma: no cover — the row was eligible one statement ago
        raise HTTPException(
            status_code=400,
            detail="The OpenAI provider for voice is no longer available.",
        )
    return row


async def _mint_client_secret(
    api_key: str, model: str, voice: Optional[str] = None, options: Optional[dict] = None
) -> Dict[str, Any]:
    """POST OpenAI's client-secrets endpoint and return the parsed body.

    Audio, transcription, VAD and token settings travel with the mint, before
    any user audio arrives. The sideband uses the same pinned options and adds
    instructions/tools via ``session.update``. The VOICE must ride here: it can only
    be set before the model first speaks, and by the time the sideband attaches
    the model may already be talking.

    Checked again on 2026-08-14 against the ``POST /v1/realtime/client_secrets``
    reference while chasing the sideband's ``call_id_not_found``: the request
    body has NO sideband/observer/attach flag to set — the only session knobs
    are audio/tools/instructions/tracing and none of them gate server-side
    attachment. So the mint stays as-is; what the sideband needed was the minted
    secret itself, kept by :func:`_remember_client_secret` as its fallback
    bearer.
    """
    from app.core.http_client import get_http_client

    from app.core.voice_options import realtime_session_options
    configured = realtime_session_options(options)
    audio = configured["audio"]
    if voice:
        audio["output"] = {"voice": voice}
    payload = {
        "expires_after": {"anchor": "created_at", "seconds": CLIENT_SECRET_TTL_SECONDS},
        "session": {
            "type": "realtime",
            "model": model,
            **configured,
            "audio": audio,
        },
    }
    try:
        response = await get_http_client().post(
            OPENAI_CLIENT_SECRETS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=MINT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Voice: realtime client-secret mint failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach OpenAI to start the voice session: {exc}",
        ) from exc

    if response.status_code >= 400:
        body = (response.text or "").strip()
        logger.warning(
            "Voice: OpenAI rejected the client-secret mint (%s): %s",
            response.status_code, body[:500],
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"OpenAI rejected the voice session request "
                f"(HTTP {response.status_code}): {body[:500]}"
            ),
        )

    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="OpenAI returned an unreadable response to the voice session request.",
        ) from exc


def _extract_client_secret(body: Dict[str, Any]) -> tuple[str, Optional[int]]:
    """Pull ``(value, expires_at)`` out of a client-secrets response body.

    Accepts both the flat shape (``{"value": ..., "expires_at": ...}``) and the
    nested ``client_secret`` object older/other responses use.
    """
    value = body.get("value")
    expires_at = body.get("expires_at")
    nested = body.get("client_secret")
    if not value and isinstance(nested, dict):
        value = nested.get("value")
        expires_at = nested.get("expires_at", expires_at)
    if not value:
        raise HTTPException(
            status_code=502,
            detail="OpenAI returned no client secret for the voice session.",
        )
    try:
        expires_at = int(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expires_at = None
    return str(value), expires_at


# ----------------------------------------------------------------------------
# Minted-secret memory (sideband fallback credential)
# ----------------------------------------------------------------------------
#
# ``session_id -> (client secret, unix expiry)``. The browser creates the WebRTC
# call with this ``ek_`` credential, and when OpenAI answers an API-key sideband
# handshake with ``call_id_not_found`` the bridge retries with it (see
# ``voice_bridge._sideband_credentials``). In-process and short-lived on
# purpose: it is the same short-lived token the browser already holds, it is
# never returned by any route that did not mint it, and it is dropped on hangup.
_SESSION_CLIENT_SECRETS: Dict[str, Tuple[str, float]] = {}


def _remember_client_secret(
    session_id: str, secret: str, expires_at: Optional[int]
) -> None:
    """Keep the secret just minted for ``session_id`` until it expires."""
    if not session_id or not secret:
        return
    now = time.time()
    expiry = float(expires_at) if expires_at else now + CLIENT_SECRET_TTL_SECONDS
    for stale in [sid for sid, (_, exp) in _SESSION_CLIENT_SECRETS.items() if exp <= now]:
        _SESSION_CLIENT_SECRETS.pop(stale, None)
    _SESSION_CLIENT_SECRETS[session_id] = (secret, expiry)
    while len(_SESSION_CLIENT_SECRETS) > MAX_REMEMBERED_SECRETS:
        oldest = min(_SESSION_CLIENT_SECRETS, key=lambda sid: _SESSION_CLIENT_SECRETS[sid][1])
        _SESSION_CLIENT_SECRETS.pop(oldest, None)


def _recall_client_secret(session_id: str) -> Optional[str]:
    """The session's last minted secret while it is still valid, else ``None``."""
    entry = _SESSION_CLIENT_SECRETS.get(session_id)
    if entry is None:
        return None
    secret, expiry = entry
    if expiry <= time.time():
        _SESSION_CLIENT_SECRETS.pop(session_id, None)
        return None
    return secret


def _forget_client_secret(session_id: str) -> None:
    """Drop the session's remembered secret (hangup)."""
    _SESSION_CLIENT_SECRETS.pop(session_id, None)


async def _provider_api_key(provider_row: Dict[str, Any]) -> str:
    """Decrypt the row's stored API key, or raise a 400 explaining the fix.

    Reuses the fluid credential layer's decryption so crypto lives in exactly one
    place; an eligible row always HAS a token, but a locked encryption service
    can still yield nothing.
    """
    from app.core.provider.fluid_credentials import _decrypt_auth_token

    if provider_row["id"] == FAL_PROVIDER_ID:
        from app.api.settings import get_decrypted_api_key
        api_key = await run_sync(get_decrypted_api_key, "fal_api_key")
    else:
        api_key = await run_sync(_decrypt_auth_token, provider_row)
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The API key for provider {provider_row.get('name') or provider_row['id']!r} "
                "could not be read. Re-enter it in Settings → Providers."
            ),
        )
    return api_key


async def _require_voice_enabled() -> Dict[str, Any]:
    """The voice settings, or a 403 when the mode is switched off (VM13)."""
    from app.api.settings import get_voice_settings

    voice_settings = await run_sync(get_voice_settings)
    if not voice_settings["voice_mode_enabled"]:
        raise HTTPException(
            status_code=403,
            detail="Voice mode is disabled. Enable it in Settings → Voice.",
        )
    return voice_settings


def _reject_when_capped(session_id: str) -> None:
    """409 while the session sits inside the post-cap grace window (VW5).

    The call-length cap is measured from the call's epoch, so the FE's silent
    re-handshake would otherwise just re-attach and keep going. Answering 409
    here — on BOTH the re-mint and the attach — is what forces a deliberate
    hangup instead of an accidentally endless call.
    """
    from app.core import voice_bridge

    minutes = voice_bridge.registry.capped_minutes(session_id)
    if minutes is None:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Call length cap reached ({minutes} minutes). Hang up and start a "
            "new call."
        ),
    )


async def _require_voice_session(session_id: str) -> Dict[str, Any]:
    """The session row, or a 404 when it is missing or is not a voice session.

    A chat session is 404 rather than 400 on purpose: the voice call surface
    simply does not exist for it (ruling VM7 — modes never mix in V1).
    """
    row = await run_sync(database.get_session, session_id)
    if not row or row.get("mode") != "voice":
        raise HTTPException(status_code=404, detail="Voice session not found.")
    return row


@router.post("/sessions", response_model=VoiceSessionResponse)
async def create_voice_session(
    http_request: Request,
    request: VoiceSessionCreateRequest,
    token: str = Depends(require_auth),
):
    """Create a voice session row and mint its realtime client secret.

    Order matters: project authorization happens before the upstream mint, and
    the mint happens before the row is written, so neither a denied request nor
    a rejected mint leaves an orphaned session on the board.

    A declared ``session_mode`` of "command" stamps ``hidden=TRUE`` at INSERT —
    the row never exists in a visible state, so the list can't even flash it.
    """
    from app.api.settings import get_voice_settings
    from app.core import voice_bridge

    session_mode = (request.session_mode or "").strip().lower() or None
    if session_mode is not None and session_mode not in voice_bridge.DEFAULT_MODE_INSTRUCTIONS:
        raise HTTPException(
            status_code=400,
            detail="session_mode must be 'command' or 'chat'.",
        )

    # Voice uses the same origin authorization as normal branch sessions.
    # Prefer the legacy id when both forms are supplied by the compose store,
    # then verify that it resolves to the supplied V2 project.
    from app.api.auth import get_api_user_from_request
    from app.api.common import require_origin_access

    execution_mode = (request.execution_mode or "").strip().lower() or None
    if execution_mode not in (None, "local", "worktree"):
        raise HTTPException(400, "execution_mode must be 'local' or 'worktree'.")
    if request.worktree_branch and not request.worktree_id:
        if execution_mode == "local":
            raise HTTPException(400, "A branch cannot be used with local execution.")
        execution_mode = "worktree"

    access = None
    if request.project_id or request.project_v2_id:
        access = await run_sync(
            require_origin_access,
            request=http_request,
            project_id=request.project_id or None,
            project_v2_id=None if request.project_id else request.project_v2_id,
        )
        resolved_pv2_id = (access.get("project_v2") or {}).get("id")
        if request.project_v2_id and resolved_pv2_id != request.project_v2_id:
            raise HTTPException(
                400, "project_id and project_v2_id refer to different projects."
            )
    else:
        resolved_pv2_id = None

    from app.core.config import settings

    target_instance_id = (request.target_instance_id or "").strip() or None
    if target_instance_id == settings.effective_instance_id:
        target_instance_id = None

    voice_settings = await run_sync(get_voice_settings)
    if not voice_settings["voice_mode_enabled"]:
        raise HTTPException(
            status_code=403,
            detail="Voice mode is disabled. Enable it in Settings → Voice.",
        )

    provider_row = await run_sync(
        _resolve_provider_row, request.provider_id, voice_settings["voice_provider_id"]
    )

    api_key = await _provider_api_key(provider_row)

    model = voice_settings["voice_model"]
    if (model == PERSONAPLEX_MODEL) != (provider_row["id"] == FAL_PROVIDER_ID):
        raise HTTPException(400, "The voice model does not belong to the selected provider.")
    options = voice_settings.get("voice_options", {})
    selected_model, selected_effort = request.model, request.effort
    if model == LIVE_MODEL:
        from app.core.voice_stream import validate_delegation_options
        await run_sync(validate_delegation_options, options, provider_row["id"])
        if options.get("delegation_type") == "responses":
            selected_model = options["delegation_model"]
            selected_effort = options.get("delegation_effort") or None
        elif not selected_model:
            selected_model = options.get("delegation_model")
            selected_effort = selected_effort or options.get("delegation_effort") or None
        if not (selected_model or "").strip():
            raise HTTPException(400, "Choose a delegation model before starting GPT Live 1.")
    client_secret, expires_at = "", None
    if transport_for(model) == "webrtc":
        body = await _mint_client_secret(api_key, model, voice_settings["voice_voice"], options)
        client_secret, expires_at = _extract_client_secret(body)

    # Resolve the profile the same way normal session creation does (explicit →
    # project default → first configured agent). A literal fallback id here hits
    # the sessions.profile_id FK the moment no such agent row exists.
    profile_id = request.profile_id
    if not profile_id and request.project_id:
        project = await run_sync(database.get_project, request.project_id)
        profile_id = ((project or {}).get("settings") or {}).get("default_profile_id")
    if not profile_id:
        profiles = await run_sync(database.get_all_profiles)
        if not profiles:
            raise HTTPException(
                status_code=409,
                detail="No agents are configured; create an agent before starting a voice session.",
            )
        profile_id = profiles[0]["id"]

    session_create_overrides = {
        "title": "Voice session",
        "is_active": True,
        "provider_id": None if model == LIVE_MODEL else provider_row["id"],
        "derive_provider_from_profile": model != LIVE_MODEL,
        "model": selected_model,
        "effort": selected_effort,
        "permission_mode": request.permission_mode,
        "context_window": request.context_window,
        "fast": request.fast,
        "mode": "voice",
        "hidden": session_mode == "command",
        "voice_config": {
            "model": model, "voice": voice_settings["voice_voice"],
            "provider_id": provider_row["id"], "options": options,
            "greet": session_mode is None,
        },
    }

    # Current branch sessions have no legacy worktree row. Reuse the normal V2
    # path so existing branches are recovered and Auto creates a real branch;
    # remote runner picks also go through its eligibility check and durable op.
    if execution_mode == "worktree" and not request.worktree_id:
        if not access or not resolved_pv2_id:
            raise HTTPException(400, "A project is required for worktree execution.")
        from app.api.sessions_helpers import CreateWorktreeSessionRequest
        from app.api.sessions_worktree import _create_v2_branch_session

        selected_branch = (request.worktree_branch or "").strip()
        branch_name = selected_branch or f"voice/{uuid.uuid4().hex[:12]}"
        branch_request = CreateWorktreeSessionRequest(
            project_id=request.project_id or None,
            project_v2_id=None if request.project_id else resolved_pv2_id,
            branch_name=branch_name,
            base_branch=(request.base_branch or "main").strip() or "main",
            profile_id=profile_id,
            create_new_branch=not bool(selected_branch),
            target_instance_id=target_instance_id,
        )
        created = await _create_v2_branch_session(
            http_request,
            branch_request,
            access.get("legacy"),
            (get_api_user_from_request(http_request) or {}).get("id"),
            access.get("project_v2"),
            session_create_overrides=session_create_overrides,
        )
        session_id = created.session_id
    else:
        # Legacy worktree rows remain supported, but branch-only identifiers are
        # never interpreted as legacy ids and never discarded.
        if request.worktree_id:
            if not request.project_id:
                raise HTTPException(400, "project_id is required with worktree_id.")
            if target_instance_id:
                raise HTTPException(400, "A legacy worktree cannot be moved to another runner.")
            worktree = await run_sync(database.get_worktree, request.worktree_id)
            repository = await run_sync(
                database.get_git_repository_by_project, request.project_id
            )
            if (
                not worktree
                or not repository
                or worktree.get("repository_id") != repository.get("id")
            ):
                raise HTTPException(404, "Worktree not found in this project.")
            worktree_branch = worktree.get("branch") or request.worktree_branch
            worktree_path = worktree.get("path") or worktree.get("worktree_path")
        else:
            worktree_branch = None
            worktree_path = None

        if target_instance_id:
            if access and resolved_pv2_id:
                from app.api.sessions_worktree import _preflight_remote_target

                await _preflight_remote_target(target_instance_id, access["project_v2"])
            else:
                install = await run_sync(database.get_install, target_instance_id)
                if not install:
                    raise HTTPException(409, "Unknown device — pick another device.")
                device = install.get("device_name") or target_instance_id
                if not install.get("approved", True):
                    raise HTTPException(
                        409,
                        f"{device} is waiting for admin approval. Approve and restart it first.",
                    )
                if not install.get("online"):
                    raise HTTPException(
                        409, f"{device} is offline. Bring it online or pick another device."
                    )

        session_id = str(uuid.uuid4())
        await run_sync(
            database.create_session,
            session_id=session_id,
            profile_id=profile_id,
            project_id=request.project_id,
            project_v2_id=resolved_pv2_id,
            worktree_id=request.worktree_id,
            worktree_branch=worktree_branch,
            worktree_path=worktree_path,
            target_instance_id=target_instance_id,
            **session_create_overrides,
        )
    logger.info(
        "Voice: created session %s on provider %s (model %s)",
        session_id, provider_row["id"], model,
    )
    _remember_client_secret(session_id, client_secret, expires_at)

    return VoiceSessionResponse(
        session_id=session_id,
        client_secret=client_secret,
        expires_at=expires_at,
        model=model, options=options,
    )


# ============================================================================
# Call surface (VW3) — re-mint, attach, hangup, state
# ============================================================================

@router.post("/sessions/{session_id}/secret", response_model=VoiceSecretResponse)
async def mint_voice_session_secret(
    session_id: str,
    token: str = Depends(require_auth),
):
    """Re-mint the browser's realtime credential for an EXISTING voice session.

    Two callers: reopening a voice session later, and the FE's reconnect as the
    call approaches OpenAI's session wall (ruling VM15) — it re-handshakes with
    a fresh secret, gets a new ``call_id``, and posts it to ``/call``, which
    replaces the sideband and reseeds context.

    The session's own ``provider_id`` (stamped at creation) leads the provider
    resolution, so a call keeps being funded by the row it started on.
    """
    voice_settings = await _require_voice_enabled()
    session_row = await _require_voice_session(session_id)
    _reject_when_capped(session_id)

    config = session_config(session_row, voice_settings)
    provider_row = await run_sync(_resolve_provider_row, config["provider_id"], None)
    api_key = await _provider_api_key(provider_row)

    model = config["model"]
    client_secret, expires_at = "", None
    if transport_for(model) == "webrtc":
        body = await _mint_client_secret(api_key, model, config["voice"], config.get("options"))
        client_secret, expires_at = _extract_client_secret(body)
    _remember_client_secret(session_id, client_secret, expires_at)
    logger.info("Voice: re-minted client secret for session %s", session_id)

    return VoiceSecretResponse(
        client_secret=client_secret, expires_at=expires_at, model=model, options=config.get("options", {}),
    )


# Client-declared tools are serviced by the app, but their schemas travel
# through us into the realtime session — so bound them defensively.
MAX_CLIENT_TOOLS = 32
MAX_CLIENT_INSTRUCTIONS_CHARS = 6000
_TOOL_NAME_MAX = 64


def _sanitize_client_tools(
    raw_tools: Optional[List[Dict[str, Any]]],
    reserved_names: set,
) -> List[Dict[str, Any]]:
    """Validate and normalize client-declared realtime tools.

    Raises a 400 on anything malformed rather than silently dropping it: a
    tool the client thinks it declared but the model can't see is a confusing
    half-working assistant, and the client is ours — it should be fixed.
    """
    if not raw_tools:
        return []
    if len(raw_tools) > MAX_CLIENT_TOOLS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_CLIENT_TOOLS} client tools are allowed.",
        )
    sanitized: List[Dict[str, Any]] = []
    seen: set = set()
    for tool in raw_tools:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=400, detail="Each client tool must be an object.")
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail="Every client tool needs a name.")
        name = name.strip()
        if len(name) > _TOOL_NAME_MAX or not name.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(
                status_code=400,
                detail=f"Invalid client tool name {name!r}.",
            )
        if name in reserved_names:
            raise HTTPException(
                status_code=400,
                detail=f"Client tool name {name!r} collides with a server tool.",
            )
        if name in seen:
            raise HTTPException(
                status_code=400, detail=f"Duplicate client tool name {name!r}.",
            )
        seen.add(name)
        description = tool.get("description")
        parameters = tool.get("parameters")
        sanitized.append({
            "type": "function",
            "name": name,
            "description": description if isinstance(description, str) else "",
            "parameters": parameters if isinstance(parameters, dict)
            else {"type": "object", "properties": {}},
        })
    return sanitized


@router.post("/sessions/{session_id}/call", response_model=VoiceOkResponse)
async def attach_voice_call(
    session_id: str,
    request: VoiceCallRequest,
    token: str = Depends(require_auth),
):
    """Join the browser's realtime call from the backend (ruling VM2/VM8).

    Idempotent in the useful sense: posting a NEW ``call_id`` for a session that
    already has a bridge replaces it (the old attachment is closed first), which
    is exactly what the reconnect path needs — and the replacement keeps the
    call's epoch, so the length cap is not restarted by reconnecting.

    Answers 409 inside the post-cap grace window (see :func:`_reject_when_capped`).
    """
    from app.core import voice_bridge

    voice_settings = await _require_voice_enabled()
    session_row = await _require_voice_session(session_id)
    _reject_when_capped(session_id)

    call_id = (request.call_id or "").strip()
    if not call_id:
        raise HTTPException(status_code=400, detail="call_id is required.")

    client_tools = _sanitize_client_tools(
        request.client_tools,
        reserved_names={t["name"] for t in voice_bridge.VOICE_TOOLS}
        | {t["name"] for t in voice_bridge.VOICE_WEB_TOOLS},
    )
    client_instructions = (request.client_instructions or "").strip() or None
    if client_instructions and len(client_instructions) > MAX_CLIENT_INSTRUCTIONS_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                "client_instructions is limited to "
                f"{MAX_CLIENT_INSTRUCTIONS_CHARS} characters."
            ),
        )

    session_mode = (request.session_mode or "").strip().lower() or None
    if session_mode is not None and session_mode not in voice_bridge.DEFAULT_MODE_INSTRUCTIONS:
        raise HTTPException(
            status_code=400,
            detail="session_mode must be 'command' or 'chat'.",
        )

    # A "command" session is list-hidden (v75). Creation already stamps this
    # when the mode is declared up front; the shipped app only declares it
    # here at attach time, so backfill the flag for that client too — and
    # broadcast the flip, so a list that rendered the card during the
    # create→attach window drops it live instead of on the next refetch.
    # User-level fan-out on purpose: no device ever watches a wake-word
    # session, so the session-scoped broadcast would reach nobody.
    if session_mode == "command" and not session_row.get("hidden"):
        await run_sync(database.set_session_hidden, session_id, True)
        try:
            from app.core.sync_engine import sync_engine
            await sync_engine.broadcast_user_session_updated(
                user_id=session_row.get("api_user_id") or "admin",
                session_id=session_id,
                updates={"hidden": True},
                source_device_id=None,
            )
        except Exception:
            logger.warning(
                "Voice: hidden-backfill broadcast failed for session %s",
                session_id, exc_info=True,
            )

    # Compose the agent's system prompt: the base contract, plus the appendix
    # for the client's session mode (browser calls have none).
    config = session_config(session_row, voice_settings)
    from app.core.voice_options import effective_options
    options = effective_options(config)
    instructions_override = options["instructions"] or voice_bridge.VOICE_INSTRUCTIONS
    if session_mode is not None:
        appendix = voice_bridge.DEFAULT_MODE_INSTRUCTIONS[session_mode]
        instructions_override = f"{instructions_override}\n\n{appendix}"

    # The app-configuration tools the voice agent may use directly. Same
    # admission rule as a chat session's app-config server: never for an
    # API-key user. Always available to the owner, independent of legacy
    # settings. Admin is asserted for write tools as chat does (``is_admin=True``)
    # because the caller has already passed the same cookie/pairing gate.
    app_tools: List[Any] = []
    if not session_row.get("api_user_id"):
        app_tools = await run_sync(
            _voice_app_tools, session_row.get("project_id"),
        )

    # A browser call opens with a greeting; a native session must stay silent
    # until the user speaks (its mode appendix says so), so it never greets.
    greet = session_mode is None

    config = session_config(session_row, voice_settings)
    if transport_for(config["model"]) != "webrtc":
        raise HTTPException(400, "This voice model uses the audio stream, not the Realtime sideband.")
    provider_row = await run_sync(_resolve_provider_row, config["provider_id"], None)
    api_key = await _provider_api_key(provider_row)

    try:
        await voice_bridge.registry.attach(
            session_id,
            call_id,
            api_key=api_key,
            model=config["model"],
            max_call_minutes=voice_settings["voice_max_call_minutes"],
            # Fallback bearer for the handshake — see _remember_client_secret.
            client_secret=_recall_client_secret(session_id),
            client_tools=client_tools,
            client_instructions=client_instructions,
            instructions_override=instructions_override,
            voice_options=options,
            app_tools=app_tools,
            greet=greet,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Voice: sideband attach failed for session %s: %s", session_id, exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not open the voice sideband connection: {exc}",
        ) from exc

    return VoiceOkResponse()


def _voice_app_tools(project_id: Optional[str]) -> List[Any]:
    """The app-config ``@tool`` objects for a voice call (sync — ``run_sync``).

    Scoped to the session's project like a chat session's server: project
    hooks/automations resolve against it, and the MCP-server tools' local and
    project scopes write under its directory. Never raises — a missing SDK or a
    failing factory leaves the voice agent with dispatch only.
    """
    try:
        from app.mcp.app_config_sdk import collect_app_config_tools

        project_dir: Optional[str] = None
        if project_id:
            try:
                from app.core.project_utils import get_project_working_dir

                project = database.get_project(project_id)
                if project:
                    project_dir = get_project_working_dir(project)
            except Exception:  # noqa: BLE001 — scope is a nicety
                logger.debug("Voice: project dir lookup failed", exc_info=True)
        return collect_app_config_tools(
            is_admin=True, project_id=project_id, project_dir=project_dir,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Voice: app-config tools unavailable", exc_info=True)
        return []


@router.post("/sessions/{session_id}/hangup", response_model=VoiceOkResponse)
async def hangup_voice_call(
    session_id: str,
    token: str = Depends(require_auth),
):
    """Detach and stop the session's bridge. ``ok`` even when there was none.

    Tears down ONLY the bridge and the realtime session — dispatched workers
    deliberately survive the call ending (ruling VM12). No flag gate: ending a
    call must keep working even if voice mode was switched off mid-call.
    """
    from app.core import voice_bridge

    await _require_voice_session(session_id)
    _forget_client_secret(session_id)
    from app.core import voice_stream
    stream_stopped = await voice_stream.stop(session_id)
    stopped = await voice_bridge.registry.detach(session_id) or stream_stopped

    # Creation stamps ``is_active`` True and, until this, nothing in the voice
    # lane ever wrote it back — so every finished call left a session card that
    # looked live forever. Unconditional: the bridge clears it when there IS one
    # to stop, and a hangup after the sideband already dropped is exactly the
    # case that needs the write here.
    try:
        await run_sync(database.set_session_active, session_id, False)
    except Exception:  # noqa: BLE001 — ending a call must always succeed
        logger.warning(
            "Voice: could not clear is_active for session %s", session_id,
            exc_info=True,
        )

    logger.info(
        "Voice: hangup for session %s (%s)",
        session_id, "bridge stopped" if stopped else "no live bridge",
    )
    return VoiceOkResponse()


@router.get("/sessions/{session_id}/state", response_model=VoiceStateResponse)
async def get_voice_call_state(
    session_id: str,
    token: str = Depends(require_auth),
):
    """Whether this session has a live sideband, and whether it is connected.

    ``active`` without ``connected`` is the FE's cue that the sideband dropped:
    the server never reconnects on its own (ruling VM15 puts reconnect on the
    FE, with a fresh ``call_id``).
    """
    from app.core import voice_bridge

    await _require_voice_session(session_id)
    from app.core import voice_stream
    return VoiceStateResponse(**(voice_stream.state(session_id) or voice_bridge.registry.state(session_id)))

@router.get("/sessions/{session_id}/config")
async def get_voice_session_config(session_id: str, token: str = Depends(require_auth)):
    from app.api.settings import get_voice_settings
    row = await _require_voice_session(session_id)
    return session_config(row, await run_sync(get_voice_settings))


@router.websocket("/sessions/{session_id}/audio")
async def voice_audio_socket(websocket: WebSocket, session_id: str, token: Optional[str] = Query(None)):
    from app.api.ws_auth import authenticate_websocket
    from app.core import voice_stream

    authenticated, api_user = await authenticate_websocket(websocket, token)
    if not authenticated:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    try:
        settings = await _require_voice_enabled()
        row = await _require_voice_session(session_id)
        if api_user and row.get("api_user_id") != api_user["id"]:
            raise HTTPException(403, "This voice session belongs to another user.")
        _reject_when_capped(session_id)
        config = session_config(row, settings)
        if transport_for(config["model"]) != "stream":
            raise HTTPException(400, "This voice model uses WebRTC.")
        provider = await run_sync(_resolve_provider_row, config["provider_id"], None)
        key = await _provider_api_key(provider)
        if session_id in voice_stream.calls:
            raise HTTPException(409, "This voice session already has an audio connection.")
        if len(voice_stream.calls) >= voice_stream.MAX_CONNECTIONS:
            raise HTTPException(429, "Too many voice calls are connected.")
        call = voice_stream.VoiceStream(websocket, row, config, key, settings["voice_max_call_minutes"])
        # Reserve before accepting: there is no await between capacity check
        # and reservation, so concurrent handshakes cannot exceed the bound.
        voice_stream.calls[session_id] = call
    except HTTPException as exc:
        await websocket.close(code=4400 + (exc.status_code % 100), reason=str(exc.detail)[:120])
        return
    try:
        await websocket.accept()
        await call.run()
    finally:
        if voice_stream.calls.get(session_id) is call:
            voice_stream.calls.pop(session_id, None)
