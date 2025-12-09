"""
Settings API routes - integration settings, API keys, and configuration
"""

import asyncio
import logging
import httpx
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from app.db import database
from app.api.auth import require_admin, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])


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


# Available Nano Banana models
# Verified via ListModels API and tested with generateContent
# See: https://ai.google.dev/gemini-api/docs/models
NANO_BANANA_MODELS = {
    "gemini-2.5-flash-image": {
        "name": "Nano Banana (Gemini 2.5 Flash)",
        "description": "Fast image generation with good quality - ~$0.039/image",
        "price_per_image": 0.039
    },
    "gemini-3-pro-image-preview": {
        "name": "Nano Banana Pro (Gemini 3 Pro)",
        "description": "Studio-quality, better text rendering, 2K/4K support - ~$0.10/image",
        "price_per_image": 0.10
    }
}


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
    openai_key = database.get_system_setting("openai_api_key")
    image_provider = database.get_system_setting("image_provider")
    image_model = database.get_system_setting("image_model")
    image_api_key = database.get_system_setting("image_api_key")
    video_provider = database.get_system_setting("video_provider")
    video_model = database.get_system_setting("video_model")

    return IntegrationSettingsResponse(
        openai_api_key_set=bool(openai_key),
        openai_api_key_masked=mask_api_key(openai_key) if openai_key else "",
        image_provider=image_provider,
        image_model=image_model,
        image_api_key_set=bool(image_api_key),
        image_api_key_masked=mask_api_key(image_api_key) if image_api_key else "",
        video_provider=video_provider,
        video_model=video_model
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

    # Save to database
    database.set_system_setting("openai_api_key", api_key)

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
    Transcribe audio to text using OpenAI Whisper.

    Accepts audio files (webm, mp3, mp4, wav, m4a, etc.) and returns transcribed text.
    Requires an OpenAI API key to be configured in settings.
    """
    # Check if OpenAI key is configured
    openai_key = database.get_system_setting("openai_api_key")
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

    # Call OpenAI Whisper API with retry logic for rate limits
    max_retries = 3
    retry_delay = 2  # seconds

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Prepare the multipart form data
            files = {
                "file": (filename, BytesIO(audio_data), content_type or "audio/webm"),
                "model": (None, "whisper-1"),
                "response_format": (None, "json"),
            }

            response = None
            last_error = None

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
                    last_error = "rate_limit"
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
            elif response.status_code != 200:
                error_detail = response.text
                logger.error(f"OpenAI transcription failed: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Transcription failed: {error_detail}"
                )

            result = response.json()
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
        logger.error(f"Transcription error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Transcription failed: {str(e)}"
        )


# ============================================================================
# Image Generation (Nano Banana / Google Gemini)
# ============================================================================

@router.get("/integrations/image/models")
async def get_image_models(token: str = Depends(require_auth)):
    """
    Get available image generation models.
    """
    return {
        "models": [
            {
                "id": model_id,
                "name": model_info["name"],
                "description": model_info["description"],
                "price_per_image": model_info["price_per_image"]
            }
            for model_id, model_info in NANO_BANANA_MODELS.items()
        ]
    }


@router.post("/integrations/image", response_model=ImageGenerationResponse)
async def set_image_generation(
    request: ImageGenerationRequest,
    token: str = Depends(require_admin)
):
    """
    Configure image generation provider and API key (admin only).

    Currently supports Google Gemini (Nano Banana) models.
    """
    provider = request.provider.lower().strip()
    model = request.model.strip()
    api_key = request.api_key.strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    if provider != "google":
        raise HTTPException(status_code=400, detail="Currently only 'google' provider is supported for Nano Banana")

    if model not in NANO_BANANA_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model. Available models: {', '.join(NANO_BANANA_MODELS.keys())}"
        )

    # Validate the API key by making a test request to Google AI
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Test the API key with a simple models list request
            response = await client.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            )
            if response.status_code == 400 and "API_KEY_INVALID" in response.text:
                raise HTTPException(status_code=400, detail="Invalid API key. Please check your Google AI API key.")
            elif response.status_code == 403:
                raise HTTPException(status_code=400, detail="API key doesn't have permission. Enable the Generative Language API.")
            elif response.status_code not in [200, 401]:  # 401 might be okay for some key types
                logger.warning(f"Google AI API check returned status {response.status_code}: {response.text}")
    except httpx.TimeoutException:
        logger.warning("Google AI API validation timed out, saving key anyway")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error validating Google AI key: {e}")

    # Save to database
    database.set_system_setting("image_provider", provider)
    database.set_system_setting("image_model", model)
    database.set_system_setting("image_api_key", api_key)

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


class ImageModelUpdateRequest(BaseModel):
    """Request to update just the image model"""
    model: str


@router.patch("/integrations/image/model")
async def update_image_model(
    request: ImageModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """
    Update just the image model without changing the API key (admin only).

    Allows users to switch between models on the fly without re-entering credentials.
    """
    model = request.model.strip()

    # Check if image generation is configured
    api_key = database.get_system_setting("image_api_key")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="Image generation not configured. Please set up with an API key first."
        )

    if model not in NANO_BANANA_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model. Available models: {', '.join(NANO_BANANA_MODELS.keys())}"
        )

    # Update just the model
    database.set_system_setting("image_model", model)

    return {
        "success": True,
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
    api_key = database.get_system_setting("image_api_key")

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
    # Google Veo models
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
    openai_key = database.get_system_setting("openai_api_key")
    image_api_key = database.get_system_setting("image_api_key")

    # Build models list with availability info
    models = []
    for model_id, model_info in VIDEO_MODELS.items():
        provider_id = model_info["provider"]
        provider_info = VIDEO_PROVIDERS[provider_id]

        # Check if this provider's API key is configured
        required_key = provider_info["requires_key"]
        is_available = bool(database.get_system_setting(required_key))

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

@router.post("/transcribe", response_model=VoiceToTextResponse)
async def transcribe_audio(
    audio: UploadFile = File(...),
    language: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Transcribe audio to text using OpenAI Whisper API.

    Accepts audio files (mp3, mp4, mpeg, mpga, m4a, wav, webm).
    Returns the transcribed text.

    Args:
        audio: Audio file to transcribe
        language: Optional language code (e.g., 'en', 'es', 'fr')
    """
    # Check if OpenAI API key is configured
    api_key = database.get_system_setting("openai_api_key")

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="OpenAI API key not configured. Go to Settings > Integrations to set up voice-to-text."
        )

    # Validate file type
    allowed_types = ["audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a",
                     "audio/wav", "audio/webm", "audio/x-wav", "video/mp4",
                     "video/webm", "audio/ogg", "audio/flac"]
    content_type = audio.content_type or ""

    # Also check by extension
    filename = audio.filename or ""
    allowed_extensions = [".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".flac"]
    ext = "." + filename.split(".")[-1].lower() if "." in filename else ""

    if content_type not in allowed_types and ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format: {content_type or ext}. Supported: mp3, mp4, m4a, wav, webm, ogg, flac"
        )

    # Read audio file
    try:
        audio_content = await audio.read()

        # Check file size (25MB limit for Whisper API)
        if len(audio_content) > 25 * 1024 * 1024:
            raise HTTPException(
                status_code=400,
                detail="Audio file too large. Maximum size is 25MB."
            )

        if len(audio_content) == 0:
            raise HTTPException(
                status_code=400,
                detail="Audio file is empty."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reading audio file: {e}")
        raise HTTPException(status_code=400, detail="Failed to read audio file")

    # Call OpenAI Whisper API
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Prepare multipart form data
            files = {
                "file": (filename or "audio.webm", audio_content, content_type or "audio/webm"),
                "model": (None, "whisper-1"),
            }

            if language:
                files["language"] = (None, language)

            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                files=files,
                headers={"Authorization": f"Bearer {api_key}"}
            )

            if response.status_code == 401:
                raise HTTPException(
                    status_code=400,
                    detail="OpenAI API key is invalid or expired. Please update it in Settings > Integrations."
                )

            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="Rate limit exceeded. Please wait a moment and try again."
                )

            elif response.status_code != 200:
                error_text = response.text
                logger.error(f"Whisper API error: {response.status_code} - {error_text}")
                try:
                    error_data = response.json()
                    error_msg = error_data.get("error", {}).get("message", error_text)
                except Exception:
                    error_msg = error_text

                raise HTTPException(
                    status_code=500,
                    detail=f"Transcription failed: {error_msg}"
                )

            result = response.json()

            return VoiceToTextResponse(
                success=True,
                text=result.get("text", ""),
                language=result.get("language"),
                duration=result.get("duration")
            )

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Transcription timed out. Please try with a shorter audio file."
        )
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Transcription failed: {str(e)}"
        )
