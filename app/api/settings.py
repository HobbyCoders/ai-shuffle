"""
Settings API routes - integration settings, API keys, and configuration.

This module re-exports symbols from sub-modules for backward compatibility:
- settings_crypto: API key encryption/decryption/masking
- settings_audio: TTS/STT endpoints and model data
- settings_media: Media model selection and generation settings
- settings_credentials: Credential policy management
"""

import logging
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import database
from app.db.async_database import run_sync
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
    # Anthropic API key — a Claude Code AUTHENTICATION credential. When set, it
    # is injected as ANTHROPIC_API_KEY into every Claude Code subprocess and
    # overrides the connected Claude account (API-key login). When unset, Claude
    # Code uses the connected account / OAuth credentials.
    anthropic_api_key_set: bool = False
    anthropic_api_key_masked: str = ""
    # Which auth method Claude Code is currently using:
    #   "api_key"  — an anthropic_api_key is set (overrides the account)
    #   "account"  — no key set; the connected Claude OAuth account is used
    # Lets the UI surface the active method without a silent billing switch.
    claude_code_auth_method: str = "account"
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
    audio_model: Optional[str] = None
    audio_to_audio_model: Optional[str] = None
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


class AnthropicKeyRequest(BaseModel):
    """Request to set Anthropic API key"""
    api_key: str


class AnthropicKeyResponse(BaseModel):
    """Response after setting Anthropic API key"""
    success: bool
    masked_key: str


@router.get("/integrations", response_model=IntegrationSettingsResponse)
async def get_integration_settings(token: str = Depends(require_auth)):
    """
    Get current integration settings status.

    Returns whether API keys are configured (without exposing the actual keys).
    """
    def _load() -> IntegrationSettingsResponse:
        """Both key reads (+ decrypt) and all 19 setting reads in ONE hop.

        This endpoint made 21 blocking round trips on the event loop; the
        whole body is straight-line synchronous work, so it collapses into a
        single closure with the query order untouched.
        """
        fal_key = get_decrypted_api_key("fal_api_key")
        anthropic_key = get_decrypted_api_key("anthropic_api_key")
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
        audio_model = database.get_system_setting("audio_model")
        audio_to_audio_model = database.get_system_setting("audio_to_audio_model")
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
            anthropic_api_key_set=bool(anthropic_key),
            anthropic_api_key_masked=mask_api_key(anthropic_key) if anthropic_key else "",
            # Key set → Claude Code authenticates with the API key (overrides the
            # connected account). Key absent → connected OAuth account is used.
            claude_code_auth_method="api_key" if anthropic_key else "account",
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
            audio_model=audio_model,
            audio_to_audio_model=audio_to_audio_model,
            image_aspect_ratio=image_aspect_ratio,
            image_resolution=image_resolution,
            video_aspect_ratio=video_aspect_ratio,
            video_duration=video_duration,
            video_resolution=video_resolution,
            stt_language=stt_language,
        )

    return await run_sync(_load)


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

    # Save to database (encrypted if encryption is ready). One hop: the fal.ai
    # validation above is a real network await, so this cannot share a closure
    # with anything before it.
    await run_sync(set_encrypted_api_key, "fal_api_key", api_key)

    return FalAIKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/integrations/fal-ai")
async def remove_fal_api_key(token: str = Depends(require_admin)):
    """
    Remove the fal.ai API key (admin only).
    """
    await run_sync(database.delete_system_setting, "fal_api_key")
    return {"success": True}


@router.post("/integrations/anthropic", response_model=AnthropicKeyResponse)
async def set_anthropic_api_key(
    request: AnthropicKeyRequest,
    token: str = Depends(require_admin)
):
    """
    Set the Anthropic API key (admin only).

    This key is a Claude Code AUTHENTICATION credential. When set, it is
    injected as ANTHROPIC_API_KEY into every Claude Code subprocess and
    OVERRIDES the connected Claude account (API-key login wins, matching
    claude.exe's native precedence). When unset, Claude Code uses the connected
    account / OAuth credentials. Validated here before storage.

    Get your API key from: https://console.anthropic.com/settings/keys
    """
    from app.core.anthropic_generation import (
        validate_api_key,
        AnthropicAuthError,
        AnthropicRateLimitError,
        AnthropicNetworkError,
        AnthropicApiError,
    )

    api_key = request.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    # Validate the key by making a 1-token messages.create call. Cheapest
    # possible request to confirm auth works before committing the key to
    # storage. Auth errors are rejected; network or server errors save the
    # key anyway (transient issues shouldn't block setup).
    try:
        await validate_api_key(api_key)
    except AnthropicAuthError:
        raise HTTPException(
            status_code=400,
            detail="Invalid API key. Anthropic rejected authentication."
        )
    except AnthropicRateLimitError:
        raise HTTPException(
            status_code=429,
            detail="Anthropic rate-limited the validation call. Try again in a moment."
        )
    except AnthropicNetworkError as e:
        logger.warning(f"Anthropic validation network error, saving key anyway: {e}")
    except AnthropicApiError as e:
        # Malformed / unexpected response — surface so the user can investigate
        raise HTTPException(status_code=400, detail=f"Anthropic API error: {e}")

    # Save to database (encrypted if encryption is ready). One hop: the
    # Anthropic validation above is a real network await.
    await run_sync(set_encrypted_api_key, "anthropic_api_key", api_key)

    return AnthropicKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/integrations/anthropic")
async def remove_anthropic_api_key(token: str = Depends(require_admin)):
    """
    Remove the Anthropic API key (admin only).

    Reverts Claude Code authentication to the connected Claude account /
    OAuth credentials. No effect on the OAuth login flow itself.
    """
    await run_sync(database.delete_system_setting, "anthropic_api_key")
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
    fal_key = await run_sync(get_decrypted_api_key, "fal_api_key")
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
    custom_instructions = await run_sync(database.get_system_setting, "security_instructions")

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

    await run_sync(database.set_system_setting, "security_instructions", instructions)

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

    def _delete() -> None:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM system_settings WHERE key = %s", ("security_instructions",))

    await run_sync(_delete)

    return {
        "success": True,
        "message": "Security instructions reverted to default"
    }


# ============================================================================
# Voice mode (ruling VM13/VM17 — docs/VOICE_MODE_PLAN.md)
# ============================================================================
#
# Five runtime-editable knobs stored as plain ``system_settings`` values (they
# are NOT secrets — the OpenAI key itself lives encrypted on the model_providers
# row the voice session is funded by, never here). Stored/read through the same
# get_system_setting/set_system_setting helpers the security-instructions
# section above uses, so a change takes effect without a restart.

# The realtime models a voice session may run on. Anything outside this
# allowlist is rejected on write, so a typo can never reach OpenAI.
VOICE_MODELS = ("gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime")
DEFAULT_VOICE_MODEL = "gpt-realtime-2.1"

# The voices the realtime models can speak with (ruling VM21). Same allowlist
# discipline as the models: rejected on write, defaulted on read, so an unknown
# name can never reach the mint. ``marin``/``cedar`` are the gpt-realtime
# generation's recommended pair; the rest are the classic realtime set.
VOICE_VOICES = (
    "marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage",
    "shimmer", "verse",
)
DEFAULT_VOICE_VOICE = "marin"
# Total wall-clock budget for one call. OpenAI's own 60-minute session cap is
# bridged by reconnect (VM15); this is the separate "don't leave a call running
# all week" ceiling.
DEFAULT_VOICE_MAX_CALL_MINUTES = 240

VOICE_MODE_ENABLED_KEY = "voice_mode_enabled"
VOICE_MODEL_KEY = "voice_model"
VOICE_VOICE_KEY = "voice_voice"
VOICE_PROVIDER_ID_KEY = "voice_provider_id"
VOICE_MAX_CALL_MINUTES_KEY = "voice_max_call_minutes"

# The voice agent's editable prompts. Empty/unset = use the built-in default
# (the constants in app.core.voice_bridge). ``voice_instructions`` is the base
# system prompt every voice call gets (web included); the command/chat variants
# are appendices applied when a native client reports its ``session_mode`` on
# call attach (the Android assistant's hands-free vs conversation modes).
VOICE_INSTRUCTIONS_KEY = "voice_instructions"
VOICE_INSTRUCTIONS_COMMAND_KEY = "voice_instructions_command"
VOICE_INSTRUCTIONS_CHAT_KEY = "voice_instructions_chat"
MAX_VOICE_INSTRUCTIONS_CHARS = 8000


def _voice_instruction_defaults() -> dict:
    """The built-in prompt texts (lazy import — voice_bridge is heavy)."""
    from app.core import voice_bridge

    return {
        "base": voice_bridge.VOICE_INSTRUCTIONS,
        "command": voice_bridge.DEFAULT_COMMAND_INSTRUCTIONS,
        "chat": voice_bridge.DEFAULT_CHAT_INSTRUCTIONS,
    }


class VoiceSettingsResponse(BaseModel):
    """Current voice-mode configuration.

    The ``voice_instructions*`` fields are the STORED overrides — empty string
    means "using the built-in default", whose text is provided alongside in
    ``voice_instructions_defaults`` so a UI can show and reset to it.
    """
    voice_mode_enabled: bool
    voice_model: str
    voice_voice: str
    voice_provider_id: Optional[str] = None
    voice_max_call_minutes: int
    voice_instructions: str = ""
    voice_instructions_command: str = ""
    voice_instructions_chat: str = ""
    voice_instructions_defaults: Dict[str, str] = {}


class VoiceSettingsUpdateRequest(BaseModel):
    """Partial update — only the keys present in the body are written.

    ``voice_provider_id: null`` explicitly CLEARS the pinned provider (the
    resolver then falls back to "the single eligible row"), which is why the
    handler keys off ``model_fields_set`` rather than treating None as absent.
    An empty string for any ``voice_instructions*`` field clears the override
    (back to the built-in default).
    """
    voice_mode_enabled: Optional[bool] = None
    voice_model: Optional[str] = None
    voice_voice: Optional[str] = None
    voice_provider_id: Optional[str] = None
    voice_max_call_minutes: Optional[int] = None
    voice_instructions: Optional[str] = None
    voice_instructions_command: Optional[str] = None
    voice_instructions_chat: Optional[str] = None


def _truthy_setting(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "on", "yes")


def get_voice_settings() -> dict:
    """Read the voice knobs, applying defaults for unset/invalid values.

    Plain function (no auth, no request) so the voice router can call it
    directly; the endpoint below is a thin wrapper over it.
    """
    raw_minutes = database.get_system_setting(VOICE_MAX_CALL_MINUTES_KEY)
    try:
        max_minutes = int(raw_minutes) if raw_minutes else DEFAULT_VOICE_MAX_CALL_MINUTES
    except (TypeError, ValueError):
        max_minutes = DEFAULT_VOICE_MAX_CALL_MINUTES
    if max_minutes <= 0:
        max_minutes = DEFAULT_VOICE_MAX_CALL_MINUTES

    model = (database.get_system_setting(VOICE_MODEL_KEY) or "").strip()
    if model not in VOICE_MODELS:
        model = DEFAULT_VOICE_MODEL

    voice = (database.get_system_setting(VOICE_VOICE_KEY) or "").strip()
    if voice not in VOICE_VOICES:
        voice = DEFAULT_VOICE_VOICE

    provider_id = (database.get_system_setting(VOICE_PROVIDER_ID_KEY) or "").strip() or None

    return {
        "voice_mode_enabled": _truthy_setting(database.get_system_setting(VOICE_MODE_ENABLED_KEY)),
        "voice_model": model,
        "voice_voice": voice,
        "voice_provider_id": provider_id,
        "voice_max_call_minutes": max_minutes,
        "voice_instructions":
            (database.get_system_setting(VOICE_INSTRUCTIONS_KEY) or "").strip(),
        "voice_instructions_command":
            (database.get_system_setting(VOICE_INSTRUCTIONS_COMMAND_KEY) or "").strip(),
        "voice_instructions_chat":
            (database.get_system_setting(VOICE_INSTRUCTIONS_CHAT_KEY) or "").strip(),
        "voice_instructions_defaults": _voice_instruction_defaults(),
    }


@router.get("/voice", response_model=VoiceSettingsResponse)
async def get_voice_settings_endpoint(token: str = Depends(require_auth)):
    """Get the current voice-mode settings (defaults applied)."""
    # get_voice_settings() is eight synchronous reads — one hop for all of them.
    return VoiceSettingsResponse(**await run_sync(get_voice_settings))


@router.put("/voice", response_model=VoiceSettingsResponse)
async def update_voice_settings(
    request: VoiceSettingsUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update voice-mode settings. Only the supplied keys are written.

    Validation: ``voice_model`` must be in the allowlist, ``voice_max_call_minutes``
    must be positive, and a non-empty ``voice_provider_id`` must name a provider
    row that is eligible to fund realtime calls (an enabled ``openai`` API-key
    row — never a ChatGPT/Codex subscription row, which cannot do Realtime).

    Admin only.
    """
    def _apply() -> dict:
        """Every validation, write and the closing re-read in ONE hop.

        Read-decide-write: the provider check reads the eligible-provider
        list and that result gates the write, so the two must not be split
        across awaits. The trailing ``get_voice_settings()`` read joins the
        same hop so the response reflects the writes made just above it.
        HTTPExceptions raised in here propagate unchanged, and the order of
        writes vs. a later 400 is exactly as before.
        """
        provided = request.model_fields_set

        if "voice_model" in provided:
            model = (request.voice_model or "").strip()
            if model not in VOICE_MODELS:
                raise HTTPException(
                    status_code=400,
                    detail=f"voice_model must be one of {list(VOICE_MODELS)}",
                )
            database.set_system_setting(VOICE_MODEL_KEY, model)

        if "voice_voice" in provided:
            voice = (request.voice_voice or "").strip()
            if voice not in VOICE_VOICES:
                raise HTTPException(
                    status_code=400,
                    detail=f"voice_voice must be one of {list(VOICE_VOICES)}",
                )
            database.set_system_setting(VOICE_VOICE_KEY, voice)

        if "voice_max_call_minutes" in provided:
            minutes = request.voice_max_call_minutes
            if minutes is None or minutes <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="voice_max_call_minutes must be a positive integer",
                )
            database.set_system_setting(VOICE_MAX_CALL_MINUTES_KEY, str(minutes))

        if "voice_mode_enabled" in provided:
            database.set_system_setting(
                VOICE_MODE_ENABLED_KEY, "true" if request.voice_mode_enabled else "false"
            )

        for field, key in (
            ("voice_instructions", VOICE_INSTRUCTIONS_KEY),
            ("voice_instructions_command", VOICE_INSTRUCTIONS_COMMAND_KEY),
            ("voice_instructions_chat", VOICE_INSTRUCTIONS_CHAT_KEY),
        ):
            if field in provided:
                text = (getattr(request, field) or "").strip()
                if len(text) > MAX_VOICE_INSTRUCTIONS_CHARS:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"{field} is limited to "
                            f"{MAX_VOICE_INSTRUCTIONS_CHARS} characters."
                        ),
                    )
                # Empty string clears the override → back to the built-in default.
                database.set_system_setting(key, text)

        if "voice_provider_id" in provided:
            # Imported lazily: app.api.voice imports this module for the reader
            # above, so a module-level import here would be circular.
            from app.api.voice import list_eligible_voice_providers

            provider_id = (request.voice_provider_id or "").strip()
            if not provider_id:
                database.delete_system_setting(VOICE_PROVIDER_ID_KEY)
            else:
                eligible_ids = {p["id"] for p in list_eligible_voice_providers()}
                if provider_id not in eligible_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Provider {provider_id!r} cannot fund voice calls. Pick an "
                            "enabled OpenAI provider that stores an API key (Realtime "
                            "does not support ChatGPT-subscription logins)."
                        ),
                    )
                database.set_system_setting(VOICE_PROVIDER_ID_KEY, provider_id)

        return get_voice_settings()

    return VoiceSettingsResponse(**await run_sync(_apply))


# ============================================================================
# Text generation (internal one-shot AI calls)
# ============================================================================

class TextGenerationSettingsResponse(BaseModel):
    """The model + effort every internal generation call runs on.

    ``model`` is the raw picker value — a bare id/alias, or a
    ``prov:<providerId>:<modelId>`` composite. Empty means "not configured",
    which resolves to the registry default at call time. ``effort`` is empty
    for the provider's own default.
    """
    text_generation_model: str = ""
    text_generation_effort: str = ""


class TextGenerationSettingsUpdateRequest(BaseModel):
    """Partial update — only the keys present in the body are written."""
    text_generation_model: Optional[str] = None
    text_generation_effort: Optional[str] = None


def get_text_generation_settings() -> Dict[str, str]:
    """Read the stored text-generation binding (empty strings when unset)."""
    from app.core.generation_options import (
        TEXT_GENERATION_EFFORT_KEY,
        TEXT_GENERATION_MODEL_KEY,
    )

    return {
        "text_generation_model":
            (database.get_system_setting(TEXT_GENERATION_MODEL_KEY) or "").strip(),
        "text_generation_effort":
            (database.get_system_setting(TEXT_GENERATION_EFFORT_KEY) or "").strip(),
    }


@router.get("/text-generation", response_model=TextGenerationSettingsResponse)
async def get_text_generation_settings_endpoint(token: str = Depends(require_auth)):
    """Get the model and effort used for internal text generation."""
    return TextGenerationSettingsResponse(**await run_sync(get_text_generation_settings))


@router.put("/text-generation", response_model=TextGenerationSettingsResponse)
async def update_text_generation_settings(
    request: TextGenerationSettingsUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the text-generation model / effort. Admin only.

    A ``prov:<providerId>:<modelId>`` model is validated against the provider
    row so a gateway model that the provider does not serve is refused here
    rather than failing every commit-message generation afterwards. An empty
    string on either key clears it back to the default.
    """
    def _apply() -> dict:
        """Validation, writes and the closing re-read in ONE hop.

        Read-decide-write: ``validate_provider_model`` reads the provider
        row and its verdict gates the write.
        """
        from app.core.generation_options import (
            TEXT_GENERATION_EFFORT_KEY,
            TEXT_GENERATION_MODEL_KEY,
            VALID_EFFORTS,
        )

        provided = request.model_fields_set

        if "text_generation_model" in provided:
            model = (request.text_generation_model or "").strip()
            if not model:
                database.delete_system_setting(TEXT_GENERATION_MODEL_KEY)
            else:
                from app.core.query_options import decode_provider_model

                decoded = decode_provider_model(model)
                if decoded:
                    from app.core.provider_agent import validate_provider_model

                    validation = validate_provider_model(
                        decoded[0], decoded[1], require_servable=False
                    )
                    if not validation.ok:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                validation.reason
                                or f"Model {decoded[1]!r} is not available on that provider."
                            ),
                        )
                database.set_system_setting(TEXT_GENERATION_MODEL_KEY, model)

        if "text_generation_effort" in provided:
            effort = (request.text_generation_effort or "").strip()
            if not effort:
                database.delete_system_setting(TEXT_GENERATION_EFFORT_KEY)
            elif effort not in VALID_EFFORTS:
                raise HTTPException(
                    status_code=400,
                    detail=f"text_generation_effort must be one of {list(VALID_EFFORTS)}",
                )
            else:
                database.set_system_setting(TEXT_GENERATION_EFFORT_KEY, effort)

        return get_text_generation_settings()

    return TextGenerationSettingsResponse(**await run_sync(_apply))
