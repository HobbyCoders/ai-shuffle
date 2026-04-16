"""
Realtime voice — provider selection + catalog endpoints.

Backs the Settings → Voice UI (Phase 3). Keeps provider choice, voice ID,
turn detector, interruption toggle, and budget caps in system_settings so
the pipeline can pick them up on next call without redeploy.

Endpoints:
    GET    /voice/config                 — current config (authed)
    PATCH  /voice/config                 — update config (admin)
    GET    /voice/catalogs/{provider}    — list voices from Cartesia / ElevenLabs
    GET    /voice/pricing                — static pricing reference
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_admin, require_auth
from app.api.settings_crypto import get_decrypted_api_key
from app.db import database
from app.voice import pricing as voice_pricing
from app.voice.providers import (
    DEFAULT_CARTESIA_VOICE,
    DEFAULT_ELEVENLABS_VOICE,
    DEFAULT_LLM_MODEL,
    DEFAULT_MAX_DURATION,
    DEFAULT_STT_PROVIDER,
    DEFAULT_TTS_PROVIDER,
    DEFAULT_TURN_DETECTOR,
    INTERRUPTIONS_KEY,
    LLM_MODEL_KEY,
    MAX_DURATION_KEY,
    STT_PROVIDER_KEY,
    TTS_PROVIDER_KEY,
    TTS_VOICE_ID_KEY,
    TURN_DETECTOR_KEY,
    USE_AGENT_SDK_KEY,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/voice", tags=["Voice"])


VALID_STT = {"deepgram", "groq"}
VALID_TTS = {"cartesia", "elevenlabs"}
VALID_TURN = {"vad", "semantic"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class VoiceConfigResponse(BaseModel):
    stt_provider: str
    tts_provider: str
    tts_voice_id: str
    turn_detector: str
    llm_model: str
    interruptions_enabled: bool
    max_duration_seconds: int
    use_agent_sdk: bool
    # Availability flags (based on configured API keys)
    deepgram_available: bool
    groq_available: bool
    cartesia_available: bool
    elevenlabs_available: bool


class VoiceConfigPatch(BaseModel):
    stt_provider: Optional[str] = Field(None, description="deepgram | groq")
    tts_provider: Optional[str] = Field(None, description="cartesia | elevenlabs")
    tts_voice_id: Optional[str] = None
    turn_detector: Optional[str] = Field(None, description="vad | semantic")
    llm_model: Optional[str] = None
    interruptions_enabled: Optional[bool] = None
    max_duration_seconds: Optional[int] = Field(None, ge=30, le=3600)
    use_agent_sdk: Optional[bool] = None


class VoiceCatalogEntry(BaseModel):
    id: str
    name: str
    language: Optional[str] = None
    description: Optional[str] = None


class VoiceCatalogResponse(BaseModel):
    provider: str
    voices: list[VoiceCatalogEntry]
    cached_at: float


class PricingEntry(BaseModel):
    provider: str
    model: str
    unit: str
    price_usd: float


class PricingResponse(BaseModel):
    stt: list[PricingEntry]
    tts: list[PricingEntry]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@router.get("/config", response_model=VoiceConfigResponse)
async def get_voice_config(token: str = Depends(require_auth)):
    def _bool(key: str, default: bool) -> bool:
        raw = database.get_system_setting(key)
        if raw is None:
            return default
        return raw != "false" if default else raw == "true"

    def _int(key: str, default: int) -> int:
        raw = database.get_system_setting(key)
        try:
            return int(raw) if raw else default
        except (TypeError, ValueError):
            return default

    tts_provider = (
        database.get_system_setting(TTS_PROVIDER_KEY) or DEFAULT_TTS_PROVIDER
    ).lower()
    tts_voice_default = (
        DEFAULT_ELEVENLABS_VOICE if tts_provider == "elevenlabs" else DEFAULT_CARTESIA_VOICE
    )

    return VoiceConfigResponse(
        stt_provider=(
            database.get_system_setting(STT_PROVIDER_KEY) or DEFAULT_STT_PROVIDER
        ).lower(),
        tts_provider=tts_provider,
        tts_voice_id=database.get_system_setting(TTS_VOICE_ID_KEY) or tts_voice_default,
        turn_detector=(
            database.get_system_setting(TURN_DETECTOR_KEY) or DEFAULT_TURN_DETECTOR
        ).lower(),
        llm_model=database.get_system_setting(LLM_MODEL_KEY) or DEFAULT_LLM_MODEL,
        interruptions_enabled=_bool(INTERRUPTIONS_KEY, True),
        max_duration_seconds=_int(MAX_DURATION_KEY, DEFAULT_MAX_DURATION),
        use_agent_sdk=_bool(USE_AGENT_SDK_KEY, False),
        deepgram_available=bool(get_decrypted_api_key("deepgram_api_key")),
        groq_available=bool(get_decrypted_api_key("groq_api_key")),
        cartesia_available=bool(get_decrypted_api_key("cartesia_api_key")),
        elevenlabs_available=bool(get_decrypted_api_key("elevenlabs_api_key")),
    )


@router.patch("/config")
async def update_voice_config(
    patch: VoiceConfigPatch,
    token: str = Depends(require_admin),
):
    updates: dict[str, str] = {}

    if patch.stt_provider is not None:
        val = patch.stt_provider.lower()
        if val not in VALID_STT:
            raise HTTPException(400, f"stt_provider must be one of {sorted(VALID_STT)}")
        updates[STT_PROVIDER_KEY] = val

    if patch.tts_provider is not None:
        val = patch.tts_provider.lower()
        if val not in VALID_TTS:
            raise HTTPException(400, f"tts_provider must be one of {sorted(VALID_TTS)}")
        updates[TTS_PROVIDER_KEY] = val

    if patch.tts_voice_id is not None:
        updates[TTS_VOICE_ID_KEY] = patch.tts_voice_id

    if patch.turn_detector is not None:
        val = patch.turn_detector.lower()
        if val not in VALID_TURN:
            raise HTTPException(400, f"turn_detector must be one of {sorted(VALID_TURN)}")
        updates[TURN_DETECTOR_KEY] = val

    if patch.llm_model is not None:
        updates[LLM_MODEL_KEY] = patch.llm_model

    if patch.interruptions_enabled is not None:
        updates[INTERRUPTIONS_KEY] = "true" if patch.interruptions_enabled else "false"

    if patch.max_duration_seconds is not None:
        updates[MAX_DURATION_KEY] = str(patch.max_duration_seconds)

    if patch.use_agent_sdk is not None:
        updates[USE_AGENT_SDK_KEY] = "true" if patch.use_agent_sdk else "false"

    for k, v in updates.items():
        database.set_system_setting(k, v)

    return {"success": True, "updated": list(updates.keys())}


# ---------------------------------------------------------------------------
# Voice catalogs (cached per-provider for 24h)
# ---------------------------------------------------------------------------

_CATALOG_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CATALOG_TTL_SECONDS = 24 * 60 * 60


@router.get("/catalogs/{provider}", response_model=VoiceCatalogResponse)
async def get_voice_catalog(
    provider: str,
    token: str = Depends(require_auth),
):
    provider = provider.lower()
    if provider not in VALID_TTS:
        raise HTTPException(404, f"Unknown TTS provider: {provider}")

    now = time.time()
    cached = _CATALOG_CACHE.get(provider)
    if cached and now - cached[0] < _CATALOG_TTL_SECONDS:
        return VoiceCatalogResponse(
            provider=provider,
            voices=[VoiceCatalogEntry(**v) for v in cached[1]],
            cached_at=cached[0],
        )

    voices: list[dict] = []
    if provider == "cartesia":
        voices = await _fetch_cartesia_voices()
    elif provider == "elevenlabs":
        voices = await _fetch_elevenlabs_voices()

    _CATALOG_CACHE[provider] = (now, voices)
    return VoiceCatalogResponse(
        provider=provider,
        voices=[VoiceCatalogEntry(**v) for v in voices],
        cached_at=now,
    )


async def _fetch_cartesia_voices() -> list[dict]:
    key = get_decrypted_api_key("cartesia_api_key")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.cartesia.ai/voices",
                headers={"X-API-Key": key, "Cartesia-Version": "2024-06-10"},
            )
            if r.status_code != 200:
                logger.warning("Cartesia voices fetch failed: %s", r.status_code)
                return []
            data = r.json() or []
    except Exception as e:
        logger.warning("Cartesia voices fetch error: %s", e)
        return []

    return [
        {
            "id": v.get("id", ""),
            "name": v.get("name", "Unnamed"),
            "language": v.get("language"),
            "description": v.get("description"),
        }
        for v in data
        if isinstance(v, dict) and v.get("id")
    ]


async def _fetch_elevenlabs_voices() -> list[dict]:
    key = get_decrypted_api_key("elevenlabs_api_key")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": key},
            )
            if r.status_code != 200:
                logger.warning("ElevenLabs voices fetch failed: %s", r.status_code)
                return []
            payload = r.json() or {}
    except Exception as e:
        logger.warning("ElevenLabs voices fetch error: %s", e)
        return []

    out: list[dict] = []
    for v in payload.get("voices", []):
        if not isinstance(v, dict):
            continue
        vid = v.get("voice_id")
        if not vid:
            continue
        labels = v.get("labels") or {}
        desc_parts = [labels.get(k) for k in ("accent", "description", "age", "gender")]
        out.append(
            {
                "id": vid,
                "name": v.get("name", "Unnamed"),
                "language": labels.get("language"),
                "description": " · ".join(p for p in desc_parts if p) or None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Pricing reference
# ---------------------------------------------------------------------------


@router.get("/pricing", response_model=PricingResponse)
async def get_pricing(token: str = Depends(require_auth)):
    stt = [
        PricingEntry(
            provider=key.split("-", 1)[0],
            model=key.split("-", 1)[1] if "-" in key else key,
            unit="minute",
            price_usd=rate,
        )
        for key, rate in voice_pricing.STT_PRICE_PER_MIN.items()
    ]
    tts = [
        PricingEntry(
            provider=key.split("-", 1)[0],
            model=key.split("-", 1)[1] if "-" in key else key,
            unit="1K chars",
            price_usd=rate,
        )
        for key, rate in voice_pricing.TTS_PRICE_PER_1K_CHARS.items()
    ]
    return PricingResponse(stt=stt, tts=tts)
