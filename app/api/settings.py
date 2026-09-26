"""
Settings API routes - integration settings, API keys, and configuration.

Includes the sub-routers from settings_audio, stt_realtime, settings_media,
settings_credentials and settings_updates.
"""

import logging
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_admin, require_auth
from app.api.settings_updates import updates_router
from app.core import encryption
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
)
from app.api.stt_realtime import stt_stream_router  # noqa: F401
from app.api.settings_media import (  # noqa: F401
    media_router,
)
from app.api.settings_credentials import credentials_router  # noqa: F401

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])

# Include sub-routers (they share the same prefix via this parent router)
router.include_router(audio_router)
router.include_router(stt_stream_router)
router.include_router(media_router)
router.include_router(credentials_router)
router.include_router(updates_router)


# ============================================================================
# Integration Settings
# ============================================================================

class IntegrationSettingsResponse(BaseModel):
    """Response containing integration configuration status"""
    fal_api_key_set: bool
    fal_billing_api_key_set: bool = False
    fal_api_key_masked: str
    # Anthropic API key — a Claude Code AUTHENTICATION credential. When set, it
    # is injected as ANTHROPIC_API_KEY into every Claude Code subprocess and
    # overrides the connected Claude account (API-key login). When unset, Claude
    # Code uses the connected account / OAuth credentials.
    anthropic_api_key_set: bool = False
    anthropic_api_key_masked: str = ""
    # Which credential Claude Code runs on, from the explicit auth mode
    # (``claude_auth_mode``; see GET /settings/claude-auth):
    #   "api_key"  — api_key mode: the stored key is injected
    #   "account"  — subscription mode: the CLI's own sign-in is used
    # Kept for older clients; ``claude_auth_mode`` carries the mode itself.
    claude_code_auth_method: str = "account"
    claude_auth_mode: str = "subscription"
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
    """Response after setting fal.ai API key.

    ``valid`` is ``True`` when fal.ai accepted the key and ``None`` when it
    could not be checked (network error, timeout, rate limit or a fal.ai
    fault); the key is saved either way. A key fal.ai rejects is not saved
    (400). ``reason`` explains an unverified key.
    """
    success: bool
    masked_key: str
    valid: Optional[bool] = None
    reason: Optional[str] = None


# fal.ai's queue gateway checks the key before anything else, and a GET on a
# model's queue URL submits nothing, so it costs nothing to ask.
FAL_KEY_CHECK_URL = "https://queue.fal.run/fal-ai/fast-sdxl"
# Answers that mean the gateway accepted the key: the probe itself asks for
# nothing, so "not found" or "method not allowed" still prove authentication.
_FAL_KEY_ACCEPTED = {200, 201, 202, 204, 404, 405, 422}


def _require_credential_storage() -> None:
    """409 with the recovery guidance when credential storage is locked.

    Keys are only ever stored encrypted; a locked store used to surface as an
    unhandled 500 from ``encrypt_value``.
    """
    if not encryption.is_encryption_ready():
        raise HTTPException(status_code=409, detail=encryption.credential_storage_error())


def _store_api_key(setting_name: str, api_key: str) -> None:
    """Encrypt and store a key, or 409 when storage is (or becomes) locked.

    Synchronous: call via ``run_sync``.
    """
    _require_credential_storage()
    try:
        set_encrypted_api_key(setting_name, api_key)
    except RuntimeError:
        # Locked between the check and the write.
        if encryption.is_encryption_ready():
            raise
        raise HTTPException(status_code=409, detail=encryption.credential_storage_error()) from None


async def _check_fal_key(api_key: str) -> tuple[Optional[bool], Optional[str]]:
    """Ask fal.ai whether it accepts ``api_key``: ``(valid, reason)``.

    ``False`` only for an explicit rejection (401/403); anything that does not
    answer the question is ``None`` with a reason.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                FAL_KEY_CHECK_URL,
                headers={"Authorization": f"Key {api_key}"}
            )
    except httpx.TimeoutException:
        logger.warning("fal.ai API validation timed out, saving key anyway")
        return None, "fal.ai did not answer in time; the key was saved without verification."
    except Exception as e:
        logger.warning(f"Error validating fal.ai key: {type(e).__name__}")
        return None, "fal.ai could not be reached; the key was saved without verification."

    if response.status_code in (401, 403):
        return False, "fal.ai rejected the key."
    if response.status_code in _FAL_KEY_ACCEPTED:
        return True, None
    if response.status_code == 429:
        return None, "fal.ai rate-limited the check; the key was saved without verification."
    return None, f"fal.ai answered the check with HTTP {response.status_code}; the key was saved without verification."


@router.post("/integrations/fal-billing")
async def set_fal_billing_key(request: FalAIKeyRequest, token: str = Depends(require_admin)):
    key = request.api_key.strip()
    if not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise HTTPException(400, "Enter an Admin API key without whitespace")
    await run_sync(_store_api_key, "fal_billing_api_key", key)
    from app.core.provider_usage import FAL_BILLING_URL
    from app.core.usage_polling import UsageDeferred
    from app.network.usage_budget import resume_authentication

    try:
        await resume_authentication(FAL_BILLING_URL, {"Authorization": f"Key {key}"})
    except UsageDeferred:
        raise HTTPException(503, "Key saved. Reconnect the network founder and save again to resume usage polling.") from None
    return {"success": True}


@router.delete("/integrations/fal-billing")
async def remove_fal_billing_key(token: str = Depends(require_admin)):
    await run_sync(database.delete_system_setting, "fal_billing_api_key")
    return {"success": True}


class AnthropicKeyRequest(BaseModel):
    """Request to set Anthropic API key"""
    api_key: str
    # True also switches Claude Code to API-key mode in the same call. Saving a
    # key never changes the mode by itself.
    use_for_claude_code: bool = False


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
        """Both key reads (+ decrypt) and every setting read in ONE hop.

        The whole body is straight-line synchronous work, so it runs as a
        single off-loop closure with the query order untouched.
        """
        fal_key = get_decrypted_api_key("fal_api_key")
        anthropic_key = get_decrypted_api_key("anthropic_api_key")
        from app.core import claude_auth_mode
        auth_mode = claude_auth_mode.current_mode()
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
            fal_billing_api_key_set=bool(database.get_system_setting("fal_billing_api_key")),
            fal_api_key_masked=mask_api_key(fal_key) if fal_key else "",
            anthropic_api_key_set=bool(anthropic_key),
            anthropic_api_key_masked=mask_api_key(anthropic_key) if anthropic_key else "",
            # The explicit mode decides, never the key's mere presence.
            claude_code_auth_method="api_key" if auth_mode == "api_key" else "account",
            claude_auth_mode=auth_mode,
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
    # Before the network check: a locked store cannot save the key anyway.
    _require_credential_storage()

    valid, reason = await _check_fal_key(api_key)
    if valid is False:
        raise HTTPException(status_code=400, detail="Invalid API key. Authentication failed.")

    # Save to database, always encrypted. One hop: the fal.ai validation above
    # is a real network await, so this cannot share a closure with it.
    await run_sync(_store_api_key, "fal_api_key", api_key)

    return FalAIKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key),
        valid=valid,
        reason=reason,
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

    The key is a Claude Code credential used only in ``api_key`` auth mode,
    where it is injected as ANTHROPIC_API_KEY into the CLI processes this
    worker starts. Saving it does not change the mode unless
    ``use_for_claude_code`` is true. It is stored encrypted on this worker
    only. Validated here before storage.

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
    # Before the paid validation call: a locked store cannot save the key.
    _require_credential_storage()

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

    # Save to database, always encrypted. One hop: the Anthropic validation
    # above is a real network await.
    await run_sync(_store_api_key, "anthropic_api_key", api_key)
    if request.use_for_claude_code:
        from app.core import claude_auth_mode
        await run_sync(claude_auth_mode.set_mode, claude_auth_mode.MODE_API_KEY)

    return AnthropicKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/integrations/anthropic")
async def remove_anthropic_api_key(token: str = Depends(require_admin)):
    """
    Remove the Anthropic API key (admin only).

    The auth mode is left as it is: in ``api_key`` mode new agent runs then
    wait for a key (or for the user to choose subscription mode) instead of
    falling back to the CLI's sign-in.
    """
    await run_sync(database.delete_system_setting, "anthropic_api_key")
    return {"success": True}


# ============================================================================
# Claude Code auth mode (subscription or API key)
# ============================================================================

class ClaudeAuthModeRequest(BaseModel):
    mode: str = Field(..., description="'subscription' or 'api_key'")


async def claude_auth_payload(refresh: bool = False) -> dict:
    """The mode, the CLI account from the probe, the key's last four, and the
    credential gate (whether new agent runs can start, and why not)."""
    from app.core import claude_auth_mode, claude_cli_status, credential_gate

    status = await claude_cli_status.get_status_async(refresh)
    cli = status.to_dict()

    def _load() -> dict:
        return {**claude_auth_mode.summary(cli), "gate": credential_gate.status_payload(cli)}

    return await run_sync(_load)


@router.get("/claude-auth")
async def get_claude_auth(refresh: bool = False, token: str = Depends(require_auth)):
    """Which credential Claude Code runs on.

    ``mode`` is ``subscription`` or ``api_key``; ``explicit`` is false only
    before startup has recorded the default. ``cli`` is what `claude auth
    status` reports (account shown only when signed in); ``api_key`` says
    whether a key is stored and its last four characters; ``gate`` is the
    credential gate (``ready``, ``reason``, ``license_allows_runs``...).
    """
    return await claude_auth_payload(refresh)


@router.put("/claude-auth")
async def set_claude_auth(request: ClaudeAuthModeRequest, token: str = Depends(require_admin)):
    """Choose the mode (admin only). Takes effect for the next agent run."""
    from app.core import claude_auth_mode

    try:
        await run_sync(claude_auth_mode.set_mode, request.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await claude_auth_payload()


# ============================================================================
# Subscription loop guard
# ============================================================================

class LoopGuardUpdate(BaseModel):
    enabled: Optional[bool] = None
    session_turns_per_hour: Optional[int] = None
    install_turns_per_hour: Optional[int] = None
    worker_max_minutes: Optional[int] = None


@router.get("/loop-guard")
async def get_loop_guard(token: str = Depends(require_auth)):
    """The subscription loop guard: settings, defaults, whether it applies
    (enabled and Claude Code in subscription mode), this hour's unattended
    turn count, and the turns it holds for confirmation."""
    from app.core import loop_guard

    return await run_sync(loop_guard.status_payload)


@router.put("/loop-guard")
async def set_loop_guard(request: LoopGuardUpdate, token: str = Depends(require_admin)):
    """Change the loop guard settings (admin only)."""
    from app.core import loop_guard

    try:
        await run_sync(lambda: loop_guard.update_config(**request.model_dump()))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await run_sync(loop_guard.status_payload)


@router.post("/loop-guard/held/{held_id}/confirm")
async def confirm_loop_guard_turn(held_id: str, token: str = Depends(require_admin)):
    """Run a held turn now, past the budget (admin only)."""
    from app.core import loop_guard

    turn_id = await loop_guard.confirm_held(held_id)
    if turn_id is None:
        raise HTTPException(status_code=404, detail="That held turn is gone")
    return {"success": True, "turn_id": turn_id}


@router.post("/loop-guard/held/{held_id}/dismiss")
async def dismiss_loop_guard_turn(held_id: str, token: str = Depends(require_admin)):
    """Drop a held turn without running it (admin only)."""
    from app.core import loop_guard

    if await run_sync(loop_guard.take_held, held_id) is None:
        raise HTTPException(status_code=404, detail="That held turn is gone")
    return {"success": True}


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
# Voice mode
# ============================================================================
#
# Runtime-editable knobs stored as plain ``system_settings`` values (they
# are NOT secrets — the OpenAI key itself lives encrypted on the model_providers
# row the voice session is funded by, never here). Stored/read through the same
# get_system_setting/set_system_setting helpers the security-instructions
# section above uses, so a change takes effect without a restart.

# The realtime models a voice session may run on. Anything outside this
# allowlist is rejected on write, so a typo can never reach OpenAI.
from app.core.voice_models import (
    FAL_PROVIDER_ID, voices_for,
)
DEFAULT_VOICE_MODEL = "gpt-live-1"

# Voice names are validated per model by ``voices_for(model)``.
DEFAULT_VOICE_VOICE = "marin"
# Total wall-clock budget for one call. OpenAI's own 60-minute session cap is
# bridged by reconnect; this is the separate "don't leave a call running
# all week" ceiling.
DEFAULT_VOICE_MAX_CALL_MINUTES = 240

VOICE_MODE_ENABLED_KEY = "voice_mode_enabled"
VOICE_MODEL_KEY = "voice_model"
VOICE_VOICE_KEY = "voice_voice"
VOICE_PROVIDER_ID_KEY = "voice_provider_id"
VOICE_MAX_CALL_MINUTES_KEY = "voice_max_call_minutes"


class VoiceSettingsResponse(BaseModel):
    """Current voice-mode configuration."""
    voice_mode_enabled: bool
    voice_model: str
    voice_voice: str
    voice_provider_id: Optional[str] = None
    voice_max_call_minutes: int
    voice_input_model: str = "fal-ai/whisper"
    voice_input_models: list[dict] = Field(default_factory=list)
    voice_input_mode: str = "auto"
    voice_input_silence_seconds: int = 5
    # Compatibility field for older clients; app tools are no longer a preference.
    voice_app_tools_enabled: bool = True
    model_details: dict = Field(default_factory=dict)
    voice_options: dict = Field(default_factory=dict)


class VoiceSettingsUpdateRequest(BaseModel):
    """Partial update — only the keys present in the body are written.

    ``voice_provider_id: null`` explicitly CLEARS the pinned provider (the
    resolver then falls back to "the single eligible row"), which is why the
    handler keys off ``model_fields_set`` rather than treating None as absent.
    """
    voice_mode_enabled: Optional[bool] = None
    voice_model: Optional[str] = None
    voice_voice: Optional[str] = None
    voice_provider_id: Optional[str] = None
    voice_max_call_minutes: Optional[int] = None
    voice_input_model: Optional[str] = None
    voice_input_mode: Optional[str] = None
    voice_input_silence_seconds: Optional[int] = Field(default=None, strict=True, ge=1, le=60)
    voice_options: Optional[dict] = None
    voice_options_model: Optional[str] = None
    # Deprecated compatibility input: accepted but ignored, including False/null.
    voice_app_tools_enabled: Optional[bool] = None


def _truthy_setting(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "on", "yes")


def get_voice_options(model: str) -> dict:
    import json
    from app.core.voice_options import validate_options
    retired = ("delegation_type", "delegation_model", "delegation_effort")
    try:
        value = json.loads(database.get_system_setting(f"voice_options:{model}") or "{}")
        if not isinstance(value, dict):
            raise ValueError("Voice options must be an object")
        if model == "gpt-live-1":
            # Retire settings-level overrides without discarding saved prompts.
            for key in retired:
                value.pop(key, None)
        options = validate_options(model, value)
    except (ValueError, TypeError):
        logger.warning("Invalid saved options for voice model %s; using defaults", model)
        options = validate_options(model, {})
    if model == "gpt-live-1":
        for key in retired:
            options.pop(key, None)
    return options


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

    from app.core.voice_input_settings import get_voice_input_model

    input_mode = database.get_system_setting("voice_input_mode")
    if input_mode not in ("auto", "toggle"):
        input_mode = "auto"
    try:
        silence_seconds = int(database.get_system_setting("voice_input_silence_seconds") or "5")
    except (TypeError, ValueError):
        silence_seconds = 5
    if not 1 <= silence_seconds <= 60:
        silence_seconds = 5

    # New calls are always GPT Live. Pinned session bindings remain readable.
    model = DEFAULT_VOICE_MODEL

    voice = (database.get_system_setting(VOICE_VOICE_KEY) or "").strip()
    if voice not in voices_for(model):
        voice = DEFAULT_VOICE_VOICE

    provider_id = (database.get_system_setting(VOICE_PROVIDER_ID_KEY) or "").strip() or None
    if provider_id == FAL_PROVIDER_ID:
        provider_id = None

    return {
        "voice_mode_enabled": _truthy_setting(database.get_system_setting(VOICE_MODE_ENABLED_KEY)),
        "voice_model": model,
        "voice_voice": voice,
        "voice_provider_id": provider_id,
        "voice_max_call_minutes": max_minutes,
        "voice_input_model": get_voice_input_model(),
        "voice_input_mode": input_mode,
        "voice_input_silence_seconds": silence_seconds,
        "voice_app_tools_enabled": True,
        "voice_options": get_voice_options(model),
    }


def _voice_settings_response(value: dict) -> VoiceSettingsResponse:
    from app.core.voice_models import model_details
    from app.api.settings_audio import _model_provider
    return VoiceSettingsResponse(
        **value,
        voice_input_models=[
            {"id": model_id, "name": info["name"], "provider": _model_provider(model_id, info)}
            for model_id, info in STT_MODELS.items()
        ],
        model_details=model_details(value["voice_model"], value.get("voice_options")),
    )


@router.get("/voice", response_model=VoiceSettingsResponse)
async def get_voice_settings_endpoint(token: str = Depends(require_auth)):
    """Get the current voice-mode settings (defaults applied)."""
    # Keep all synchronous settings reads in one worker hop.
    return _voice_settings_response(await run_sync(get_voice_settings))


@router.put("/voice", response_model=VoiceSettingsResponse)
async def update_voice_settings(
    request: VoiceSettingsUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update voice-mode settings. Only the supplied keys are written.

    Validation: ``voice_model`` must be in the allowlist, ``voice_max_call_minutes``
    must be positive, and a non-empty ``voice_provider_id`` must name a provider
    row that is eligible to fund voice calls, or the configured fal.ai voice
    provider. Provider, model, and voice must form a valid combination.

    Admin only.
    """
    def _apply() -> dict:
        """Validate the complete provider/model/voice combination before writing."""

        supplied = request.model_fields_set
        if "voice_input_model" in supplied:
            from app.api.settings_audio import STT_MODELS
            if request.voice_input_model not in STT_MODELS:
                raise HTTPException(400, "Choose a supported voice transcription model.")
        current = get_voice_settings()
        if "voice_input_mode" in supplied and request.voice_input_mode not in ("auto", "toggle"):
            raise HTTPException(400, "voice_input_mode must be auto or toggle")
        if "voice_input_silence_seconds" in supplied and request.voice_input_silence_seconds is None:
            raise HTTPException(400, "voice_input_silence_seconds must be an integer from 1 to 60")
        model = request.voice_model if "voice_model" in supplied else current["voice_model"]
        provider = request.voice_provider_id if "voice_provider_id" in supplied else current["voice_provider_id"]
        provider = (provider or "").strip() or None
        if model != DEFAULT_VOICE_MODEL:
            raise HTTPException(400, "Voice mode uses GPT Live 1.")
        if "voice_provider_id" in supplied and provider:
            raise HTTPException(400, "Voice providers are selected automatically from configured OpenAI API providers.")
        voice = request.voice_voice if "voice_voice" in supplied else current["voice_voice"]
        if voice not in voices_for(model):
            if "voice_voice" in supplied:
                raise HTTPException(400, f"voice_voice must be one of {list(voices_for(model))}")
            voice = DEFAULT_VOICE_VOICE
        minutes = request.voice_max_call_minutes if "voice_max_call_minutes" in supplied else current["voice_max_call_minutes"]
        if minutes is None or minutes <= 0:
            raise HTTPException(400, "voice_max_call_minutes must be a positive integer")

        options = get_voice_options(model)
        if "voice_options" in supplied:
            if request.voice_options_model is not None and request.voice_options_model != model:
                raise HTTPException(409, "The selected voice model changed. Reload settings before saving.")
            if request.voice_options is None:
                raise HTTPException(400, "voice_options must be an object; use an empty string to reset a prompt.")
            from app.core.voice_options import validate_options
            if request.voice_options.keys() & {"delegation_type", "delegation_model", "delegation_effort"}:
                raise HTTPException(400, "Choose the model and effort in the chat composer, not voice settings.")
            try:
                options = validate_options(model, {**options, **request.voice_options})
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, f"Invalid voice options: {exc}") from exc

        if supplied & {"voice_model", "voice_provider_id", "voice_voice"}:
            database.set_system_setting(VOICE_MODEL_KEY, model)
            database.set_system_setting(VOICE_VOICE_KEY, voice)
            if provider:
                database.set_system_setting(VOICE_PROVIDER_ID_KEY, provider)
            else:
                database.delete_system_setting(VOICE_PROVIDER_ID_KEY)
        if "voice_options" in supplied:
            import json
            for key in ("delegation_type", "delegation_model", "delegation_effort"):
                options.pop(key, None)
            database.set_system_setting(f"voice_options:{model}", json.dumps(options))
        if "voice_max_call_minutes" in supplied:
            database.set_system_setting(VOICE_MAX_CALL_MINUTES_KEY, str(minutes))
        if "voice_mode_enabled" in supplied:
            database.set_system_setting(VOICE_MODE_ENABLED_KEY, "true" if request.voice_mode_enabled else "false")
        for key in ("voice_input_model", "voice_input_mode", "voice_input_silence_seconds"):
            if key in supplied:
                database.set_system_setting(key, str(getattr(request, key)))
        return get_voice_settings()

    return _voice_settings_response(await run_sync(_apply))


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
    row so a model that the provider does not serve is refused here
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
