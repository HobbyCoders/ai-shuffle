"""
Settings API routes - integration settings, API keys, and configuration
"""

import logging
import os
import uuid
import httpx
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.db import database
from app.api.auth import require_admin, require_auth
from app.core import encryption
from app.core.ai_tools import AI_TOOLS
from app.core.query_engine import DEFAULT_SECURITY_INSTRUCTIONS

logger = logging.getLogger(__name__)


def get_decrypted_api_key(setting_name: str) -> Optional[str]:
    """
    Get an API key from the database and decrypt it if encrypted.
    Returns None if not set or if decryption fails.
    """
    value = database.get_system_setting(setting_name)
    if not value:
        return None

    # If encrypted, try to decrypt
    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            logger.warning(f"Cannot decrypt {setting_name} - encryption key not loaded")
            return None
        try:
            return encryption.decrypt_value(value)
        except Exception as e:
            logger.error(f"Failed to decrypt {setting_name}: {e}")
            return None
    else:
        # Plaintext (legacy or migration pending)
        return value


def set_encrypted_api_key(setting_name: str, api_key: str) -> None:
    """
    Encrypt and store an API key in the database.
    If encryption is not ready, stores as plaintext (will be encrypted on next admin login).
    """
    if encryption.is_encryption_ready():
        encrypted = encryption.encrypt_value(api_key)
        database.set_system_setting(setting_name, encrypted)
    else:
        # Fallback to plaintext if encryption not ready
        # This will be encrypted when admin logs in
        logger.warning(f"Storing {setting_name} as plaintext - encryption key not loaded")
        database.set_system_setting(setting_name, api_key)

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])


def mask_api_key(key: str) -> str:
    """Mask an API key for display, showing first 3 and last 4 characters"""
    if not key or len(key) < 10:
        return ""
    return f"{key[:7]}...{key[-4:]}"


# ============================================================================
# Internal API Key Access (for AI tools)
# ============================================================================

@router.get("/internal/api-keys/{key_name}")
async def get_internal_api_key(key_name: str, _: str = Depends(require_admin)):
    """
    Internal endpoint for AI tools to get decrypted API keys.

    Only allows specific key names to prevent arbitrary setting access.
    Requires admin authentication to prevent unauthorized access.
    """
    allowed_keys = ["fal_api_key"]

    if key_name not in allowed_keys:
        raise HTTPException(status_code=400, detail=f"Invalid key name. Allowed: {allowed_keys}")

    api_key = get_decrypted_api_key(key_name)

    if not api_key:
        raise HTTPException(status_code=404, detail=f"API key '{key_name}' not configured or encryption not ready")

    return {"key": api_key}


# ============================================================================
# Integration Settings
# ============================================================================

class IntegrationSettingsResponse(BaseModel):
    """Response containing integration configuration status"""
    fal_api_key_set: bool
    fal_api_key_masked: str
    # Model selections (all fal.ai model IDs)
    image_model: Optional[str] = None
    image_edit_model: Optional[str] = None
    video_model: Optional[str] = None
    i2v_model: Optional[str] = None
    model3d_model: Optional[str] = None
    image_to_3d_model: Optional[str] = None
    tts_model: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_speed: Optional[float] = None
    tts_output_format: Optional[str] = None
    stt_model: Optional[str] = None
    # Per-model generation settings
    image_aspect_ratio: Optional[str] = None
    image_resolution: Optional[str] = None
    video_aspect_ratio: Optional[str] = None
    video_duration: Optional[int] = None
    video_resolution: Optional[str] = None
    stt_language: Optional[str] = None


class FalAIKeyRequest(BaseModel):
    """Request to set fal.ai API key"""
    api_key: str


class FalAIKeyResponse(BaseModel):
    """Response after setting fal.ai API key"""
    success: bool
    masked_key: str


@router.get("/integrations", response_model=IntegrationSettingsResponse)
async def get_integration_settings(token: str = Depends(require_auth)):
    """
    Get current integration settings status.

    Returns whether API keys are configured (without exposing the actual keys).
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    image_model = database.get_system_setting("image_model")
    image_edit_model = database.get_system_setting("image_edit_model")
    video_model = database.get_system_setting("video_model")
    i2v_model = database.get_system_setting("i2v_model")
    model3d_model = database.get_system_setting("model3d_model")
    image_to_3d_model = database.get_system_setting("image_to_3d_model")
    tts_model = database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"
    tts_voice = database.get_system_setting("tts_voice") or "af_heart"
    tts_speed_str = database.get_system_setting("tts_speed")
    tts_speed = float(tts_speed_str) if tts_speed_str else 1.0
    tts_output_format = database.get_system_setting("tts_output_format") or "wav"
    stt_model = database.get_system_setting("stt_model") or "fal-ai/whisper"
    # Per-model generation settings
    image_aspect_ratio = database.get_system_setting("image_aspect_ratio")
    image_resolution = database.get_system_setting("image_resolution")
    video_aspect_ratio = database.get_system_setting("video_aspect_ratio")
    video_duration_str = database.get_system_setting("video_duration")
    video_duration = int(video_duration_str) if video_duration_str else None
    video_resolution = database.get_system_setting("video_resolution")
    stt_language = database.get_system_setting("stt_language")

    return IntegrationSettingsResponse(
        fal_api_key_set=bool(fal_key),
        fal_api_key_masked=mask_api_key(fal_key) if fal_key else "",
        image_model=image_model,
        image_edit_model=image_edit_model,
        video_model=video_model,
        i2v_model=i2v_model,
        model3d_model=model3d_model,
        image_to_3d_model=image_to_3d_model,
        tts_model=tts_model,
        tts_voice=tts_voice,
        tts_speed=tts_speed,
        tts_output_format=tts_output_format,
        stt_model=stt_model,
        image_aspect_ratio=image_aspect_ratio,
        image_resolution=image_resolution,
        video_aspect_ratio=video_aspect_ratio,
        video_duration=video_duration,
        video_resolution=video_resolution,
        stt_language=stt_language,
    )


@router.post("/integrations/fal-ai", response_model=FalAIKeyResponse)
async def set_fal_api_key(
    request: FalAIKeyRequest,
    token: str = Depends(require_admin)
):
    """
    Set the fal.ai API key (admin only).

    The key is used for all AI tools: image, video, 3D, TTS, and STT.
    Get your API key from: https://fal.ai/dashboard/keys
    """
    api_key = request.api_key.strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    # Validate the key by making a lightweight API call to fal.ai
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://queue.fal.run/fal-ai/fast-sdxl",
                headers={"Authorization": f"Key {api_key}"}
            )
            if response.status_code == 401:
                raise HTTPException(status_code=400, detail="Invalid API key. Authentication failed.")
    except httpx.TimeoutException:
        # Don't fail on timeout - key might still be valid
        logger.warning("fal.ai API validation timed out, saving key anyway")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error validating fal.ai key: {e}")
        # Don't fail on network errors - save the key

    # Save to database (encrypted if encryption is ready)
    set_encrypted_api_key("fal_api_key", api_key)

    return FalAIKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/integrations/fal-ai")
async def remove_fal_api_key(token: str = Depends(require_admin)):
    """
    Remove the fal.ai API key (admin only).
    """
    database.delete_system_setting("fal_api_key")
    return {"success": True}


# ============================================================================
# Audio Settings (TTS/STT)
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


class AudioModelUpdateRequest(BaseModel):
    """Request to update TTS or STT model"""
    model: str


class TTSSettingsRequest(BaseModel):
    """Request to update TTS settings (model, voice, speed, output_format)"""
    model: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    output_format: Optional[str] = None


@router.get("/integrations/audio/models")
async def get_audio_models(token: str = Depends(require_auth)):
    """
    Get available TTS and STT models.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    current_tts = database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"
    current_stt = database.get_system_setting("stt_model") or "fal-ai/whisper"

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


@router.patch("/integrations/audio/tts")
async def update_tts_model(
    request: AudioModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the default TTS model."""
    model = request.model.strip()

    if model not in TTS_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid TTS model. Available: {', '.join(TTS_MODELS.keys())}"
        )

    database.set_system_setting("tts_model", model)
    return {"success": True, "model": model}


@router.patch("/integrations/tts")
async def update_tts_settings(
    request: TTSSettingsRequest,
    token: str = Depends(require_admin)
):
    """
    Update TTS settings including model, voice, and speed.

    All fields are optional - only provided fields will be updated.
    """
    if request.model:
        model = request.model.strip()
        if model not in TTS_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid TTS model. Available: {', '.join(TTS_MODELS.keys())}"
            )
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

    return {"success": True}


@router.patch("/integrations/audio/stt")
async def update_stt_model(
    request: AudioModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the default STT model."""
    model = request.model.strip()

    if model not in STT_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid STT model. Available: {', '.join(STT_MODELS.keys())}"
        )

    database.set_system_setting("stt_model", model)
    return {"success": True, "model": model}


@router.get("/integrations/tts/models")
async def get_tts_models(token: str = Depends(require_auth)):
    """
    Get available TTS models with voice options.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    current_tts = database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"

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


@router.get("/integrations/stt/models")
async def get_stt_models(token: str = Depends(require_auth)):
    """
    Get available STT models.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    current_stt = database.get_system_setting("stt_model") or "fal-ai/whisper"

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

class TranscriptionResponse(BaseModel):
    """Response from voice transcription"""
    text: str
    duration_seconds: Optional[float] = None


@router.post("/transcribe", response_model=TranscriptionResponse)
async def transcribe_audio(
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """
    Transcribe audio to text using fal.ai's Whisper model.

    Accepts audio files (webm, mp3, mp4, wav, m4a, etc.) and returns transcribed text.
    The file is uploaded to fal.ai's file storage first, then passed to the Whisper model.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
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

    # Build a data URI from the audio bytes (same approach as the Node.js STT tool).
    # This avoids a separate file upload step — fal.ai accepts data URIs in audio_url.
    import base64
    mime_type = content_type or "audio/webm"
    audio_b64 = base64.b64encode(audio_data).decode("ascii")
    audio_data_uri = f"data:{mime_type};base64,{audio_b64}"

    # Call fal.ai STT model with the data URI
    stt_model = database.get_system_setting("stt_model") or "fal-ai/whisper"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "audio_url": audio_data_uri,
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

class TTSGenerateRequest(BaseModel):
    """Request to generate TTS audio"""
    text: str
    model: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    output_format: Optional[str] = "mp3"


class TTSGenerateResponse(BaseModel):
    """Response from TTS generation"""
    success: bool
    audio_url: Optional[str] = None
    file_path: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None


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


@router.post("/tts/generate", response_model=TTSGenerateResponse)
async def generate_tts(
    request: TTSGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Generate TTS audio using fal.ai.

    Returns a URL to the generated audio file.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    if not fal_key:
        raise HTTPException(
            status_code=400,
            detail="fal.ai API key not configured. Go to Settings > Integrations to add your key."
        )

    # Get configured model or use request override
    tts_model = request.model or database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"
    model_info = TTS_MODELS.get(tts_model)

    if not model_info:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid TTS model: {tts_model}. Available: {', '.join(TTS_MODELS.keys())}"
        )

    # Get voice and other settings
    voice = request.voice or database.get_system_setting("tts_voice") or "af_heart"
    speed = request.speed
    if speed is None:
        speed_str = database.get_system_setting("tts_speed")
        speed = float(speed_str) if speed_str else 1.0

    # Validate text
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Text too long. Maximum 4096 characters.")

    # Validate speed if model supports it
    if model_info.get("supports_speed"):
        speed_range = model_info.get("speed_range", [0.5, 2.0])
        if speed < speed_range[0] or speed > speed_range[1]:
            raise HTTPException(status_code=400, detail=f"Speed must be between {speed_range[0]} and {speed_range[1]}")

    # Build model-specific payload
    payload = _build_tts_payload(tts_model, text, voice, speed, model_info)

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


@router.post("/tts/stream")
async def stream_tts(
    request: TTSGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Stream TTS audio directly from fal.ai without saving to disk.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    if not fal_key:
        raise HTTPException(
            status_code=400,
            detail="fal.ai API key not configured. Go to Settings > Integrations to add your key."
        )

    tts_model = request.model or database.get_system_setting("tts_model") or "fal-ai/kokoro/american-english"
    model_info = TTS_MODELS.get(tts_model)
    if not model_info:
        raise HTTPException(status_code=400, detail=f"Invalid TTS model: {tts_model}")

    voice = request.voice or database.get_system_setting("tts_voice") or "af_heart"
    speed = request.speed
    if speed is None:
        speed_str = database.get_system_setting("tts_speed")
        speed = float(speed_str) if speed_str else 1.0

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Text too long. Maximum 4096 characters.")

    payload = _build_tts_payload(tts_model, text, voice, speed, model_info)

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


# ============================================================================
# Model Selection (Image, Video, 3D)
# ============================================================================

class ModelUpdateRequest(BaseModel):
    """Request to update a model selection"""
    model: str


@router.patch("/integrations/image/model")
async def update_image_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image generation model (admin only). Accepts any fal.ai model ID."""
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    database.set_system_setting("image_model", model)
    return {"success": True, "model": model}


@router.patch("/integrations/image/edit-model")
async def update_image_edit_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image editing model (admin only). Accepts any fal.ai model ID."""
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    database.set_system_setting("image_edit_model", model)
    return {"success": True, "model": model}


@router.patch("/integrations/video/model")
async def update_video_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the video generation model (admin only). Accepts any fal.ai model ID."""
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    database.set_system_setting("video_model", model)
    return {"success": True, "model": model}


@router.patch("/integrations/video/i2v-model")
async def update_i2v_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image-to-video model (admin only). Accepts any fal.ai model ID."""
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    database.set_system_setting("i2v_model", model)
    return {"success": True, "model": model}


@router.patch("/integrations/3d/model")
async def update_3d_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the 3D model generation model (admin only). Accepts any fal.ai model ID."""
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    database.set_system_setting("model3d_model", model)
    return {"success": True, "model": model}


@router.patch("/integrations/3d/i2-3d-model")
async def update_image_to_3d_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image-to-3D model (admin only). Accepts any fal.ai model ID."""
    model = request.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")

    database.set_system_setting("image_to_3d_model", model)
    return {"success": True, "model": model}


# ============================================================================
# Per-Model Generation Settings
# ============================================================================

class ImageSettingsRequest(BaseModel):
    """Request to update image generation settings"""
    aspect_ratio: Optional[str] = None
    resolution: Optional[str] = None


class VideoSettingsRequest(BaseModel):
    """Request to update video generation settings"""
    aspect_ratio: Optional[str] = None
    duration: Optional[int] = None
    resolution: Optional[str] = None


class STTSettingsRequest(BaseModel):
    """Request to update STT settings"""
    language: Optional[str] = None


@router.patch("/integrations/image/settings")
async def update_image_settings(
    request: ImageSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update image generation defaults (aspect ratio, resolution)."""
    if request.aspect_ratio is not None:
        database.set_system_setting("image_aspect_ratio", request.aspect_ratio)
    if request.resolution is not None:
        database.set_system_setting("image_resolution", request.resolution)
    return {"success": True}


@router.patch("/integrations/video/settings")
async def update_video_settings(
    request: VideoSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update video generation defaults (aspect ratio, duration, resolution)."""
    if request.aspect_ratio is not None:
        database.set_system_setting("video_aspect_ratio", request.aspect_ratio)
    if request.duration is not None:
        database.set_system_setting("video_duration", str(request.duration))
    if request.resolution is not None:
        database.set_system_setting("video_resolution", request.resolution)
    return {"success": True}


@router.patch("/integrations/stt/settings")
async def update_stt_settings(
    request: STTSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update STT defaults (language)."""
    if request.language is not None:
        database.set_system_setting("stt_language", request.language)
    return {"success": True}


# ============================================================================
# AI Tools Configuration (for Profile Management)
# ============================================================================

# AI_TOOLS imported at top of file from app.core.ai_tools


@router.get("/ai-tools/available")
async def get_available_ai_tools(token: str = Depends(require_auth)):
    """
    Get list of AI tools available based on configured providers.

    Returns tools grouped by category with availability status.
    Used by the profile management UI to show only available tools.
    """
    fal_key = get_decrypted_api_key("fal_api_key")
    available_providers = {"fal-ai"} if fal_key else set()

    # Build tools list with availability
    tools = []
    categories = {}

    for tool_id, tool_info in AI_TOOLS.items():
        # Check if any of the tool's providers are available
        tool_providers = tool_info["providers"]
        is_available = any(p in available_providers for p in tool_providers)

        # Active provider is fal-ai if available
        active_provider = "fal-ai" if is_available else None

        tool_data = {
            "id": tool_id,
            "name": tool_info["name"],
            "description": tool_info["description"],
            "category": tool_info["category"],
            "available": is_available,
            "providers": tool_providers,
            "active_provider": active_provider
        }
        tools.append(tool_data)

        # Group by category
        cat = tool_info["category"]
        if cat not in categories:
            categories[cat] = {
                "id": cat,
                "name": f"{cat.title()} Tools",
                "tools": []
            }
        categories[cat]["tools"].append(tool_data)

    return {
        "tools": tools,
        "categories": list(categories.values()),
        "available_providers": list(available_providers),
    }


# ============================================================================
# Credential Policies (Admin control over which keys users must provide)
# ============================================================================

class CredentialPolicyResponse(BaseModel):
    """A credential policy"""
    id: str
    policy: str  # 'admin_provided', 'user_provided', 'optional'
    description: Optional[str]
    updated_at: str


class UpdateCredentialPolicyRequest(BaseModel):
    """Request to update a credential policy"""
    policy: str  # 'admin_provided', 'user_provided', 'optional'


class AllCredentialPoliciesResponse(BaseModel):
    """All credential policies"""
    policies: list[CredentialPolicyResponse]


CREDENTIAL_POLICY_INFO = {
    "fal_api_key": {
        "name": "fal.ai API Key",
        "description": "For all AI tools (image, video, 3D, TTS, STT)",
        "admin_has_key": lambda: bool(get_decrypted_api_key("fal_api_key"))
    },
    "github_pat": {
        "name": "GitHub Personal Access Token",
        "description": "For accessing GitHub repositories",
        "admin_has_key": lambda: False
    }
}


@router.get("/credential-policies", response_model=AllCredentialPoliciesResponse)
async def get_credential_policies(token: str = Depends(require_auth)):
    """
    Get all credential policies.

    Returns policies that control whether users must provide their own API keys
    or can use admin-provided keys.
    """
    policies = database.get_all_credential_policies()
    enriched = []
    for policy in policies:
        info = CREDENTIAL_POLICY_INFO.get(policy["id"], {})
        enriched.append({
            **policy,
            "name": info.get("name", policy["id"]),
            "admin_has_key": info.get("admin_has_key", lambda: False)()
        })
    return AllCredentialPoliciesResponse(
        policies=[CredentialPolicyResponse(**p) for p in policies]
    )


@router.put("/credential-policies/{policy_id}")
async def update_credential_policy(
    policy_id: str,
    request: UpdateCredentialPolicyRequest,
    token: str = Depends(require_admin)
):
    """
    Update a credential policy (admin only).

    Policy options:
    - 'admin_provided': All users use admin's key, users cannot set their own
    - 'user_provided': Each user must provide their own key
    - 'optional': Users can optionally provide their own key, falls back to admin's
    """
    valid_policies = ['admin_provided', 'user_provided', 'optional']
    if request.policy not in valid_policies:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid policy. Must be one of: {', '.join(valid_policies)}"
        )

    # Special case: github_pat cannot be admin_provided (doesn't make sense)
    if policy_id == "github_pat" and request.policy == "admin_provided":
        raise HTTPException(
            status_code=400,
            detail="GitHub PAT cannot be admin-provided. Each user must use their own GitHub account."
        )

    # Special case: can't set to admin_provided if admin doesn't have the key
    if request.policy == "admin_provided" and policy_id in CREDENTIAL_POLICY_INFO:
        info = CREDENTIAL_POLICY_INFO[policy_id]
        if not info.get("admin_has_key", lambda: False)():
            raise HTTPException(
                status_code=400,
                detail="Cannot set to 'admin_provided' - admin has not configured this key yet"
            )

    updated = database.update_credential_policy(policy_id, request.policy)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Policy '{policy_id}' not found")

    return {
        "success": True,
        "id": policy_id,
        "policy": request.policy
    }


@router.get("/credential-policies/summary")
async def get_credential_policies_summary(token: str = Depends(require_admin)):
    """
    Get a summary of credential policies with admin key status.

    Used by admin UI to show which keys are configured and policy status.
    """
    policies = database.get_all_credential_policies()
    summary = []

    for policy in policies:
        info = CREDENTIAL_POLICY_INFO.get(policy["id"], {})
        admin_has_key = info.get("admin_has_key", lambda: False)()

        summary.append({
            "id": policy["id"],
            "name": info.get("name", policy["id"]),
            "description": info.get("description", policy.get("description", "")),
            "policy": policy["policy"],
            "admin_has_key": admin_has_key,
            # Compute effective status
            "effective_status": _compute_effective_status(policy["policy"], admin_has_key),
            "updated_at": policy["updated_at"]
        })

    return {"policies": summary}


def _compute_effective_status(policy: str, admin_has_key: bool) -> str:
    """Compute the effective status for a credential policy"""
    if policy == "admin_provided":
        return "admin_provides" if admin_has_key else "needs_admin_key"
    elif policy == "user_provided":
        return "user_must_provide"
    else:  # optional
        return "optional_with_fallback" if admin_has_key else "optional_no_fallback"


# ============================================================================
# Per-User Credential Policy Overrides (Admin control per user)
# ============================================================================

class UserCredentialPolicyResponse(BaseModel):
    """A user's credential policy override"""
    api_user_id: str
    credential_type: str
    policy: str  # 'admin_provided', 'user_provided', 'optional'
    source: str  # 'user' (override) or 'global' (fallback)


class SetUserCredentialPolicyRequest(BaseModel):
    """Request to set a user's credential policy override"""
    policy: str  # 'admin_provided', 'user_provided', 'optional'


@router.get("/users/{user_id}/credential-policies")
async def get_user_credential_policies(
    user_id: str,
    token: str = Depends(require_admin)
):
    """
    Get all credential policies for a specific user (admin only).

    Returns the policy for each credential type. Policies are per-user only.
    """
    # Verify user exists
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get user's policies
    user_policies = {
        p["credential_type"]: p["policy"]
        for p in database.get_all_user_credential_policies(user_id)
    }

    # Build result from known credential types
    result = []
    for credential_type, info in CREDENTIAL_POLICY_INFO.items():
        policy = user_policies.get(credential_type, "user_provided")  # Default to user_provided

        result.append({
            "credential_type": credential_type,
            "name": info.get("name", credential_type),
            "description": info.get("description", ""),
            "policy": policy,
            "source": "user" if credential_type in user_policies else "default"
        })

    return {
        "user_id": user_id,
        "user_name": user.get("name", "Unknown"),
        "policies": result
    }


@router.put("/users/{user_id}/credential-policies/{credential_type}")
async def set_user_credential_policy(
    user_id: str,
    credential_type: str,
    request: SetUserCredentialPolicyRequest,
    token: str = Depends(require_admin)
):
    """
    Set the credential policy for a specific user (admin only).

    Policies are per-user only - this sets the user's policy for this credential type.
    """
    # Verify user exists
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Validate policy
    valid_policies = ['admin_provided', 'user_provided', 'optional']
    if request.policy not in valid_policies:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid policy. Must be one of: {', '.join(valid_policies)}"
        )

    # Validate credential type
    if credential_type not in CREDENTIAL_POLICY_INFO:
        raise HTTPException(status_code=400, detail=f"Unknown credential type: {credential_type}")

    # Special case: github_pat cannot be admin_provided
    if credential_type == "github_pat" and request.policy == "admin_provided":
        raise HTTPException(
            status_code=400,
            detail="GitHub PAT cannot be admin-provided. Each user must use their own GitHub account."
        )

    # Special case: can't set to admin_provided if admin doesn't have the key
    if request.policy == "admin_provided":
        info = CREDENTIAL_POLICY_INFO[credential_type]
        if not info.get("admin_has_key", lambda: False)():
            raise HTTPException(
                status_code=400,
                detail="Cannot set to 'admin_provided' - admin has not configured this key yet"
            )

    # Set the override
    updated = database.set_user_credential_policy(user_id, credential_type, request.policy)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to set policy override")

    return {
        "success": True,
        "user_id": user_id,
        "credential_type": credential_type,
        "policy": request.policy,
        "source": "user"
    }


@router.delete("/users/{user_id}/credential-policies/{credential_type}")
async def delete_user_credential_policy(
    user_id: str,
    credential_type: str,
    token: str = Depends(require_admin)
):
    """
    Reset a credential policy to default for a user (admin only).

    The user will revert to the default policy (user_provided).
    """
    # Verify user exists
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    deleted = database.delete_user_credential_policy(user_id, credential_type)

    # Default policy when none is set
    fallback_policy = "user_provided"

    return {
        "success": True,
        "user_id": user_id,
        "credential_type": credential_type,
        "deleted": deleted,
        "fallback_policy": fallback_policy,
        "source": "default"
    }


# ============================================================================
# Credential Resolution (for internal use by AI tools)
# ============================================================================

@router.get("/internal/resolve-credential/{credential_type}")
async def resolve_credential(
    credential_type: str,
    user_id: Optional[str] = None,
    _: str = Depends(require_admin)
):
    """
    Internal endpoint to resolve which API key to use.

    Resolution order based on per-user policy:
    1. If policy is 'user_provided' or 'optional': check user's credential first
    2. If not found and policy is 'admin_provided' or 'optional': use admin's key
    3. If not found: return error

    Policies are per-user only. Default policy is 'user_provided'.

    This endpoint is for internal use by AI tools only.
    Requires admin authentication as it returns decrypted API keys.
    """
    from app.core import encryption as enc

    # Map credential types to admin settings
    admin_setting_map = {
        "fal_api_key": "fal_api_key",
        "github_pat": None  # No admin fallback
    }

    if credential_type not in admin_setting_map:
        raise HTTPException(status_code=400, detail=f"Unknown credential type: {credential_type}")

    # Get per-user policy (no global fallback, default to user_provided)
    if user_id:
        effective = database.get_effective_credential_policy(user_id, credential_type)
        policy = effective.get("policy", "user_provided")
    else:
        # No user specified, default to user_provided (won't resolve any key)
        policy = "user_provided"

    resolved_key = None
    source = None

    # Try user's credential if applicable
    if user_id and policy in ["user_provided", "optional"]:
        user_cred = database.get_user_credential(user_id, credential_type)
        if user_cred:
            encrypted = user_cred.get("encrypted_value")
            if encrypted:
                if enc.is_encrypted(encrypted) and enc.is_encryption_ready():
                    try:
                        resolved_key = enc.decrypt_value(encrypted)
                        source = "user"
                    except Exception:
                        pass
                elif not enc.is_encrypted(encrypted):
                    resolved_key = encrypted
                    source = "user"

    # Try admin's key if applicable
    if not resolved_key and policy in ["admin_provided", "optional"]:
        admin_setting = admin_setting_map.get(credential_type)
        if admin_setting:
            resolved_key = get_decrypted_api_key(admin_setting)
            if resolved_key:
                source = "admin"

    if not resolved_key:
        if policy == "user_provided":
            raise HTTPException(
                status_code=404,
                detail=f"Credential '{credential_type}' not configured. User must provide this key."
            )
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Credential '{credential_type}' not configured."
            )

    return {
        "key": resolved_key,
        "source": source,
        "credential_type": credential_type
    }


# ============================================================================
# Security Instructions (System Prompt)
# ============================================================================

class SecurityInstructionsResponse(BaseModel):
    """Response model for security instructions."""
    current: str
    default: str
    is_customized: bool


class SecurityInstructionsUpdateRequest(BaseModel):
    """Request model for updating security instructions."""
    instructions: str


@router.get("/security-instructions", response_model=SecurityInstructionsResponse)
async def get_security_instructions(token: str = Depends(require_admin)):
    """
    Get the current security instructions that are added to all system prompts.

    Returns both the current active instructions (custom or default) and the
    original default instructions for comparison/revert purposes.

    Admin only.
    """
    custom_instructions = database.get_system_setting("security_instructions")

    return SecurityInstructionsResponse(
        current=custom_instructions if custom_instructions else DEFAULT_SECURITY_INSTRUCTIONS,
        default=DEFAULT_SECURITY_INSTRUCTIONS,
        is_customized=custom_instructions is not None
    )


@router.put("/security-instructions")
async def update_security_instructions(
    request: SecurityInstructionsUpdateRequest,
    token: str = Depends(require_admin)
):
    """
    Update the security instructions that are added to all system prompts.

    The instructions are prepended to all system prompts sent to Claude.
    This allows customizing the base behavior across all profiles.

    Admin only.
    """
    instructions = request.instructions.strip()

    if not instructions:
        raise HTTPException(
            status_code=400,
            detail="Instructions cannot be empty. Use DELETE to revert to default."
        )

    database.set_system_setting("security_instructions", instructions)

    return {
        "success": True,
        "message": "Security instructions updated successfully"
    }


@router.delete("/security-instructions")
async def revert_security_instructions(token: str = Depends(require_admin)):
    """
    Revert security instructions to the default.

    This removes any custom instructions and returns to using the built-in default.

    Admin only.
    """
    # Delete the custom setting to revert to default
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = %s", ("security_instructions",))

    return {
        "success": True,
        "message": "Security instructions reverted to default"
    }
