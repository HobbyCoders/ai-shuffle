"""
Settings API routes - integration settings, API keys, and configuration
"""

import asyncio
import logging
import os
import uuid
import httpx
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
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


# ============================================================================
# Internal API Key Access (for AI tools)
# ============================================================================

@router.get("/internal/api-keys/{key_name}")
async def get_internal_api_key(key_name: str, _: str = Depends(require_admin)):
    """
    Internal endpoint for AI tools to get decrypted API keys.

    This endpoint is used by the Node.js AI tools (image generation, video generation)
    to access API keys that are encrypted in the database.

    Only allows specific key names to prevent arbitrary setting access.
    Requires admin authentication to prevent unauthorized access.
    """
    allowed_keys = ["openai_api_key", "image_api_key", "gemini_api_key", "video_api_key", "meshy_api_key"]

    if key_name not in allowed_keys:
        raise HTTPException(status_code=400, detail=f"Invalid key name. Allowed: {allowed_keys}")

    api_key = get_decrypted_api_key(key_name)

    if not api_key:
        raise HTTPException(status_code=404, detail=f"API key '{key_name}' not configured or encryption not ready")

    return {"key": api_key}


@router.get("/internal/image-config")
async def get_internal_image_config(_: str = Depends(require_admin)):
    """
    Internal endpoint for AI tools to get image generation configuration.
    Returns provider, model, and decrypted API key.
    Requires admin authentication to prevent unauthorized access.
    """
    provider = database.get_system_setting("image_provider")
    model = database.get_system_setting("image_model")
    api_key = get_decrypted_api_key("image_api_key")

    if not all([provider, model, api_key]):
        raise HTTPException(status_code=404, detail="Image generation not configured")

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key
    }


@router.get("/internal/video-config")
async def get_internal_video_config(_: str = Depends(require_admin)):
    """
    Internal endpoint for AI tools to get video generation configuration.
    Returns provider, model, and decrypted API key.
    Requires admin authentication to prevent unauthorized access.
    """
    provider = database.get_system_setting("video_provider")
    model = database.get_system_setting("video_model")

    # Video can use either OpenAI key (Sora) or image_api_key (Veo/Google)
    if provider == "openai-sora":
        api_key = get_decrypted_api_key("openai_api_key")
    else:
        api_key = get_decrypted_api_key("image_api_key")

    if not all([provider, model, api_key]):
        raise HTTPException(status_code=404, detail="Video generation not configured")

    return {
        "provider": provider,
        "model": model,
        "api_key": api_key
    }


# ============================================================================
# Integration Settings
# ============================================================================

class IntegrationSettingsResponse(BaseModel):
    """Response containing integration configuration status"""
    openai_api_key_set: bool
    openai_api_key_masked: str
    # Image generation settings
    image_provider: Optional[str] = None  # "google" for Nano Banana
    image_model: Optional[str] = None  # "gemini-2.0-flash-preview-image-generation" or "gemini-2.0-flash-exp"
    image_api_key_set: bool = False
    image_api_key_masked: str = ""
    # Video generation settings
    video_provider: Optional[str] = None  # "google-veo" or "openai-sora"
    video_model: Optional[str] = None  # "veo-3.1-fast-generate-preview", "sora-2", etc.
    # Audio settings (TTS/STT)
    tts_model: Optional[str] = None  # "gpt-4o-mini-tts", "tts-1", "tts-1-hd"
    tts_voice: Optional[str] = None  # "alloy", "echo", "nova", etc.
    tts_speed: Optional[float] = None  # 0.25 to 4.0
    tts_output_format: Optional[str] = None  # "mp3", "wav", "opus", etc.
    tts_instructions: Optional[str] = None  # Voice instructions for steerable models
    stt_model: Optional[str] = None  # "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"
    # 3D Model generation settings (Meshy)
    meshy_api_key_set: bool = False
    meshy_api_key_masked: str = ""
    model3d_provider: Optional[str] = None  # "meshy"
    model3d_model: Optional[str] = None  # "latest", "meshy-5", "meshy-4"
    # DeepSeek settings
    deepseek_api_key_set: bool = False
    deepseek_api_key_masked: str = ""


class OpenAIKeyRequest(BaseModel):
    """Request to set OpenAI API key"""
    api_key: str


class OpenAIKeyResponse(BaseModel):
    """Response after setting OpenAI API key"""
    success: bool
    masked_key: str


class ImageGenerationRequest(BaseModel):
    """Request to configure image generation"""
    provider: str  # "google" for Nano Banana
    model: str  # "gemini-2.0-flash-preview-image-generation" or "gemini-2.0-flash-exp"
    api_key: str


class ImageGenerationResponse(BaseModel):
    """Response after configuring image generation"""
    success: bool
    provider: str
    model: str
    masked_key: str


class ImageGenerateRequest(BaseModel):
    """Request to generate an image"""
    prompt: str
    aspect_ratio: Optional[str] = "1:1"  # "1:1", "16:9", "9:16", "4:3", "3:4"


class ImageGenerateResponse(BaseModel):
    """Response from image generation"""
    success: bool
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    mime_type: Optional[str] = None
    error: Optional[str] = None


class VoiceToTextResponse(BaseModel):
    """Response from voice-to-text transcription"""
    success: bool
    text: Optional[str] = None
    language: Optional[str] = None
    duration: Optional[float] = None
    error: Optional[str] = None


# ============================================================================
# Image Generation Model Selection
# ============================================================================

# Available image providers and their models
IMAGE_PROVIDERS = {
    "google-gemini": {
        "name": "Google Gemini (Nano Banana)",
        "description": "Google's Gemini image generation API",
        "requires_key": "image_api_key"
    },
    "google-imagen": {
        "name": "Google Imagen 4",
        "description": "Google's flagship image generation",
        "requires_key": "image_api_key"
    },
    "openai-gpt-image": {
        "name": "OpenAI GPT Image",
        "description": "OpenAI's GPT-4o native image generation",
        "requires_key": "openai_api_key"
    }
}

# Available image models by provider
IMAGE_MODELS = {
    # Google Gemini (Nano Banana) models
    "gemini-2.5-flash-image": {
        "name": "Nano Banana",
        "description": "Fast image generation with good quality",
        "price_per_image": 0.039,
        "provider": "google-gemini",
        "capabilities": ["text-to-image", "image-edit", "image-reference"]
    },
    "gemini-3-pro-image-preview": {
        "name": "Nano Banana Pro",
        "description": "Studio-quality, better text rendering, 2K/4K support",
        "price_per_image": 0.10,
        "provider": "google-gemini",
        "capabilities": ["text-to-image", "image-edit", "image-reference"]
    },
    # Google Imagen 4 models
    "imagen-4.0-fast-generate-001": {
        "name": "Imagen 4 Fast",
        "description": "Speed-optimized for rapid generation",
        "price_per_image": 0.02,
        "provider": "google-imagen",
        "capabilities": ["text-to-image"]
    },
    "imagen-4.0-generate-001": {
        "name": "Imagen 4",
        "description": "Flagship quality with superior text rendering",
        "price_per_image": 0.04,
        "provider": "google-imagen",
        "capabilities": ["text-to-image"]
    },
    "imagen-4.0-ultra-generate-001": {
        "name": "Imagen 4 Ultra",
        "description": "Highest quality with 2K resolution support",
        "price_per_image": 0.06,
        "provider": "google-imagen",
        "capabilities": ["text-to-image"]
    },
    # OpenAI GPT Image models
    "gpt-image-1": {
        "name": "GPT Image 1",
        "description": "GPT-4o native image generation - high quality, accurate text",
        "price_per_image": 0.07,
        "provider": "openai-gpt-image",
        "capabilities": ["text-to-image", "image-edit"]
    }
}

# Legacy alias for backwards compatibility
NANO_BANANA_MODELS = {k: v for k, v in IMAGE_MODELS.items() if v.get("provider") == "google-gemini"}


def mask_api_key(key: str) -> str:
    """Mask an API key for display, showing first 3 and last 4 characters"""
    if not key or len(key) < 10:
        return ""
    return f"{key[:7]}...{key[-4:]}"


@router.get("/integrations", response_model=IntegrationSettingsResponse)
async def get_integration_settings(token: str = Depends(require_auth)):
    """
    Get current integration settings status.

    Returns whether API keys are configured (without exposing the actual keys).
    """
    # Get decrypted API keys for masking display
    openai_key = get_decrypted_api_key("openai_api_key")
    image_api_key = get_decrypted_api_key("image_api_key")
    meshy_api_key = get_decrypted_api_key("meshy_api_key")
    deepseek_api_key = get_decrypted_api_key("deepseek_api_key")

    # Non-sensitive settings
    image_provider = database.get_system_setting("image_provider")
    image_model = database.get_system_setting("image_model")
    video_provider = database.get_system_setting("video_provider")
    video_model = database.get_system_setting("video_model")
    tts_model = database.get_system_setting("tts_model") or "gpt-4o-mini-tts"
    tts_voice = database.get_system_setting("tts_voice") or "alloy"
    tts_speed_str = database.get_system_setting("tts_speed")
    tts_speed = float(tts_speed_str) if tts_speed_str else 1.0
    tts_output_format = database.get_system_setting("tts_output_format") or "mp3"
    tts_instructions = database.get_system_setting("tts_instructions") or ""
    stt_model = database.get_system_setting("stt_model") or "whisper-1"
    model3d_provider = database.get_system_setting("model3d_provider")
    model3d_model = database.get_system_setting("model3d_model")

    return IntegrationSettingsResponse(
        openai_api_key_set=bool(openai_key),
        openai_api_key_masked=mask_api_key(openai_key) if openai_key else "",
        image_provider=image_provider,
        image_model=image_model,
        image_api_key_set=bool(image_api_key),
        image_api_key_masked=mask_api_key(image_api_key) if image_api_key else "",
        video_provider=video_provider,
        video_model=video_model,
        tts_model=tts_model,
        tts_voice=tts_voice,
        tts_speed=tts_speed,
        tts_output_format=tts_output_format,
        tts_instructions=tts_instructions,
        stt_model=stt_model,
        meshy_api_key_set=bool(meshy_api_key),
        meshy_api_key_masked=mask_api_key(meshy_api_key) if meshy_api_key else "",
        model3d_provider=model3d_provider,
        model3d_model=model3d_model,
        deepseek_api_key_set=bool(deepseek_api_key),
        deepseek_api_key_masked=mask_api_key(deepseek_api_key) if deepseek_api_key else ""
    )


@router.post("/integrations/openai", response_model=OpenAIKeyResponse)
async def set_openai_api_key(
    request: OpenAIKeyRequest,
    token: str = Depends(require_admin)
):
    """
    Set the OpenAI API key (admin only).

    The key is stored securely and used for voice-to-text transcription.
    """
    api_key = request.api_key.strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    if not api_key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Invalid API key format. OpenAI keys start with 'sk-'")

    # Validate the key by making a test request to OpenAI
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}
            )
            if response.status_code == 401:
                raise HTTPException(status_code=400, detail="Invalid API key. Authentication failed.")
            elif response.status_code != 200:
                logger.warning(f"OpenAI API check returned status {response.status_code}")
    except httpx.TimeoutException:
        # Don't fail on timeout - key might still be valid
        logger.warning("OpenAI API validation timed out, saving key anyway")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error validating OpenAI key: {e}")
        # Don't fail on network errors - save the key

    # Save to database (encrypted if encryption is ready)
    set_encrypted_api_key("openai_api_key", api_key)

    return OpenAIKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/integrations/openai")
async def remove_openai_api_key(token: str = Depends(require_admin)):
    """
    Remove the OpenAI API key (admin only).
    """
    database.delete_system_setting("openai_api_key")
    return {"success": True}


# ============================================================================
# DeepSeek API Key
# ============================================================================

class DeepSeekKeyRequest(BaseModel):
    api_key: str


class DeepSeekKeyResponse(BaseModel):
    success: bool
    masked_key: str


@router.post("/integrations/deepseek", response_model=DeepSeekKeyResponse)
async def set_deepseek_api_key(
    request: DeepSeekKeyRequest,
    token: str = Depends(require_admin)
):
    """
    Set the DeepSeek API key (admin only).

    Get your API key from: https://platform.deepseek.com/api_keys
    """
    api_key = request.api_key.strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    # DeepSeek API keys start with "sk-"
    if not api_key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Invalid API key format. DeepSeek keys start with 'sk-'")

    # Validate the key by making a test request to DeepSeek
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 1
                }
            )
            if response.status_code == 401:
                raise HTTPException(status_code=400, detail="Invalid API key. Authentication failed.")
            elif response.status_code not in (200, 429):  # 429 = rate limit, key is still valid
                logger.warning(f"DeepSeek API check returned status {response.status_code}")
    except httpx.TimeoutException:
        # Don't fail on timeout - key might still be valid
        logger.warning("DeepSeek API validation timed out, saving key anyway")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error validating DeepSeek key: {e}")
        # Don't fail on network errors - save the key

    # Save to database (encrypted if encryption is ready)
    set_encrypted_api_key("deepseek_api_key", api_key)

    return DeepSeekKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/integrations/deepseek")
async def remove_deepseek_api_key(token: str = Depends(require_admin)):
    """
    Remove the DeepSeek API key (admin only).
    """
    database.delete_system_setting("deepseek_api_key")
    return {"success": True}


# ============================================================================
# Audio Settings (TTS/STT)
# ============================================================================

# Available TTS models
# supports_streaming: True = model supports SSE streaming audio
# supports_instructions: True = model supports voice style instructions (steerable)
# Providers:
#   - "gemini-tts": Gemini API (generativelanguage.googleapis.com) - uses image_api_key
#   - "google-cloud-tts": Google Cloud TTS API (texttospeech.googleapis.com) - uses image_api_key
#   - "openai-tts": OpenAI API - uses openai_api_key
TTS_MODELS = {
    # Gemini API TTS Models (generativelanguage.googleapis.com)
    "gemini-2.5-flash-tts": {
        "name": "Gemini TTS",
        "provider": "gemini-tts",
        "description": "Natural language style control, emotion, multi-speaker",
        "price_display": "~$0.015/1M chars",
        "price_per_million": 15.0,
        "supports_streaming": False,  # REST API doesn't stream, need Live API
        "supports_instructions": True
    },
    # Google Cloud TTS Models (texttospeech.googleapis.com)
    "chirp-3-hd": {
        "name": "Chirp 3 HD",
        "provider": "google-cloud-tts",
        "description": "Low-latency, real-time communication optimized",
        "price_display": "~$0.016/1M chars",
        "price_per_million": 16.0,
        "supports_streaming": False,  # REST API returns full audio
        "supports_instructions": False,
        "voice_prefix": "Chirp3-HD"  # Voice names: en-US-Chirp3-HD-Charon
    },
    "neural2": {
        "name": "Neural2",
        "provider": "google-cloud-tts",
        "description": "High-quality natural speech, no training required",
        "price_display": "~$0.016/1M chars",
        "price_per_million": 16.0,
        "supports_streaming": False,
        "supports_instructions": False,
        "voice_prefix": "Neural2"  # Voice names: en-US-Neural2-A
    },
    "wavenet": {
        "name": "WaveNet",
        "provider": "google-cloud-tts",
        "description": "Premium quality with natural intonation",
        "price_display": "~$0.016/1M chars",
        "price_per_million": 16.0,
        "supports_streaming": False,
        "supports_instructions": False,
        "voice_prefix": "Wavenet"  # Voice names: en-US-Wavenet-A
    },
    "standard": {
        "name": "Standard",
        "provider": "google-cloud-tts",
        "description": "Cost-effective basic synthesis",
        "price_display": "~$0.004/1M chars",
        "price_per_million": 4.0,
        "supports_streaming": False,
        "supports_instructions": False,
        "voice_prefix": "Standard"  # Voice names: en-US-Standard-A
    },
    "studio": {
        "name": "Studio",
        "provider": "google-cloud-tts",
        "description": "Professional narration quality voices",
        "price_display": "~$0.160/1M chars",
        "price_per_million": 160.0,
        "supports_streaming": False,
        "supports_instructions": False,
        "voice_prefix": "Studio"  # Voice names: en-US-Studio-M
    },
    "polyglot": {
        "name": "Polyglot",
        "provider": "google-cloud-tts",
        "description": "Multi-language support for international content",
        "price_display": "~$0.016/1M chars",
        "price_per_million": 16.0,
        "supports_streaming": False,
        "supports_instructions": False,
        "voice_prefix": "Polyglot"  # Voice names: es-ES-Polyglot-1
    },
    # OpenAI TTS Models
    "gpt-4o-mini-tts": {
        "name": "GPT-4o Mini TTS",
        "provider": "openai-tts",
        "description": "Steerable TTS with natural speech and instructions support",
        "price_display": "~$0.015/min",
        "price_per_minute": 0.015,
        "supports_streaming": True,  # Supports SSE streaming
        "supports_instructions": True
    },
    "tts-1": {
        "name": "TTS-1",
        "provider": "openai-tts",
        "description": "Standard quality TTS, optimized for speed",
        "price_display": "~$0.015/1K chars",
        "price_per_1k_chars": 0.015,
        "supports_streaming": True,  # OpenAI TTS supports streaming response
        "supports_instructions": False
    },
    "tts-1-hd": {
        "name": "TTS-1 HD",
        "provider": "openai-tts",
        "description": "High quality TTS with better clarity",
        "price_display": "~$0.030/1K chars",
        "price_per_1k_chars": 0.030,
        "supports_streaming": True,  # OpenAI TTS supports streaming response
        "supports_instructions": False
    }
}

# Available STT models
STT_MODELS = {
    # Google STT Models (diarization requires Google Cloud implementation)
    "gemini-2.5-flash": {
        "name": "Gemini Audio",
        "provider": "google-stt",
        "description": "Long-form audio, diarization, context-aware transcription",
        "price_display": "~$0.006/min",
        "price_per_minute": 0.006,
        "supports_diarization": True
    },
    "chirp-3": {
        "name": "Chirp 3",
        "provider": "google-stt",
        "description": "Multilingual, noisy environments, latest accuracy",
        "price_display": "~$0.016/min",
        "price_per_minute": 0.016,
        "supports_diarization": True
    },
    "latest-long": {
        "name": "Latest Long",
        "provider": "google-stt",
        "description": "Long-form content, media, conversations",
        "price_display": "~$0.016/min",
        "price_per_minute": 0.016,
        "supports_diarization": True
    },
    "latest-short": {
        "name": "Latest Short",
        "provider": "google-stt",
        "description": "Voice commands, short utterances",
        "price_display": "~$0.016/min",
        "price_per_minute": 0.016,
        "supports_diarization": False
    },
    # OpenAI STT Models
    "gpt-4o-transcribe-diarize": {
        "name": "GPT-4o Transcribe + Diarize",
        "provider": "openai-stt",
        "description": "Ultra-fast transcription with speaker identification",
        "price_display": "~$0.006/min",
        "price_per_minute": 0.006,
        "supports_diarization": True,
        "requires_chunking_strategy": True  # Required for audio > 30s
    },
    "gpt-4o-transcribe": {
        "name": "GPT-4o Transcribe",
        "provider": "openai-stt",
        "description": "Fast transcription (10 min in ~15 seconds)",
        "price_display": "~$0.006/min",
        "price_per_minute": 0.006,
        "supports_diarization": False
    },
    "gpt-4o-mini-transcribe": {
        "name": "GPT-4o Mini Transcribe",
        "provider": "openai-stt",
        "description": "Cost-efficient transcription",
        "price_display": "~$0.003/min",
        "price_per_minute": 0.003,
        "supports_diarization": False
    },
    "whisper-1": {
        "name": "Whisper",
        "provider": "openai-stt",
        "description": "General-purpose with translation to English",
        "price_display": "~$0.006/min",
        "price_per_minute": 0.006,
        "supports_diarization": False
    }
}


class AudioModelUpdateRequest(BaseModel):
    """Request to update TTS or STT model"""
    model: str


class TTSSettingsRequest(BaseModel):
    """Request to update TTS settings (model, voice, speed, format, instructions)"""
    model: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    output_format: Optional[str] = None
    instructions: Optional[str] = None  # Voice instructions for steerable models


@router.get("/integrations/audio/models")
async def get_audio_models(token: str = Depends(require_auth)):
    """
    Get available TTS and STT models.
    """
    openai_key = get_decrypted_api_key("openai_api_key")
    current_tts = database.get_system_setting("tts_model") or "gpt-4o-mini-tts"
    current_stt = database.get_system_setting("stt_model") or "whisper-1"

    tts_models = [
        {
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "price_display": info["price_display"],
            "available": bool(openai_key),
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
            "available": bool(openai_key),
            "is_current": model_id == current_stt
        }
        for model_id, info in STT_MODELS.items()
    ]

    return {
        "tts_models": tts_models,
        "stt_models": stt_models,
        "current_tts": current_tts,
        "current_stt": current_stt,
        "openai_configured": bool(openai_key)
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
    Update TTS settings including model, voice, speed, and output format.

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
        if request.speed < 0.25 or request.speed > 4.0:
            raise HTTPException(
                status_code=400,
                detail="Speed must be between 0.25 and 4.0"
            )
        database.set_system_setting("tts_speed", str(request.speed))

    if request.output_format:
        valid_formats = ["mp3", "wav", "opus", "aac", "flac", "pcm"]
        if request.output_format not in valid_formats:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid output format. Available: {', '.join(valid_formats)}"
            )
        database.set_system_setting("tts_output_format", request.output_format)

    if request.instructions is not None:
        # Allow empty string to clear instructions
        database.set_system_setting("tts_instructions", request.instructions.strip())

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
    Get available TTS models.

    Returns list of TTS models with their availability status.
    """
    openai_key = get_decrypted_api_key("openai_api_key")
    current_tts = database.get_system_setting("tts_model") or "gpt-4o-mini-tts"

    # OpenAI TTS voices
    voices = [
        {"id": "alloy", "name": "Alloy", "description": "Neutral, versatile"},
        {"id": "ash", "name": "Ash", "description": "Clear, professional"},
        {"id": "ballad", "name": "Ballad", "description": "Warm, storytelling"},
        {"id": "coral", "name": "Coral", "description": "Friendly, conversational"},
        {"id": "echo", "name": "Echo", "description": "Resonant, dramatic"},
        {"id": "fable", "name": "Fable", "description": "Engaging, expressive"},
        {"id": "onyx", "name": "Onyx", "description": "Deep, authoritative"},
        {"id": "nova", "name": "Nova", "description": "Soft, gentle"},
        {"id": "sage", "name": "Sage", "description": "Calm, measured"},
        {"id": "shimmer", "name": "Shimmer", "description": "Bright, energetic"},
        {"id": "verse", "name": "Verse", "description": "Lyrical, musical"},
    ]

    models = [
        {
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "price_display": info["price_display"],
            "available": bool(openai_key),
            "is_current": model_id == current_tts,
            "supports_instructions": model_id == "gpt-4o-mini-tts"
        }
        for model_id, info in TTS_MODELS.items()
    ]

    return {
        "models": models,
        "voices": voices,
        "output_formats": ["mp3", "opus", "aac", "flac", "wav", "pcm"],
        "current_model": current_tts,
        "openai_configured": bool(openai_key)
    }


@router.get("/integrations/stt/models")
async def get_stt_models(token: str = Depends(require_auth)):
    """
    Get available STT models.

    Returns list of STT models with their availability status.
    """
    openai_key = get_decrypted_api_key("openai_api_key")
    current_stt = database.get_system_setting("stt_model") or "whisper-1"

    models = [
        {
            "id": model_id,
            "name": info["name"],
            "description": info["description"],
            "price_display": info["price_display"],
            "available": bool(openai_key),
            "is_current": model_id == current_stt,
            "supports_diarization": False  # Not yet supported
        }
        for model_id, info in STT_MODELS.items()
    ]

    return {
        "models": models,
        "supported_formats": ["mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "ogg", "flac"],
        "current_model": current_stt,
        "openai_configured": bool(openai_key)
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
    Transcribe audio to text using OpenAI or Google STT models.

    Accepts audio files (webm, mp3, mp4, wav, m4a, etc.) and returns transcribed text.
    Uses the configured STT model from settings.

    OpenAI models: whisper-1, gpt-4o-transcribe, gpt-4o-mini-transcribe, gpt-4o-transcribe-diarize
    Google models: gemini-2.5-flash, chirp-3, latest-long, latest-short (uses Gemini API)
    """
    # Get configured STT model (default to whisper-1 for backwards compatibility)
    stt_model = database.get_system_setting("stt_model") or "whisper-1"
    model_info = STT_MODELS.get(stt_model, {})
    provider = model_info.get("provider", "openai-stt")

    # Route to appropriate provider
    if provider == "google-stt":
        return await _transcribe_with_gemini(file, stt_model, model_info)
    else:
        return await _transcribe_with_openai(file, stt_model, model_info)


async def _transcribe_with_gemini(
    file: UploadFile,
    stt_model: str,
    model_info: dict
) -> TranscriptionResponse:
    """Transcribe audio using Google Gemini API."""
    import base64

    # Check for Gemini API key
    gemini_key = get_decrypted_api_key("image_api_key")  # Gemini key is stored as image_api_key
    if not gemini_key:
        raise HTTPException(
            status_code=400,
            detail="Google Gemini API key not configured. Go to Settings > Integrations to add your key."
        )

    # Validate file type - Gemini supports various audio formats
    content_type = file.content_type or ""
    allowed_types = [
        "audio/webm", "audio/mp3", "audio/mpeg", "audio/mp4",
        "audio/wav", "audio/x-wav", "audio/m4a", "audio/x-m4a",
        "audio/ogg", "audio/flac", "audio/aac", "audio/aiff",
        "video/webm"  # video/webm often contains audio only
    ]

    # Also check by extension
    filename = file.filename or "audio.webm"
    allowed_extensions = [".webm", ".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".mpeg", ".flac", ".aac", ".aiff"]
    file_ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if content_type not in allowed_types and file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format: {content_type or file_ext}. Supported: webm, mp3, mp4, wav, m4a, ogg, flac, aac, aiff"
        )

    # Read the audio file
    try:
        audio_data = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read audio file: {str(e)}")

    if len(audio_data) == 0:
        raise HTTPException(status_code=400, detail="Audio file is empty")

    # Gemini supports larger files - up to 1GB via File API, but inline is limited
    # For inline, max is about 20MB base64 encoded
    if len(audio_data) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Audio file too large. Maximum size is 20MB for inline transcription.")

    # Map content types to Gemini MIME types
    mime_type_map = {
        "audio/webm": "audio/webm",
        "audio/mp3": "audio/mp3",
        "audio/mpeg": "audio/mpeg",
        "audio/mp4": "audio/mp4",
        "audio/wav": "audio/wav",
        "audio/x-wav": "audio/wav",
        "audio/m4a": "audio/mp4",
        "audio/x-m4a": "audio/mp4",
        "audio/ogg": "audio/ogg",
        "audio/flac": "audio/flac",
        "audio/aac": "audio/aac",
        "audio/aiff": "audio/aiff",
        "video/webm": "audio/webm",
    }

    # Determine MIME type
    gemini_mime_type = mime_type_map.get(content_type)
    if not gemini_mime_type and file_ext:
        ext_to_mime = {
            ".webm": "audio/webm",
            ".mp3": "audio/mp3",
            ".mp4": "audio/mp4",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
            ".mpeg": "audio/mpeg",
            ".flac": "audio/flac",
            ".aac": "audio/aac",
            ".aiff": "audio/aiff",
        }
        gemini_mime_type = ext_to_mime.get(file_ext, "audio/webm")

    if not gemini_mime_type:
        gemini_mime_type = "audio/webm"  # Default fallback

    # Base64 encode the audio data
    audio_base64 = base64.standard_b64encode(audio_data).decode("utf-8")

    # Check if diarization is supported and requested
    supports_diarization = model_info.get("supports_diarization", False)

    # Build the prompt for transcription
    if supports_diarization:
        transcription_prompt = (
            "Please transcribe this audio accurately. "
            "If there are multiple speakers, identify them and format the output as:\n"
            "Speaker 1: [text]\nSpeaker 2: [text]\n...\n"
            "If there's only one speaker, just provide the transcription."
        )
    else:
        transcription_prompt = "Please transcribe this audio accurately. Provide only the transcription text."

    # Determine which Gemini model to use for transcription
    # Map STT model IDs to actual Gemini model names
    gemini_model_map = {
        "gemini-2.5-flash": "gemini-2.5-flash-preview-05-20",  # Latest flash model
        "chirp-3": "gemini-2.0-flash",  # Chirp uses Gemini flash for audio
        "latest-long": "gemini-2.0-flash",  # Long-form content
        "latest-short": "gemini-2.0-flash",  # Short utterances
    }
    gemini_model = gemini_model_map.get(stt_model, "gemini-2.0-flash")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Build the Gemini API request with audio content
            payload = {
                "contents": [
                    {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": gemini_mime_type,
                                    "data": audio_base64
                                }
                            },
                            {
                                "text": transcription_prompt
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.1,  # Low temperature for accurate transcription
                    "maxOutputTokens": 8192
                }
            }

            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}",
                json=payload,
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 400:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Bad request")
                logger.error(f"Gemini transcription failed: {response.status_code} - {error_msg}")

                # Check for specific error types
                if "API_KEY_INVALID" in str(error_data):
                    raise HTTPException(
                        status_code=400,
                        detail="Google AI API key is invalid. Please update it in Settings > Integrations."
                    )
                elif "SAFETY" in str(error_data) or "blocked" in error_msg.lower():
                    raise HTTPException(
                        status_code=400,
                        detail="Audio was blocked by safety filters."
                    )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Transcription failed: {error_msg}"
                    )

            elif response.status_code == 401 or response.status_code == 403:
                raise HTTPException(
                    status_code=400,
                    detail="Google AI API key is invalid or expired. Please update it in Settings > Integrations."
                )

            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Google AI rate limit exceeded. Please wait a moment and try again."
                )

            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"Gemini transcription failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Transcription failed: {error_detail}"
                )

            result = response.json()

            # Extract the transcription from the response
            candidates = result.get("candidates", [])
            if not candidates:
                raise HTTPException(
                    status_code=500,
                    detail="No transcription was generated. The model may have refused the request."
                )

            parts = candidates[0].get("content", {}).get("parts", [])
            transcribed_text = ""

            for part in parts:
                if "text" in part:
                    transcribed_text = part["text"].strip()
                    break

            if not transcribed_text:
                raise HTTPException(
                    status_code=500,
                    detail="Transcription returned empty result."
                )

            return TranscriptionResponse(
                text=transcribed_text,
                duration_seconds=None  # Gemini doesn't return duration
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Transcription timed out. Please try with a shorter audio clip."
        )
    except Exception as e:
        logger.error(f"Gemini transcription error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Transcription failed: {str(e)}"
        )


async def _transcribe_with_openai(
    file: UploadFile,
    stt_model: str,
    model_info: dict
) -> TranscriptionResponse:
    """Transcribe audio using OpenAI API."""
    # Check for OpenAI API key
    openai_key = get_decrypted_api_key("openai_api_key")
    if not openai_key:
        raise HTTPException(
            status_code=400,
            detail="OpenAI API key not configured. Go to Settings > Integrations to add your key."
        )

    # Validate file type
    content_type = file.content_type or ""
    allowed_types = [
        "audio/webm", "audio/mp3", "audio/mpeg", "audio/mp4",
        "audio/wav", "audio/x-wav", "audio/m4a", "audio/x-m4a",
        "audio/ogg", "video/webm"  # video/webm often contains audio only
    ]

    # Also check by extension
    filename = file.filename or "audio.webm"
    allowed_extensions = [".webm", ".mp3", ".mp4", ".wav", ".m4a", ".ogg", ".mpeg"]
    file_ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if content_type not in allowed_types and file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format: {content_type or file_ext}. Supported: webm, mp3, mp4, wav, m4a, ogg"
        )

    # Read the audio file
    try:
        audio_data = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read audio file: {str(e)}")

    if len(audio_data) == 0:
        raise HTTPException(status_code=400, detail="Audio file is empty")

    # Max 25MB (OpenAI limit)
    if len(audio_data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Audio file too large. Maximum size is 25MB.")

    # Call OpenAI Audio Transcription API with retry logic for rate limits
    max_retries = 3
    retry_delay = 2  # seconds

    # Check if this model requires special handling for diarization
    is_diarize_model = stt_model == "gpt-4o-transcribe-diarize"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Prepare the multipart form data with selected model
            # For gpt-4o-transcribe-diarize, we need:
            # - chunking_strategy: "auto" (required for audio > 30s)
            # - response_format: "diarized_json" to get speaker annotations
            if is_diarize_model:
                response_format = "diarized_json"
            else:
                response_format = "json"

            files = {
                "file": (filename, BytesIO(audio_data), content_type or "audio/webm"),
                "model": (None, stt_model),
                "response_format": (None, response_format),
            }

            # Add chunking_strategy for diarize model (required for audio > 30s)
            if is_diarize_model:
                files["chunking_strategy"] = (None, "auto")

            response = None
            _last_error = None  # Tracks error type for potential logging

            for attempt in range(max_retries):
                # Need to recreate BytesIO for each attempt since it gets consumed
                files["file"] = (filename, BytesIO(audio_data), content_type or "audio/webm")

                response = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {openai_key}"},
                    files=files
                )

                if response.status_code == 429:
                    # Rate limited - wait and retry
                    _last_error = "rate_limit"  # Tracked for potential logging/metrics
                    if attempt < max_retries - 1:
                        # Try to get retry-after header, otherwise use exponential backoff
                        retry_after = response.headers.get("retry-after")
                        wait_time = int(retry_after) if retry_after else retry_delay * (2 ** attempt)
                        logger.info(f"Rate limited, waiting {wait_time}s before retry {attempt + 2}/{max_retries}")
                        await asyncio.sleep(wait_time)
                        continue
                else:
                    # Not a rate limit error, break out of retry loop
                    break

            if response is None:
                raise HTTPException(status_code=500, detail="Failed to connect to OpenAI API")

            if response.status_code == 401:
                raise HTTPException(
                    status_code=400,
                    detail="OpenAI API key is invalid. Please update it in Settings > Integrations."
                )
            elif response.status_code == 429:
                # Still rate limited after all retries
                raise HTTPException(
                    status_code=429,
                    detail="OpenAI rate limit exceeded. Please wait 30-60 seconds and try again. If this persists, check your OpenAI account usage limits."
                )
            elif response.status_code == 400:
                # Parse the error to give a better message
                error_detail = response.text
                logger.error(f"OpenAI transcription failed: {response.status_code} - {error_detail}")

                # Check for specific error types
                if "does not support the format" in error_detail or "unsupported_format" in error_detail:
                    raise HTTPException(
                        status_code=400,
                        detail=f"The model '{stt_model}' does not support this audio format. Try using 'whisper-1' which supports more formats, or convert your audio to mp3/wav."
                    )
                elif "chunking_strategy is required" in error_detail:
                    raise HTTPException(
                        status_code=400,
                        detail="This model requires diarization settings. Please use a different model or contact support."
                    )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Transcription failed: {error_detail}"
                    )
            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"OpenAI transcription failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Transcription failed: {error_detail}"
                )

            result = response.json()

            # Handle diarized_json format which has segments with speaker labels
            if is_diarize_model and "segments" in result:
                # Extract text with speaker labels from segments
                # Format: "Speaker A: text\nSpeaker B: text\n..."
                segments = result.get("segments", [])
                if segments:
                    lines = []
                    for seg in segments:
                        speaker = seg.get("speaker", "Unknown")
                        text = seg.get("text", "").strip()
                        if text:
                            lines.append(f"{speaker}: {text}")
                    transcribed_text = "\n".join(lines)
                else:
                    transcribed_text = result.get("text", "").strip()
            else:
                transcribed_text = result.get("text", "").strip()

            return TranscriptionResponse(
                text=transcribed_text,
                duration_seconds=result.get("duration")
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Transcription timed out. Please try with a shorter audio clip."
        )
    except Exception as e:
        logger.error(f"OpenAI transcription error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Transcription failed: {str(e)}"
        )


# ============================================================================
# Text-to-Speech Generation
# ============================================================================


class TTSGenerateRequest(BaseModel):
    """Request to generate TTS audio"""
    text: str
    model: Optional[str] = None  # If not provided, uses configured default
    voice: Optional[str] = None  # If not provided, uses configured default
    speed: Optional[float] = None  # 0.25 to 4.0
    output_format: Optional[str] = "mp3"  # mp3, wav, opus, aac, flac, pcm
    instructions: Optional[str] = None  # Voice style instructions (for steerable models)


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


@router.post("/tts/generate", response_model=TTSGenerateResponse)
async def generate_tts(
    request: TTSGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Generate TTS audio (non-streaming).

    Returns a URL to the generated audio file.
    Use this for:
    - Studio TTS generator
    - Downloading audio files
    - Models that don't support streaming

    For streaming audio (conversation mode), use /tts/stream instead.
    """
    # Get configured model or use request override
    tts_model = request.model or database.get_system_setting("tts_model") or "gpt-4o-mini-tts"
    model_info = TTS_MODELS.get(tts_model)

    if not model_info:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid TTS model: {tts_model}. Available: {', '.join(TTS_MODELS.keys())}"
        )

    provider = model_info.get("provider", "openai-tts")

    # Get voice and other settings
    voice = request.voice or database.get_system_setting("tts_voice") or "alloy"
    speed = request.speed
    if speed is None:
        speed_str = database.get_system_setting("tts_speed")
        speed = float(speed_str) if speed_str else 1.0
    output_format = request.output_format or database.get_system_setting("tts_output_format") or "mp3"
    instructions = request.instructions or database.get_system_setting("tts_instructions") or ""

    # Validate text
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Text too long. Maximum 4096 characters.")

    # Validate speed
    if speed < 0.25 or speed > 4.0:
        raise HTTPException(status_code=400, detail="Speed must be between 0.25 and 4.0")

    # Route to appropriate provider
    if provider == "gemini-tts":
        return await _generate_tts_gemini(text, tts_model, voice, speed, output_format, instructions, model_info)
    elif provider == "google-cloud-tts":
        return await _generate_tts_google_cloud(text, tts_model, voice, speed, output_format, instructions, model_info)
    else:
        return await _generate_tts_openai(text, tts_model, voice, speed, output_format, instructions, model_info)


async def _generate_tts_openai(
    text: str,
    model: str,
    voice: str,
    speed: float,
    output_format: str,
    instructions: str,
    model_info: dict
) -> TTSGenerateResponse:
    """Generate TTS using OpenAI API."""
    openai_key = get_decrypted_api_key("openai_api_key")
    if not openai_key:
        raise HTTPException(
            status_code=400,
            detail="OpenAI API key not configured. Go to Settings > Integrations to add your key."
        )

    # OpenAI valid voices
    valid_voices = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse", "marin", "cedar"]
    if voice not in valid_voices:
        voice = "alloy"  # Default fallback

    # Valid output formats
    valid_formats = ["mp3", "opus", "aac", "flac", "wav", "pcm"]
    if output_format not in valid_formats:
        output_format = "mp3"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            request_body = {
                "model": model,
                "input": text,
                "voice": voice,
                "speed": speed,
                "response_format": output_format
            }

            # Add instructions for steerable models
            if model_info.get("supports_instructions") and instructions:
                request_body["instructions"] = instructions

            response = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json"
                },
                json=request_body
            )

            if response.status_code == 401:
                raise HTTPException(
                    status_code=400,
                    detail="OpenAI API key is invalid. Please update it in Settings > Integrations."
                )
            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please wait and try again."
                )
            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"OpenAI TTS failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"TTS generation failed: {error_detail}"
                )

            # Save audio to file
            audio_data = response.content
            audio_dir = get_generated_audio_dir()
            filename = f"tts-{uuid.uuid4().hex[:8]}.{output_format}"
            file_path = audio_dir / filename

            with open(file_path, "wb") as f:
                f.write(audio_data)

            # Return URL to the audio file
            encoded_path = str(file_path).replace("\\", "/")
            audio_url = f"/api/generated-audio/by-path?path={encoded_path}"

            return TTSGenerateResponse(
                success=True,
                audio_url=audio_url,
                file_path=str(file_path)
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="TTS generation timed out. Please try with shorter text."
        )
    except Exception as e:
        logger.error(f"OpenAI TTS error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"TTS generation failed: {str(e)}"
        )


async def _generate_tts_gemini(
    text: str,
    model: str,
    voice: str,
    speed: float,
    output_format: str,
    instructions: str,
    model_info: dict
) -> TTSGenerateResponse:
    """Generate TTS using Gemini API (generativelanguage.googleapis.com)."""
    import base64

    gemini_key = get_decrypted_api_key("image_api_key")
    if not gemini_key:
        raise HTTPException(
            status_code=400,
            detail="Google AI API key not configured. Go to Settings > Integrations to add your key."
        )

    # Map model IDs to Gemini model names
    gemini_model_map = {
        "gemini-2.5-flash-tts": "gemini-2.5-flash-preview-tts",
    }
    gemini_model = gemini_model_map.get(model, "gemini-2.0-flash")

    # For Gemini TTS, we use the generateContent endpoint with speech config
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Build speech config
            speech_config = {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice if voice else "Zephyr"
                    }
                }
            }

            # Build the request
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": text}
                        ]
                    }
                ],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": speech_config
                }
            }

            # Add style instructions if supported
            if model_info.get("supports_instructions") and instructions:
                # Prepend instructions to the text as a system-like prompt
                payload["contents"][0]["parts"].insert(0, {
                    "text": f"[Voice style: {instructions}]\n\n"
                })

            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}",
                json=payload,
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 400:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Bad request")
                logger.error(f"Gemini TTS failed: {response.status_code} - {error_msg}")
                raise HTTPException(status_code=400, detail=f"TTS generation failed: {error_msg}")
            elif response.status_code == 401 or response.status_code == 403:
                raise HTTPException(
                    status_code=400,
                    detail="Google AI API key is invalid. Please update it in Settings > Integrations."
                )
            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please wait and try again."
                )
            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"Gemini TTS failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"TTS generation failed: {error_detail}"
                )

            result = response.json()

            # Extract audio from response
            candidates = result.get("candidates", [])
            if not candidates:
                raise HTTPException(
                    status_code=500,
                    detail="No audio was generated."
                )

            parts = candidates[0].get("content", {}).get("parts", [])

            for part in parts:
                if "inlineData" in part:
                    inline_data = part["inlineData"]
                    audio_base64 = inline_data.get("data")
                    mime_type = inline_data.get("mimeType", "audio/mp3")

                    if audio_base64:
                        # Decode and save
                        audio_data = base64.b64decode(audio_base64)
                        audio_dir = get_generated_audio_dir()

                        # Determine extension from mime type
                        ext_map = {
                            "audio/mp3": "mp3",
                            "audio/mpeg": "mp3",
                            "audio/wav": "wav",
                            "audio/ogg": "ogg",
                            "audio/opus": "opus"
                        }
                        ext = ext_map.get(mime_type, "mp3")

                        filename = f"tts-{uuid.uuid4().hex[:8]}.{ext}"
                        file_path = audio_dir / filename

                        with open(file_path, "wb") as f:
                            f.write(audio_data)

                        encoded_path = str(file_path).replace("\\", "/")
                        audio_url = f"/api/generated-audio/by-path?path={encoded_path}"

                        return TTSGenerateResponse(
                            success=True,
                            audio_url=audio_url,
                            file_path=str(file_path)
                        )

            raise HTTPException(
                status_code=500,
                detail="No audio data in response."
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="TTS generation timed out. Please try with shorter text."
        )
    except Exception as e:
        logger.error(f"Gemini TTS error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"TTS generation failed: {str(e)}"
        )




async def _generate_tts_google_cloud(
    text: str,
    model: str,
    voice: str,
    speed: float,
    output_format: str,
    instructions: str,
    model_info: dict
) -> TTSGenerateResponse:
    """Generate TTS using Google Cloud Text-to-Speech API (texttospeech.googleapis.com)."""
    import base64

    # Google Cloud TTS can use the same API key as Gemini
    api_key = get_decrypted_api_key("image_api_key")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Google AI API key not configured. Go to Settings > Integrations to add your key."
        )

    # Map output format to Google Cloud TTS audio encoding
    encoding_map = {
        "mp3": "MP3",
        "wav": "LINEAR16",
        "ogg": "OGG_OPUS",
        "opus": "OGG_OPUS",
        "alaw": "ALAW",
        "mulaw": "MULAW"
    }
    audio_encoding = encoding_map.get(output_format, "MP3")

    # Build voice name from model prefix and user voice selection
    # Format: {language}-{voice_prefix}-{voiceName}
    # e.g., en-US-Chirp3-HD-Charon, en-US-Neural2-A, en-US-Wavenet-D
    voice_prefix = model_info.get("voice_prefix", "Neural2")
    language_code = "en-US"  # Default language

    # OpenAI voice names that should be ignored for Google Cloud TTS
    openai_voices = {"alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse"}

    # Default voices for each model type
    default_voices = {
        "Chirp3-HD": "Charon",
        "Neural2": "C",
        "Wavenet": "D",
        "Standard": "A",
        "Studio": "M",
        "Polyglot": "1"
    }

    # Determine voice name
    if voice and voice.lower() in openai_voices:
        # OpenAI voice selected - use default for this model type
        default_voice = default_voices.get(voice_prefix, "A")
        voice_name = f"{language_code}-{voice_prefix}-{default_voice}"
    elif voice and "-" in voice and ("standard" in voice.lower() or "wavenet" in voice.lower() or "neural2" in voice.lower() or "chirp" in voice.lower() or "studio" in voice.lower() or "polyglot" in voice.lower()):
        # Full Google Cloud voice name provided (e.g., en-US-Neural2-A)
        voice_name = voice
        # Extract language code from voice name
        parts = voice.split("-")
        if len(parts) >= 2:
            language_code = f"{parts[0]}-{parts[1]}"
    elif voice and len(voice) <= 2:
        # Single letter/number variant (e.g., "A", "B", "C", "1")
        voice_name = f"{language_code}-{voice_prefix}-{voice.upper()}"
    elif voice:
        # Assume it's a voice variant name for Chirp3-HD style voices (e.g., "Charon", "Leda")
        voice_name = f"{language_code}-{voice_prefix}-{voice}"
    else:
        # No voice specified - use default
        default_voice = default_voices.get(voice_prefix, "A")
        voice_name = f"{language_code}-{voice_prefix}-{default_voice}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Build the request payload
            payload = {
                "input": {
                    "text": text
                },
                "voice": {
                    "languageCode": language_code,
                    "name": voice_name
                },
                "audioConfig": {
                    "audioEncoding": audio_encoding,
                    "speakingRate": speed  # 0.25 to 4.0
                }
            }

            # Make request to Google Cloud TTS API
            response = await client.post(
                f"https://texttospeech.googleapis.com/v1/text:synthesize?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 400:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Bad request")
                logger.error(f"Google Cloud TTS failed: {response.status_code} - {error_msg}")
                raise HTTPException(status_code=400, detail=f"TTS generation failed: {error_msg}")
            elif response.status_code == 401 or response.status_code == 403:
                raise HTTPException(
                    status_code=400,
                    detail="Google API key is invalid or doesn't have Text-to-Speech API enabled. Please check your API key permissions."
                )
            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please wait and try again."
                )
            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"Google Cloud TTS failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"TTS generation failed: {error_detail}"
                )

            result = response.json()

            # Extract audio content (base64 encoded)
            audio_content = result.get("audioContent")
            if not audio_content:
                raise HTTPException(
                    status_code=500,
                    detail="No audio content in response."
                )

            # Decode and save
            audio_data = base64.b64decode(audio_content)
            audio_dir = get_generated_audio_dir()

            # Determine file extension
            ext_map = {
                "MP3": "mp3",
                "LINEAR16": "wav",
                "OGG_OPUS": "ogg",
                "ALAW": "wav",
                "MULAW": "wav"
            }
            ext = ext_map.get(audio_encoding, "mp3")

            filename = f"tts-{uuid.uuid4().hex[:8]}.{ext}"
            file_path = audio_dir / filename

            with open(file_path, "wb") as f:
                f.write(audio_data)

            encoded_path = str(file_path).replace("\\", "/")
            audio_url = f"/api/generated-audio/by-path?path={encoded_path}"

            return TTSGenerateResponse(
                success=True,
                audio_url=audio_url,
                file_path=str(file_path)
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="TTS generation timed out. Please try with shorter text."
        )
    except Exception as e:
        logger.error(f"Google Cloud TTS error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"TTS generation failed: {str(e)}"
        )

@router.post("/tts/stream")
async def stream_tts(
    request: TTSGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Stream TTS audio in real-time.

    Returns audio chunks as they're generated for low-latency playback.
    Only supported by certain models (gpt-4o-mini-tts).

    For models that don't support streaming, falls back to generating
    the full audio and returning it as a single response.
    """
    # Get configured model or use request override
    tts_model = request.model or database.get_system_setting("tts_model") or "gpt-4o-mini-tts"
    model_info = TTS_MODELS.get(tts_model)

    if not model_info:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid TTS model: {tts_model}. Available: {', '.join(TTS_MODELS.keys())}"
        )

    # Check if model supports streaming
    if not model_info.get("supports_streaming", False):
        # Fall back to non-streaming - generate full audio and stream as single chunk
        result = await generate_tts(request, token)
        if not result.success:
            raise HTTPException(status_code=500, detail=result.error or "TTS generation failed")

        # Read the file and return as streaming response
        if result.file_path:
            async def single_chunk_stream():
                with open(result.file_path, "rb") as f:
                    yield f.read()

            output_format = request.output_format or "mp3"
            mime_types = {
                "mp3": "audio/mpeg",
                "opus": "audio/opus",
                "aac": "audio/aac",
                "flac": "audio/flac",
                "wav": "audio/wav",
                "pcm": "audio/pcm"
            }
            return StreamingResponse(
                single_chunk_stream(),
                media_type=mime_types.get(output_format, "audio/mpeg")
            )

    # Determine provider and route to appropriate streaming implementation
    provider = model_info.get("provider", "openai-tts")

    # Google TTS (Gemini or Cloud TTS) - fall back to non-streaming
    if provider in ("gemini-tts", "google-cloud-tts"):
        return await _stream_tts_google(request, tts_model, model_info)

    # OpenAI streaming
    openai_key = get_decrypted_api_key("openai_api_key")
    if not openai_key:
        raise HTTPException(
            status_code=400,
            detail="OpenAI API key not configured. Go to Settings > Integrations to add your key."
        )

    # Get voice and settings
    voice = request.voice or database.get_system_setting("tts_voice") or "alloy"
    speed = request.speed
    if speed is None:
        speed_str = database.get_system_setting("tts_speed")
        speed = float(speed_str) if speed_str else 1.0
    output_format = request.output_format or "mp3"
    instructions = request.instructions or database.get_system_setting("tts_instructions") or ""

    # Validate text
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Text too long. Maximum 4096 characters.")

    # OpenAI valid voices
    valid_voices = ["alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse", "marin", "cedar"]
    if voice not in valid_voices:
        voice = "alloy"

    async def stream_openai_audio():
        """Stream audio from OpenAI TTS API."""
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                request_body = {
                    "model": tts_model,
                    "input": text,
                    "voice": voice,
                    "speed": speed,
                    "response_format": output_format,
                    "stream": True  # Enable streaming
                }

                # Add instructions for steerable models
                if model_info.get("supports_instructions") and instructions:
                    request_body["instructions"] = instructions

                async with client.stream(
                    "POST",
                    "https://api.openai.com/v1/audio/speech",
                    headers={
                        "Authorization": f"Bearer {openai_key}",
                        "Content-Type": "application/json"
                    },
                    json=request_body
                ) as response:
                    if response.status_code != 200:
                        error_text = ""
                        async for chunk in response.aiter_bytes():
                            error_text += chunk.decode("utf-8", errors="ignore")
                        logger.error(f"OpenAI TTS streaming failed: {response.status_code} - {error_text}")
                        return

                    async for chunk in response.aiter_bytes():
                        if chunk:
                            yield chunk

        except Exception as e:
            logger.error(f"OpenAI TTS streaming error: {e}")

    mime_types = {
        "mp3": "audio/mpeg",
        "opus": "audio/opus",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "pcm": "audio/pcm"
    }

    return StreamingResponse(
        stream_openai_audio(),
        media_type=mime_types.get(output_format, "audio/mpeg"),
        headers={
            "Cache-Control": "no-cache",
            "Transfer-Encoding": "chunked"
        }
    )


async def _stream_tts_google(
    request: TTSGenerateRequest,
    model: str,
    model_info: dict
) -> StreamingResponse:
    """
    Stream TTS audio from Google Cloud TTS API.

    Uses the streaming_synthesize method for Chirp 3 HD voices.
    For models that don't support true streaming, falls back to
    generating full audio and streaming it as a single chunk.
    """
    gemini_key = get_decrypted_api_key("image_api_key")
    if not gemini_key:
        raise HTTPException(
            status_code=400,
            detail="Google AI API key not configured. Go to Settings > Integrations to add your key."
        )

    # Get voice and settings
    voice = request.voice or database.get_system_setting("tts_voice") or "en-US-Chirp3-HD-Achernar"
    speed = request.speed
    if speed is None:
        speed_str = database.get_system_setting("tts_speed")
        speed = float(speed_str) if speed_str else 1.0
    output_format = request.output_format or "mp3"

    # Validate text
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    if len(text) > 4096:
        raise HTTPException(status_code=400, detail="Text too long. Maximum 4096 characters.")

    # Google Cloud TTS streaming requires the Cloud TTS API with specific voices
    # Chirp 3 HD voices follow the pattern: {language}-Chirp3-HD-{voiceName}
    # For now, we'll use the REST API and stream the result as a single chunk
    # True streaming would require gRPC client which adds complexity

    # Fall back to non-streaming generation for Google
    # The streaming_synthesize API requires gRPC, not REST
    provider = model_info.get("provider", "gemini-tts")
    if provider == "google-cloud-tts":
        result = await _generate_tts_google_cloud(
            text=text,
            model=model,
            voice=voice,
            speed=speed,
            output_format=output_format,
            instructions="",
            model_info=model_info
        )
    else:
        result = await _generate_tts_gemini(
            text=text,
            model=model,
            voice=voice,
            speed=speed,
            output_format=output_format,
            instructions="",
            model_info=model_info
        )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error or "TTS generation failed")

    # Stream the generated file
    if result.file_path:
        async def stream_file():
            with open(result.file_path, "rb") as f:
                # Stream in chunks for better memory efficiency
                while chunk := f.read(8192):
                    yield chunk

        mime_types = {
            "mp3": "audio/mpeg",
            "opus": "audio/opus",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "wav": "audio/wav",
            "pcm": "audio/pcm",
            "LINEAR16": "audio/wav"
        }
        return StreamingResponse(
            stream_file(),
            media_type=mime_types.get(output_format, "audio/mpeg"),
            headers={
                "Cache-Control": "no-cache",
                "Transfer-Encoding": "chunked"
            }
        )

    raise HTTPException(status_code=500, detail="TTS generation failed - no file produced")


# ============================================================================
# Image Generation (Multi-Provider)
# ============================================================================

class ImageModelUpdateRequest(BaseModel):
    """Request to update the image model"""
    model: str


class ImageProviderUpdateRequest(BaseModel):
    """Request to update the image provider"""
    provider: str


@router.get("/integrations/image/models")
async def get_image_models(token: str = Depends(require_auth)):
    """
    Get available image generation models grouped by provider.
    """
    # Get current settings
    current_provider = database.get_system_setting("image_provider")
    current_model = database.get_system_setting("image_model")
    # API keys retrieved for availability checking in model iteration below
    _openai_key = get_decrypted_api_key("openai_api_key")
    _image_api_key = get_decrypted_api_key("image_api_key")

    # Build models list with availability info
    models = []
    for model_id, model_info in IMAGE_MODELS.items():
        provider_id = model_info["provider"]
        provider_info = IMAGE_PROVIDERS[provider_id]

        # Check if this provider's API key is configured
        required_key = provider_info["requires_key"]
        is_available = bool(get_decrypted_api_key(required_key))

        models.append({
            "id": model_id,
            "name": model_info["name"],
            "description": model_info["description"],
            "price_per_image": model_info["price_per_image"],
            "provider": provider_id,
            "provider_name": provider_info["name"],
            "capabilities": model_info.get("capabilities", []),
            "available": is_available,
            "is_current": model_id == current_model
        })

    return {
        "models": models,
        "providers": [
            {
                "id": provider_id,
                "name": provider_info["name"],
                "description": provider_info["description"],
                "available": bool(database.get_system_setting(provider_info["requires_key"])),
                "is_current": provider_id == current_provider
            }
            for provider_id, provider_info in IMAGE_PROVIDERS.items()
        ],
        "current_provider": current_provider,
        "current_model": current_model
    }


@router.post("/integrations/image", response_model=ImageGenerationResponse)
async def set_image_generation(
    request: ImageGenerationRequest,
    token: str = Depends(require_admin)
):
    """
    Configure image generation provider and API key (admin only).

    Supports Google Gemini (Nano Banana) models.
    For OpenAI GPT Image, use the OpenAI API key configured in Whisper section.
    """
    provider = request.provider.lower().strip()
    model = request.model.strip()
    api_key = request.api_key.strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    # Map legacy "google" provider to new ID
    if provider == "google":
        provider = "google-gemini"

    # Validate provider
    if provider not in ["google-gemini"]:
        raise HTTPException(
            status_code=400,
            detail="This endpoint is for Google Gemini API key. OpenAI GPT Image uses the OpenAI key from Whisper settings."
        )

    # Get models for this provider
    provider_models = [m for m in IMAGE_MODELS.keys() if IMAGE_MODELS[m]["provider"] == provider]
    if model not in provider_models:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model for {provider}. Available models: {', '.join(provider_models)}"
        )

    # Validate the API key by making a test request to Google AI
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            )
            if response.status_code == 400 and "API_KEY_INVALID" in response.text:
                raise HTTPException(status_code=400, detail="Invalid API key. Please check your Google AI API key.")
            elif response.status_code == 403:
                raise HTTPException(status_code=400, detail="API key doesn't have permission. Enable the Generative Language API.")
            elif response.status_code not in [200, 401]:
                logger.warning(f"Google AI API check returned status {response.status_code}: {response.text}")
    except httpx.TimeoutException:
        logger.warning("Google AI API validation timed out, saving key anyway")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error validating Google AI key: {e}")

    # Save to database (API key encrypted)
    database.set_system_setting("image_provider", provider)
    database.set_system_setting("image_model", model)
    set_encrypted_api_key("image_api_key", api_key)

    return ImageGenerationResponse(
        success=True,
        provider=provider,
        model=model,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/integrations/image")
async def remove_image_generation(token: str = Depends(require_admin)):
    """
    Remove the image generation configuration (admin only).
    """
    database.delete_system_setting("image_provider")
    database.delete_system_setting("image_model")
    database.delete_system_setting("image_api_key")
    return {"success": True}


@router.patch("/integrations/image/model")
async def update_image_model(
    request: ImageModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """
    Update the image generation model (admin only).

    The provider is automatically determined from the selected model.
    """
    model = request.model.strip()

    if model not in IMAGE_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model. Available models: {', '.join(IMAGE_MODELS.keys())}"
        )

    model_info = IMAGE_MODELS[model]
    provider = model_info["provider"]

    # Check if the required API key is configured
    required_key = IMAGE_PROVIDERS[provider]["requires_key"]
    api_key = database.get_system_setting(required_key)

    if not api_key:
        key_name = "Google AI API key (Nano Banana)" if required_key == "image_api_key" else "OpenAI API key"
        raise HTTPException(
            status_code=400,
            detail=f"{key_name} is required for {IMAGE_PROVIDERS[provider]['name']}. Configure it in Settings first."
        )

    # Save the provider and model
    database.set_system_setting("image_provider", provider)
    database.set_system_setting("image_model", model)

    return {
        "success": True,
        "provider": provider,
        "model": model
    }


@router.post("/generate-image", response_model=ImageGenerateResponse)
async def generate_image(
    request: ImageGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Generate an image using the configured provider (Nano Banana / Gemini).

    Returns the generated image as base64 data.
    """
    # Check if image generation is configured
    provider = database.get_system_setting("image_provider")
    model = database.get_system_setting("image_model")
    api_key = get_decrypted_api_key("image_api_key")

    if not all([provider, model, api_key]):
        raise HTTPException(
            status_code=400,
            detail="Image generation not configured. Go to Settings > Integrations to set up Nano Banana."
        )

    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    if len(prompt) > 10000:
        raise HTTPException(status_code=400, detail="Prompt is too long. Maximum 10,000 characters.")

    # Call Google Gemini API for image generation
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Gemini image generation request
            # See: https://ai.google.dev/gemini-api/docs/image-generation
            payload = {
                "contents": [
                    {
                        "parts": [
                            {"text": prompt}
                        ]
                    }
                ],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"]
                }
            }

            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                json=payload,
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 400:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", "Bad request")
                # Check for safety filters
                if "SAFETY" in str(error_data) or "blocked" in error_msg.lower():
                    return ImageGenerateResponse(
                        success=False,
                        error="Image generation was blocked by safety filters. Please try a different prompt."
                    )
                raise HTTPException(status_code=400, detail=f"API error: {error_msg}")

            elif response.status_code == 401 or response.status_code == 403:
                raise HTTPException(
                    status_code=400,
                    detail="API key is invalid or expired. Please update it in Settings > Integrations."
                )

            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please wait a moment and try again."
                )

            elif response.status_code != 200:
                logger.error(f"Gemini API error: {response.status_code} - {response.text}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Image generation failed: {response.text}"
                )

            result = response.json()

            # Extract image from response
            candidates = result.get("candidates", [])
            if not candidates:
                return ImageGenerateResponse(
                    success=False,
                    error="No image was generated. The model may have refused the request."
                )

            parts = candidates[0].get("content", {}).get("parts", [])

            for part in parts:
                if "inlineData" in part:
                    inline_data = part["inlineData"]
                    return ImageGenerateResponse(
                        success=True,
                        image_base64=inline_data.get("data"),
                        mime_type=inline_data.get("mimeType", "image/png")
                    )

            # No image found in response
            # Check if there's a text response (model might have refused)
            for part in parts:
                if "text" in part:
                    return ImageGenerateResponse(
                        success=False,
                        error=f"Model response: {part['text'][:500]}"
                    )

            return ImageGenerateResponse(
                success=False,
                error="No image was generated. Please try a different prompt."
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Image generation timed out. Please try again with a simpler prompt."
        )
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Image generation failed: {str(e)}"
        )


# ============================================================================
# Video Generation Model Selection
# ============================================================================

# Available video providers and their models
VIDEO_PROVIDERS = {
    "google-veo": {
        "name": "Google Veo",
        "description": "Google's video generation API",
        "requires_key": "image_api_key"  # Uses the same key as Gemini/Nano Banana
    },
    "openai-sora": {
        "name": "OpenAI Sora",
        "description": "OpenAI's video generation API",
        "requires_key": "openai_api_key"
    }
}

# Available video models by provider
VIDEO_MODELS = {
    # Google Veo 3 models with native audio
    "veo-3-fast-generate-preview": {
        "name": "Veo 3 Fast",
        "description": "Fast video with native audio (dialogue, effects, music)",
        "price_per_second": 0.40,
        "provider": "google-veo",
        "max_duration": 8,
        "capabilities": ["text-to-video", "image-to-video", "video-with-audio"]
    },
    "veo-3-generate-preview": {
        "name": "Veo 3",
        "description": "Highest quality video with native audio",
        "price_per_second": 0.75,
        "provider": "google-veo",
        "max_duration": 8,
        "capabilities": ["text-to-video", "image-to-video", "video-with-audio"]
    },
    # Google Veo 3.1 models (no audio)
    "veo-3.1-fast-generate-preview": {
        "name": "Veo 3.1 Fast",
        "description": "Fast video generation - lower latency",
        "price_per_second": 0.15,
        "provider": "google-veo",
        "max_duration": 8,
        "capabilities": ["text-to-video", "image-to-video", "video-extend", "frame-bridge"]
    },
    "veo-3.1-generate-preview": {
        "name": "Veo 3.1",
        "description": "High quality video generation",
        "price_per_second": 0.40,
        "provider": "google-veo",
        "max_duration": 8,
        "capabilities": ["text-to-video", "image-to-video", "video-extend", "frame-bridge"]
    },
    # OpenAI Sora models
    "sora-2": {
        "name": "Sora 2",
        "description": "Fast video generation with good quality",
        "price_per_second": 0.10,
        "provider": "openai-sora",
        "max_duration": 12,
        "capabilities": ["text-to-video", "image-to-video"]
    },
    "sora-2-pro": {
        "name": "Sora 2 Pro",
        "description": "High quality video generation with better details",
        "price_per_second": 0.40,
        "provider": "openai-sora",
        "max_duration": 12,
        "capabilities": ["text-to-video", "image-to-video"]
    }
}

# Legacy alias for backwards compatibility
VEO_MODELS = {k: v for k, v in VIDEO_MODELS.items() if v.get("provider") == "google-veo"}


class VideoModelUpdateRequest(BaseModel):
    """Request to update the video model"""
    model: str


class VideoProviderUpdateRequest(BaseModel):
    """Request to update the video provider"""
    provider: str


@router.patch("/integrations/video/model")
async def update_video_model(
    request: VideoModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """
    Update the video generation model (admin only).

    The provider is automatically determined from the selected model.
    """
    model = request.model.strip()

    if model not in VIDEO_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model. Available models: {', '.join(VIDEO_MODELS.keys())}"
        )

    model_info = VIDEO_MODELS[model]
    provider = model_info["provider"]

    # Check if the required API key is configured
    required_key = VIDEO_PROVIDERS[provider]["requires_key"]
    api_key = database.get_system_setting(required_key)

    if not api_key:
        key_name = "Google AI API key (Nano Banana)" if required_key == "image_api_key" else "OpenAI API key"
        raise HTTPException(
            status_code=400,
            detail=f"{key_name} is required for {VIDEO_PROVIDERS[provider]['name']}. Configure it in Settings first."
        )

    # Save the provider and model
    database.set_system_setting("video_provider", provider)
    database.set_system_setting("video_model", model)

    return {
        "success": True,
        "provider": provider,
        "model": model
    }


@router.patch("/integrations/video/provider")
async def update_video_provider(
    request: VideoProviderUpdateRequest,
    token: str = Depends(require_admin)
):
    """
    Update the video generation provider (admin only).

    This will also reset the model to the first available model for that provider.
    """
    provider = request.provider.strip()

    if provider not in VIDEO_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider. Available providers: {', '.join(VIDEO_PROVIDERS.keys())}"
        )

    # Check if the required API key is configured
    required_key = VIDEO_PROVIDERS[provider]["requires_key"]
    api_key = database.get_system_setting(required_key)

    if not api_key:
        key_name = "Google AI API key (Nano Banana)" if required_key == "image_api_key" else "OpenAI API key"
        raise HTTPException(
            status_code=400,
            detail=f"{key_name} is required for {VIDEO_PROVIDERS[provider]['name']}. Configure it in Settings first."
        )

    # Get first model for this provider
    provider_models = [m for m, info in VIDEO_MODELS.items() if info["provider"] == provider]
    default_model = provider_models[0] if provider_models else None

    # Save the provider and model
    database.set_system_setting("video_provider", provider)
    if default_model:
        database.set_system_setting("video_model", default_model)

    return {
        "success": True,
        "provider": provider,
        "model": default_model
    }


@router.get("/integrations/video/models")
async def get_video_models(token: str = Depends(require_auth)):
    """
    Get available video generation models grouped by provider.
    """
    # Get current settings
    current_provider = database.get_system_setting("video_provider")
    current_model = database.get_system_setting("video_model")
    # API keys retrieved for availability checking in model iteration below
    _openai_key = get_decrypted_api_key("openai_api_key")
    _image_api_key = get_decrypted_api_key("image_api_key")

    # Build models list with availability info
    models = []
    for model_id, model_info in VIDEO_MODELS.items():
        provider_id = model_info["provider"]
        provider_info = VIDEO_PROVIDERS[provider_id]

        # Check if this provider's API key is configured
        required_key = provider_info["requires_key"]
        is_available = bool(get_decrypted_api_key(required_key))

        models.append({
            "id": model_id,
            "name": model_info["name"],
            "description": model_info["description"],
            "price_per_second": model_info["price_per_second"],
            "provider": provider_id,
            "provider_name": provider_info["name"],
            "max_duration": model_info.get("max_duration", 8),
            "capabilities": model_info.get("capabilities", []),
            "available": is_available,
            "is_current": model_id == current_model
        })

    return {
        "models": models,
        "providers": [
            {
                "id": provider_id,
                "name": provider_info["name"],
                "description": provider_info["description"],
                "available": bool(database.get_system_setting(provider_info["requires_key"])),
                "is_current": provider_id == current_provider
            }
            for provider_id, provider_info in VIDEO_PROVIDERS.items()
        ],
        "current_provider": current_provider,
        "current_model": current_model
    }


# ============================================================================
# Voice-to-Text (Speech Recognition)
# ============================================================================

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
    # Check which API keys are configured
    openai_key = get_decrypted_api_key("openai_api_key")
    image_api_key = get_decrypted_api_key("image_api_key")
    meshy_api_key = get_decrypted_api_key("meshy_api_key")

    # Determine which providers are available
    available_providers = set()
    if image_api_key:
        available_providers.add("google-gemini")
        available_providers.add("google-imagen")
        available_providers.add("google-veo")
        available_providers.add("google-gemini-video")
        available_providers.add("google")  # For collaboration tools
    if openai_key:
        available_providers.add("openai-gpt-image")
        available_providers.add("openai-sora")
        available_providers.add("openai")  # For collaboration tools
    if meshy_api_key:
        available_providers.add("meshy")
    # DeepSeek for collaboration tools
    deepseek_api_key = get_decrypted_api_key("deepseek_api_key")
    if deepseek_api_key:
        available_providers.add("deepseek")

    # Get current configured providers
    current_image_provider = database.get_system_setting("image_provider")
    current_video_provider = database.get_system_setting("video_provider")
    current_model3d_provider = database.get_system_setting("model3d_provider")

    # Build tools list with availability
    tools = []
    categories = {}

    for tool_id, tool_info in AI_TOOLS.items():
        # Check if any of the tool's providers are available
        tool_providers = tool_info["providers"]
        is_available = any(p in available_providers for p in tool_providers)

        # Determine which provider would be used for this tool
        active_provider = None
        if tool_info["category"] == "image" and current_image_provider in tool_providers:
            active_provider = current_image_provider
        elif tool_info["category"] == "video" and current_video_provider in tool_providers:
            active_provider = current_video_provider
        elif tool_info["category"] == "3d" and current_model3d_provider in tool_providers:
            active_provider = current_model3d_provider
        elif is_available:
            # Use first available provider
            for p in tool_providers:
                if p in available_providers:
                    active_provider = p
                    break

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
        "current_image_provider": current_image_provider,
        "current_video_provider": current_video_provider
    }


# NOTE: Duplicate transcribe_audio endpoint removed - use the one defined earlier
# in the "Voice Transcription" section (around line 652) which has:
# - Retry logic for rate limits
# - Configurable STT model selection from settings
# - More comprehensive error handling


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
    "openai_api_key": {
        "name": "OpenAI API Key",
        "description": "For TTS, STT, GPT Image, and Sora video generation",
        "admin_has_key": lambda: bool(get_decrypted_api_key("openai_api_key"))
    },
    "gemini_api_key": {
        "name": "Google Gemini API Key",
        "description": "For Nano Banana image generation, Imagen, and Veo video",
        "admin_has_key": lambda: bool(get_decrypted_api_key("image_api_key"))
    },
    "meshy_api_key": {
        "name": "Meshy API Key",
        "description": "For 3D model generation (text-to-3D, image-to-3D, rigging, animation)",
        "admin_has_key": lambda: bool(get_decrypted_api_key("meshy_api_key"))
    },
    "github_pat": {
        "name": "GitHub Personal Access Token",
        "description": "For accessing GitHub repositories",
        "admin_has_key": lambda: False  # Admin can't provide this for users
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
        "openai_api_key": "openai_api_key",
        "gemini_api_key": "image_api_key",
        "meshy_api_key": "meshy_api_key",
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
    from app.db.database import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM system_settings WHERE key = ?", ("security_instructions",))

    return {
        "success": True,
        "message": "Security instructions reverted to default"
    }
