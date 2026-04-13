"""
Settings API routes - integration settings, API keys, and configuration.

This module re-exports symbols from sub-modules for backward compatibility:
- settings_crypto: API key encryption/decryption/masking
- settings_audio: TTS/STT endpoints and model data
- settings_media: Media model selection and generation settings
- settings_credentials: Credential policy management
"""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import database
from app.api.auth import require_admin, require_auth
from app.core.ai_tools import AI_TOOLS
from app.core.query_engine import DEFAULT_SECURITY_INSTRUCTIONS

# Import from sub-modules
from app.api.settings_crypto import (  # noqa: F401
    get_decrypted_api_key,
    set_encrypted_api_key,
    mask_api_key,
)
from app.api.settings_audio import (  # noqa: F401
    audio_router,
    TTS_MODELS,
    STT_MODELS,
    AudioModelUpdateRequest,
    TTSSettingsRequest,
    TranscriptionResponse,
    TTSGenerateRequest,
    TTSGenerateResponse,
    get_generated_audio_dir,
    _build_tts_payload,
    _extract_audio_url,
)
from app.api.settings_media import (  # noqa: F401
    media_router,
    ModelUpdateRequest,
    ImageSettingsRequest,
    VideoSettingsRequest,
    STTSettingsRequest,
    _update_model_setting,
)
from app.api.settings_credentials import (  # noqa: F401
    credentials_router,
    CredentialPolicyResponse,
    UpdateCredentialPolicyRequest,
    AllCredentialPoliciesResponse,
    UserCredentialPolicyResponse,
    SetUserCredentialPolicyRequest,
    CREDENTIAL_POLICY_INFO,
    _compute_effective_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

# Include sub-routers (they share the same prefix via this parent router)
router.include_router(audio_router)
router.include_router(media_router)
router.include_router(credentials_router)


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
