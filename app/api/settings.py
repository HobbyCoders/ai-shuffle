"""
Settings API routes - integration settings, API keys, and configuration
"""

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


class OpenAIKeyRequest(BaseModel):
    """Request to set OpenAI API key"""
    api_key: str


class OpenAIKeyResponse(BaseModel):
    """Response after setting OpenAI API key"""
    success: bool
    masked_key: str


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

    return IntegrationSettingsResponse(
        openai_api_key_set=bool(openai_key),
        openai_api_key_masked=mask_api_key(openai_key) if openai_key else ""
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

    # Call OpenAI Whisper API
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Prepare the multipart form data
            files = {
                "file": (filename, BytesIO(audio_data), content_type or "audio/webm"),
                "model": (None, "whisper-1"),
                "response_format": (None, "json"),
            }

            response = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {openai_key}"},
                files=files
            )

            if response.status_code == 401:
                raise HTTPException(
                    status_code=400,
                    detail="OpenAI API key is invalid. Please update it in Settings > Integrations."
                )
            elif response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail="OpenAI rate limit exceeded. Please try again in a moment."
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
