"""Voice-mode API routes (wave VW1 of docs/VOICE_MODE_PLAN.md).

Two endpoints, deliberately small:

* ``GET  /api/v1/voice/providers`` — which ``model_providers`` rows can fund a
  realtime call. OpenAI Realtime is **API-key only**: a ChatGPT/Codex
  subscription row (ruling VM3) can never be used, so those are filtered out
  here rather than failing at mint time.
* ``POST /api/v1/voice/sessions`` — create the session row (ruling VM5: a voice
  session is a REAL ``sessions`` row, created eagerly, ``mode='voice'``) and
  mint a short-lived OpenAI client secret for the browser's WebRTC leg, so the
  provider's API key never leaves this process.

The call surface (register call_id / hangup / state) and the sideband bridge
are wave VW3 and deliberately absent here.
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.auth import require_auth
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["Voice"])

# OpenAI's ephemeral-credential endpoint. The returned secret is what the
# browser uses to open its WebRTC connection; the real API key stays here.
OPENAI_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

# How long a minted secret stays usable (OpenAI's own default is 600s). It only
# has to survive the browser's connect handshake.
CLIENT_SECRET_TTL_SECONDS = 600

# Input-transcription model for the user's side of the conversation. Kept here
# because VW1 mints with a MINIMAL session config — the VW3 sideband owns the
# full ``session.update`` (instructions, tools, VAD, retention).
INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

# Upstream mint is a single small request; fail fast rather than hanging the
# user's "start call" click.
MINT_TIMEOUT_SECONDS = 15.0


# ============================================================================
# Provider eligibility
# ============================================================================

def _is_eligible_voice_provider(row: Dict[str, Any]) -> bool:
    """Whether a provider row can fund an OpenAI Realtime call.

    Requires an enabled ``provider_type='openai'`` row carrying a stored API
    key. ChatGPT/Codex subscription rows are excluded via the same discriminator
    the omni credential layer uses — Realtime has no subscription-OAuth path
    (ruling VM3).
    """
    from app.core.provider.omni_credentials import _is_codex_subscription

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
    return [
        {"id": row["id"], "name": row.get("name") or row["id"]}
        for row in rows
        if _is_eligible_voice_provider(row)
    ]


def _eligible_provider_row(provider_id: str) -> Optional[Dict[str, Any]]:
    """The full (token-carrying) provider row for ``provider_id``, if eligible."""
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


class VoiceSessionCreateRequest(BaseModel):
    """Start a voice session. Every field is optional."""
    profile_id: Optional[str] = None
    project_id: Optional[str] = None
    provider_id: Optional[str] = None


class VoiceSessionResponse(BaseModel):
    """The new session plus the browser's short-lived realtime credential."""
    session_id: str
    client_secret: str
    expires_at: Optional[int] = None
    model: str


# ============================================================================
# Endpoints
# ============================================================================

@router.get("/providers", response_model=List[VoiceProvider])
async def get_voice_providers(token: str = Depends(require_auth)):
    """List the provider rows eligible to fund an OpenAI Realtime call.

    An empty list is the signal for the UI's "add an OpenAI API-key provider"
    setup pointer (ruling VM3).
    """
    return await run_sync(list_eligible_voice_providers)


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
                    "enabled OpenAI provider that stores an API key (Realtime "
                    "does not support ChatGPT-subscription logins)."
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
                    "eligible — it must be an enabled OpenAI provider with a "
                    "stored API key. Update the voice settings."
                ),
            )
        return row

    eligible = list_eligible_voice_providers()
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


async def _mint_client_secret(api_key: str, model: str) -> Dict[str, Any]:
    """POST OpenAI's client-secrets endpoint and return the parsed body.

    Our session config stays MINIMAL here (type/model/input transcription): the
    VW3 sideband owns instructions, tools and VAD via ``session.update``.
    """
    from app.core.http_client import get_http_client

    payload = {
        "expires_after": {"anchor": "created_at", "seconds": CLIENT_SECRET_TTL_SECONDS},
        "session": {
            "type": "realtime",
            "model": model,
            "audio": {
                "input": {"transcription": {"model": INPUT_TRANSCRIPTION_MODEL}}
            },
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


@router.post("/sessions", response_model=VoiceSessionResponse)
async def create_voice_session(
    request: VoiceSessionCreateRequest,
    token: str = Depends(require_auth),
):
    """Create a voice session row and mint its realtime client secret.

    Order matters: the upstream mint happens BEFORE the row is written, so a
    rejected mint leaves no orphaned session on the board.
    """
    from app.api.settings import get_voice_settings

    voice_settings = await run_sync(get_voice_settings)
    if not voice_settings["voice_mode_enabled"]:
        raise HTTPException(
            status_code=403,
            detail="Voice mode is disabled. Enable it in Settings → Voice.",
        )

    provider_row = await run_sync(
        _resolve_provider_row, request.provider_id, voice_settings["voice_provider_id"]
    )

    # Reuse the omni credential layer's decryption (crypto lives in exactly one
    # place); an eligible row always has a token, but a locked encryption
    # service can still yield nothing.
    from app.core.provider.omni_credentials import _decrypt_auth_token

    api_key = await run_sync(_decrypt_auth_token, provider_row)
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=(
                f"The API key for provider {provider_row.get('name') or provider_row['id']!r} "
                "could not be read. Re-enter it in Settings → Providers."
            ),
        )

    model = voice_settings["voice_model"]
    body = await _mint_client_secret(api_key, model)
    client_secret, expires_at = _extract_client_secret(body)

    session_id = str(uuid.uuid4())
    await run_sync(
        database.create_session,
        session_id=session_id,
        profile_id=request.profile_id or "default",
        project_id=request.project_id,
        title="Voice session",
        provider_id=provider_row["id"],
        mode="voice",
    )
    logger.info(
        "Voice: created session %s on provider %s (model %s)",
        session_id, provider_row["id"], model,
    )

    return VoiceSessionResponse(
        session_id=session_id,
        client_secret=client_secret,
        expires_at=expires_at,
        model=model,
    )
