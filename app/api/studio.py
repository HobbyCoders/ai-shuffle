"""
Studio API routes for AI content generation.

This module provides endpoints for generating images and videos using
various AI providers, managing generations, and organizing assets.
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
import uuid

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query, status, Depends
from pydantic import BaseModel, Field

from app.api.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/studio", tags=["Studio"])

# In-memory storage for MVP
generations_db: Dict[str, Dict[str, Any]] = {}
assets_db: Dict[str, Dict[str, Any]] = {}


# ============================================================================
# Request/Response Models
# ============================================================================

class ImageGenerateRequest(BaseModel):
    """Request to generate an image"""
    prompt: str = Field(..., min_length=1, max_length=4000, description="Image generation prompt")
    provider: str = Field("google-gemini", description="AI provider to use")
    aspect_ratio: str = Field("1:1", description="Aspect ratio (1:1, 16:9, 9:16, 4:3, 3:4)")
    style_preset: Optional[str] = Field(None, description="Optional style preset")
    negative_prompt: Optional[str] = Field(None, description="What to avoid in the image")


class VideoGenerateRequest(BaseModel):
    """Request to generate a video"""
    prompt: str = Field(..., min_length=1, max_length=4000, description="Video generation prompt")
    provider: str = Field("google-veo", description="AI provider to use")
    duration: int = Field(8, ge=4, le=16, description="Video duration in seconds (4, 6, 8, 12, 16)")
    aspect_ratio: str = Field("16:9", description="Aspect ratio (16:9, 9:16, 1:1)")
    image_path: Optional[str] = Field(None, description="Image path for image-to-video generation")
    image_url: Optional[str] = Field(None, description="Image URL for image-to-video generation")


class GenerationResponse(BaseModel):
    """Generation status response"""
    id: str
    type: str  # image, video
    prompt: str
    status: str  # pending, generating, completed, failed
    progress: float = Field(..., ge=0, le=100)
    thumbnail_url: Optional[str] = None
    result_url: Optional[str] = None
    provider: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class AssetResponse(BaseModel):
    """Saved asset response"""
    id: str
    type: str  # image, video
    url: str
    thumbnail_url: Optional[str] = None
    prompt: str
    provider: str
    created_at: datetime
    tags: List[str] = []
    metadata: Optional[Dict[str, Any]] = None


class AssetCreateRequest(BaseModel):
    """Request to save a generation as an asset"""
    generation_id: str = Field(..., description="ID of the completed generation to save")
    tags: List[str] = Field(default=[], description="Tags to apply to the asset")


class ProviderCapability(BaseModel):
    """AI provider capability information"""
    id: str
    name: str
    type: str  # image, video, both
    models: List[str]
    aspect_ratios: List[str]
    max_prompt_length: int
    supports_negative_prompt: bool = False
    supports_style_presets: bool = False
    style_presets: List[str] = []
    # Video-specific
    durations: Optional[List[int]] = None
    supports_image_to_video: bool = False


class GenerationListResponse(BaseModel):
    """List of generations response"""
    generations: List[GenerationResponse]
    total: int


class AssetListResponse(BaseModel):
    """List of assets response"""
    assets: List[AssetResponse]
    total: int


# ============================================================================
# Provider Configuration
# ============================================================================

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "google-gemini": {
        "id": "google-gemini",
        "name": "Google Gemini",
        "type": "image",
        "models": ["gemini-2.0-flash-exp"],
        "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
        "max_prompt_length": 4000,
        "supports_negative_prompt": False,
        "supports_style_presets": False,
        "style_presets": []
    },
    "google-imagen": {
        "id": "google-imagen",
        "name": "Google Imagen",
        "type": "image",
        "models": ["imagen-3.0-generate-001"],
        "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
        "max_prompt_length": 4000,
        "supports_negative_prompt": True,
        "supports_style_presets": False,
        "style_presets": []
    },
    "openai-gpt-image": {
        "id": "openai-gpt-image",
        "name": "OpenAI GPT Image",
        "type": "image",
        "models": ["gpt-image-1"],
        "aspect_ratios": ["1:1", "16:9", "9:16"],
        "max_prompt_length": 4000,
        "supports_negative_prompt": False,
        "supports_style_presets": True,
        "style_presets": ["natural", "vivid"]
    },
    "google-veo": {
        "id": "google-veo",
        "name": "Google Veo",
        "type": "video",
        "models": ["veo-2.0-generate-001"],
        "aspect_ratios": ["16:9", "9:16"],
        "max_prompt_length": 4000,
        "supports_negative_prompt": False,
        "supports_style_presets": False,
        "style_presets": [],
        "durations": [5, 6, 8],
        "supports_image_to_video": True
    },
    "openai-sora": {
        "id": "openai-sora",
        "name": "OpenAI Sora",
        "type": "video",
        "models": ["sora-1"],
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "max_prompt_length": 4000,
        "supports_negative_prompt": False,
        "supports_style_presets": False,
        "style_presets": [],
        "durations": [5, 10, 15, 20],
        "supports_image_to_video": False
    }
}


# ============================================================================
# Helper Functions
# ============================================================================

def _create_generation_response(gen_data: Dict[str, Any]) -> GenerationResponse:
    """Convert internal generation data to response model"""
    return GenerationResponse(
        id=gen_data["id"],
        type=gen_data["type"],
        prompt=gen_data["prompt"],
        status=gen_data["status"],
        progress=gen_data.get("progress", 0.0),
        thumbnail_url=gen_data.get("thumbnail_url"),
        result_url=gen_data.get("result_url"),
        provider=gen_data["provider"],
        created_at=gen_data["created_at"],
        completed_at=gen_data.get("completed_at"),
        error=gen_data.get("error"),
        metadata=gen_data.get("metadata")
    )


def _create_asset_response(asset_data: Dict[str, Any]) -> AssetResponse:
    """Convert internal asset data to response model"""
    return AssetResponse(
        id=asset_data["id"],
        type=asset_data["type"],
        url=asset_data["url"],
        thumbnail_url=asset_data.get("thumbnail_url"),
        prompt=asset_data["prompt"],
        provider=asset_data["provider"],
        created_at=asset_data["created_at"],
        tags=asset_data.get("tags", []),
        metadata=asset_data.get("metadata")
    )


# ============================================================================
# Image Generation Endpoints
# ============================================================================

@router.post("/image/generate", response_model=GenerationResponse, status_code=status.HTTP_201_CREATED)
async def generate_image(
    request: ImageGenerateRequest,
    background_tasks: BackgroundTasks,
    token: str = Depends(require_auth)
):
    """
    Start an image generation task.

    Returns immediately with a generation ID. Poll the status endpoint
    or use WebSocket to monitor progress.

    Supported providers:
    - google-gemini: Google Gemini image generation
    - google-imagen: Google Imagen 3
    - openai-gpt-image: OpenAI GPT Image
    """
    # Validate provider
    if request.provider not in PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown provider: {request.provider}"
        )

    provider = PROVIDERS[request.provider]
    if provider["type"] not in ("image", "both"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider {request.provider} does not support image generation"
        )

    # Validate aspect ratio
    if request.aspect_ratio not in provider["aspect_ratios"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid aspect ratio for {request.provider}. Supported: {provider['aspect_ratios']}"
        )

    gen_id = str(uuid.uuid4())
    now = datetime.utcnow()

    generation = {
        "id": gen_id,
        "type": "image",
        "prompt": request.prompt,
        "status": "pending",
        "progress": 0.0,
        "thumbnail_url": None,
        "result_url": None,
        "provider": request.provider,
        "created_at": now,
        "completed_at": None,
        "error": None,
        "metadata": {
            "aspect_ratio": request.aspect_ratio,
            "style_preset": request.style_preset,
            "negative_prompt": request.negative_prompt
        }
    }

    generations_db[gen_id] = generation

    # In a real implementation, we would start the generation in background_tasks
    # For MVP, we just mark it as pending
    logger.info(f"Created image generation {gen_id} with provider {request.provider}")

    return _create_generation_response(generation)


# ============================================================================
# Video Generation Endpoints
# ============================================================================

@router.post("/video/generate", response_model=GenerationResponse, status_code=status.HTTP_201_CREATED)
async def generate_video(
    request: VideoGenerateRequest,
    background_tasks: BackgroundTasks,
    token: str = Depends(require_auth)
):
    """
    Start a video generation task.

    Returns immediately with a generation ID. Poll the status endpoint
    or use WebSocket to monitor progress.

    Supported providers:
    - google-veo: Google Veo 2 (supports image-to-video)
    - openai-sora: OpenAI Sora
    """
    # Validate provider
    if request.provider not in PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown provider: {request.provider}"
        )

    provider = PROVIDERS[request.provider]
    if provider["type"] not in ("video", "both"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider {request.provider} does not support video generation"
        )

    # Validate aspect ratio
    if request.aspect_ratio not in provider["aspect_ratios"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid aspect ratio for {request.provider}. Supported: {provider['aspect_ratios']}"
        )

    # Validate duration
    if provider.get("durations") and request.duration not in provider["durations"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid duration for {request.provider}. Supported: {provider['durations']}"
        )

    # Validate image-to-video
    if (request.image_path or request.image_url) and not provider.get("supports_image_to_video"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider {request.provider} does not support image-to-video generation"
        )

    gen_id = str(uuid.uuid4())
    now = datetime.utcnow()

    generation = {
        "id": gen_id,
        "type": "video",
        "prompt": request.prompt,
        "status": "pending",
        "progress": 0.0,
        "thumbnail_url": None,
        "result_url": None,
        "provider": request.provider,
        "created_at": now,
        "completed_at": None,
        "error": None,
        "metadata": {
            "aspect_ratio": request.aspect_ratio,
            "duration": request.duration,
            "image_path": request.image_path,
            "image_url": request.image_url
        }
    }

    generations_db[gen_id] = generation

    logger.info(f"Created video generation {gen_id} with provider {request.provider}")

    return _create_generation_response(generation)


# ============================================================================
# Generation Management Endpoints
# ============================================================================

@router.get("/generations", response_model=GenerationListResponse)
async def list_generations(
    type_filter: Optional[str] = Query(None, alias="type", description="Filter by type (image, video)"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    List all generations, optionally filtered by type and status.

    Types: image, video
    Status values: pending, generating, completed, failed
    """
    generations = list(generations_db.values())

    # Filter by type
    if type_filter:
        generations = [g for g in generations if g["type"] == type_filter]

    # Filter by status
    if status_filter:
        generations = [g for g in generations if g["status"] == status_filter]

    # Sort by created_at descending (newest first)
    generations.sort(key=lambda x: x["created_at"], reverse=True)

    total = len(generations)

    # Apply pagination
    generations = generations[offset:offset + limit]

    return GenerationListResponse(
        generations=[_create_generation_response(g) for g in generations],
        total=total
    )


@router.get("/generations/{gen_id}", response_model=GenerationResponse)
async def get_generation(gen_id: str, token: str = Depends(require_auth)):
    """Get the status of a specific generation"""
    if gen_id not in generations_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generation not found: {gen_id}"
        )

    return _create_generation_response(generations_db[gen_id])


@router.delete("/generations/{gen_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_generation(gen_id: str, token: str = Depends(require_auth)):
    """
    Delete a generation record.

    This only removes the record, not the generated files.
    """
    if gen_id not in generations_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generation not found: {gen_id}"
        )

    del generations_db[gen_id]
    logger.info(f"Deleted generation {gen_id}")


# ============================================================================
# Asset Management Endpoints
# ============================================================================

@router.get("/assets", response_model=AssetListResponse)
async def list_assets(
    type_filter: Optional[str] = Query(None, alias="type", description="Filter by type (image, video)"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    List saved assets, optionally filtered by type and tag.
    """
    assets = list(assets_db.values())

    # Filter by type
    if type_filter:
        assets = [a for a in assets if a["type"] == type_filter]

    # Filter by tag
    if tag:
        assets = [a for a in assets if tag in a.get("tags", [])]

    # Sort by created_at descending (newest first)
    assets.sort(key=lambda x: x["created_at"], reverse=True)

    total = len(assets)

    # Apply pagination
    assets = assets[offset:offset + limit]

    return AssetListResponse(
        assets=[_create_asset_response(a) for a in assets],
        total=total
    )


@router.post("/assets", response_model=AssetResponse, status_code=status.HTTP_201_CREATED)
async def save_asset(request: AssetCreateRequest, token: str = Depends(require_auth)):
    """
    Save a completed generation to the asset library.

    The generation must be in 'completed' status.
    """
    if request.generation_id not in generations_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generation not found: {request.generation_id}"
        )

    generation = generations_db[request.generation_id]

    if generation["status"] != "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot save generation in '{generation['status']}' status. Must be 'completed'."
        )

    if not generation.get("result_url"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Generation has no result URL"
        )

    asset_id = str(uuid.uuid4())
    now = datetime.utcnow()

    asset = {
        "id": asset_id,
        "type": generation["type"],
        "url": generation["result_url"],
        "thumbnail_url": generation.get("thumbnail_url"),
        "prompt": generation["prompt"],
        "provider": generation["provider"],
        "created_at": now,
        "tags": request.tags,
        "metadata": {
            "generation_id": request.generation_id,
            **generation.get("metadata", {})
        }
    }

    assets_db[asset_id] = asset

    logger.info(f"Saved asset {asset_id} from generation {request.generation_id}")

    return _create_asset_response(asset)


@router.get("/assets/{asset_id}", response_model=AssetResponse)
async def get_asset(asset_id: str, token: str = Depends(require_auth)):
    """Get details of a specific asset"""
    if asset_id not in assets_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}"
        )

    return _create_asset_response(assets_db[asset_id])


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(asset_id: str, token: str = Depends(require_auth)):
    """
    Delete an asset from the library.

    This only removes the asset record, not the actual files.
    """
    if asset_id not in assets_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}"
        )

    del assets_db[asset_id]
    logger.info(f"Deleted asset {asset_id}")


@router.patch("/assets/{asset_id}/tags")
async def update_asset_tags(
    asset_id: str,
    tags: List[str],
    token: str = Depends(require_auth)
):
    """Update the tags on an asset"""
    if asset_id not in assets_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}"
        )

    assets_db[asset_id]["tags"] = tags

    return {"status": "ok", "tags": tags}


# ============================================================================
# Provider Information Endpoints
# ============================================================================

@router.get("/providers", response_model=List[ProviderCapability])
async def list_providers(
    type_filter: Optional[str] = Query(None, alias="type", description="Filter by type (image, video)"),
    token: str = Depends(require_auth)
):
    """
    List available AI providers with their capabilities.

    Filter by type to get only image or video providers.
    """
    providers = []

    for provider_id, provider_data in PROVIDERS.items():
        # Filter by type
        if type_filter:
            if provider_data["type"] != type_filter and provider_data["type"] != "both":
                continue

        providers.append(ProviderCapability(
            id=provider_data["id"],
            name=provider_data["name"],
            type=provider_data["type"],
            models=provider_data["models"],
            aspect_ratios=provider_data["aspect_ratios"],
            max_prompt_length=provider_data["max_prompt_length"],
            supports_negative_prompt=provider_data.get("supports_negative_prompt", False),
            supports_style_presets=provider_data.get("supports_style_presets", False),
            style_presets=provider_data.get("style_presets", []),
            durations=provider_data.get("durations"),
            supports_image_to_video=provider_data.get("supports_image_to_video", False)
        ))

    return providers


@router.get("/providers/{provider_id}", response_model=ProviderCapability)
async def get_provider(provider_id: str, token: str = Depends(require_auth)):
    """Get details of a specific provider"""
    if provider_id not in PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider not found: {provider_id}"
        )

    provider_data = PROVIDERS[provider_id]

    return ProviderCapability(
        id=provider_data["id"],
        name=provider_data["name"],
        type=provider_data["type"],
        models=provider_data["models"],
        aspect_ratios=provider_data["aspect_ratios"],
        max_prompt_length=provider_data["max_prompt_length"],
        supports_negative_prompt=provider_data.get("supports_negative_prompt", False),
        supports_style_presets=provider_data.get("supports_style_presets", False),
        style_presets=provider_data.get("style_presets", []),
        durations=provider_data.get("durations"),
        supports_image_to_video=provider_data.get("supports_image_to_video", False)
    )
