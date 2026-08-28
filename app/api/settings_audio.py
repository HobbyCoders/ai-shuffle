"""
Settings API routes - audio (TTS/STT) endpoints.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_admin, require_auth
from app.api.settings_crypto import get_decrypted_api_key
from app.api.settings_media import get_model_params
from app.services.fal_schema import schema_service, ModelSchema

logger = logging.getLogger(__name__)

audio_router = APIRouter(tags=["Settings"])

# ============================================================================
# Audio Model Data
# ============================================================================

TTS_MODELS = {
    "fal-ai/kokoro/american-english": {
        "name": "Kokoro (American English)",
        "description": "Lightweight, fast, 19 voices, multi-language support",
        "price_display": "$0.02/1K chars",
        "voices": [
            "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica",
            "af_kore", "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
            "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam",
            "am_michael", "am_onyx", "am_puck", "am_santa"
        ],
        "supports_speed": True,
        "speed_range": [0.1, 5.0]
    },
    "fal-ai/minimax/speech-2.8-hd": {
        "name": "MiniMax Speech 2.8 HD",
        "description": "High-definition speech generation with multiple voices",
        "price_display": "~$0.03/1K chars",
        "voices": [
            "Wise_Woman", "English_Trustworthy_Female", "English_Trustworthy_Male",
            "English_Gentle_Female", "English_Friendly_Male", "English_Lively_Female"
        ],
        "supports_speed": True,
        "speed_range": [0.5, 2.0]
    },
    "fal-ai/minimax/speech-02-hd": {
        "name": "MiniMax Speech 02 HD",
        "description": "Advanced text-to-speech with natural prosody",
        "price_display": "~$0.02/1K chars",
        "voices": [
            "Wise_Woman", "English_Trustworthy_Female", "English_Trustworthy_Male",
            "English_Gentle_Female", "English_Friendly_Male", "English_Lively_Female"
        ],
        "supports_speed": True,
        "speed_range": [0.5, 2.0]
    },
    "fal-ai/chatterbox/text-to-speech": {
        "name": "Chatterbox",
        "description": "Expressive TTS by Resemble AI -- great for memes, games, AI agents",
        "price_display": "~$0.02/1K chars",
        "voices": [],
        "supports_speed": False
    },
    "fal-ai/dia-tts/voice-clone": {
        "name": "Dia TTS",
        "description": "Nari Labs Dia — voice cloning and multi-speaker dialogue TTS",
        "price_display": "~$0.04/1K chars",
        "voices": [],
        "supports_speed": False
    },
    "fal-ai/elevenlabs/tts/eleven-v3": {
        "name": "ElevenLabs v3",
        "description": "ElevenLabs flagship v3 — 29+ languages, streaming, 21 voices",
        "price_display": "~$0.10/1K chars",
        "voices": [
            "Aria", "Sarah", "Laura", "Charlotte", "Alice", "Matilda", "Jessica",
            "Lily", "Rachel", "River", "Roger", "Charlie", "George", "Callum",
            "Liam", "Will", "Eric", "Chris", "Brian", "Daniel", "Bill"
        ],
        "supports_speed": True,
        "speed_range": [0.7, 1.2]
    },
    "fal-ai/elevenlabs/tts/multilingual-v2": {
        "name": "ElevenLabs Multilingual v2",
        "description": "ElevenLabs 29-language TTS — best accent accuracy and stability",
        "price_display": "~$0.10/1K chars",
        "voices": [
            "Aria", "Sarah", "Laura", "Charlotte", "Alice", "Matilda", "Jessica",
            "Lily", "Rachel", "River", "Roger", "Charlie", "George", "Callum",
            "Liam", "Will", "Eric", "Chris", "Brian", "Daniel", "Bill"
        ],
        "supports_speed": True,
        "speed_range": [0.7, 1.2]
    },
    "fal-ai/elevenlabs/tts/turbo-v2.5": {
        "name": "ElevenLabs Turbo v2.5",
        "description": "ElevenLabs low-latency TTS — 32 languages, ideal for real-time apps",
        "price_display": "~$0.05/1K chars",
        "voices": [
            "Aria", "Sarah", "Laura", "Charlotte", "Alice", "Matilda", "Jessica",
            "Lily", "Rachel", "River", "Roger", "Charlie", "George", "Callum",
            "Liam", "Will", "Eric", "Chris", "Brian", "Daniel", "Bill"
        ],
        "supports_speed": True,
        "speed_range": [0.7, 1.2]
    },
    "fal-ai/f5-tts": {
        "name": "F5 TTS",
        "description": "Zero-shot voice cloning from a single audio sample, multi-language",
        "price_display": "~$0.05/1K chars",
        "voices": [],
        "supports_speed": False
    },
    "fal-ai/orpheus-tts": {
        "name": "Orpheus TTS",
        "description": "Llama-based Speech-LLM — 8 emotive tags, inline expression markup",
        "price_display": "~$0.05/1K chars",
        "voices": [
            "tara", "leah", "jess", "mia", "zoe", "leo", "dan", "zac"
        ],
        "supports_speed": False
    },
    "fal-ai/vibevoice": {
        "name": "VibeVoice 1.5B",
        "description": "Microsoft VibeVoice — multi-speaker, long-form, expressive TTS",
        "price_display": "~$0.04/min",
        "voices": [
            "Frank [EN]", "Carter [EN]", "Luna [EN]", "Sage [EN]"
        ],
        "supports_speed": False
    },
}

STT_MODELS = {
    "fal-ai/whisper": {
        "name": "Whisper (Large v3)",
        "description": "Speech transcription and translation, 99+ languages, speaker diarization",
        "price_display": "~$0.005/min",
        "supports_diarization": True
    },
    "fal-ai/elevenlabs/speech-to-text": {
        "name": "ElevenLabs Scribe v1",
        "description": "ElevenLabs Scribe v1 — 99 languages, diarization, audio event tagging",
        "price_display": "~$0.03/min",
        "supports_diarization": True
    },
    "fal-ai/elevenlabs/speech-to-text/scribe-v2": {
        "name": "ElevenLabs Scribe v2",
        "description": "ElevenLabs Scribe v2 — improved accuracy, entity detection, keyterm biasing",
        "price_display": "~$0.008/min",
        "supports_diarization": True
    },
    "fal-ai/speech-to-text": {
        "name": "fal Speech-to-Text",
        "description": "fal.ai native real-time STT with streaming and punctuation",
        "price_display": "~$0.048/min",
        "supports_diarization": False
    },
}

# ============================================================================
# Pydantic Models
# ============================================================================


class AudioModelUpdateRequest(BaseModel):
    """Request to update TTS or STT model"""
    model: str


class TTSSettingsRequest(BaseModel):
    """Request to update TTS settings (model, voice, speed, output_format)"""
    model: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    output_format: Optional[str] = None


class TranscriptionResponse(BaseModel):
    """Response from voice transcription"""
    text: str
    duration_seconds: Optional[float] = None


class TTSGenerateRequest(BaseModel):
    """Request to generate TTS audio"""
    text: str
    model: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    output_format: Optional[str] = "mp3"
    # Arbitrary model-specific params from the schema-driven settings form,
    # merged onto the resolved payload (overriding inferred voice/speed).
    params: Optional[dict[str, Any]] = None


class TTSGenerateResponse(BaseModel):
    """Response from TTS generation"""
    success: bool
    audio_url: Optional[str] = None
    file_path: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None


# ============================================================================
# Helper Functions
# ============================================================================


def get_generated_audio_dir() -> Path:
    """Get the directory for generated audio files."""
    audio_dir = Path(os.environ.get("GENERATED_AUDIO_DIR", "generated-audio"))
    audio_dir.mkdir(parents=True, exist_ok=True)
    return audio_dir


def _build_tts_payload(model_id: str, text: str, voice: str, speed: float, model_info: dict) -> dict:
    """Build the correct fal.ai payload for each TTS model's API contract."""

    # Kokoro: uses "prompt" for text
    if model_id.startswith("fal-ai/kokoro"):
        payload = {"prompt": text, "voice": voice}
        if model_info.get("supports_speed") and speed != 1.0:
            payload["speed"] = speed
        return payload

    # MiniMax 2.8: uses "prompt" + voice_setting object
    if model_id == "fal-ai/minimax/speech-2.8-hd":
        payload: dict = {"prompt": text, "output_format": "url"}
        voice_setting: dict = {"voice_id": voice or "Wise_Woman"}
        if model_info.get("supports_speed") and speed != 1.0:
            voice_setting["speed"] = speed
        payload["voice_setting"] = voice_setting
        return payload

    # MiniMax 02: uses "text" + voice_setting object
    if model_id == "fal-ai/minimax/speech-02-hd":
        payload = {"text": text, "output_format": "url"}
        voice_setting = {"voice_id": voice or "Wise_Woman"}
        if model_info.get("supports_speed") and speed != 1.0:
            voice_setting["speed"] = speed
        payload["voice_setting"] = voice_setting
        return payload

    # Chatterbox: text only, no voice param
    if model_id == "fal-ai/chatterbox/text-to-speech":
        return {"text": text}

    # Dia TTS: text only
    if model_id == "fal-ai/dia-tts/voice-clone":
        return {"text": text}

    # ElevenLabs: uses "text" + capitalized "voice"
    if model_id.startswith("fal-ai/elevenlabs/tts"):
        payload = {"text": text}
        if voice:
            payload["voice"] = voice.capitalize()
        return payload

    # Orpheus: uses "text" + "voice"
    if model_id == "fal-ai/orpheus-tts":
        payload = {"text": text}
        if voice:
            payload["voice"] = voice
        return payload

    # F5-TTS: uses "gen_text"
    if model_id == "fal-ai/f5-tts":
        return {"gen_text": text}

    # VibeVoice: uses "script" + "speakers" array
    if model_id.startswith("fal-ai/vibevoice"):
        return {
            "script": f"Speaker 0: {text}",
            "speakers": [{"preset": voice or "Frank [EN]"}]
        }

    # Fallback: generic text + voice
    payload = {"text": text}
    if voice:
        payload["voice"] = voice
    if model_info.get("supports_speed") and speed != 1.0:
        payload["speed"] = speed
    return payload


# Field-name candidates used to map our generic voice/speed settings onto an
# arbitrary model's schema when the model isn't one of the curated ones.
_VOICE_FIELD_CANDIDATES = ("voice", "voice_id", "voice_name", "speaker", "preset")
_SPEED_FIELD_CANDIDATES = ("speed", "rate", "speaking_rate")


def _build_payload_from_schema(
    text: str,
    voice: Optional[str],
    speed: Optional[float],
    schema: Optional["ModelSchema"],
    extra_params: Optional[dict] = None,
) -> dict:
    """Build a fal payload for an *uncurated* TTS model from its OpenAPI schema.

    Routes the user's text into the model's primary text field, best-effort maps
    our voice/speed knobs onto matching schema fields, then layers any explicit
    per-model params (from the dynamic settings form) on top.
    """
    extra_params = extra_params or {}

    if not schema:
        # No schema available — best effort. "text" is the most common key.
        payload: dict = {"text": text}
        if voice:
            payload["voice"] = voice
        payload.update(extra_params)
        return payload

    field_names = {f.name for f in schema.fields}
    payload = {}

    primary = schema.primary_text_field or ("prompt" if "prompt" in field_names else "text")
    payload[primary] = text

    if voice:
        for cand in _VOICE_FIELD_CANDIDATES:
            if cand in field_names:
                payload[cand] = voice
                break

    if speed is not None:
        for cand in _SPEED_FIELD_CANDIDATES:
            if cand in field_names:
                spec = next(f for f in schema.fields if f.name == cand)
                lo = spec.minimum if spec.minimum is not None else 0.1
                hi = spec.maximum if spec.maximum is not None else 5.0
                payload[cand] = max(lo, min(hi, speed))
                break

    # Explicit params from the dynamic form win over our inferred mapping.
    payload.update(extra_params)
    return payload


async def _resolve_tts_payload(
    tts_model: str,
    text: str,
    voice: Optional[str],
    speed: Optional[float],
    model_info: Optional[dict],
    fal_key: Optional[str],
    extra_params: Optional[dict] = None,
) -> dict:
    """Build the fal payload for any TTS model — curated or arbitrary.

    Curated models use their hand-tuned payload shape; unknown models are
    resolved dynamically from the fal OpenAPI schema so any model just works.
    """
    if model_info:
        payload = _build_tts_payload(
            tts_model, text, voice or "", speed if speed is not None else 1.0, model_info
        )
        if extra_params:
            payload.update(extra_params)
        return payload

    schema = await schema_service.get_schema(tts_model, fal_key)
    return _build_payload_from_schema(text, voice, speed, schema, extra_params)


def _extract_audio_url(result: dict) -> Optional[str]:
    """Extract audio URL from fal.ai response (varies by model)."""
    # Most models: {audio: {url: "..."}}
    audio = result.get("audio")
    if isinstance(audio, dict):
        url = audio.get("url")
        if url:
            return url
    elif isinstance(audio, str) and audio.startswith("http"):
        return audio
    # Some models: {audio_url: "..."}
    return result.get("audio_url")


def _normalize_mime(content_type: str, fallback: str = "audio/webm") -> str:
    """Strip codec parameters (e.g. 'audio/webm;codecs=opus') and return a clean MIME."""
    if not content_type:
        return fallback
    return content_type.split(";", 1)[0].strip() or fallback


async def _upload_to_fal_storage(
    client: httpx.AsyncClient,
    fal_key: str,
    data: bytes,
    content_type: str,
    file_name: str,
) -> str:
    """
    Upload bytes to fal.ai storage and return a public file URL suitable
    for use as `audio_url` / `image_url` in fal model payloads.

    fal.ai does not accept base64 data URIs for Whisper and most STT models
    — a real HTTPS URL is required. This helper performs the two-step
    initiate+PUT flow that `fal_client.upload` wraps internally.
    """
    initiate_resp = await client.post(
        "https://rest.alpha.fal.ai/storage/upload/initiate",
        headers={
            "Authorization": f"Key {fal_key}",
            "Content-Type": "application/json",
        },
        json={"content_type": content_type, "file_name": file_name},
        timeout=30.0,
    )
    if initiate_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"fal.ai storage initiate failed: {initiate_resp.status_code} {initiate_resp.text}",
        )

    initiate = initiate_resp.json()
    upload_url = initiate.get("upload_url")
    file_url = initiate.get("file_url")
    if not upload_url or not file_url:
        raise HTTPException(
            status_code=502,
            detail="fal.ai storage initiate returned no upload_url/file_url",
        )

    put_resp = await client.put(
        upload_url,
        content=data,
        headers={"Content-Type": content_type},
        timeout=60.0,
    )
    if put_resp.status_code not in (200, 201, 204):
        raise HTTPException(
            status_code=502,
            detail=f"fal.ai storage upload failed: {put_resp.status_code} {put_resp.text}",
        )

    return file_url


# ============================================================================
# Audio Endpoints
# ============================================================================


@audio_router.get("/integrations/audio/models")
async def get_audio_models(token: str = Depends(require_auth)):
    """
    Get available TTS and STT models.
    """
    def _load() -> tuple[str | None, str, str]:
        """The key read (+ decrypt) and both model settings in ONE hop."""
        fal_key = get_decrypted_api_key("fal_api_key")
        current_tts = database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"
        current_stt = database.get_system_setting("stt_model") or "fal-ai/whisper"
        return fal_key, current_tts, current_stt

    fal_key, current_tts, current_stt = await run_sync(_load)

    tts_models = [
        {
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "price_display": info["price_display"],
            "available": bool(fal_key),
            "is_current": model_id == current_tts
        }
        for model_id, info in TTS_MODELS.items()
    ]

    stt_models = [
        {
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "price_display": info["price_display"],
            "available": bool(fal_key),
            "is_current": model_id == current_stt
        }
        for model_id, info in STT_MODELS.items()
    ]

    return {
        "tts_models": tts_models,
        "stt_models": stt_models,
        "current_tts": current_tts,
        "current_stt": current_stt,
        "fal_configured": bool(fal_key)
    }


@audio_router.patch("/integrations/audio/tts")
async def update_tts_model(
    request: AudioModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the default TTS model. Accepts any fal.ai TTS model ID.

    The catalog is the source of truth, so any text-to-speech model fal.ai
    offers is selectable; uncurated models are driven by their OpenAPI schema
    at generation time.
    """
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    # One isolated write — one thread hop, no closure needed.
    await run_sync(database.set_system_setting, "tts_model", model)
    return {"success": True, "model": model}


@audio_router.patch("/integrations/tts")
async def update_tts_settings(
    request: TTSSettingsRequest,
    token: str = Depends(require_admin)
):
    """
    Update TTS settings including model, voice, and speed.

    All fields are optional - only provided fields will be updated.
    """
    def _write() -> None:
        """Up to four writes in ONE hop, in the original order.

        The order matters and is preserved: an out-of-range speed raises AFTER
        the model/voice writes have already landed, exactly as before.
        """
        if request.model:
            model = request.model.strip()
            if model:
                database.set_system_setting("tts_model", model)

        if request.voice:
            database.set_system_setting("tts_voice", request.voice.strip())

        if request.speed is not None:
            if request.speed < 0.1 or request.speed > 5.0:
                raise HTTPException(
                    status_code=400,
                    detail="Speed must be between 0.1 and 5.0"
                )
            database.set_system_setting("tts_speed", str(request.speed))

        if request.output_format:
            fmt = request.output_format.strip().lower()
            if fmt in ("wav", "mp3", "ogg", "flac"):
                database.set_system_setting("tts_output_format", fmt)

    await run_sync(_write)
    return {"success": True}


@audio_router.patch("/integrations/audio/stt")
async def update_stt_model(
    request: AudioModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the default STT model. Accepts any fal.ai speech-to-text model ID."""
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    # One isolated write — one thread hop, no closure needed.
    await run_sync(database.set_system_setting, "stt_model", model)
    return {"success": True, "model": model}


@audio_router.get("/integrations/tts/models")
async def get_tts_models(token: str = Depends(require_auth)):
    """
    Get available TTS models with voice options.
    """
    def _load() -> tuple[str | None, str]:
        """Key read (+ decrypt) and the current model in ONE hop."""
        fal_key = get_decrypted_api_key("fal_api_key")
        current_tts = database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"
        return fal_key, current_tts

    fal_key, current_tts = await run_sync(_load)

    models = [
        {
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "price_display": info["price_display"],
            "available": bool(fal_key),
            "is_current": model_id == current_tts,
            "voices": info.get("voices", []),
            "supports_speed": info.get("supports_speed", False),
            "speed_range": info.get("speed_range", [0.1, 5.0]),
        }
        for model_id, info in TTS_MODELS.items()
    ]

    return {
        "models": models,
        "current_model": current_tts,
        "fal_configured": bool(fal_key)
    }


@audio_router.get("/integrations/stt/models")
async def get_stt_models(token: str = Depends(require_auth)):
    """
    Get available STT models.
    """
    def _load() -> tuple[str | None, str]:
        """Key read (+ decrypt) and the current model in ONE hop."""
        fal_key = get_decrypted_api_key("fal_api_key")
        current_stt = database.get_system_setting("stt_model") or "fal-ai/whisper"
        return fal_key, current_stt

    fal_key, current_stt = await run_sync(_load)

    models = [
        {
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "price_display": info["price_display"],
            "available": bool(fal_key),
            "is_current": model_id == current_stt,
            "supports_diarization": info.get("supports_diarization", False)
        }
        for model_id, info in STT_MODELS.items()
    ]

    return {
        "models": models,
        "supported_formats": ["mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "ogg", "flac"],
        "current_model": current_stt,
        "fal_configured": bool(fal_key)
    }


# ============================================================================
# Voice Transcription
# ============================================================================


@audio_router.post("/transcribe", response_model=TranscriptionResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """
    Transcribe audio to text using fal.ai's Whisper model.

    Accepts audio files (webm, mp3, mp4, wav, m4a, etc.) and returns transcribed text.
    The file is uploaded to fal.ai's file storage first, then passed to the Whisper model.
    """
    fal_key = await run_sync(get_decrypted_api_key, "fal_api_key")
    if not fal_key:
        raise HTTPException(
            status_code=400,
            detail="fal.ai API key not configured. Go to Settings > Integrations to add your key."
        )

    # Validate file type
    content_type = file.content_type or ""
    allowed_types = [
        "audio/webm", "audio/mp3", "audio/mpeg", "audio/mp4",
        "audio/wav", "audio/x-wav", "audio/m4a", "audio/x-m4a",
        "audio/ogg", "audio/flac", "video/webm"
    ]

    filename = file.filename or "audio.webm"
    allowed_extensions = [".webm", ".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".mpeg", ".flac"]
    file_ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if content_type not in allowed_types and file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format: {content_type or file_ext}. Supported: webm, mp3, mp4, wav, m4a, ogg, flac"
        )

    # Read the audio file
    try:
        audio_data = await file.read()
    except Exception:
        logger.exception("Failed to read audio file for transcription")
        raise HTTPException(status_code=400, detail="Failed to read audio file")

    if len(audio_data) == 0:
        raise HTTPException(status_code=400, detail="Audio file is empty")

    if len(audio_data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Audio file too large. Maximum size is 25MB.")

    # fal.ai Whisper (and other STT models) reject base64 `data:` URIs with
    # HTTP 422 "Unsupported data URL". Upload the bytes to fal storage first
    # to obtain a real HTTPS URL, then reference that URL in the STT payload.
    mime_type = _normalize_mime(content_type, fallback="audio/webm")

    # Call fal.ai STT model with the uploaded file URL.
    # A SECOND hop: the request body read (``await file.read()``) and the
    # size/format validation that can reject it sit between this and the key
    # read above, so the two cannot share a closure.
    stt_model = await run_sync(database.get_system_setting, "stt_model") or "fal-ai/whisper"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            audio_url = await _upload_to_fal_storage(
                client,
                fal_key,
                audio_data,
                content_type=mime_type,
                file_name=filename,
            )

            payload = {
                "audio_url": audio_url,
                "task": "transcribe",
                "chunk_level": "segment",
            }

            response = await client.post(
                f"https://fal.run/{stt_model}",
                headers={
                    "Authorization": f"Key {fal_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code == 401:
                raise HTTPException(
                    status_code=400,
                    detail="fal.ai API key is invalid. Please update it in Settings > Integrations."
                )
            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="fal.ai rate limit exceeded. Please wait a moment and try again."
                )
            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"fal.ai Whisper failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Transcription failed: {error_detail}"
                )

            result = response.json()
            transcribed_text = result.get("text", "").strip()

            if not transcribed_text:
                # Try extracting from chunks/segments
                chunks = result.get("chunks", [])
                if chunks:
                    transcribed_text = " ".join(c.get("text", "") for c in chunks).strip()

            if not transcribed_text:
                raise HTTPException(
                    status_code=500,
                    detail="Transcription returned empty result."
                )

            return TranscriptionResponse(
                text=transcribed_text,
                duration_seconds=None
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Transcription timed out. Please try with a shorter audio clip."
        )
    except Exception:
        logger.exception("fal.ai transcription failed")
        raise HTTPException(
            status_code=500,
            detail="Transcription failed"
        )


# ============================================================================
# Text-to-Speech Generation
# ============================================================================


@audio_router.post("/tts/generate", response_model=TTSGenerateResponse)
async def generate_tts(
    request: TTSGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Generate TTS audio using fal.ai.

    Returns a URL to the generated audio file.
    """
    def _load_config() -> tuple[str | None, str, str, float]:
        """The key read (+ decrypt) and the three TTS settings in ONE hop.

        Each ``request.*`` override short-circuits its read exactly as before,
        so an override still costs no query.
        """
        fal_key = get_decrypted_api_key("fal_api_key")
        if not fal_key:
            raise HTTPException(
                status_code=400,
                detail="fal.ai API key not configured. Go to Settings > Integrations to add your key."
            )

        # Get configured model or use request override. Any fal.ai TTS model is
        # accepted — curated models get tuned payloads, others are schema-driven.
        tts_model = request.model or database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"

        # Get voice and other settings
        voice = request.voice or database.get_system_setting("tts_voice") or "af_heart"
        speed = request.speed
        if speed is None:
            speed_str = database.get_system_setting("tts_speed")
            speed = float(speed_str) if speed_str else 1.0
        return fal_key, tts_model, voice, speed

    fal_key, tts_model, voice, speed = await run_sync(_load_config)
    model_info = TTS_MODELS.get(tts_model)

    # Validate text
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Text too long. Maximum 4096 characters.")

    # Validate speed for curated models that declare a supported range.
    if model_info and model_info.get("supports_speed"):
        speed_range = model_info.get("speed_range", [0.5, 2.0])
        if speed < speed_range[0] or speed > speed_range[1]:
            raise HTTPException(status_code=400, detail=f"Speed must be between {speed_range[0]} and {speed_range[1]}")

    # Stored schema-driven params (set via the dynamic settings form) are used
    # when the request doesn't override them. A SECOND hop: the text/speed
    # validation above can reject the request with a 400, and folding this read
    # into the first hop would run it for requests that never get here.
    extra_params = request.params
    if extra_params is None:
        extra_params = await run_sync(get_model_params, "tts_model")

    # Build the fal payload (curated shape, or schema-driven for any other model)
    payload = await _resolve_tts_payload(
        tts_model, text, voice, speed, model_info, fal_key, extra_params
    )

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"https://fal.run/{tts_model}",
                headers={
                    "Authorization": f"Key {fal_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code == 401:
                raise HTTPException(
                    status_code=400,
                    detail="fal.ai API key is invalid. Please update it in Settings > Integrations."
                )
            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please wait and try again."
                )
            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"fal.ai TTS failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"TTS generation failed: {error_detail}"
                )

            result = response.json()
            audio_url_from_fal = _extract_audio_url(result)

            if not audio_url_from_fal:
                logger.error(f"No audio URL in fal.ai response for {tts_model}: {result}")
                raise HTTPException(status_code=500, detail="No audio URL in fal.ai response")

            # Download the audio and save locally
            audio_response = await client.get(audio_url_from_fal, timeout=60.0)
            if audio_response.status_code != 200:
                raise HTTPException(status_code=500, detail="Failed to download generated audio")

            audio_data = audio_response.content
            audio_dir = get_generated_audio_dir()
            output_format = request.output_format or "mp3"
            local_filename = f"tts-{uuid.uuid4().hex[:8]}.{output_format}"
            file_path = audio_dir / local_filename

            with open(file_path, "wb") as f:
                f.write(audio_data)

            encoded_path = str(file_path).replace("\\", "/")
            local_audio_url = f"/api/generated-audio/by-path?path={encoded_path}"

            return TTSGenerateResponse(
                success=True,
                audio_url=local_audio_url,
                file_path=str(file_path)
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="TTS generation timed out. Please try with shorter text."
        )
    except Exception:
        logger.exception("fal.ai TTS generation failed")
        raise HTTPException(
            status_code=500,
            detail="TTS generation failed"
        )


@audio_router.post("/tts/stream")
async def stream_tts(
    request: TTSGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Stream TTS audio directly from fal.ai without saving to disk.
    """
    def _load_config() -> tuple[str | None, str, str, float]:
        """The key read (+ decrypt) and the three TTS settings in ONE hop."""
        fal_key = get_decrypted_api_key("fal_api_key")
        if not fal_key:
            raise HTTPException(
                status_code=400,
                detail="fal.ai API key not configured. Go to Settings > Integrations to add your key."
            )

        tts_model = request.model or database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"

        voice = request.voice or database.get_system_setting("tts_voice") or "af_heart"
        speed = request.speed
        if speed is None:
            speed_str = database.get_system_setting("tts_speed")
            speed = float(speed_str) if speed_str else 1.0
        return fal_key, tts_model, voice, speed

    fal_key, tts_model, voice, speed = await run_sync(_load_config)
    model_info = TTS_MODELS.get(tts_model)

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Text too long. Maximum 4096 characters.")

    # A SECOND hop, for the same reason as /tts/generate: the text validation
    # above can reject the request before this read is reachable.
    extra_params = request.params
    if extra_params is None:
        extra_params = await run_sync(get_model_params, "tts_model")
    payload = await _resolve_tts_payload(
        tts_model, text, voice, speed, model_info, fal_key, extra_params
    )

    output_format = request.output_format or "mp3"
    mime_types = {
        "mp3": "audio/mpeg",
        "opus": "audio/opus",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "pcm": "audio/pcm"
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Call fal.ai to generate audio
            response = await client.post(
                f"https://fal.run/{tts_model}",
                headers={
                    "Authorization": f"Key {fal_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if response.status_code == 401:
                raise HTTPException(status_code=400, detail="fal.ai API key is invalid.")
            elif response.status_code == 429:
                raise HTTPException(status_code=429, detail="Rate limit exceeded.")
            elif response.status_code != 200:
                logger.error(f"fal.ai TTS failed: {response.status_code} - {response.text}")
                raise HTTPException(status_code=500, detail=f"TTS generation failed: {response.text}")

            result = response.json()
            audio_url_from_fal = _extract_audio_url(result)

            if not audio_url_from_fal:
                logger.error(f"No audio URL in fal.ai response for {tts_model}: {result}")
                raise HTTPException(status_code=500, detail="No audio URL in fal.ai response")

            # Stream directly from fal.ai CDN to the client (no disk round-trip)
            audio_response = await client.get(audio_url_from_fal, timeout=60.0)
            if audio_response.status_code != 200:
                raise HTTPException(status_code=500, detail="Failed to download generated audio")

            return Response(
                content=audio_response.content,
                media_type=mime_types.get(output_format, "audio/mpeg"),
                headers={"Cache-Control": "no-cache"}
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="TTS generation timed out. Try shorter text.")
    except Exception:
        logger.exception("fal.ai TTS streaming failed")
        raise HTTPException(status_code=500, detail="TTS generation failed")
