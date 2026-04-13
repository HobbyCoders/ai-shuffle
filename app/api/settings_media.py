"""
Settings API routes - media model selection and generation settings endpoints.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import database
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
    database.set_system_setting(setting_name, model)
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


# ============================================================================
# Per-Model Generation Settings
# ============================================================================


@media_router.patch("/integrations/image/settings")
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


@media_router.patch("/integrations/video/settings")
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


@media_router.patch("/integrations/stt/settings")
async def update_stt_settings(
    request: STTSettingsRequest,
    token: str = Depends(require_admin)
):
    """Update STT defaults (language)."""
    if request.language is not None:
        database.set_system_setting("stt_language", request.language)
    return {"success": True}
