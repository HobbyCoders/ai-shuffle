"""
Studio API routes for AI content generation

Provides endpoints for generating images, videos, and managing generated assets.
Integrates with AI tools at /opt/ai-tools/ for actual generation.
"""

import asyncio
import logging
import os
import subprocess
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Request, Query, UploadFile, File, Form, Depends, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.auth import require_auth, require_admin, get_api_user_from_request
from app.core.config import settings
from app.db import database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/studio", tags=["Studio"])


# ============================================================================
# Pydantic Models
# ============================================================================

class ImageGenerateRequest(BaseModel):
    """Request to generate an image"""
    prompt: str = Field(..., description="Text prompt describing the image")
    provider: str = Field("google-gemini", description="Provider: google-gemini, openai-gpt-image")
    model: Optional[str] = Field(None, description="Model override (provider-specific)")
    aspect_ratio: str = Field("1:1", description="Aspect ratio: 1:1, 16:9, 9:16, etc.")
    image_size: str = Field("1K", description="Size: 1K, 2K, 4K")
    number_of_images: int = Field(1, ge=1, le=4, description="Number of images (1-4)")
    style: Optional[str] = Field(None, description="Style modifier for the prompt")


class VideoGenerateRequest(BaseModel):
    """Request to generate a video"""
    prompt: str = Field(..., description="Text prompt describing the video")
    provider: str = Field("google-veo", description="Provider: google-veo, openai-sora")
    model: Optional[str] = Field(None, description="Model override (provider-specific)")
    duration: int = Field(8, description="Duration in seconds (4, 6, 8, or 12)")
    aspect_ratio: str = Field("16:9", description="Aspect ratio: 16:9, 9:16, 1:1")
    resolution: str = Field("720p", description="Resolution: 720p, 1080p")
    with_audio: bool = Field(False, description="Generate with audio (Veo 3 only)")


class ImageEditRequest(BaseModel):
    """Request to edit an existing image"""
    prompt: str = Field(..., description="Edit instructions")
    image_path: str = Field(..., description="Path to the image to edit")
    provider: str = Field("google-gemini", description="Provider for editing")
    model: Optional[str] = Field(None, description="Model override")
    mask_path: Optional[str] = Field(None, description="Optional mask for targeted edits")


class VideoExtendRequest(BaseModel):
    """Request to extend a video"""
    video_path: str = Field(..., description="Path to the video to extend")
    prompt: Optional[str] = Field(None, description="Optional prompt for extension direction")
    duration: int = Field(4, description="Duration to add in seconds")


class GeneratedItem(BaseModel):
    """A generated image or video item"""
    id: str
    type: str  # image, video
    prompt: str
    provider: str
    model: Optional[str] = None
    status: str  # pending, generating, completed, failed
    progress: float = 0.0
    file_path: Optional[str] = None
    url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    metadata: Dict[str, Any] = {}
    error: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None


class AssetItem(BaseModel):
    """A saved asset in the library"""
    id: str
    type: str  # image, video
    name: str
    description: Optional[str] = None
    file_path: str
    url: str
    thumbnail_url: Optional[str] = None
    tags: List[str] = []
    metadata: Dict[str, Any] = {}
    created_at: datetime


class GenerationResponse(BaseModel):
    """Response for a generation request"""
    id: str
    type: str
    status: str
    progress: float = 0.0
    prompt: str
    provider: str
    result_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    file_path: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = {}


# ============================================================================
# In-Memory Storage (for MVP - will be persisted to DB later)
# ============================================================================

_generations: Dict[str, Dict[str, Any]] = {}
_assets: Dict[str, Dict[str, Any]] = {}
_generation_tasks: Dict[str, asyncio.Task] = {}


def _get_generated_images_dir() -> Path:
    """Get the directory for generated images"""
    gen_dir = settings.effective_workspace_dir / "generated-images"
    gen_dir.mkdir(parents=True, exist_ok=True)
    return gen_dir


def _get_generated_videos_dir() -> Path:
    """Get the directory for generated videos"""
    gen_dir = settings.effective_workspace_dir / "generated-videos"
    gen_dir.mkdir(parents=True, exist_ok=True)
    return gen_dir


def _get_assets_dir() -> Path:
    """Get the directory for saved assets"""
    assets_dir = settings.effective_workspace_dir / "studio-assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    return assets_dir


# ============================================================================
# AI Tools Integration
# ============================================================================

async def _run_image_generation(
    gen_id: str,
    prompt: str,
    provider: str,
    model: Optional[str],
    aspect_ratio: str,
    image_size: str,
    number_of_images: int
):
    """Run image generation using AI tools"""
    gen = _generations.get(gen_id)
    if not gen:
        return

    try:
        gen["status"] = "generating"
        gen["progress"] = 0.1

        # Build the generation script
        # This calls the TypeScript tools via node
        script = f"""
import {{ generateImage }} from '/opt/ai-tools/dist/image-generation/generateImage.js';

const result = await generateImage({{
    prompt: {json.dumps(prompt)},
    provider: {json.dumps(provider)},
    {"model: " + json.dumps(model) + "," if model else ""}
    aspect_ratio: {json.dumps(aspect_ratio)},
    image_size: {json.dumps(image_size)},
    number_of_images: {number_of_images}
}});

console.log(JSON.stringify(result));
"""

        # Write temp script
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.mjs', delete=False) as f:
            f.write(script)
            script_path = f.name

        try:
            # Run the script
            gen["progress"] = 0.3

            # Set up environment with API keys from settings
            env = os.environ.copy()

            # Get AI settings from database
            ai_settings = database.get_system_setting("ai_providers")
            if ai_settings:
                try:
                    providers_config = json.loads(ai_settings) if isinstance(ai_settings, str) else ai_settings
                    # Set provider-specific keys
                    if "google-gemini" in providers_config:
                        gemini_config = providers_config["google-gemini"]
                        if gemini_config.get("api_key"):
                            env["GEMINI_API_KEY"] = gemini_config["api_key"]
                            env["IMAGE_API_KEY"] = gemini_config["api_key"]
                    if "openai-gpt-image" in providers_config:
                        openai_config = providers_config["openai-gpt-image"]
                        if openai_config.get("api_key"):
                            env["OPENAI_API_KEY"] = openai_config["api_key"]
                except Exception as e:
                    logger.warning(f"Failed to parse AI settings: {e}")

            result = subprocess.run(
                ["node", script_path],
                capture_output=True,
                text=True,
                env=env,
                timeout=120
            )

            gen["progress"] = 0.8

            if result.returncode != 0:
                raise Exception(f"Generation failed: {result.stderr}")

            # Parse result
            output = result.stdout.strip()
            if not output:
                raise Exception("No output from generation")

            gen_result = json.loads(output)

            if not gen_result.get("success"):
                raise Exception(gen_result.get("error", "Unknown error"))

            # Update generation record
            gen["status"] = "completed"
            gen["progress"] = 1.0
            gen["file_path"] = gen_result.get("file_path")
            gen["url"] = gen_result.get("image_url")
            gen["completed_at"] = datetime.utcnow()
            gen["metadata"] = {
                "provider_used": gen_result.get("provider_used"),
                "model_used": gen_result.get("model_used"),
                "capabilities": gen_result.get("capabilities", []),
                "images": gen_result.get("images", [])
            }

            logger.info(f"Image generation {gen_id} completed: {gen['url']}")

        finally:
            # Clean up temp script
            try:
                os.unlink(script_path)
            except Exception:
                pass

    except asyncio.CancelledError:
        gen["status"] = "cancelled"
        gen["error"] = "Generation cancelled"

    except Exception as e:
        logger.error(f"Image generation {gen_id} failed: {e}", exc_info=True)
        gen["status"] = "failed"
        gen["error"] = str(e)
        gen["completed_at"] = datetime.utcnow()

    finally:
        if gen_id in _generation_tasks:
            del _generation_tasks[gen_id]


async def _run_video_generation(
    gen_id: str,
    prompt: str,
    provider: str,
    model: Optional[str],
    duration: int,
    aspect_ratio: str,
    resolution: str
):
    """Run video generation using AI tools"""
    gen = _generations.get(gen_id)
    if not gen:
        return

    try:
        gen["status"] = "generating"
        gen["progress"] = 0.1

        # Build the generation script
        script = f"""
import {{ generateVideo }} from '/opt/ai-tools/dist/video-generation/generateVideo.js';

const result = await generateVideo({{
    prompt: {json.dumps(prompt)},
    provider: {json.dumps(provider)},
    {"model: " + json.dumps(model) + "," if model else ""}
    duration: {duration},
    aspect_ratio: {json.dumps(aspect_ratio)},
    resolution: {json.dumps(resolution)}
}});

console.log(JSON.stringify(result));
"""

        # Write temp script
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.mjs', delete=False) as f:
            f.write(script)
            script_path = f.name

        try:
            gen["progress"] = 0.2

            # Set up environment with API keys
            env = os.environ.copy()

            ai_settings = database.get_system_setting("ai_providers")
            if ai_settings:
                try:
                    providers_config = json.loads(ai_settings) if isinstance(ai_settings, str) else ai_settings
                    if "google-veo" in providers_config:
                        veo_config = providers_config["google-veo"]
                        if veo_config.get("api_key"):
                            env["GEMINI_API_KEY"] = veo_config["api_key"]
                            env["VIDEO_API_KEY"] = veo_config["api_key"]
                    if "openai-sora" in providers_config:
                        sora_config = providers_config["openai-sora"]
                        if sora_config.get("api_key"):
                            env["OPENAI_API_KEY"] = sora_config["api_key"]
                except Exception as e:
                    logger.warning(f"Failed to parse AI settings: {e}")

            # Video generation can take a long time
            result = subprocess.run(
                ["node", script_path],
                capture_output=True,
                text=True,
                env=env,
                timeout=600  # 10 minutes
            )

            gen["progress"] = 0.9

            if result.returncode != 0:
                raise Exception(f"Generation failed: {result.stderr}")

            output = result.stdout.strip()
            if not output:
                raise Exception("No output from generation")

            gen_result = json.loads(output)

            if not gen_result.get("success"):
                raise Exception(gen_result.get("error", "Unknown error"))

            gen["status"] = "completed"
            gen["progress"] = 1.0
            gen["file_path"] = gen_result.get("file_path")
            gen["url"] = gen_result.get("video_url")
            gen["thumbnail_url"] = gen_result.get("thumbnail_url")
            gen["completed_at"] = datetime.utcnow()
            gen["metadata"] = gen_result.get("provider_metadata", {})

            logger.info(f"Video generation {gen_id} completed: {gen['url']}")

        finally:
            try:
                os.unlink(script_path)
            except Exception:
                pass

    except asyncio.CancelledError:
        gen["status"] = "cancelled"
        gen["error"] = "Generation cancelled"

    except subprocess.TimeoutExpired:
        gen["status"] = "failed"
        gen["error"] = "Generation timed out (10 minutes)"
        gen["completed_at"] = datetime.utcnow()

    except Exception as e:
        logger.error(f"Video generation {gen_id} failed: {e}", exc_info=True)
        gen["status"] = "failed"
        gen["error"] = str(e)
        gen["completed_at"] = datetime.utcnow()

    finally:
        if gen_id in _generation_tasks:
            del _generation_tasks[gen_id]


# ============================================================================
# Image Generation Endpoints
# ============================================================================

@router.post("/image/generate", response_model=GenerationResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate_image(
    request: ImageGenerateRequest,
    req: Request,
    token: str = Depends(require_auth)
):
    """
    Generate an image from a text prompt.

    The generation runs asynchronously. Poll the status endpoint or use WebSocket
    to get real-time updates.

    Available providers:
    - google-gemini: Gemini 2.5 Flash Image, Gemini 3 Pro Image
    - openai-gpt-image: GPT-4o native image generation
    """
    gen_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Combine style with prompt if provided
    full_prompt = request.prompt
    if request.style:
        full_prompt = f"{request.prompt}, {request.style} style"

    # Create generation record
    gen = {
        "id": gen_id,
        "type": "image",
        "prompt": full_prompt,
        "provider": request.provider,
        "model": request.model,
        "status": "pending",
        "progress": 0.0,
        "file_path": None,
        "url": None,
        "thumbnail_url": None,
        "metadata": {
            "aspect_ratio": request.aspect_ratio,
            "image_size": request.image_size,
            "number_of_images": request.number_of_images
        },
        "error": None,
        "created_at": now,
        "completed_at": None
    }
    _generations[gen_id] = gen

    # Start generation task
    task = asyncio.create_task(
        _run_image_generation(
            gen_id=gen_id,
            prompt=full_prompt,
            provider=request.provider,
            model=request.model,
            aspect_ratio=request.aspect_ratio,
            image_size=request.image_size,
            number_of_images=request.number_of_images
        )
    )
    _generation_tasks[gen_id] = task

    logger.info(f"Started image generation {gen_id}: {request.prompt[:50]}...")

    return GenerationResponse(
        id=gen_id,
        type="image",
        status="pending",
        progress=0.0,
        prompt=full_prompt,
        provider=request.provider
    )


@router.post("/image/edit", response_model=GenerationResponse, status_code=status.HTTP_202_ACCEPTED)
async def edit_image(
    request: ImageEditRequest,
    req: Request,
    token: str = Depends(require_auth)
):
    """
    Edit an existing image using AI.

    Provide the path to an existing image and instructions for how to modify it.
    Optionally provide a mask to target specific areas.
    """
    gen_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Validate image exists
    image_path = Path(request.image_path)
    if not image_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Image not found: {request.image_path}"
        )

    gen = {
        "id": gen_id,
        "type": "image",
        "prompt": request.prompt,
        "provider": request.provider,
        "model": request.model,
        "status": "pending",
        "progress": 0.0,
        "file_path": None,
        "url": None,
        "thumbnail_url": None,
        "metadata": {
            "edit_mode": True,
            "source_image": request.image_path,
            "mask_path": request.mask_path
        },
        "error": None,
        "created_at": now,
        "completed_at": None
    }
    _generations[gen_id] = gen

    # For now, return a placeholder - actual editing would need editImage tool
    # This is a stub that can be expanded later
    gen["status"] = "failed"
    gen["error"] = "Image editing is not yet implemented in this endpoint. Use the generateImage tool with reference image instead."
    gen["completed_at"] = datetime.utcnow()

    return GenerationResponse(
        id=gen_id,
        type="image",
        status=gen["status"],
        progress=0.0,
        prompt=request.prompt,
        provider=request.provider,
        error=gen["error"]
    )


# ============================================================================
# Video Generation Endpoints
# ============================================================================

@router.post("/video/generate", response_model=GenerationResponse, status_code=status.HTTP_202_ACCEPTED)
async def generate_video(
    request: VideoGenerateRequest,
    req: Request,
    token: str = Depends(require_auth)
):
    """
    Generate a video from a text prompt.

    The generation runs asynchronously and may take several minutes.
    Poll the status endpoint to check progress.

    Available providers:
    - google-veo: Veo 3, Veo 3.1 (supports audio with Veo 3)
    - openai-sora: Sora 2, Sora 2 Pro
    """
    gen_id = str(uuid.uuid4())
    now = datetime.utcnow()

    gen = {
        "id": gen_id,
        "type": "video",
        "prompt": request.prompt,
        "provider": request.provider,
        "model": request.model,
        "status": "pending",
        "progress": 0.0,
        "file_path": None,
        "url": None,
        "thumbnail_url": None,
        "metadata": {
            "duration": request.duration,
            "aspect_ratio": request.aspect_ratio,
            "resolution": request.resolution,
            "with_audio": request.with_audio
        },
        "error": None,
        "created_at": now,
        "completed_at": None
    }
    _generations[gen_id] = gen

    # Start generation task
    task = asyncio.create_task(
        _run_video_generation(
            gen_id=gen_id,
            prompt=request.prompt,
            provider=request.provider,
            model=request.model,
            duration=request.duration,
            aspect_ratio=request.aspect_ratio,
            resolution=request.resolution
        )
    )
    _generation_tasks[gen_id] = task

    logger.info(f"Started video generation {gen_id}: {request.prompt[:50]}...")

    return GenerationResponse(
        id=gen_id,
        type="video",
        status="pending",
        progress=0.0,
        prompt=request.prompt,
        provider=request.provider
    )


@router.post("/video/extend", response_model=GenerationResponse, status_code=status.HTTP_202_ACCEPTED)
async def extend_video(
    request: VideoExtendRequest,
    req: Request,
    token: str = Depends(require_auth)
):
    """
    Extend an existing video (Veo only).

    Adds additional duration to the end of an existing video.
    """
    gen_id = str(uuid.uuid4())
    now = datetime.utcnow()

    # Validate video exists
    video_path = Path(request.video_path)
    if not video_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video not found: {request.video_path}"
        )

    gen = {
        "id": gen_id,
        "type": "video",
        "prompt": request.prompt or "Continue the video naturally",
        "provider": "google-veo",
        "model": None,
        "status": "pending",
        "progress": 0.0,
        "file_path": None,
        "url": None,
        "thumbnail_url": None,
        "metadata": {
            "extend_mode": True,
            "source_video": request.video_path,
            "extend_duration": request.duration
        },
        "error": None,
        "created_at": now,
        "completed_at": None
    }
    _generations[gen_id] = gen

    # For now, return a placeholder - actual extension would need extendVideo tool
    gen["status"] = "failed"
    gen["error"] = "Video extension is not yet implemented in this endpoint. Use the extendVideo tool directly."
    gen["completed_at"] = datetime.utcnow()

    return GenerationResponse(
        id=gen_id,
        type="video",
        status=gen["status"],
        progress=0.0,
        prompt=gen["prompt"],
        provider="google-veo",
        error=gen["error"]
    )


# ============================================================================
# Generation Management Endpoints
# ============================================================================

@router.get("/generations", response_model=List[GeneratedItem])
async def list_generations(
    request: Request,
    type_filter: Optional[str] = Query(None, description="Filter by type: image, video"),
    status_filter: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """List recent generations"""
    gens = list(_generations.values())

    if type_filter:
        gens = [g for g in gens if g["type"] == type_filter]

    if status_filter:
        gens = [g for g in gens if g["status"] == status_filter]

    # Sort by created_at descending
    gens.sort(key=lambda g: g["created_at"], reverse=True)

    # Paginate
    gens = gens[offset:offset + limit]

    return [GeneratedItem(**g) for g in gens]


@router.get("/generations/{gen_id}", response_model=GeneratedItem)
async def get_generation(
    gen_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Get status and details of a specific generation"""
    gen = _generations.get(gen_id)
    if not gen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generation not found: {gen_id}"
        )

    return GeneratedItem(**gen)


@router.delete("/generations/{gen_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_generation(
    gen_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Delete a generation record and optionally its file"""
    gen = _generations.get(gen_id)
    if not gen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Generation not found: {gen_id}"
        )

    # Cancel if still running
    if gen_id in _generation_tasks:
        _generation_tasks[gen_id].cancel()
        try:
            await _generation_tasks[gen_id]
        except asyncio.CancelledError:
            pass
        del _generation_tasks[gen_id]

    # Optionally delete file
    if gen.get("file_path"):
        try:
            file_path = Path(gen["file_path"])
            if file_path.exists():
                file_path.unlink()
        except Exception as e:
            logger.warning(f"Failed to delete generation file: {e}")

    del _generations[gen_id]


# ============================================================================
# Asset Library Endpoints
# ============================================================================

@router.get("/assets", response_model=List[AssetItem])
async def list_assets(
    request: Request,
    type_filter: Optional[str] = Query(None, description="Filter by type: image, video"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """List saved assets in the library"""
    assets = list(_assets.values())

    if type_filter:
        assets = [a for a in assets if a["type"] == type_filter]

    if tag:
        assets = [a for a in assets if tag in a.get("tags", [])]

    # Sort by created_at descending
    assets.sort(key=lambda a: a["created_at"], reverse=True)

    # Paginate
    assets = assets[offset:offset + limit]

    return [AssetItem(**a) for a in assets]


class SaveAssetRequest(BaseModel):
    """Request to save an asset to the library"""
    generation_id: Optional[str] = Field(None, description="ID of generation to save")
    file_path: Optional[str] = Field(None, description="Path to existing file to save")
    name: str = Field(..., description="Name for the asset")
    description: Optional[str] = Field(None, description="Description")
    tags: List[str] = Field(default_factory=list, description="Tags for organizing")


@router.post("/assets", response_model=AssetItem, status_code=status.HTTP_201_CREATED)
async def save_asset(
    request: SaveAssetRequest,
    req: Request,
    token: str = Depends(require_auth)
):
    """Save a generation or file as an asset to the library"""
    asset_id = str(uuid.uuid4())
    now = datetime.utcnow()

    source_path: Optional[Path] = None
    asset_type: str = "image"
    metadata: Dict[str, Any] = {}

    if request.generation_id:
        # Save from generation
        gen = _generations.get(request.generation_id)
        if not gen:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Generation not found: {request.generation_id}"
            )

        if gen["status"] != "completed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Generation is not completed"
            )

        if not gen.get("file_path"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Generation has no file"
            )

        source_path = Path(gen["file_path"])
        asset_type = gen["type"]
        metadata = {
            "generation_id": request.generation_id,
            "prompt": gen.get("prompt"),
            "provider": gen.get("provider"),
            "model": gen.get("model"),
            **gen.get("metadata", {})
        }

    elif request.file_path:
        # Save from existing file
        source_path = Path(request.file_path)
        if not source_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File not found: {request.file_path}"
            )

        # Determine type from extension
        suffix = source_path.suffix.lower()
        if suffix in (".mp4", ".webm", ".mov", ".avi"):
            asset_type = "video"
        else:
            asset_type = "image"

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either generation_id or file_path is required"
        )

    # Copy file to assets directory
    assets_dir = _get_assets_dir()
    dest_filename = f"{asset_id}{source_path.suffix}"
    dest_path = assets_dir / dest_filename

    import shutil
    shutil.copy2(source_path, dest_path)

    # Build URL
    url = f"/api/v1/studio/assets/{asset_id}/file"

    asset = {
        "id": asset_id,
        "type": asset_type,
        "name": request.name,
        "description": request.description,
        "file_path": str(dest_path),
        "url": url,
        "thumbnail_url": None,
        "tags": request.tags,
        "metadata": metadata,
        "created_at": now
    }
    _assets[asset_id] = asset

    logger.info(f"Saved asset {asset_id}: {request.name}")

    return AssetItem(**asset)


@router.get("/assets/{asset_id}", response_model=AssetItem)
async def get_asset(
    asset_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Get details of a specific asset"""
    asset = _assets.get(asset_id)
    if not asset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}"
        )

    return AssetItem(**asset)


@router.get("/assets/{asset_id}/file")
async def get_asset_file(
    asset_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Download an asset file"""
    asset = _assets.get(asset_id)
    if not asset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}"
        )

    file_path = Path(asset["file_path"])
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset file not found"
        )

    # Determine media type
    suffix = file_path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime"
    }
    media_type = media_types.get(suffix, "application/octet-stream")

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name
    )


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset(
    asset_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """Delete an asset"""
    asset = _assets.get(asset_id)
    if not asset:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}"
        )

    # Delete file
    try:
        file_path = Path(asset["file_path"])
        if file_path.exists():
            file_path.unlink()
    except Exception as e:
        logger.warning(f"Failed to delete asset file: {e}")

    del _assets[asset_id]


# ============================================================================
# Provider Information Endpoints
# ============================================================================

@router.get("/providers")
async def list_providers(
    request: Request,
    token: str = Depends(require_auth)
):
    """
    List available AI providers for image and video generation.

    Returns configured providers based on available API keys.
    """
    # Define available providers
    image_providers = [
        {
            "id": "google-gemini",
            "name": "Google Gemini",
            "models": [
                {"id": "gemini-2.5-flash-image", "name": "Gemini 2.5 Flash Image", "default": True},
                {"id": "gemini-3-pro-image-preview", "name": "Gemini 3 Pro Image Preview", "default": False}
            ],
            "capabilities": ["text-to-image", "image-edit", "reference-image"],
            "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
            "sizes": ["1K", "2K", "4K"]
        },
        {
            "id": "openai-gpt-image",
            "name": "OpenAI GPT Image",
            "models": [
                {"id": "gpt-image-1", "name": "GPT-4o Image", "default": True}
            ],
            "capabilities": ["text-to-image", "image-edit"],
            "aspect_ratios": ["1:1", "16:9", "9:16"],
            "sizes": ["1K", "2K"]
        }
    ]

    video_providers = [
        {
            "id": "google-veo",
            "name": "Google Veo",
            "models": [
                {"id": "veo-3.1-fast-generate-preview", "name": "Veo 3.1 Fast", "default": True},
                {"id": "veo-3.1-generate-preview", "name": "Veo 3.1", "default": False},
                {"id": "veo-3-fast-generate-preview", "name": "Veo 3 Fast (with audio)", "default": False}
            ],
            "capabilities": ["text-to-video", "image-to-video", "video-extend"],
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "durations": [4, 6, 8],
            "resolutions": ["720p", "1080p"]
        },
        {
            "id": "openai-sora",
            "name": "OpenAI Sora",
            "models": [
                {"id": "sora-2", "name": "Sora 2", "default": True},
                {"id": "sora-2-pro", "name": "Sora 2 Pro", "default": False}
            ],
            "capabilities": ["text-to-video"],
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "durations": [4, 8, 12],
            "resolutions": ["720p", "1080p"]
        }
    ]

    return {
        "image_providers": image_providers,
        "video_providers": video_providers
    }
