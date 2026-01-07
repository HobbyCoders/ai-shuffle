"""
Rate limits API endpoints.

Provides endpoints for viewing rate limit status and managing rate limit configurations.
"""

import hashlib
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.db import database
from app.core.rate_limiter import rate_limiter
from app.core.queue_manager import request_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rate-limits", tags=["Rate Limits"])


# =============================================================================
# Pydantic Models
# =============================================================================

class RateLimitConfigModel(BaseModel):
    """Rate limit configuration"""
    id: str
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None
    requests_per_minute: int = 20
    requests_per_hour: int = 200
    requests_per_day: int = 1000
    concurrent_requests: int = 3
    priority: int = 0
    is_unlimited: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RateLimitConfigCreate(BaseModel):
    """Create a rate limit configuration"""
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None
    requests_per_minute: int = Field(default=20, ge=1, le=10000)
    requests_per_hour: int = Field(default=200, ge=1, le=100000)
    requests_per_day: int = Field(default=1000, ge=1, le=1000000)
    concurrent_requests: int = Field(default=3, ge=1, le=100)
    priority: int = Field(default=0, ge=-100, le=100)
    is_unlimited: bool = False


class RateLimitConfigUpdate(BaseModel):
    """Update a rate limit configuration"""
    requests_per_minute: Optional[int] = Field(None, ge=1, le=10000)
    requests_per_hour: Optional[int] = Field(None, ge=1, le=100000)
    requests_per_day: Optional[int] = Field(None, ge=1, le=1000000)
    concurrent_requests: Optional[int] = Field(None, ge=1, le=100)
    priority: Optional[int] = Field(None, ge=-100, le=100)
    is_unlimited: Optional[bool] = None


class RateLimitStatusResponse(BaseModel):
    """Current rate limit status for a user"""
    minute_count: int
    minute_limit: int
    minute_remaining: int
    minute_reset: datetime
    hour_count: int
    hour_limit: int
    hour_remaining: int
    hour_reset: datetime
    day_count: int
    day_limit: int
    day_remaining: int
    day_reset: datetime
    concurrent_count: int
    concurrent_limit: int
    concurrent_remaining: int
    is_limited: bool
    is_unlimited: bool
    priority: int


class QueueStatusResponse(BaseModel):
    """Queue status for a user"""
    position: int
    estimated_wait_seconds: int
    total_queued: int
    is_queued: bool


# =============================================================================
# Authentication Helpers
# =============================================================================

async def get_current_user(
    session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None)
) -> tuple[Optional[str], Optional[str], bool]:
    """
    Get current user from session or API key.

    Returns:
        Tuple of (user_id, api_key_id, is_admin)
    """
    user_id = None
    api_key_id = None
    is_admin = False

    # Check session cookie
    if session:
        auth_session = database.get_auth_session(session)
        if auth_session:
            is_admin = True
            user_id = "admin"

    # Check Authorization header
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        if token.startswith("aih_"):
            # API key
            key_hash = hashlib.sha256(token.encode()).hexdigest()
            api_user = database.get_api_user_by_key_hash(key_hash)
            if api_user:
                api_key_id = api_user["id"]
                user_id = api_user.get("username") or api_user["name"]
        else:
            # API key session
            api_session = database.get_api_key_session(token)
            if api_session:
                api_user = database.get_api_user(api_session["api_user_id"])
                if api_user:
                    api_key_id = api_user["id"]
                    user_id = api_user.get("username") or api_user["name"]

    return user_id, api_key_id, is_admin


async def require_admin(
    session: Optional[str] = Cookie(None)
) -> bool:
    """Require admin authentication"""
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )

    auth_session = database.get_auth_session(session)
    if not auth_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session"
        )

    return True


# =============================================================================
# User Endpoints
# =============================================================================

@router.get("", response_model=RateLimitStatusResponse)
async def get_current_rate_limits(
    session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None)
):
    """
    Get current user's rate limit status and usage.

    Returns the current request counts, limits, and remaining allowances.
    """
    user_id, api_key_id, is_admin = await get_current_user(session, authorization)

    if not user_id and not api_key_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )

    # Get configuration
    config = rate_limiter.get_limit_config(user_id, api_key_id)

    # Get current status
    rate_status = rate_limiter.get_rate_limit_status(user_id, api_key_id, is_admin)

    return RateLimitStatusResponse(
        minute_count=rate_status.minute_count,
        minute_limit=config.requests_per_minute,
        minute_remaining=rate_status.minute_remaining,
        minute_reset=rate_status.minute_reset,
        hour_count=rate_status.hour_count,
        hour_limit=config.requests_per_hour,
        hour_remaining=rate_status.hour_remaining,
        hour_reset=rate_status.hour_reset,
        day_count=rate_status.day_count,
        day_limit=config.requests_per_day,
        day_remaining=rate_status.day_remaining,
        day_reset=rate_status.day_reset,
        concurrent_count=rate_status.concurrent_count,
        concurrent_limit=config.concurrent_requests,
        concurrent_remaining=rate_status.concurrent_remaining,
        is_limited=rate_status.is_limited,
        is_unlimited=config.is_unlimited,
        priority=config.priority
    )


@router.get("/queue", response_model=QueueStatusResponse)
async def get_queue_status(
    session: Optional[str] = Cookie(None),
    authorization: Optional[str] = Header(None)
):
    """
    Get current user's position in the request queue.

    If the user has a request queued, returns their position and estimated wait time.
    """
    user_id, api_key_id, is_admin = await get_current_user(session, authorization)

    if not user_id and not api_key_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )

    queue_status = await request_queue.get_position(user_id, api_key_id)

    return QueueStatusResponse(
        position=queue_status.position,
        estimated_wait_seconds=queue_status.estimated_wait_seconds,
        total_queued=queue_status.total_queued,
        is_queued=queue_status.is_queued
    )


# =============================================================================
# Admin Endpoints
# =============================================================================

@router.get("/config", response_model=List[RateLimitConfigModel])
async def list_rate_limit_configs(
    _: bool = Depends(require_admin)
):
    """
    List all rate limit configurations.

    Admin only. Returns all configured rate limits.
    """
    configs = database.get_all_rate_limits()
    return [RateLimitConfigModel(**config) for config in configs]


@router.post("/config", response_model=RateLimitConfigModel, status_code=status.HTTP_201_CREATED)
async def create_rate_limit_config(
    config: RateLimitConfigCreate,
    _: bool = Depends(require_admin)
):
    """
    Create a new rate limit configuration.

    Admin only. Creates a rate limit configuration for a user or API key.
    If both user_id and api_key_id are null, creates a default configuration.
    """
    # Check for existing config
    existing = database.get_rate_limit_for_user(config.user_id, config.api_key_id)
    if existing:
        # Check if it's an exact match (same user_id and api_key_id)
        if existing.get("user_id") == config.user_id and existing.get("api_key_id") == config.api_key_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Rate limit configuration already exists for this user/API key"
            )

    # Generate ID
    rate_limit_id = str(uuid.uuid4())

    # Create config
    created = database.create_rate_limit(
        rate_limit_id=rate_limit_id,
        user_id=config.user_id,
        api_key_id=config.api_key_id,
        requests_per_minute=config.requests_per_minute,
        requests_per_hour=config.requests_per_hour,
        requests_per_day=config.requests_per_day,
        concurrent_requests=config.concurrent_requests,
        priority=config.priority,
        is_unlimited=config.is_unlimited
    )

    # Clear cache
    rate_limiter.clear_cache()

    logger.info(f"Created rate limit config {rate_limit_id} for user={config.user_id}, api_key={config.api_key_id}")
    return RateLimitConfigModel(**created)


@router.patch("/config/{config_id}", response_model=RateLimitConfigModel)
async def update_rate_limit_config(
    config_id: str,
    updates: RateLimitConfigUpdate,
    _: bool = Depends(require_admin)
):
    """
    Update a rate limit configuration.

    Admin only. Updates the specified rate limit configuration.
    """
    existing = database.get_rate_limit(config_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rate limit configuration not found"
        )

    updated = database.update_rate_limit(
        rate_limit_id=config_id,
        requests_per_minute=updates.requests_per_minute,
        requests_per_hour=updates.requests_per_hour,
        requests_per_day=updates.requests_per_day,
        concurrent_requests=updates.concurrent_requests,
        priority=updates.priority,
        is_unlimited=updates.is_unlimited
    )

    # Clear cache
    rate_limiter.clear_cache()

    logger.info(f"Updated rate limit config {config_id}")
    return RateLimitConfigModel(**updated)


@router.delete("/config/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rate_limit_config(
    config_id: str,
    _: bool = Depends(require_admin)
):
    """
    Delete a rate limit configuration.

    Admin only. Deletes the specified rate limit configuration.
    Users will fall back to default limits.
    """
    existing = database.get_rate_limit(config_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rate limit configuration not found"
        )

    database.delete_rate_limit(config_id)

    # Clear cache
    rate_limiter.clear_cache()

    logger.info(f"Deleted rate limit config {config_id}")
    return None


@router.get("/config/defaults", response_model=RateLimitConfigCreate)
async def get_default_limits(
    _: bool = Depends(require_admin)
):
    """
    Get the default rate limit values.

    Admin only. Returns the default limits that apply when no specific configuration exists.
    """
    return RateLimitConfigCreate(
        requests_per_minute=20,
        requests_per_hour=200,
        requests_per_day=1000,
        concurrent_requests=3,
        priority=0,
        is_unlimited=False
    )


@router.get("/queue/admin", response_model=dict)
async def get_queue_admin_info(
    _: bool = Depends(require_admin)
):
    """
    Get queue statistics for admin.

    Admin only. Returns overall queue statistics.
    """
    queue_size = await request_queue.get_queue_size()

    return {
        "queue_size": queue_size,
        "max_size": request_queue._max_size,
        "process_time_estimate": request_queue._process_time_estimate
    }


@router.post("/queue/clear", status_code=status.HTTP_204_NO_CONTENT)
async def clear_queue(
    _: bool = Depends(require_admin)
):
    """
    Clear all requests from the queue.

    Admin only. Use with caution.
    """
    count = await request_queue.clear()
    logger.warning(f"Admin cleared request queue ({count} requests)")
    return None
