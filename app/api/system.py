"""
System API routes - health, version, stats
"""

import subprocess
from typing import Optional

from fastapi import APIRouter, Depends

from app.core.models import HealthResponse, VersionResponse, StatsResponse
from app.core.auth import auth_service
from app.core.config import settings
from app.db import database
from app.api.auth import require_auth

router = APIRouter(tags=["System"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint (no auth required)"""
    return {
        "status": "healthy",
        "service": settings.service_name,
        "version": settings.version,
        "authenticated": False,  # Not checking session for health
        "setup_required": auth_service.is_setup_required(),
        "claude_authenticated": auth_service.is_claude_authenticated()
    }


@router.get("/", response_model=HealthResponse)
async def root():
    """Root endpoint (same as health)"""
    return await health_check()


@router.get("/api/v1/health", response_model=HealthResponse)
async def api_health_check():
    """API health check endpoint"""
    return await health_check()


@router.get("/api/v1/version", response_model=VersionResponse)
async def get_version():
    """Get API and Claude Code versions"""
    claude_version = None

    try:
        result = subprocess.run(
            ['claude', '--version'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            claude_version = result.stdout.strip()
    except Exception:
        pass

    return {
        "api_version": settings.version,
        "claude_version": claude_version
    }


@router.get("/api/v1/stats", response_model=StatsResponse)
async def get_stats(token: str = Depends(require_auth)):
    """Get usage statistics"""
    stats = database.get_usage_stats()
    return stats
