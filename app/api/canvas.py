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

from fastapi import APIRouter, HTTPException, Depends, status, Query, UploadFile, File
from pydantic import BaseModel, Field

from app.core.config import settings
from app.api.auth import require_auth, require_admin
from app.db import database
from app.core import encryption

logger = logging.getLogger(__name__)


def _get_decrypted_api_key(setting_name: str) -> Optional[str]:
    """
    Get an API key from the database and decrypt it if encrypted.

    Args:
        setting_name: The name of the setting (e.g., "openai_api_key", "image_api_key")

    Returns:
        The decrypted API key, or None if not found or decryption fails
    """
    value = database.get_system_setting(setting_name)
    if not value:
        return None

    # Check if the value is encrypted
    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            logger.warning(f"Cannot decrypt {setting_name}: encryption key not available")
            return None
        try:
            decrypted = encryption.decrypt_value(value)
            return decrypted
        except Exception as e:
            logger.error(f"Failed to decrypt {setting_name}: {e}")
            return None

    # Return plaintext value (for backwards compatibility during migration)
    return value

router = APIRouter(prefix="/api/v1/canvas", tags=["Canvas"])

# AI Tools path
AI_TOOLS_PATH = Path("/opt/ai-tools/dist")


# ============================================================================
# Provider/Model Definitions
# ============================================================================

IMAGE_PROVIDERS = {
    "google-gemini": {
        "id": "google-gemini",
        "name": "Nano Banana",
        "description": "Fast iteration, editing, reference images",
        "supports_edit": True,
        "supports_reference": True,
        "models": [
            {"id": "gemini-2.5-flash-image", "name": "Nano Banana", "default": True},
            {"id": "gemini-3-pro-image-preview", "name": "Nano Banana Pro"},
        ],
        "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4", "2:3", "3:2", "4:5", "5:4", "21:9"],
        "resolutions": ["1K", "2K"],
    },
    "google-imagen": {
        "id": "google-imagen",
        "name": "Imagen 4",
        "description": "Highest quality, photo-realism",
        "supports_edit": False,
        "supports_reference": False,
        "models": [
            {"id": "imagen-4.0-generate-001", "name": "Imagen 4", "default": True},
            {"id": "imagen-4.0-ultra-generate-001", "name": "Imagen 4 Ultra"},
            {"id": "imagen-4.0-fast-generate-001", "name": "Imagen 4 Fast"},
        ],
        "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
        "resolutions": ["1K", "2K"],
    },
    "openai-gpt-image": {
        "id": "openai-gpt-image",
        "name": "GPT Image",
        "description": "Accurate text in images, inpainting",
        "supports_edit": True,
        "supports_reference": False,
        "models": [
            {"id": "gpt-image-1", "name": "GPT Image", "default": True},
        ],
        "aspect_ratios": ["1:1", "16:9", "9:16"],
        "resolutions": ["1K", "2K", "4K"],
    }
}

VIDEO_PROVIDERS = {
    "google-veo": {
        "id": "google-veo",
        "name": "Google Veo",
        "description": "Video extension, frame bridging, native audio",
        "supports_extend": True,
        "supports_image_to_video": True,
        "models": [
            {"id": "veo-3.1-generate-preview", "name": "Veo 3.1", "default": True},
            {"id": "veo-3.1-fast-generate-preview", "name": "Veo 3.1 Fast"},
            {"id": "veo-3-generate-preview", "name": "Veo 3", "has_audio": True},
            {"id": "veo-3-fast-generate-preview", "name": "Veo 3 Fast", "has_audio": True},
        ],
        "aspect_ratios": ["16:9", "9:16"],
        "durations": [4, 6, 8],
        "max_duration": 8,
    },
    "openai-sora": {
        "id": "openai-sora",
        "name": "Sora",
        "description": "Fast generation, good quality",
        "supports_extend": False,
        "supports_image_to_video": True,
        "models": [
            {"id": "sora-2", "name": "Sora 2", "default": True},
        ],
        "aspect_ratios": ["16:9", "9:16", "1:1"],
        "durations": [4, 8, 12],
        "max_duration": 12,
    }
}


# ============================================================================
# TTS/STT Provider/Model Definitions
# ============================================================================

TTS_PROVIDERS = {
    "openai-tts": {
        "id": "openai-tts",
        "name": "OpenAI TTS",
        "description": "Natural-sounding speech with multiple voices",
        "models": [
            {"id": "gpt-4o-mini-tts", "name": "GPT-4o Mini TTS", "default": True, "supports_instructions": True},
            {"id": "tts-1", "name": "TTS-1", "supports_instructions": False},
            {"id": "tts-1-hd", "name": "TTS-1 HD", "supports_instructions": False},
        ],
        "voices": [
            {"id": "alloy", "name": "Alloy", "description": "Neutral, versatile"},
            {"id": "ash", "name": "Ash", "description": "Clear, professional"},
            {"id": "ballad", "name": "Ballad", "description": "Warm, storytelling"},
            {"id": "coral", "name": "Coral", "description": "Friendly, conversational"},
            {"id": "echo", "name": "Echo", "description": "Resonant, dramatic"},
            {"id": "fable", "name": "Fable", "description": "Engaging, expressive"},
            {"id": "onyx", "name": "Onyx", "description": "Deep, authoritative"},
            {"id": "nova", "name": "Nova", "description": "Soft, gentle"},
            {"id": "sage", "name": "Sage", "description": "Calm, measured"},
            {"id": "shimmer", "name": "Shimmer", "description": "Bright, energetic"},
            {"id": "verse", "name": "Verse", "description": "Lyrical, musical"},
        ],
        "output_formats": ["mp3", "opus", "aac", "flac", "wav", "pcm"],
    }
}

STT_PROVIDERS = {
    "openai-stt": {
        "id": "openai-stt",
        "name": "OpenAI STT",
        "description": "Accurate speech-to-text transcription",
        "models": [
            {"id": "gpt-4o-transcribe", "name": "GPT-4o Transcribe", "default": True, "supports_diarization": False},
            {"id": "gpt-4o-mini-transcribe", "name": "GPT-4o Mini Transcribe", "supports_diarization": False},
            {"id": "whisper-1", "name": "Whisper", "supports_diarization": False},
        ],
        "supported_formats": ["mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "ogg", "flac"],
    }
}


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
    url: Optional[str] = None  # API URL to access the file
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


class CanvasListResponse(BaseModel):
    """Response containing a list of canvas items"""
    items: List[CanvasItem]
    total: int


class ProviderInfo(BaseModel):
    """Information about a provider"""
    id: str
    name: str
    description: str
    models: List[dict]
    aspect_ratios: List[str]
    # Image-specific
    supports_edit: Optional[bool] = None
    supports_reference: Optional[bool] = None
    resolutions: Optional[List[str]] = None
    # Video-specific
    supports_extend: Optional[bool] = None
    supports_image_to_video: Optional[bool] = None
    durations: Optional[List[int]] = None
    max_duration: Optional[int] = None


class ProvidersResponse(BaseModel):
    """Response containing available providers"""
    image_providers: List[ProviderInfo]
    video_providers: List[ProviderInfo]


# TTS Request/Response Models
class TTSGenerateRequest(BaseModel):
    """Request to generate text-to-speech audio"""
    text: str = Field(..., min_length=1, max_length=4096, description="Text to convert to speech")
    provider: str = Field(default="openai", description="TTS provider to use")
    model: Optional[str] = Field(default=None, description="TTS model (gpt-4o-mini-tts, tts-1, tts-1-hd)")
    voice: str = Field(default="alloy", description="Voice to use")
    voice_instructions: Optional[str] = Field(default=None, description="Instructions for how to speak (only for gpt-4o-mini-tts)")
    speed: float = Field(default=1.0, ge=0.25, le=4.0, description="Speech speed (0.25 to 4.0)")
    output_format: str = Field(default="mp3", description="Output format (mp3, opus, aac, flac, wav, pcm)")


class TTSGenerateResponse(BaseModel):
    """Response from TTS generation"""
    id: str
    url: str
    file_path: str
    filename: str
    duration: Optional[float] = None
    mime_type: str
    text_length: int
    provider: str
    model: str
    voice: str
    created_at: str


# STT Request/Response Models
class STTTranscribeRequest(BaseModel):
    """Request to transcribe audio to text"""
    audio_url: Optional[str] = Field(default=None, description="URL or path to audio file")
    provider: str = Field(default="openai", description="STT provider to use")
    model: Optional[str] = Field(default=None, description="STT model (gpt-4o-transcribe, whisper-1, etc.)")
    language: Optional[str] = Field(default=None, description="Language code (e.g., 'en', 'es', 'fr')")
    diarization: bool = Field(default=False, description="Enable speaker diarization (not yet supported)")
    translate: bool = Field(default=False, description="Translate to English")
    timestamp_granularity: Optional[str] = Field(default=None, description="Timestamp level: 'word' or 'segment'")


class Speaker(BaseModel):
    """Speaker info for diarization"""
    id: str
    label: Optional[str] = None


class TranscriptSegment(BaseModel):
    """A segment of transcribed text"""
    id: int
    text: str
    start: float
    end: float
    speaker: Optional[str] = None


class STTTranscribeResponse(BaseModel):
    """Response from STT transcription"""
    id: str
    transcript: str
    duration: Optional[float] = None
    language: Optional[str] = None
    speakers: Optional[List[Speaker]] = None
    segments: Optional[List[TranscriptSegment]] = None
    words: Optional[List[dict]] = None
    provider: str
    model: str
    created_at: str


# Audio Canvas Item (similar to CanvasItem but for audio)
class AudioCanvasItem(BaseModel):
    """A canvas audio item (TTS-generated audio)"""
    id: str
    type: str = "audio"
    text: str  # Original text for TTS
    provider: str
    model: Optional[str] = None
    voice: str
    file_path: str
    file_name: str
    url: Optional[str] = None
    file_size: int = 0
    duration: Optional[float] = None
    mime_type: str = "audio/mpeg"
    metadata: Optional[dict] = None
    created_at: str
    updated_at: str


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


def get_uploads_dir() -> Path:
    """Get the uploads directory path for temporary source files"""
    return get_canvas_dir() / "uploads"


def get_audio_dir() -> Path:
    """Get the audio directory path for TTS-generated audio"""
    return get_canvas_dir() / "audio"


def ensure_canvas_directories() -> None:
    """Ensure all canvas directories exist"""
    get_canvas_dir().mkdir(parents=True, exist_ok=True)
    get_images_dir().mkdir(parents=True, exist_ok=True)
    get_videos_dir().mkdir(parents=True, exist_ok=True)
    get_uploads_dir().mkdir(parents=True, exist_ok=True)
    get_audio_dir().mkdir(parents=True, exist_ok=True)


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


def get_file_url(file_path: str, item_type: str) -> str:
    """Generate the API URL for a canvas file"""
    file_name = Path(file_path).name
    if item_type == "image":
        return f"/api/v1/canvas/files/images/{file_name}"
    else:
        return f"/api/v1/canvas/files/videos/{file_name}"


def execute_ai_tool(script: str, item_type: str = "image", timeout: int = 300) -> dict:
    """
    Execute a Node.js AI tool script (ESM) and parse the result.

    Args:
        script: The JavaScript code to execute (ESM format)
        item_type: Type of media being generated ("image" or "video")
        timeout: Timeout in seconds

    Returns:
        Parsed JSON result from the script
    """
    # Create a temporary script file with .mjs extension for ESM
    script_id = uuid.uuid4().hex
    script_path = get_canvas_dir() / f"temp_script_{script_id}.mjs"

    try:
        ensure_canvas_directories()

        # Write the script to a temp file
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        # Set up environment with output directories for the AI tools
        env = {
            **os.environ,
            "GENERATED_IMAGES_DIR": str(get_images_dir()),
            "GENERATED_VIDEOS_DIR": str(get_videos_dir()),
            "GENERATED_AUDIO_DIR": str(get_audio_dir()),
        }

        # Inject API keys from database settings
        # Use image_api_key for Gemini-based providers (Nano Banana, Imagen, Veo)
        gemini_api_key = _get_decrypted_api_key("image_api_key")
        if gemini_api_key:
            env["GEMINI_API_KEY"] = gemini_api_key
            env["IMAGE_API_KEY"] = gemini_api_key
            env["VIDEO_API_KEY"] = gemini_api_key

        # Use openai_api_key for OpenAI providers (GPT Image, Sora, TTS, STT)
        openai_api_key = _get_decrypted_api_key("openai_api_key")
        if openai_api_key:
            env["OPENAI_API_KEY"] = openai_api_key
            env["AUDIO_API_KEY"] = openai_api_key

        # Execute with Node.js
        result = subprocess.run(
            ["node", str(script_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "Unknown error"
            logger.error(f"AI tool execution failed: {error_msg}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"AI tool execution failed: {error_msg[:500]}"
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
        "url": get_file_url(str(file_path), item_type),
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

class FileUploadResponse(BaseModel):
    """Response from file upload"""
    path: str
    filename: str
    size: int


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """
    Upload a file for use with image-to-video or reference images.

    Files are stored in the canvas uploads directory and the path is returned
    for use in subsequent API calls.
    """
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/gif", "image/webp"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid file type: {file.content_type}. Allowed: {allowed_types}"
        )

    ensure_canvas_directories()

    # Generate unique filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    ext = Path(file.filename or "image.png").suffix or ".png"
    filename = f"upload_{timestamp}_{file_id}{ext}"
    file_path = get_uploads_dir() / filename

    # Save file
    try:
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
    except IOError as e:
        logger.error(f"Failed to save uploaded file: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save uploaded file"
        )

    return FileUploadResponse(
        path=str(file_path),
        filename=filename,
        size=len(content)
    )


@router.get("/providers", response_model=ProvidersResponse)
async def get_providers(token: str = Depends(require_auth)):
    """
    Get available AI providers and their capabilities.

    Returns information about supported providers, models, aspect ratios,
    resolutions, and feature support for both image and video generation.
    """
    image_providers = [ProviderInfo(**p) for p in IMAGE_PROVIDERS.values()]
    video_providers = [ProviderInfo(**p) for p in VIDEO_PROVIDERS.values()]

    return ProvidersResponse(
        image_providers=image_providers,
        video_providers=video_providers
    )


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


@router.get("/files/images/{filename}")
async def serve_canvas_image(filename: str, token: str = Depends(require_auth)):
    """Serve a canvas image file"""
    from fastapi.responses import FileResponse

    file_path = get_images_dir() / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(file_path, media_type="image/png")


@router.get("/files/videos/{filename}")
async def serve_canvas_video(filename: str, token: str = Depends(require_auth)):
    """Serve a canvas video file"""
    from fastapi.responses import FileResponse

    file_path = get_videos_dir() / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")

    return FileResponse(file_path, media_type="video/mp4")


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
    style/character consistency (only supported by google-gemini).
    """
    # Validate provider
    if request.provider not in IMAGE_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid provider: {request.provider}. Must be one of: {list(IMAGE_PROVIDERS.keys())}"
        )

    provider_info = IMAGE_PROVIDERS[request.provider]

    # Validate reference images only work with supporting providers
    if request.reference_images and not provider_info.get("supports_reference"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider {request.provider} does not support reference images"
        )

    # Validate aspect ratio
    if request.aspect_ratio not in provider_info["aspect_ratios"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Aspect ratio {request.aspect_ratio} not supported by {request.provider}. Supported: {provider_info['aspect_ratios']}"
        )

    # Validate resolution
    if request.resolution not in provider_info["resolutions"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Resolution {request.resolution} not supported by {request.provider}. Supported: {provider_info['resolutions']}"
        )

    ensure_canvas_directories()

    # Generate unique filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    output_path = get_images_dir() / f"img_{timestamp}_{file_id}.png"

    # Get default model if not specified
    model = request.model
    if not model:
        for m in provider_info["models"]:
            if m.get("default"):
                model = m["id"]
                break

    # Prepare the AI tool script (ESM format)
    # Note: output directory is set via GENERATED_IMAGES_DIR env var in execute_ai_tool
    if request.reference_images:
        # Use generateWithReference for style/character consistency
        ref_images_json = json.dumps(request.reference_images)
        script = f"""
import {{ generateWithReference }} from '/opt/ai-tools/dist/image-generation/generateWithReference.js';

const result = await generateWithReference({{
    prompt: {json.dumps(request.prompt)},
    reference_images: {ref_images_json},
    provider: {json.dumps(request.provider)},
    model: {json.dumps(model)},
    aspect_ratio: {json.dumps(request.aspect_ratio)}
}});
console.log(JSON.stringify(result));
"""
    else:
        # Standard image generation
        script = f"""
import {{ generateImage }} from '/opt/ai-tools/dist/image-generation/generateImage.js';

const result = await generateImage({{
    prompt: {json.dumps(request.prompt)},
    provider: {json.dumps(request.provider)},
    model: {json.dumps(model)},
    aspect_ratio: {json.dumps(request.aspect_ratio)},
    resolution: {json.dumps(request.resolution)}
}});
console.log(JSON.stringify(result));
"""

    # Execute the AI tool (env vars set output directory)
    result = execute_ai_tool(script, item_type="image")

    # Check if generation succeeded
    if not result.get("success", True) or result.get("error"):
        error_msg = result.get("error", "Image generation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )

    # Get the actual output path from result
    actual_path = result.get("file_path") or result.get("outputPath") or str(output_path)

    # Verify the file was actually created
    if not Path(actual_path).exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Image generation completed but file was not created"
        )

    # Create and save the canvas item
    item = create_canvas_item(
        item_type="image",
        prompt=request.prompt,
        provider=request.provider,
        model=model or result.get("model_used"),
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
    # Validate provider
    if request.provider not in VIDEO_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid provider: {request.provider}. Must be one of: {list(VIDEO_PROVIDERS.keys())}"
        )

    provider_info = VIDEO_PROVIDERS[request.provider]

    # Validate duration
    if request.duration > provider_info["max_duration"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Duration {request.duration}s exceeds max for {request.provider} ({provider_info['max_duration']}s)"
        )

    # Validate aspect ratio
    if request.aspect_ratio not in provider_info["aspect_ratios"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Aspect ratio {request.aspect_ratio} not supported by {request.provider}. Supported: {provider_info['aspect_ratios']}"
        )

    ensure_canvas_directories()

    # Generate unique filename
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    output_path = get_videos_dir() / f"vid_{timestamp}_{file_id}.mp4"

    # Get default model if not specified
    model = request.model
    if not model:
        for m in provider_info["models"]:
            if m.get("default"):
                model = m["id"]
                break

    # Prepare the AI tool script (ESM format)
    # Note: output directory is set via GENERATED_VIDEOS_DIR env var in execute_ai_tool
    if request.source_image:
        # Image-to-video generation
        script = f"""
import {{ imageToVideo }} from '/opt/ai-tools/dist/video-generation/imageToVideo.js';

const result = await imageToVideo({{
    prompt: {json.dumps(request.prompt)},
    image_path: {json.dumps(request.source_image)},
    provider: {json.dumps(request.provider)},
    model: {json.dumps(model)},
    aspect_ratio: {json.dumps(request.aspect_ratio)},
    duration: {request.duration}
}});
console.log(JSON.stringify(result));
"""
    else:
        # Standard video generation
        script = f"""
import {{ generateVideo }} from '/opt/ai-tools/dist/video-generation/generateVideo.js';

const result = await generateVideo({{
    prompt: {json.dumps(request.prompt)},
    provider: {json.dumps(request.provider)},
    model: {json.dumps(model)},
    aspect_ratio: {json.dumps(request.aspect_ratio)},
    duration: {request.duration}
}});
console.log(JSON.stringify(result));
"""

    # Execute the AI tool (videos can take longer, env vars set output directory)
    result = execute_ai_tool(script, item_type="video", timeout=600)

    # Check if generation succeeded
    if not result.get("success", True) or result.get("error"):
        error_msg = result.get("error", "Video generation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )

    # Get the actual output path from result
    actual_path = result.get("file_path") or result.get("video_url") or str(output_path)

    # If it's a URL, extract filename
    if actual_path.startswith("/api/"):
        actual_path = result.get("file_path", str(output_path))

    # Verify the file was actually created
    if not Path(actual_path).exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Video generation completed but file was not created"
        )

    # Create and save the canvas item
    item = create_canvas_item(
        item_type="video",
        prompt=request.prompt,
        provider=request.provider,
        model=model or result.get("model_used"),
        file_path=actual_path,
        aspect_ratio=request.aspect_ratio,
        duration=request.duration,
        metadata={
            "source_image": request.source_image,
            "source_video_uri": result.get("source_video_uri"),  # For extending Veo videos
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
    # Validate provider supports editing
    if request.provider not in IMAGE_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid provider: {request.provider}"
        )

    provider_info = IMAGE_PROVIDERS[request.provider]
    if not provider_info.get("supports_edit"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider {request.provider} does not support image editing"
        )

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

    ensure_canvas_directories()

    # Generate unique filename for edited image
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    file_id = uuid.uuid4().hex[:8]
    output_path = get_images_dir() / f"img_{timestamp}_{file_id}_edit.png"

    # Get default model
    model = None
    for m in provider_info["models"]:
        if m.get("default"):
            model = m["id"]
            break

    # Prepare the AI tool script (ESM format)
    # Note: output directory is set via GENERATED_IMAGES_DIR env var in execute_ai_tool
    script = f"""
import {{ editImage }} from '/opt/ai-tools/dist/image-generation/editImage.js';

const result = await editImage({{
    prompt: {json.dumps(request.prompt)},
    image_path: {json.dumps(original_item["file_path"])},
    provider: {json.dumps(request.provider)},
    model: {json.dumps(model)}
}});
console.log(JSON.stringify(result));
"""

    # Execute the AI tool (env vars set output directory)
    result = execute_ai_tool(script, item_type="image")

    # Check if generation succeeded
    if not result.get("success", True) or result.get("error"):
        error_msg = result.get("error", "Image edit failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )

    # Get the actual output path from result
    actual_path = result.get("file_path") or result.get("outputPath") or str(output_path)

    # Verify the file was actually created
    if not Path(actual_path).exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Image edit completed but file was not created"
        )

    # Create and save the canvas item
    item = create_canvas_item(
        item_type="image",
        prompt=request.prompt,
        provider=request.provider,
        model=model or result.get("model_used"),
        file_path=actual_path,
        aspect_ratio=original_item.get("aspect_ratio", "16:9"),
        resolution=original_item.get("resolution"),
        parent_id=request.item_id,
        metadata={
            "original_prompt": original_item.get("prompt"),
            "edit_instruction": request.prompt,
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

    By default, only removes the item from the database.
    Set delete_file=true to also delete the actual media file.
    """
    items = load_canvas_items()
    item_to_delete = None

    for i, item in enumerate(items):
        if item.get("id") == item_id:
            item_to_delete = items.pop(i)
            break

    if not item_to_delete:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Canvas item not found: {item_id}"
        )

    # Optionally delete the file
    if delete_file and item_to_delete.get("file_path"):
        try:
            file_path = Path(item_to_delete["file_path"])
            if file_path.exists():
                file_path.unlink()
                logger.info(f"Deleted canvas file: {file_path}")
        except IOError as e:
            logger.warning(f"Failed to delete canvas file: {e}")

    # Save updated items list
    save_canvas_items(items)


# ============================================================================
# TTS Endpoints
# ============================================================================

@router.get("/files/audio/{filename}")
async def serve_canvas_audio(filename: str, token: str = Depends(require_auth)):
    """Serve a canvas audio file"""
    from fastapi.responses import FileResponse

    file_path = get_audio_dir() / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")

    # Determine content type based on extension
    ext = file_path.suffix.lower()
    mime_types = {
        ".mp3": "audio/mpeg",
        ".opus": "audio/opus",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".wav": "audio/wav",
        ".pcm": "audio/pcm",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }
    mime_type = mime_types.get(ext, "audio/mpeg")

    return FileResponse(file_path, media_type=mime_type)


@router.post("/generate/tts", response_model=TTSGenerateResponse, status_code=status.HTTP_201_CREATED)
async def generate_tts(
    request: TTSGenerateRequest,
    token: str = Depends(require_auth)
):
    """
    Generate text-to-speech audio using AI.

    Converts text to natural-sounding speech using OpenAI TTS models.
    Saves the audio file to {workspace}/canvas/audio/.

    Models:
    - gpt-4o-mini-tts: Steerable TTS with voice instructions support
    - tts-1: Standard quality, optimized for speed
    - tts-1-hd: High quality with better clarity

    Voices: alloy, ash, ballad, coral, echo, fable, onyx, nova, sage, shimmer, verse
    """
    # Validate provider
    if request.provider not in TTS_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid TTS provider: {request.provider}. Available: {list(TTS_PROVIDERS.keys())}"
        )

    provider_info = TTS_PROVIDERS[request.provider]

    # Get model (use default if not specified)
    model = request.model
    if not model:
        for m in provider_info["models"]:
            if m.get("default"):
                model = m["id"]
                break
        if not model:
            model = provider_info["models"][0]["id"]

    # Validate model
    model_ids = [m["id"] for m in provider_info["models"]]
    if model not in model_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid TTS model: {model}. Available: {model_ids}"
        )

    # Validate voice
    voice_ids = [v["id"] for v in provider_info["voices"]]
    if request.voice not in voice_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid voice: {request.voice}. Available: {voice_ids}"
        )

    # Validate output format
    if request.output_format not in provider_info["output_formats"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid output format: {request.output_format}. Available: {provider_info['output_formats']}"
        )

    # Voice instructions only work with gpt-4o-mini-tts
    model_info = next((m for m in provider_info["models"] if m["id"] == model), None)
    if request.voice_instructions and model_info and not model_info.get("supports_instructions"):
        logger.warning(f"Voice instructions ignored for model {model} (only supported by gpt-4o-mini-tts)")

    ensure_canvas_directories()

    # Build the AI tool script
    instructions_param = ""
    if request.voice_instructions and model == "gpt-4o-mini-tts":
        instructions_param = f"instructions: {json.dumps(request.voice_instructions)},"

    # Map frontend provider ID to AI tools provider ID
    # Frontend: openai-tts -> AI tools: openai-audio
    ai_tools_provider = "openai-audio" if request.provider == "openai-tts" else request.provider.replace("-tts", "-audio")

    script = f"""
import {{ textToSpeech }} from '/opt/ai-tools/dist/audio-generation/textToSpeech.js';

const result = await textToSpeech({{
    text: {json.dumps(request.text)},
    provider: {json.dumps(ai_tools_provider)},
    model: {json.dumps(model)},
    voice: {json.dumps(request.voice)},
    speed: {request.speed},
    response_format: {json.dumps(request.output_format)},
    {instructions_param}
}});
console.log(JSON.stringify(result));
"""

    # Set up environment with audio output directory
    env_override = {
        "GENERATED_AUDIO_DIR": str(get_audio_dir()),
    }

    # Execute the AI tool
    result = execute_ai_tool(script, item_type="audio", timeout=120)

    # Check if generation succeeded
    if not result.get("success", True) or result.get("error"):
        error_msg = result.get("error", "TTS generation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )

    # Get the output file path
    file_path = result.get("file_path")
    if not file_path or not Path(file_path).exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="TTS generation completed but audio file was not created"
        )

    # Get file info
    file_path_obj = Path(file_path)
    filename = file_path_obj.name
    file_size = file_path_obj.stat().st_size if file_path_obj.exists() else 0

    # Determine mime type
    ext = file_path_obj.suffix.lower().lstrip(".")
    mime_types = {
        "mp3": "audio/mpeg",
        "opus": "audio/opus",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "pcm": "audio/pcm",
    }
    mime_type = mime_types.get(ext, "audio/mpeg")

    now = datetime.utcnow().isoformat() + "Z"
    item_id = str(uuid.uuid4())

    return TTSGenerateResponse(
        id=item_id,
        url=f"/api/v1/canvas/files/audio/{filename}",
        file_path=str(file_path),
        filename=filename,
        duration=None,  # Would need to parse audio file to get duration
        mime_type=mime_type,
        text_length=len(request.text),
        provider=request.provider,
        model=model,
        voice=request.voice,
        created_at=now
    )


@router.get("/tts/providers")
async def get_tts_providers(token: str = Depends(require_auth)):
    """
    Get available TTS providers, models, and voices.

    Returns provider information including supported models, voices, and output formats.
    """
    openai_key = _get_decrypted_api_key("openai_api_key")

    providers = []
    for provider_id, provider_info in TTS_PROVIDERS.items():
        is_available = bool(openai_key) if provider_id == "openai-tts" else False

        providers.append({
            "id": provider_id,
            "name": provider_info["name"],
            "description": provider_info["description"],
            "available": is_available,
            "models": provider_info["models"],
            "voices": provider_info["voices"],
            "output_formats": provider_info["output_formats"],
        })

    return {
        "providers": providers,
        "openai_configured": bool(openai_key)
    }


# ============================================================================
# STT Endpoints
# ============================================================================

@router.post("/transcribe", response_model=STTTranscribeResponse, status_code=status.HTTP_201_CREATED)
async def transcribe_audio(
    request: STTTranscribeRequest = None,
    file: UploadFile = File(None),
    token: str = Depends(require_auth)
):
    """
    Transcribe audio to text using AI.

    Accepts either:
    - audio_url in request body (path to existing audio file)
    - file upload via multipart form

    Models:
    - gpt-4o-transcribe: Best quality transcription
    - gpt-4o-mini-transcribe: Fast, affordable transcription
    - whisper-1: Original Whisper model

    Returns the transcribed text along with optional timestamps and segments.
    """
    # Handle the case where request is None (form data upload)
    if request is None:
        request = STTTranscribeRequest()

    # Validate provider
    if request.provider not in STT_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid STT provider: {request.provider}. Available: {list(STT_PROVIDERS.keys())}"
        )

    provider_info = STT_PROVIDERS[request.provider]

    # Get model (use default if not specified)
    model = request.model
    if not model:
        for m in provider_info["models"]:
            if m.get("default"):
                model = m["id"]
                break
        if not model:
            model = provider_info["models"][0]["id"]

    # Validate model
    model_ids = [m["id"] for m in provider_info["models"]]
    if model not in model_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid STT model: {model}. Available: {model_ids}"
        )

    # Determine audio source
    audio_path = None

    if file and file.filename:
        # Handle file upload
        ensure_canvas_directories()

        # Validate file type
        allowed_types = ["audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a",
                        "audio/wav", "audio/webm", "audio/x-wav", "video/mp4",
                        "video/webm", "audio/ogg", "audio/flac"]
        content_type = file.content_type or ""

        # Check by extension
        filename = file.filename or ""
        allowed_extensions = [".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".flac"]
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        if content_type not in allowed_types and ext not in allowed_extensions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported audio format: {content_type or ext}. Supported: {provider_info['supported_formats']}"
            )

        # Save uploaded file temporarily
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        file_id = uuid.uuid4().hex[:8]
        upload_filename = f"stt_upload_{timestamp}_{file_id}{ext or '.webm'}"
        audio_path = get_uploads_dir() / upload_filename

        try:
            content = await file.read()
            if len(content) == 0:
                raise HTTPException(status_code=400, detail="Audio file is empty")
            if len(content) > 25 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="Audio file too large. Maximum 25MB.")

            with open(audio_path, "wb") as f:
                f.write(content)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to save uploaded audio: {str(e)}")

    elif request.audio_url:
        # Use provided audio path
        audio_path = Path(request.audio_url)
        if not audio_path.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Audio file not found: {request.audio_url}"
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either audio_url or file upload is required"
        )

    # Build timestamp granularities
    timestamp_granularities = []
    if request.timestamp_granularity:
        if request.timestamp_granularity in ["word", "segment"]:
            timestamp_granularities.append(request.timestamp_granularity)

    # Build the AI tool script
    timestamp_param = ""
    if timestamp_granularities:
        timestamp_param = f"timestamp_granularities: {json.dumps(timestamp_granularities)},"

    language_param = ""
    if request.language:
        language_param = f"language: {json.dumps(request.language)},"

    # Map frontend provider ID to AI tools provider ID
    # Frontend: openai-stt -> AI tools: openai-audio
    ai_tools_provider = "openai-audio" if request.provider == "openai-stt" else request.provider.replace("-stt", "-audio")

    script = f"""
import {{ speechToText }} from '/opt/ai-tools/dist/audio-generation/speechToText.js';

const result = await speechToText({{
    audio_path: {json.dumps(str(audio_path))},
    provider: {json.dumps(ai_tools_provider)},
    model: {json.dumps(model)},
    {language_param}
    {timestamp_param}
    response_format: "verbose_json"
}});
console.log(JSON.stringify(result));
"""

    # Execute the AI tool
    result = execute_ai_tool(script, item_type="audio", timeout=300)

    # Check if transcription succeeded
    if not result.get("success", True) or result.get("error"):
        error_msg = result.get("error", "Transcription failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_msg
        )

    # Build response
    now = datetime.utcnow().isoformat() + "Z"
    item_id = str(uuid.uuid4())

    # Parse segments if available
    segments = None
    if result.get("segments"):
        segments = [
            TranscriptSegment(
                id=seg.get("id", i),
                text=seg.get("text", ""),
                start=seg.get("start", 0),
                end=seg.get("end", 0),
                speaker=None  # Diarization not yet supported
            )
            for i, seg in enumerate(result.get("segments", []))
        ]

    return STTTranscribeResponse(
        id=item_id,
        transcript=result.get("text", ""),
        duration=result.get("duration_seconds"),
        language=result.get("language"),
        speakers=None,  # Diarization not yet supported
        segments=segments,
        words=result.get("words"),
        provider=request.provider,
        model=model,
        created_at=now
    )


@router.get("/stt/providers")
async def get_stt_providers(token: str = Depends(require_auth)):
    """
    Get available STT providers and models.

    Returns provider information including supported models and audio formats.
    """
    openai_key = _get_decrypted_api_key("openai_api_key")

    providers = []
    for provider_id, provider_info in STT_PROVIDERS.items():
        is_available = bool(openai_key) if provider_id == "openai-stt" else False

        providers.append({
            "id": provider_id,
            "name": provider_info["name"],
            "description": provider_info["description"],
            "available": is_available,
            "models": provider_info["models"],
            "supported_formats": provider_info["supported_formats"],
        })

    return {
        "providers": providers,
        "openai_configured": bool(openai_key)
    }
