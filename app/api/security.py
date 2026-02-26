"""
Security API routes for audit logging
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends, status

from app.core.models import (
    AuditLogResponse,
    AuditLogEntry
)
from app.core import audit_service
from app.api.auth import require_admin
from app.db import database as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/security", tags=["Security"])


# ============================================================================
# Audit Log
# ============================================================================

@router.get("/audit-log", response_model=AuditLogResponse)
async def get_audit_log(
    request: Request,
    event_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    token: str = Depends(require_admin)
):
    """
    Get audit log entries (admin only).

    Optional filters:
        - event_type: Filter by specific event type
        - limit: Maximum entries to return (default 50, max 500)
        - offset: Pagination offset
    """
    # Cap limit at 500
    limit = min(limit, 500)

    # Get logs
    entries = db.get_audit_logs(
        event_type=event_type,
        limit=limit,
        offset=offset
    )

    # Get total count
    total = db.get_audit_log_count(event_type=event_type)

    # Convert to response model
    audit_entries = [
        AuditLogEntry(
            id=entry["id"],
            user_id=entry.get("user_id"),
            user_type=entry.get("user_type", "admin"),
            event_type=entry["event_type"],
            ip_address=entry.get("ip_address"),
            user_agent=entry.get("user_agent"),
            details=entry.get("details"),
            created_at=entry["created_at"]
        )
        for entry in entries
    ]

    return AuditLogResponse(
        entries=audit_entries,
        total=total,
        limit=limit,
        offset=offset
    )
