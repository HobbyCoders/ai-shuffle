"""Voice-mode API routes.

Deliberately small surface:

* ``GET  /api/v1/voice/providers`` — which ``model_providers`` rows can fund a
  realtime call. OpenAI Realtime is **API-key only**: a ChatGPT/Codex
  subscription row can never be used, so those are filtered out
  here rather than failing at mint time.
* ``POST /api/v1/voice/sessions`` — create the session row (a voice
  session is a REAL ``sessions`` row, created eagerly, ``mode='voice'``) and
  mint a short-lived OpenAI client secret for the browser's WebRTC leg, so the
  provider's API key never leaves this process.
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

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.auth import get_api_user_from_request, require_auth
from app.api.sessions_helpers import check_session_access_for_api_user
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
# secret is ALSO the sideband's primary credential (an API-key sideband
# handshake can 404 on browser-created calls; the call's own secret connects),
# so it must outlive the whole realtime session (OpenAI caps one at 60 minutes;
# the FE reconnects with a fresh secret near that wall), hence the maximum.
CLIENT_SECRET_TTL_SECONDS = 7200

# Upstream mint is a single small request; fail fast rather than hanging the
# user's "start call" click.
MINT_TIMEOUT_SECONDS = 15.0

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
    the fluid credential layer uses — Realtime has no subscription-OAuth path.
    """
    from app.core.provider.fluid_credentials import _is_codex_subscription

    if row.get("provider_type") != "openai":
        return False
    if not row.get("enabled"):
        return False
    if _is_codex_subscription(row):
        return False
    return bool(row.get("auth_token"))


def _fal_key_for(api_user: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """The fal.ai key ``api_user`` may spend, and the policy that chose it.

    Same resolution as ``/transcribe`` and ``/tts/stream``
    (:func:`app.api.settings_audio._resolve_fal_key`): the owner and the
    owner's paired devices (``api_user is None``) spend the owner's key; an API
    user gets only what its fal.ai credential policy allows. Synchronous.
    """
    from app.api.settings_audio import _resolve_fal_key

    return _resolve_fal_key(api_user)


def list_eligible_voice_providers(
    api_user: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """``[{id, name}]`` for every provider row eligible to fund voice calls.

    Synchronous (call via ``run_sync`` from async code). Deliberately returns
    only id + name: the rows are read with ``include_token=True`` so eligibility
    can test for a stored key, and that blob must never travel outward.

    The OpenAI rows are the owner's providers for every caller (as for the
    OpenAI audio models in :mod:`app.api.settings_audio`; there is no per-user
    OpenAI policy). fal.ai is listed when the caller's fal key resolves.
    """
    rows = database.get_enabled_model_providers(include_token=True)
    providers = [
        {"id": row["id"], "name": row.get("name") or row["id"]}
        for row in rows
        if _is_eligible_voice_provider(row)
    ]
    if api_user is None:
        from app.core import fal_credential

        fal_available = fal_credential.is_enabled() and fal_credential.key_stored()
    else:
        fal_available = bool(_fal_key_for(api_user)[0])
    if fal_available:
        providers.append({"id": FAL_PROVIDER_ID, "name": "fal.ai"})
    return providers


def _eligible_provider_row(
    provider_id: str, api_user: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """The full (token-carrying) provider row for ``provider_id``, if eligible.

    The fal.ai row carries no key. For an API user it is always returned, so
    :func:`_provider_api_key` explains a missing key in the caller's policy
    terms (403 "store your own key" / 400 "not configured") instead of a
    generic "cannot fund voice calls".
    """
    if provider_id == FAL_PROVIDER_ID:
        from app.core import fal_credential

        # A stored key keeps the row eligible while fal.ai is off, so
        # :func:`_provider_api_key` answers with the "fal.ai is off" message.
        if api_user is not None or fal_credential.key_stored():
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

    ``session_mode`` ("chat") marks a native phone call: it is validated here
    and the call attach composes its instructions appendix. Browser sessions
    send no mode and are greeted when the call opens.
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

    ``session_mode`` ("chat") is how a native client reports what kind of
    session this is; the route composes the matching instructions appendix
    into the sideband's system prompt. Browser calls omit it and get the base
    prompt only.
    """
    call_id: str
    client_tools: Optional[List[Dict[str, Any]]] = None
    client_instructions: Optional[str] = None
    session_mode: Optional[str] = None


class VoicePreviewRequest(BaseModel):
    """Which voice to synthesise a short sample for."""
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
async def get_voice_providers(http_request: Request, token: str = Depends(require_auth)):
    """List configured OpenAI and fal.ai voice providers, without credentials."""
    return await run_sync(
        list_eligible_voice_providers, get_api_user_from_request(http_request)
    )


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
    http_request: Request,
    token: str = Depends(require_auth),
):
    """Generate a short sample on the same GPT Live model used by calls."""
    from app.api.settings import get_voice_settings
    from app.core.voice_models import LIVE_VOICES
    from app.core.voice_preview import generate_live_preview

    voice = (request.voice or "").strip()
    if voice not in LIVE_VOICES:
        raise HTTPException(400, f"voice must be one of {list(LIVE_VOICES)}")
    voice_settings = await run_sync(get_voice_settings)
    api_user = get_api_user_from_request(http_request)
    provider_row = await run_sync(
        _resolve_provider_row, None, voice_settings["voice_provider_id"], api_user
    )
    api_key = await _provider_api_key(provider_row, api_user=api_user)

    finished = asyncio.Event()

    async def disconnected():
        # Starlette's is_disconnected uses an AnyIO cancel scope. A task cancel
        # racing that scope can be consumed; also use a completion flag so
        # joining this watcher can never keep a finished preview open.
        while not finished.is_set():
            if await http_request.is_disconnected():
                return
            await asyncio.sleep(.2)

    generation = asyncio.create_task(generate_live_preview(api_key, voice))
    disconnect = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({generation, disconnect}, return_when=asyncio.FIRST_COMPLETED)
        if generation not in done:
            raise HTTPException(499, "Voice preview cancelled.")
        audio = await generation
        return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except (ValueError, TimeoutError) as exc:
        raise HTTPException(502, str(exc) or "GPT Live preview timed out. Try again.") from exc
    except Exception as exc:
        logger.warning("GPT Live preview connection failed: %s", type(exc).__name__)
        raise HTTPException(502, "Could not connect to GPT Live for a voice preview. Check your OpenAI API provider.") from exc
    finally:
        finished.set()
        generation.cancel()
        disconnect.cancel()
        await asyncio.gather(generation, disconnect, return_exceptions=True)


def _resolve_provider_row(
    requested_id: Optional[str],
    settings_provider_id: Optional[str],
    api_user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Pick the provider row that funds this call, or raise a helpful 400.

    Precedence: explicit request value > the pinned ``voice_provider_id``
    setting > the single eligible row when exactly one exists. ``api_user`` is
    the caller (``None`` for the owner); it only affects the fal.ai row.
    """
    requested = (requested_id or "").strip()
    if requested:
        row = _eligible_provider_row(requested, api_user)
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
        if row is not None and row["id"] != FAL_PROVIDER_ID:
            return row
        # A stale/retired preference must not require a hidden selector to repair.


    eligible = [p for p in list_eligible_voice_providers() if p["id"] != FAL_PROVIDER_ID]
    if not eligible:
        raise HTTPException(
            status_code=400,
            detail=(
                "No OpenAI provider with a stored API key is configured. Add one "
                "in Settings > Providers before starting a voice session "
                "(Realtime does not support ChatGPT-subscription logins)."
            ),
        )
    eligible.sort(key=lambda p: (p["name"].casefold(), p["id"]))
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


async def _provider_api_key(
    provider_row: Dict[str, Any], *, api_user: Optional[Dict[str, Any]]
) -> str:
    """Decrypt the row's stored API key, or raise a 400 explaining the fix.

    Reuses the fluid credential layer's decryption so crypto lives in exactly one
    place; an eligible row always HAS a token, but a locked encryption service
    can still yield nothing.

    ``api_user`` is the caller (``None`` for the owner and paired devices), a
    required keyword so no route can forget it. The fal.ai key follows the
    caller's credential policy exactly as ``/transcribe`` and ``/tts/stream``
    do: an API user never spends the owner's fal key unless its policy allows
    it, and gets the same 403/400 refusal those routes give. OpenAI rows are
    the owner's providers for every caller (no per-user OpenAI policy exists).
    """
    from app.core.provider.fluid_credentials import _decrypt_auth_token

    if provider_row["id"] == FAL_PROVIDER_ID:
        if api_user is not None:
            from app.api.settings_audio import _require_fal_key

            fal_key, policy = await run_sync(_fal_key_for, api_user)
            return _require_fal_key(fal_key, policy)
        from app.core import fal_credential

        if not await run_sync(fal_credential.is_enabled):
            raise HTTPException(status_code=400, detail=fal_credential.OFF_MESSAGE)
        api_key = await run_sync(fal_credential.api_key)
    else:
        api_key = await run_sync(_decrypt_auth_token, provider_row)
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The API key for provider {provider_row.get('name') or provider_row['id']!r} "
                "could not be read. Re-enter it in Settings > Providers."
            ),
        )
    return api_key


async def _require_voice_enabled() -> Dict[str, Any]:
    """The voice settings, or a 403 when the mode is switched off."""
    from app.api.settings import get_voice_settings

    voice_settings = await run_sync(get_voice_settings)
    if not voice_settings["voice_mode_enabled"]:
        raise HTTPException(
            status_code=403,
            detail="Voice mode is disabled. Enable it in Settings > Voice.",
        )
    return voice_settings


def _reject_when_capped(session_id: str) -> None:
    """409 while the session sits inside the post-cap grace window.

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


async def _load_voice_session(
    session_id: str, *, check_eligible: bool = False,
) -> Dict[str, Any]:
    """A legacy voice row or an explicitly attached normal conversation.

    Ordinary sessions remain 404 until /attach pins their separate call binding.
    Attachment never changes their mode, agent, model or permission ceiling.

    Existence only. Every HTTP route goes through :func:`_require_voice_session`
    so the caller's access is checked as well; the audio WebSocket, which has
    no ``Request``, applies :func:`_check_voice_session_access` itself.

    ``check_eligible`` re-runs the attachability check, and only call starts
    pass it: hangup and state must keep working after the user archives or
    hides the chat mid-call, or the sideband keeps billing.
    """
    row = await run_sync(database.get_session, session_id)
    config = (row or {}).get("voice_config") or {}
    if isinstance(config, str):
        config = json.loads(config)
    if not row or (row.get("mode") != "voice" and not config.get("conversation_surface")):
        raise HTTPException(status_code=404, detail="Voice session not found.")
    if check_eligible and row.get("mode") != "voice":
        await run_sync(_check_attachable_conversation, row)
    return row


def _check_voice_session_access(
    api_user: Optional[Dict[str, Any]], row: Dict[str, Any],
) -> None:
    """403 unless the caller may operate this voice session.

    The install owner/admin and paired devices carry no API-user identity and
    keep full access. An API key must OWN the session (``api_user_id``), not
    merely be allowed into its project: the call surface mints realtime
    credentials, attaches and hangs up the live call, and for an admin-owned
    row hands the voice agent app-configuration tools, so project-level access
    is not enough. :func:`check_session_access_for_api_user` is that rule: it
    refuses a key that does not own the row (a worker row belongs to whoever
    owns the session that spawned it) and one whose project or agent scope
    excludes it — the same 403 as ``/api/v1/sessions/{id}``.
    """
    check_session_access_for_api_user(api_user, row)


async def _require_voice_session(
    request: Request, session_id: str, *, check_eligible: bool = False,
) -> Dict[str, Any]:
    """The voice session row the caller may operate.

    404 when the session is missing or is not a voice session — checked first,
    so a chat session never turns into a 403 — then 403 when it belongs to
    someone else (see :func:`_check_voice_session_access`).
    """
    row = await _load_voice_session(session_id, check_eligible=check_eligible)
    _check_voice_session_access(get_api_user_from_request(request), row)
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
    """
    from app.api.settings import get_voice_settings
    from app.core import voice_bridge

    session_mode = (request.session_mode or "").strip().lower() or None
    if session_mode is not None and session_mode not in voice_bridge.DEFAULT_MODE_INSTRUCTIONS:
        raise HTTPException(
            status_code=400,
            detail="session_mode must be 'chat'.",
        )

    # Voice uses the same origin authorization as normal branch sessions.
    # Prefer the legacy id when both forms are supplied by the compose store,
    # then verify that it resolves to the supplied V2 project.
    from app.api.common import require_origin_access
    from app.api.sessions_helpers import resolve_api_user_profile

    api_user = get_api_user_from_request(http_request)
    # The key that starts the call owns the session (``sessions.api_user_id``),
    # like every other session creator, so it can drive it afterwards.
    api_user_id = (api_user or {}).get("id")
    # An API user's assigned agents bound the profile (omitted → its only one).
    requested_profile_id = resolve_api_user_profile(api_user, request.profile_id)

    # The creating API key owns the row on EVERY path. The call surface,
    # ``/context`` and the audio socket all check ``api_user_id``, so a voice
    # session created without it would lock its own creator out.
    api_user_id = (get_api_user_from_request(http_request) or {}).get("id")

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
            detail="Voice mode is disabled. Enable it in Settings > Voice.",
        )

    provider_row = await run_sync(
        _resolve_provider_row, request.provider_id, voice_settings["voice_provider_id"],
        api_user,
    )

    api_key = await _provider_api_key(provider_row, api_user=api_user)

    model = voice_settings["voice_model"]
    if (model == PERSONAPLEX_MODEL) != (provider_row["id"] == FAL_PROVIDER_ID):
        raise HTTPException(400, "The voice model does not belong to the selected provider.")
    options = voice_settings.get("voice_options", {})
    selected_model, selected_effort = request.model, request.effort
    if model == LIVE_MODEL and not (selected_model or "").strip():
        raise HTTPException(400, "Choose a chat model in the composer before starting GPT Live.")
    client_secret, expires_at = "", None
    if transport_for(model) == "webrtc":
        body = await _mint_client_secret(api_key, model, voice_settings["voice_voice"], options)
        client_secret, expires_at = _extract_client_secret(body)

    # Resolve the profile the same way normal session creation does (explicit →
    # project default → first configured agent). A literal fallback id here hits
    # the sessions.profile_id FK the moment no such agent row exists.
    profile_id = requested_profile_id
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
            branch_request,
            access.get("legacy"),
            api_user_id,
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
            api_user_id=api_user_id,
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
# Call surface — re-mint, attach, hangup, state
# ============================================================================

def _check_attachable_conversation(row):
    from app.core.config import settings
    if (row.get("instance_id") != settings.effective_instance_id
            or row.get("is_worker") or row.get("parent_session_id")
            or row.get("hidden") or row.get("status") == "archived"):
        raise HTTPException(404, "Conversation is not eligible for call attachment.")
    from app.db import turns
    turn = turns.get_live_turn(row["id"]) or turns.get_latest_turn(row["id"]) or {}
    if ((turn.get("payload") or {}).get("_assistant_proactive") is not None
            or (turn.get("overrides") or {}).get("_assistant_proactive") is not None):
        raise HTTPException(403, "Background observation cannot acquire a voice surface.")


class VoiceAttachRequest(BaseModel):
    # Call funding only; never rebinds the conversation agent/text provider.
    provider_id: Optional[str] = None


@router.post("/sessions/{session_id}/attach", response_model=VoiceSessionResponse)
async def prepare_voice_attachment(
    http_request: Request, session_id: str, request: VoiceAttachRequest,
    token: str = Depends(require_auth),
):
    """Attach audio to an existing conversation without creating another history.

    Same response/handshake as POST /sessions. Saved voice bindings are reused;
    first attachment pins current voice settings independently of text bindings.
    """
    voice_settings = await _require_voice_enabled()
    row = await run_sync(database.get_session, session_id)
    if not row:
        raise HTTPException(404, "Session not found.")
    _check_voice_session_access(get_api_user_from_request(http_request), row)
    if row.get("mode") != "voice":
        await run_sync(_check_attachable_conversation, row)
    _reject_when_capped(session_id)
    config = row.get("voice_config")
    if isinstance(config, str):
        config = json.loads(config)
    if not config:
        config = {
            "model": voice_settings["voice_model"], "voice": voice_settings["voice_voice"],
            "provider_id": request.provider_id or voice_settings.get("voice_provider_id"),
            "options": voice_settings.get("voice_options", {}), "greet": False,
        }
    elif request.provider_id and request.provider_id != config.get("provider_id"):
        raise HTTPException(400, "This conversation already has a saved voice provider binding.")
    api_user = get_api_user_from_request(http_request)
    provider_row = await run_sync(_resolve_provider_row, config.get("provider_id"), None, api_user)
    api_key = await _provider_api_key(provider_row, api_user=api_user)
    if (config["model"] == PERSONAPLEX_MODEL) != (provider_row["id"] == FAL_PROVIDER_ID):
        raise HTTPException(400, "The voice model does not belong to the selected provider.")
    config = {**config, "provider_id": provider_row["id"],
              "conversation_surface": row.get("mode") != "voice"}
    secret, expires_at = "", None
    if transport_for(config["model"]) == "webrtc":
        body = await _mint_client_secret(api_key, config["model"], config["voice"], config.get("options"))
        secret, expires_at = _extract_client_secret(body)
    from app.db.sessions import set_session_voice_binding
    await run_sync(set_session_voice_binding, session_id, config)
    _remember_client_secret(session_id, secret, expires_at)
    return VoiceSessionResponse(session_id=session_id, client_secret=secret,
                                expires_at=expires_at, model=config["model"],
                                options=config.get("options", {}))


@router.post("/sessions/{session_id}/secret", response_model=VoiceSecretResponse)
async def mint_voice_session_secret(
    http_request: Request,
    session_id: str,
    token: str = Depends(require_auth),
):
    """Re-mint the browser's realtime credential for an EXISTING voice session.

    Two callers: reopening a voice session later, and the FE's reconnect as the
    call approaches OpenAI's session wall — it re-handshakes with
    a fresh secret, gets a new ``call_id``, and posts it to ``/call``, which
    replaces the sideband and reseeds context.

    The session's own ``provider_id`` (stamped at creation) leads the provider
    resolution, so a call keeps being funded by the row it started on.
    """
    voice_settings = await _require_voice_enabled()
    api_user = get_api_user_from_request(http_request)
    session_row = await _require_voice_session(http_request, session_id)
    _reject_when_capped(session_id)

    config = session_config(session_row, voice_settings)
    provider_row = await run_sync(_resolve_provider_row, config["provider_id"], None, api_user)
    api_key = await _provider_api_key(provider_row, api_user=api_user)

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
    http_request: Request,
    session_id: str,
    request: VoiceCallRequest,
    token: str = Depends(require_auth),
):
    """Join the browser's realtime call from the backend.

    Idempotent in the useful sense: posting a NEW ``call_id`` for a session that
    already has a bridge replaces it (the old attachment is closed first), which
    is exactly what the reconnect path needs — and the replacement keeps the
    call's epoch, so the length cap is not restarted by reconnecting.

    Answers 409 inside the post-cap grace window (see :func:`_reject_when_capped`).
    """
    from app.core import voice_bridge

    voice_settings = await _require_voice_enabled()
    api_user = get_api_user_from_request(http_request)
    session_row = await _require_voice_session(
        http_request, session_id, check_eligible=True,
    )
    _reject_when_capped(session_id)

    call_id = (request.call_id or "").strip()
    if not call_id:
        raise HTTPException(status_code=400, detail="call_id is required.")

    client_tools = _sanitize_client_tools(
        request.client_tools,
        reserved_names={t["name"] for t in voice_bridge.VOICE_TOOLS}
        | {t["name"] for t in voice_bridge.VOICE_WEB_TOOLS}
        | {"submit_to_chat"},
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
            detail="session_mode must be 'chat'.",
        )

    # Compose the agent's system prompt: the base contract, plus the appendix
    # for the client's session mode (browser calls have none).
    config = session_config(session_row, voice_settings)
    from app.core.voice_options import effective_options
    options = effective_options(config)
    instructions_override = options["instructions"] or voice_bridge.VOICE_INSTRUCTIONS
    if session_row.get("mode") != "voice":
        # Audio is an overlay, not a new agent. Substantive work runs through
        # the same conversation queue, profile, model and permission ceiling.
        instructions_override = await run_sync(voice_bridge.conversation_instructions, session_row)
        if client_tools:
            raise HTTPException(400, "Conversation calls do not expose client-side action tools.")
        if client_instructions:
            raise HTTPException(400, "Conversation call instructions come from the session agent.")
    if session_mode is not None:
        appendix = voice_bridge.DEFAULT_MODE_INSTRUCTIONS[session_mode]
        instructions_override = f"{instructions_override}\n\n{appendix}"

    # The app-configuration tools the voice agent may use directly. Same
    # admission rule as a chat session's app-config server: never for an
    # API-key user. Always available to the owner, independent of legacy
    # settings. Admin is asserted for write tools as chat does (``is_admin=True``)
    # because the caller has already passed the same cookie/pairing gate.
    # Both the caller and the row must be the owner's: a worker row carries no
    # ``api_user_id`` yet belongs to its parent's key, which may attach to it.
    app_tools: List[Any] = []
    if api_user is None and not session_row.get("api_user_id") and session_row.get("mode") == "voice":
        app_tools = await run_sync(
            _voice_app_tools, session_row.get("project_id"),
        )
        from app.mcp.chat_access_sdk import create_chat_access_server, GUIDANCE
        from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
        app_tools.extend(recorded_sdk_server_tools(create_chat_access_server(session_id)))
        instructions_override = f"{instructions_override}\n\n{GUIDANCE}"

    # A browser call opens with a greeting; a native session must stay silent
    # until the user speaks (its mode appendix says so), so it never greets.
    greet = session_mode is None

    config = session_config(session_row, voice_settings)
    if transport_for(config["model"]) != "webrtc":
        raise HTTPException(400, "This voice model uses the audio stream, not the Realtime sideband.")
    provider_row = await run_sync(_resolve_provider_row, config["provider_id"], None, api_user)
    api_key = await _provider_api_key(provider_row, api_user=api_user)

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
            conversation_surface=session_row.get("mode") != "voice",
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
    http_request: Request,
    session_id: str,
    token: str = Depends(require_auth),
):
    """Detach and stop the session's bridge. ``ok`` even when there was none.

    Tears down ONLY the bridge and the realtime session — dispatched workers
    deliberately survive the call ending. No flag gate: ending a
    call must keep working even if voice mode was switched off mid-call.
    """
    from app.core import voice_bridge

    row = await _require_voice_session(http_request, session_id)
    _forget_client_secret(session_id)
    from app.core import voice_stream
    stream_stopped = await voice_stream.stop(session_id)
    stopped = await voice_bridge.registry.detach(session_id) or stream_stopped

    # Creation stamps ``is_active`` True and, until this, nothing in the voice
    # lane ever wrote it back — so every finished call left a session card that
    # looked live forever. The bridge clears it when there IS one to stop, and
    # a hangup after the sideband already dropped is exactly the case that
    # needs the write here. An attached text conversation keeps its own Active
    # state: hanging up a call must not move the chat out of the Active group.
    try:
        if row.get("mode") == "voice":
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
    http_request: Request,
    session_id: str,
    token: str = Depends(require_auth),
):
    """Whether this session has a live sideband, and whether it is connected.

    ``active`` without ``connected`` is the FE's cue that the sideband dropped:
    the server never reconnects on its own; reconnect is the FE's job, with a
    fresh ``call_id``.
    """
    from app.core import voice_bridge

    await _require_voice_session(http_request, session_id)
    from app.core import voice_stream
    return VoiceStateResponse(**(voice_stream.state(session_id) or voice_bridge.registry.state(session_id)))

@router.get("/sessions/{session_id}/config")
async def get_voice_session_config(
    http_request: Request,
    session_id: str,
    token: str = Depends(require_auth),
):
    from app.api.settings import get_voice_settings
    row = await _require_voice_session(http_request, session_id)
    return session_config(row, await run_sync(get_voice_settings))


@router.websocket("/sessions/{session_id}/audio")
async def voice_audio_socket(websocket: WebSocket, session_id: str, token: Optional[str] = Query(None)):
    from app.api.ws_auth import authenticate_websocket
    from app.api.ws_key_watch import watch_api_key
    from app.core import voice_stream

    authenticated, api_user = await authenticate_websocket(websocket, token)
    if not authenticated:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    try:
        settings = await _require_voice_enabled()
        row = await _load_voice_session(session_id, check_eligible=True)
        _check_voice_session_access(api_user, row)
        _reject_when_capped(session_id)
        config = session_config(row, settings)
        if transport_for(config["model"]) != "stream":
            raise HTTPException(400, "This voice model uses WebRTC.")
        provider = await run_sync(_resolve_provider_row, config["provider_id"], None, api_user)
        key = await _provider_api_key(provider, api_user=api_user)
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
        # A revoked API key loses the call and the provider connection it
        # pays for (app.api.ws_key_watch).
        async with watch_api_key(websocket, on_revoke=lambda: call.close()):
            await call.run()
    finally:
        if voice_stream.calls.get(session_id) is call:
            voice_stream.calls.pop(session_id, None)
