"""
Settings API routes - media model selection and generation settings endpoints.
"""

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.db import database
from app.db import async_database as adb
from app.db.async_database import run_sync
from app.api.auth import require_admin

logger = logging.getLogger(__name__)

media_router = APIRouter(tags=["Settings"])

# ============================================================================
# Pydantic Models
# ============================================================================


class ModelUpdateRequest(BaseModel):
    """Request to update a model selection"""
    model: str


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


# ============================================================================
# Helper
# ============================================================================


async def _update_model_setting(setting_name: str, body: ModelUpdateRequest):
    """Shared helper: validate and persist a model selection setting."""
    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="Model cannot be empty")
    # One isolated write — the async twin is a single thread hop.
    await adb.set_system_setting(setting_name, model)
    return {"success": True, "model": model}


# ============================================================================
# Model Selection Endpoints
# ============================================================================


@media_router.patch("/integrations/image/model")
async def update_image_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image generation model (admin only). Accepts any fal.ai model ID."""
    return await _update_model_setting("image_model", request)


@media_router.patch("/integrations/image/edit-model")
async def update_image_edit_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image editing model (admin only). Accepts any fal.ai model ID."""
    return await _update_model_setting("image_edit_model", request)


@media_router.patch("/integrations/video/model")
async def update_video_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the video generation model (admin only). Accepts any fal.ai model ID."""
    return await _update_model_setting("video_model", request)


@media_router.patch("/integrations/video/i2v-model")
async def update_i2v_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image-to-video model (admin only). Accepts any fal.ai model ID."""
    return await _update_model_setting("i2v_model", request)


@media_router.patch("/integrations/3d/model")
async def update_3d_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the 3D model generation model (admin only). Accepts any fal.ai model ID."""
    return await _update_model_setting("model3d_model", request)


@media_router.patch("/integrations/3d/i2-3d-model")
async def update_image_to_3d_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the image-to-3D model (admin only). Accepts any fal.ai model ID."""
    return await _update_model_setting("image_to_3d_model", request)


@media_router.patch("/integrations/audio/generation-model")
async def update_audio_generation_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the text-to-audio (music/sound) model. Accepts any fal.ai model ID."""
    return await _update_model_setting("audio_model", request)


@media_router.patch("/integrations/audio/audio-to-audio-model")
async def update_audio_to_audio_model(
    request: ModelUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update the audio-to-audio (transform) model. Accepts any fal.ai model ID."""
    return await _update_model_setting("audio_to_audio_model", request)


# ============================================================================
# Per-Model Generation Settings
# ============================================================================


@media_router.patch("/integrations/image/settings")
async def update_image_settings(
    request: ImageSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update image generation defaults (aspect ratio, resolution)."""
    def _write() -> None:
        if request.aspect_ratio is not None:
            database.set_system_setting("image_aspect_ratio", request.aspect_ratio)
        if request.resolution is not None:
            database.set_system_setting("image_resolution", request.resolution)

    await run_sync(_write)
    return {"success": True}


@media_router.patch("/integrations/video/settings")
async def update_video_settings(
    request: VideoSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update video generation defaults (aspect ratio, duration, resolution)."""
    def _write() -> None:
        if request.aspect_ratio is not None:
            database.set_system_setting("video_aspect_ratio", request.aspect_ratio)
        if request.duration is not None:
            database.set_system_setting("video_duration", str(request.duration))
        if request.resolution is not None:
            database.set_system_setting("video_resolution", request.resolution)

    await run_sync(_write)
    return {"success": True}


@media_router.patch("/integrations/stt/settings")
async def update_stt_settings(
    request: STTSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update STT defaults (language)."""
    if request.language is not None:
        await adb.set_system_setting("stt_language", request.language)
    return {"success": True}


# ============================================================================
# Generic, schema-driven per-model parameters
# ============================================================================
#
# For uncurated fal.ai models we don't hardcode settings — the UI renders
# controls from the model's OpenAPI schema and stores the chosen values as a
# JSON blob keyed by the model-selection setting (e.g. "tts_model_params").
# Generation code merges these onto the fal payload.


class ModelParamsRequest(BaseModel):
    """Request to persist schema-driven params for a model-selection key."""
    key: str
    params: dict[str, Any] = {}


def get_model_params(setting_key: str) -> dict:
    """Load stored schema-driven params for a model-selection setting key.

    ``setting_key`` is the model-selection key (e.g. "tts_model"); params are
    stored under ``"<setting_key>_params"``. Returns {} when none/invalid.
    """
    raw = database.get_system_setting(f"{setting_key}_params")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


@media_router.get("/integrations/model-params")
async def read_model_params(
    key: str = Query(..., description="Model-selection setting key, e.g. tts_model"),
    token: str = Depends(require_admin),
):
    """Return the stored schema-driven params for a model-selection key."""
    return {"key": key, "params": await run_sync(get_model_params, key.strip())}


@media_router.patch("/integrations/model-params")
async def update_model_params(
    request: ModelParamsRequest,
    token: str = Depends(require_admin),
):
    """Replace the stored schema-driven params for a model-selection key.

    Drops blank values so we never send empty strings to fal (which would
    override a model's own defaults).
    """
    key = request.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="key cannot be empty")
    cleaned = {
        k: v for k, v in (request.params or {}).items()
        if v is not None and v != ""
    }
    await adb.set_system_setting(f"{key}_params", json.dumps(cleaned))
    return {"success": True, "params": cleaned}
