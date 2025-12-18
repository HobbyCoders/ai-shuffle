"""
Canvas API routes for media generation and management.

This module provides endpoints for generating, editing, and managing
AI-generated images and videos through the Canvas feature.

Media files are stored in {WORKSPACE_DIR}/canvas/images/ and /videos/
Metadata is stored in {WORKSPACE_DIR}/canvas/canvas_items.json
"""

import json
import logging
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Query
from pydantic import BaseModel, Field

from app.core.config import settings
from app.api.auth import require_auth, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/canvas", tags=["Canvas"])


# ============================================================================
# Pydantic Models
# ============================================================================

class CanvasItem(BaseModel):
    """A canvas media item (image or video)"""
    id: str
    type: str  # "image" or "video"
    prompt: str
    provider: str
    model: Optional[str] = None
    file_path: str
    file_name: str
    file_size: int = 0
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None  # For videos, in seconds
    aspect_ratio: str = "16:9"
    resolution: Optional[str] = None
    parent_id: Optional[str] = None  # For edited images, reference to original
    metadata: Optional[dict] = None
    created_at: str
    updated_at: str


class ImageGenerateRequest(BaseModel):
    """Request to generate a new image"""
    prompt: str = Field(..., min_length=1, description="Text prompt for image generation")
    provider: str = Field(default="google-gemini", description="AI provider to use")
    model: Optional[str] = Field(default=None, description="Specific model to use")
    aspect_ratio: str = Field(default="16:9", description="Aspect ratio (16:9, 4:3, 1:1, etc.)")
    resolution: str = Field(default="1K", description="Resolution (1K, 2K, 4K)")
    reference_images: Optional[List[str]] = Field(default=None, description="Paths to reference images for style/character consistency")


class VideoGenerateRequest(BaseModel):
    """Request to generate a new video"""
    prompt: str = Field(..., min_length=1, description="Text prompt for video generation")
    provider: str = Field(default="google-veo", description="AI provider to use")
    model: Optional[str] = Field(default=None, description="Specific model to use")
    aspect_ratio: str = Field(default="16:9", description="Aspect ratio")
    duration: int = Field(default=8, ge=1, le=60, description="Video duration in seconds")
    source_image: Optional[str] = Field(default=None, description="Path to source image for image-to-video")


class ImageEditRequest(BaseModel):
    """Request to edit an existing image"""
    item_id: str = Field(..., description="ID of the canvas item to edit")
    prompt: str = Field(..., min_length=1, description="Edit instruction")
    provider: str = Field(default="google-gemini", description="AI provider to use")


class CanvasItemResponse(BaseModel):
    """Response containing a single canvas item"""
    item: CanvasItem


class CanvasListResponse(BaseModel):
    """Response containing a list of canvas items"""
    items: List[CanvasItem]
    total: int


# ============================================================================
# Helper Functions
# ============================================================================

def get_canvas_dir() -> Path:
    """Get the canvas directory path"""
    return settings.effective_workspace_dir / "canvas"


def get_canvas_items_path() -> Path:
    """Get the path to canvas_items.json"""
    return get_canvas_dir() / "canvas_items.json"


def get_images_dir() -> Path:
    """Get the images directory path"""
    return get_canvas_dir() / "images"


def get_videos_dir() -> Path:
    """Get the videos directory path"""
    return get_canvas_dir() / "videos"


def ensure_canvas_directories() -> None:
    """Ensure all canvas directories exist"""
    get_canvas_dir().mkdir(parents=True, exist_ok=True)
    get_images_dir().mkdir(parents=True, exist_ok=True)
    get_videos_dir().mkdir(parents=True, exist_ok=True)


def load_canvas_items() -> List[dict]:
    """Load canvas items from JSON file"""
    items_path = get_canvas_items_path()
    if not items_path.exists():
        return []

    try:
        with open(items_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("items", [])
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading canvas items: {e}")
        return []


def save_canvas_items(items: List[dict]) -> None:
    """Save canvas items to JSON file"""
    ensure_canvas_directories()
    items_path = get_canvas_items_path()

    try:
        with open(items_path, "w", encoding="utf-8") as f:
            json.dump({"items": items, "updated_at": datetime.utcnow().isoformat() + "Z"}, f, indent=2)
    except IOError as e:
        logger.error(f"Error saving canvas items: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save canvas items: {str(e)}"
        )


def get_item_by_id(item_id: str) -> Optional[dict]:
    """Get a canvas item by ID"""
    items = load_canvas_items()
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def execute_ai_tool(script: str, timeout: int = 300) -> dict:
    """
    Execute a Node.js AI tool script and parse the result.

    Args:
        script: The JavaScript code to execute
        timeout: Timeout in seconds

    Returns:
        Parsed JSON result from the script
    """
    tools_dir = settings.effective_tools_dir

    # Create a temporary script file
    script_path = get_canvas_dir() / f"temp_script_{uuid.uuid4().hex}.js"

    try:
        ensure_canvas_directories()

        # Write the script to a temp file
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        # Execute with Node.js
        result = subprocess.run(
            ["node", str(script_path)],
            cwd=str(tools_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "NODE_PATH": str(tools_dir / "node_modules")}
        )

        if result.returncode != 0:
            logger.error(f"AI tool execution failed: {result.stderr}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"AI tool execution failed: {result.stderr[:500]}"
            )

        # Parse output - expect JSON on stdout
        output = result.stdout.strip()
        if not output:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="AI tool returned empty output"
            )

        try:
            return json.loads(output)
        except json.JSONDecodeError:
            # If not JSON, return as message
            return {"message": output, "success": True}

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI tool execution timed out"
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Node.js not found. Ensure Node.js is installed."
        )
    finally:
        # Clean up temp script
        if script_path.exists():
            try:
                script_path.unlink()
            except IOError:
                pass


def create_canvas_item(
    item_type: str,
    prompt: str,
    provider: str,
    file_path: str,
    model: Optional[str] = None,
    aspect_ratio: str = "16:9",
    resolution: Optional[str] = None,
    duration: Optional[int] = None,
    parent_id: Optional[str] = None,
    metadata: Optional[dict] = None
) -> dict:
    """Create a new canvas item and save it"""
    now = datetime.utcnow().isoformat() + "Z"
    file_path_obj = Path(file_path)

    # Get file size
    file_size = 0
    if file_path_obj.exists():
        file_size = file_path_obj.stat().st_size

    item = {
        "id": str(uuid.uuid4()),
        "type": item_type,
        "prompt": prompt,
        "provider": provider,
        "model": model,
        "file_path": str(file_path),
        "file_name": file_path_obj.name,
        "file_size": file_size,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "duration": duration,
        "parent_id": parent_id,
        "metadata": metadata,
        "created_at": now,
        "updated_at": now
    }

    # Load existing items and add new one
    items = load_canvas_items()
    items.insert(0, item)  # Add to beginning (newest first)
    save_canvas_items(items)

    return item


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("", response_model=CanvasListResponse)
async def list_canvas_items(
    type: Optional[str] = Query(default=None, description="Filter by type: image or video"),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum items to return"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
    token: str = Depends(require_auth)
):
    """
    List all Canvas-generated media items.

    Items are sorted by created_at descending (newest first).
    Optionally filter by type (image or video).
    """
    items = load_canvas_items()

    # Filter by type if specified
    if type:
        if type not in ("image", "video"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid type. Must be 'image' or 'video'."
            )
        items = [item for item in items if item.get("type") == type]

    total = len(items)

    # Apply pagination
    items = items[offset:offset + limit]

    return CanvasListResponse(items=items, total=total)


@router.get("/{item_id}", response_model=CanvasItem)
async def get_canvas_item(
    item_id: str,
    token: str = Depends(require_auth)
):
    """Get a single canvas item by ID"""
    item = get_item_by_id(item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Canvas item not found: {item_id}"
        )
    return item


@router.post("/generate/image", response_model=CanvasItem, status_code=status.HTTP_201_CREATED)
async def generate_image(
    request: ImageGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a new image using AI.

    Uses the specified provider (default: google-gemini) to generate
    an image from the text prompt.

    If reference_images are provided, uses generateWithReference for
    style/character consistency.
    """
    ensure_canvas_directories()

    # Generate unique filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    output_path = get_images_dir() / f"img_{timestamp}_{file_id}.png"

    # Prepare the AI tool script
    if request.reference_images:
        # Use generateWithReference for style/character consistency
        ref_images_json = json.dumps(request.reference_images)
        script = f"""
const {{ generateWithReference }} = require('./image-tools.js');

async function main() {{
    const result = await generateWithReference({{
        prompt: {json.dumps(request.prompt)},
        referenceImages: {ref_images_json},
        outputPath: {json.dumps(str(output_path))},
        provider: {json.dumps(request.provider)},
        model: {json.dumps(request.model)},
        aspectRatio: {json.dumps(request.aspect_ratio)},
        resolution: {json.dumps(request.resolution)}
    }});
    console.log(JSON.stringify(result));
}}

main().catch(err => {{
    console.error(JSON.stringify({{ error: err.message }}));
    process.exit(1);
}});
"""
    else:
        # Standard image generation
        script = f"""
const {{ generateImage }} = require('./image-tools.js');

async function main() {{
    const result = await generateImage({{
        prompt: {json.dumps(request.prompt)},
        outputPath: {json.dumps(str(output_path))},
        provider: {json.dumps(request.provider)},
        model: {json.dumps(request.model)},
        aspectRatio: {json.dumps(request.aspect_ratio)},
        resolution: {json.dumps(request.resolution)}
    }});
    console.log(JSON.stringify(result));
}}

main().catch(err => {{
    console.error(JSON.stringify({{ error: err.message }}));
    process.exit(1);
}});
"""

    # Execute the AI tool
    result = execute_ai_tool(script)

    # Get the actual output path from result if provided
    actual_path = result.get("outputPath", str(output_path))

    # Create and save the canvas item
    item = create_canvas_item(
        item_type="image",
        prompt=request.prompt,
        provider=request.provider,
        model=request.model or result.get("model"),
        file_path=actual_path,
        aspect_ratio=request.aspect_ratio,
        resolution=request.resolution,
        metadata={
            "reference_images": request.reference_images,
            "generation_result": result
        }
    )

    return item


@router.post("/generate/video", response_model=CanvasItem, status_code=status.HTTP_201_CREATED)
async def generate_video(
    request: VideoGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a new video using AI.

    Uses the specified provider (default: google-veo) to generate
    a video from the text prompt.

    If source_image is provided, generates an image-to-video animation.
    """
    ensure_canvas_directories()

    # Generate unique filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    output_path = get_videos_dir() / f"vid_{timestamp}_{file_id}.mp4"

    # Prepare the AI tool script
    if request.source_image:
        # Image-to-video generation
        script = f"""
const {{ imageToVideo }} = require('./video-tools.js');

async function main() {{
    const result = await imageToVideo({{
        prompt: {json.dumps(request.prompt)},
        sourceImage: {json.dumps(request.source_image)},
        outputPath: {json.dumps(str(output_path))},
        provider: {json.dumps(request.provider)},
        model: {json.dumps(request.model)},
        aspectRatio: {json.dumps(request.aspect_ratio)},
        duration: {request.duration}
    }});
    console.log(JSON.stringify(result));
}}

main().catch(err => {{
    console.error(JSON.stringify({{ error: err.message }}));
    process.exit(1);
}});
"""
    else:
        # Standard video generation
        script = f"""
const {{ generateVideo }} = require('./video-tools.js');

async function main() {{
    const result = await generateVideo({{
        prompt: {json.dumps(request.prompt)},
        outputPath: {json.dumps(str(output_path))},
        provider: {json.dumps(request.provider)},
        model: {json.dumps(request.model)},
        aspectRatio: {json.dumps(request.aspect_ratio)},
        duration: {request.duration}
    }});
    console.log(JSON.stringify(result));
}}

main().catch(err => {{
    console.error(JSON.stringify({{ error: err.message }}));
    process.exit(1);
}});
"""

    # Execute the AI tool (videos can take longer)
    result = execute_ai_tool(script, timeout=600)

    # Get the actual output path from result if provided
    actual_path = result.get("outputPath", str(output_path))

    # Create and save the canvas item
    item = create_canvas_item(
        item_type="video",
        prompt=request.prompt,
        provider=request.provider,
        model=request.model or result.get("model"),
        file_path=actual_path,
        aspect_ratio=request.aspect_ratio,
        duration=request.duration,
        metadata={
            "source_image": request.source_image,
            "generation_result": result
        }
    )

    return item


@router.post("/edit/image", response_model=CanvasItem, status_code=status.HTTP_201_CREATED)
async def edit_image(
    request: ImageEditRequest,
    token: str = Depends(require_auth)
):
    """
    Edit an existing image using AI.

    Creates a new canvas item for the edited version while preserving
    the original. Links to the original via parent_id.
    """
    # Get the original item
    original_item = get_item_by_id(request.item_id)
    if not original_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Canvas item not found: {request.item_id}"
        )

    if original_item.get("type") != "image":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only edit image items"
        )

    original_path = original_item.get("file_path")
    if not original_path or not Path(original_path).exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Original image file not found"
        )

    ensure_canvas_directories()

    # Generate unique filename for edited version
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    output_path = get_images_dir() / f"img_edit_{timestamp}_{file_id}.png"

    # Prepare the AI tool script for image editing
    script = f"""
const {{ editImage }} = require('./image-tools.js');

async function main() {{
    const result = await editImage({{
        inputPath: {json.dumps(original_path)},
        prompt: {json.dumps(request.prompt)},
        outputPath: {json.dumps(str(output_path))},
        provider: {json.dumps(request.provider)}
    }});
    console.log(JSON.stringify(result));
}}

main().catch(err => {{
    console.error(JSON.stringify({{ error: err.message }}));
    process.exit(1);
}});
"""

    # Execute the AI tool
    result = execute_ai_tool(script)

    # Get the actual output path from result if provided
    actual_path = result.get("outputPath", str(output_path))

    # Create and save the canvas item
    item = create_canvas_item(
        item_type="image",
        prompt=request.prompt,
        provider=request.provider,
        model=result.get("model"),
        file_path=actual_path,
        aspect_ratio=original_item.get("aspect_ratio", "16:9"),
        resolution=original_item.get("resolution"),
        parent_id=request.item_id,
        metadata={
            "edit_instruction": request.prompt,
            "original_prompt": original_item.get("prompt"),
            "generation_result": result
        }
    )

    return item


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_canvas_item(
    item_id: str,
    delete_file: bool = Query(default=False, description="Also delete the media file"),
    token: str = Depends(require_auth)
):
    """
    Delete a canvas item.

    By default, only removes the item from the metadata store.
    Set delete_file=true to also delete the actual media file.
    """
    items = load_canvas_items()

    # Find the item
    item_to_delete = None
    item_index = None
    for i, item in enumerate(items):
        if item.get("id") == item_id:
            item_to_delete = item
            item_index = i
            break

    if item_to_delete is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Canvas item not found: {item_id}"
        )

    # Optionally delete the file
    if delete_file:
        file_path = item_to_delete.get("file_path")
        if file_path:
            path = Path(file_path)
            if path.exists():
                try:
                    path.unlink()
                    logger.info(f"Deleted media file: {file_path}")
                except IOError as e:
                    logger.warning(f"Failed to delete media file {file_path}: {e}")

    # Remove from list and save
    items.pop(item_index)
    save_canvas_items(items)

    logger.info(f"Deleted canvas item: {item_id}")
