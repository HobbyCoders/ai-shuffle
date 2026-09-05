"""
Notifications API — the user-facing notification history (the bell).

User-scoped CRUD over ``app/db/notifications.py``, backing the frontend
NotificationCenter and the unified notification store:

- ``GET    /api/v1/notifications``         list recent + unread count (hydration)
- ``POST   /api/v1/notifications``         create a client-originated persisted one
- ``POST   /api/v1/notifications/{id}/read``
- ``POST   /api/v1/notifications/read-all``
- ``POST   /api/v1/notifications/{id}/dismiss``
- ``DELETE /api/v1/notifications``         clear the user's history

Auth is **user-scoped** (``require_auth``), not admin-only: every authenticated
caller manages their own notifications. ``user_id`` is derived exactly as the
WebSocket broadcast path derives it (``api_user["id"]`` or ``"admin"``) so the
server emit funnel and this REST path write/read the same rows.
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.db import notifications as notifications_db
from app.api.auth import require_auth, get_api_user_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])


# =============================================================================
# Pydantic models
# =============================================================================

class NotificationCreate(BaseModel):
    """A client-originated notification to persist (mirrors the store's POST)."""
    id: Optional[str] = None
    severity: str = Field(default="info")
    surface: Literal["notification", "banner", "toast", "inline"] = Field(
        default="notification"
    )
    category: Optional[str] = None
    title: Optional[str] = None
    message: str = ""
    session_id: Optional[str] = None
    action: Optional[Dict[str, Any]] = None


# =============================================================================
# Auth helper — resolve user_id the same way the WS broadcast path does.
# =============================================================================

def current_user_id(request: Request, _token: str = Depends(require_auth)) -> str:
    """Return the caller's user_id (``api_user["id"]`` or ``"admin"``).

    Mirrors ``app/api/websocket.py`` so notifications written by the server emit
    funnel (keyed on the same user_id) hydrate for the same caller here.
    """
    api_user = get_api_user_from_request(request)
    return notifications_db.NotificationOwner(
        api_user["id"] if api_user else "admin",
        "api_user" if api_user else "admin",
    )


# =============================================================================
# Endpoints
# =============================================================================

@router.get("")
async def list_notifications(
    include_dismissed: bool = False,
    limit: int = 100,
    user_id: str = Depends(current_user_id),
):
    """List the caller's recent notifications (newest first) + unread count.

    Used by the frontend to hydrate the bell on load.
    """
    items = notifications_db.list_notifications(
        user_id, limit=limit, include_dismissed=include_dismissed
    )
    return {
        "notifications": items,
        "unread": notifications_db.unread_count(user_id),
    }


@router.post("")
async def create_notification(
    body: NotificationCreate,
    user_id: str = Depends(current_user_id),
):
    """Persist a client-originated notification so it survives reload / syncs.

    The frontend only POSTs notifications it wants in history (``persist: true``).
    Idempotent on ``id`` so an optimistic local add + this POST never duplicate.
    """
    created = notifications_db.create_notification(
        user_id,
        severity=body.severity,
        message=body.message,
        surface=body.surface,
        category=body.category,
        source="client",
        title=body.title,
        session_id=body.session_id,
        action_kind=body.action.get("kind") if body.action else None,
        action_payload=body.action.get("payload") if body.action else None,
        notification_id=body.id,
    )
    return created


@router.post("/read-all")
async def mark_all_read(user_id: str = Depends(current_user_id)):
    """Mark every one of the caller's notifications read."""
    changed = notifications_db.mark_all_read(user_id)
    return {"updated": changed}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, user_id: str = Depends(current_user_id)):
    """Mark a single notification read."""
    ok = notifications_db.mark_read(user_id, notification_id)
    return {"ok": ok}


@router.post("/{notification_id}/dismiss")
async def dismiss(notification_id: str, user_id: str = Depends(current_user_id)):
    """Dismiss (soft-hide) a single notification."""
    ok = notifications_db.dismiss(user_id, notification_id)
    return {"ok": ok}


@router.delete("")
async def clear_all(user_id: str = Depends(current_user_id)):
    """Clear the caller's entire notification history."""
    deleted = notifications_db.clear_all(user_id)
    return {"deleted": deleted}
