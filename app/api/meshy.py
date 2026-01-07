"""
Meshy 3D Model Generation API routes.

Handles:
- Webhook callbacks from Meshy API for task status updates
- 3D generation task tracking
- Meshy API key configuration
"""

import logging
import hmac
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, HTTPException, Request, Depends, status
from pydantic import BaseModel, Field

from app.db import database
from app.api.auth import require_admin, require_auth
from app.api.settings import get_decrypted_api_key, set_encrypted_api_key, mask_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/meshy", tags=["Meshy 3D"])


# ============================================================================
# In-Memory Task Storage (MVP - could be moved to database later)
# ============================================================================

# Store 3D generation tasks: task_id -> task_data
meshy_tasks_db: Dict[str, Dict[str, Any]] = {}


# ============================================================================
# Request/Response Models
# ============================================================================

class MeshyWebhookPayload(BaseModel):
    """
    Webhook payload from Meshy API.

    Meshy sends webhook callbacks when task status changes.
    See: https://docs.meshy.ai/api-text-to-3d#webhook
    """
    id: str = Field(..., description="Task ID")
    status: str = Field(..., description="Task status: PENDING, IN_PROGRESS, SUCCEEDED, FAILED, EXPIRED")
    # Optional fields that may be present on completion
    model_urls: Optional[Dict[str, str]] = Field(None, description="URLs for generated model files")
    thumbnail_url: Optional[str] = Field(None, description="Thumbnail preview URL")
    video_url: Optional[str] = Field(None, description="360° preview video URL")
    texture_urls: Optional[List[Dict[str, str]]] = Field(None, description="Texture file URLs")
    task_error: Optional[Dict[str, Any]] = Field(None, description="Error details if failed")
    # Metadata
    created_at: Optional[int] = Field(None, description="Unix timestamp of creation")
    started_at: Optional[int] = Field(None, description="Unix timestamp when processing started")
    finished_at: Optional[int] = Field(None, description="Unix timestamp when completed")
    # Task type info
    mode: Optional[str] = Field(None, description="Generation mode: preview or refine")
    art_style: Optional[str] = Field(None, description="Art style used")
    prompt: Optional[str] = Field(None, description="Original prompt")


class TaskResponse(BaseModel):
    """Response containing task details"""
    id: str
    type: str  # text-to-3d, image-to-3d, retexture, rig, animate
    status: str  # pending, in_progress, succeeded, failed
    prompt: Optional[str] = None
    model_urls: Optional[Dict[str, str]] = None
    thumbnail_url: Optional[str] = None
    video_url: Optional[str] = None
    progress: float = 0.0
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


class TaskListResponse(BaseModel):
    """List of tasks response"""
    tasks: List[TaskResponse]
    total: int


class MeshyKeyRequest(BaseModel):
    """Request to set Meshy API key"""
    api_key: str


class MeshyKeyResponse(BaseModel):
    """Response after setting Meshy API key"""
    success: bool
    masked_key: str


# ============================================================================
# Webhook Endpoint
# ============================================================================

@router.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_meshy_webhook(request: Request):
    """
    Receive webhook callbacks from Meshy API.

    Meshy sends POST requests to this endpoint when task status changes.
    The webhook URL should be configured when creating tasks via the
    `webhook_url` parameter in the Meshy API request.

    Expected webhook URL format: https://your-domain.com/api/v1/meshy/webhook

    Security: Meshy includes a signature header that can be verified
    using your API key. For MVP, we accept all valid payloads but
    log warnings for signature verification failures.
    """
    try:
        # Parse the webhook payload
        body = await request.json()
        payload = MeshyWebhookPayload(**body)

        logger.info(f"Received Meshy webhook for task {payload.id}: status={payload.status}")

        # Optional: Verify webhook signature
        # Meshy may include X-Meshy-Signature header
        signature = request.headers.get("X-Meshy-Signature")
        if signature:
            # Get our API key for verification
            api_key = get_decrypted_api_key("meshy_api_key")
            if api_key:
                # Verify HMAC signature
                body_bytes = await request.body()
                expected_sig = hmac.new(
                    api_key.encode(),
                    body_bytes,
                    hashlib.sha256
                ).hexdigest()

                if not hmac.compare_digest(signature, expected_sig):
                    logger.warning(f"Webhook signature mismatch for task {payload.id}")
                    # Still process it for MVP, but log the warning

        # Update or create task in our storage
        task_id = payload.id

        # Map Meshy status to our internal status
        status_map = {
            "PENDING": "pending",
            "IN_PROGRESS": "in_progress",
            "SUCCEEDED": "succeeded",
            "FAILED": "failed",
            "EXPIRED": "failed"
        }
        internal_status = status_map.get(payload.status, payload.status.lower())

        # Get existing task or create minimal record
        existing_task = meshy_tasks_db.get(task_id, {})

        # Update task data
        updated_task = {
            **existing_task,
            "id": task_id,
            "status": internal_status,
            "meshy_status": payload.status,
            "updated_at": datetime.utcnow(),
        }

        # Add completion data if available
        if payload.model_urls:
            updated_task["model_urls"] = payload.model_urls
        if payload.thumbnail_url:
            updated_task["thumbnail_url"] = payload.thumbnail_url
        if payload.video_url:
            updated_task["video_url"] = payload.video_url
        if payload.texture_urls:
            updated_task["texture_urls"] = payload.texture_urls
        if payload.prompt:
            updated_task["prompt"] = payload.prompt
        if payload.art_style:
            updated_task["art_style"] = payload.art_style
        if payload.mode:
            updated_task["mode"] = payload.mode

        # Handle timestamps
        if payload.finished_at:
            updated_task["completed_at"] = datetime.fromtimestamp(payload.finished_at / 1000)
        if payload.created_at and "created_at" not in existing_task:
            updated_task["created_at"] = datetime.fromtimestamp(payload.created_at / 1000)

        # Handle errors
        if payload.task_error:
            updated_task["error"] = payload.task_error.get("message", str(payload.task_error))

        # Calculate progress
        if internal_status == "succeeded":
            updated_task["progress"] = 100.0
        elif internal_status == "in_progress":
            updated_task["progress"] = 50.0  # Approximate
        elif internal_status == "failed":
            updated_task["progress"] = 0.0

        # Store updated task
        meshy_tasks_db[task_id] = updated_task

        logger.info(f"Updated task {task_id}: {internal_status}")

        # Return 200 OK to acknowledge receipt
        return {"received": True, "task_id": task_id, "status": internal_status}

    except Exception as e:
        logger.error(f"Error processing Meshy webhook: {e}")
        # Return 200 anyway to prevent Meshy from retrying
        # Log the error for debugging
        return {"received": True, "error": str(e)}


# ============================================================================
# Task Management Endpoints
# ============================================================================

@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    status_filter: Optional[str] = None,
    type_filter: Optional[str] = None,
    limit: int = 50,
    token: str = Depends(require_auth)
):
    """
    List all 3D generation tasks.

    Optionally filter by status (pending, in_progress, succeeded, failed)
    or type (text-to-3d, image-to-3d, retexture, rig, animate).
    """
    tasks = list(meshy_tasks_db.values())

    # Apply filters
    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if type_filter:
        tasks = [t for t in tasks if t.get("type") == type_filter]

    # Sort by created_at descending
    tasks.sort(key=lambda x: x.get("created_at", datetime.min), reverse=True)

    # Apply limit
    tasks = tasks[:limit]

    # Convert to response format
    response_tasks = []
    for t in tasks:
        response_tasks.append(TaskResponse(
            id=t["id"],
            type=t.get("type", "text-to-3d"),
            status=t.get("status", "pending"),
            prompt=t.get("prompt"),
            model_urls=t.get("model_urls"),
            thumbnail_url=t.get("thumbnail_url"),
            video_url=t.get("video_url"),
            progress=t.get("progress", 0.0),
            created_at=t.get("created_at", datetime.utcnow()),
            completed_at=t.get("completed_at"),
            error=t.get("error")
        ))

    return TaskListResponse(tasks=response_tasks, total=len(meshy_tasks_db))


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, token: str = Depends(require_auth)):
    """Get details of a specific task"""
    if task_id not in meshy_tasks_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found: {task_id}"
        )

    t = meshy_tasks_db[task_id]
    return TaskResponse(
        id=t["id"],
        type=t.get("type", "text-to-3d"),
        status=t.get("status", "pending"),
        prompt=t.get("prompt"),
        model_urls=t.get("model_urls"),
        thumbnail_url=t.get("thumbnail_url"),
        video_url=t.get("video_url"),
        progress=t.get("progress", 0.0),
        created_at=t.get("created_at", datetime.utcnow()),
        completed_at=t.get("completed_at"),
        error=t.get("error")
    )


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: str, token: str = Depends(require_auth)):
    """Delete a task record"""
    if task_id not in meshy_tasks_db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task not found: {task_id}"
        )

    del meshy_tasks_db[task_id]
    logger.info(f"Deleted task {task_id}")


# ============================================================================
# Task Registration (called by AI tools when starting generation)
# ============================================================================

@router.post("/tasks/register")
async def register_task(
    task_id: str,
    task_type: str,
    prompt: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
):
    """
    Register a new task that was submitted to Meshy.

    This is called by the AI tools when they start a 3D generation.
    It creates a local record so we can track the task before
    the webhook arrives.

    Internal endpoint - no auth required (called from Node.js tools).
    """
    now = datetime.utcnow()

    task = {
        "id": task_id,
        "type": task_type,
        "status": "pending",
        "prompt": prompt,
        "progress": 0.0,
        "created_at": now,
        "metadata": metadata or {}
    }

    meshy_tasks_db[task_id] = task
    logger.info(f"Registered task {task_id}: type={task_type}")

    return {"registered": True, "task_id": task_id}


# ============================================================================
# Meshy API Key Configuration
# ============================================================================

@router.get("/config")
async def get_meshy_config(token: str = Depends(require_auth)):
    """
    Get Meshy configuration status.

    Returns whether the API key is configured (without exposing it).
    """
    api_key = get_decrypted_api_key("meshy_api_key")

    return {
        "api_key_set": bool(api_key),
        "api_key_masked": mask_api_key(api_key) if api_key else "",
        "webhook_url": "/api/v1/meshy/webhook"  # Relative URL for display
    }


@router.post("/config/api-key", response_model=MeshyKeyResponse)
async def set_meshy_api_key(
    request: MeshyKeyRequest,
    token: str = Depends(require_admin)
):
    """
    Set the Meshy API key (admin only).

    Get your API key from: https://app.meshy.ai/settings/api
    """
    api_key = request.api_key.strip()

    if not api_key:
        raise HTTPException(status_code=400, detail="API key cannot be empty")

    # Meshy API keys start with "msy_"
    if not api_key.startswith("msy_"):
        raise HTTPException(
            status_code=400,
            detail="Invalid API key format. Meshy API keys start with 'msy_'"
        )

    # Save to database (encrypted)
    set_encrypted_api_key("meshy_api_key", api_key)

    logger.info("Meshy API key configured")

    return MeshyKeyResponse(
        success=True,
        masked_key=mask_api_key(api_key)
    )


@router.delete("/config/api-key")
async def remove_meshy_api_key(token: str = Depends(require_admin)):
    """
    Remove the Meshy API key (admin only).
    """
    database.delete_system_setting("meshy_api_key")
    logger.info("Meshy API key removed")
    return {"success": True}


class Model3DSettingsRequest(BaseModel):
    """Request to update 3D model settings"""
    model: Optional[str] = None  # latest, meshy-5, meshy-4
    provider: Optional[str] = None  # meshy


@router.put("/config/settings")
async def update_meshy_settings(
    request: Model3DSettingsRequest,
    token: str = Depends(require_admin)
):
    """
    Update 3D model generation settings (admin only).

    Allows setting the default model and provider for 3D generation.
    """
    if request.model:
        # Valid models: latest (Meshy 6 Preview), meshy-5, meshy-4
        valid_models = ["latest", "meshy-5", "meshy-4"]
        if request.model not in valid_models:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid model. Valid options: {valid_models}"
            )
        database.set_system_setting("model3d_model", request.model)
        logger.info(f"3D model set to: {request.model}")

    if request.provider:
        valid_providers = ["meshy"]
        if request.provider not in valid_providers:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid provider. Valid options: {valid_providers}"
            )
        database.set_system_setting("model3d_provider", request.provider)
        logger.info(f"3D provider set to: {request.provider}")

    return {
        "success": True,
        "model": request.model or database.get_system_setting("model3d_model"),
        "provider": request.provider or database.get_system_setting("model3d_provider")
    }


# ============================================================================
# Internal Endpoint for AI Tools
# ============================================================================

@router.get("/internal/api-key")
async def get_internal_meshy_key():
    """
    Internal endpoint for AI tools to get the decrypted Meshy API key.

    Used by Node.js 3D generation tools.
    """
    api_key = get_decrypted_api_key("meshy_api_key")

    if not api_key:
        raise HTTPException(
            status_code=404,
            detail="Meshy API key not configured. Go to Settings to add your key."
        )

    return {"key": api_key}
