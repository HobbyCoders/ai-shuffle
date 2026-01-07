"""
Webhooks management API routes
"""

import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Depends, status

from app.core.models import Webhook, WebhookCreate, WebhookUpdate, WebhookTestResponse
from app.db import database
from app.api.auth import require_admin

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


# Valid event types
VALID_EVENT_TYPES = [
    "session.complete",
    "session.error",
    "session.started",
]


@router.get("", response_model=List[Webhook])
async def list_webhooks(token: str = Depends(require_admin)):
    """List all webhooks (admin only)"""
    return database.get_all_webhooks()


@router.post("", response_model=Webhook, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    webhook_data: WebhookCreate,
    token: str = Depends(require_admin)
):
    """Create a new webhook (admin only)"""
    # Validate event types
    for event in webhook_data.events:
        if event not in VALID_EVENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid event type: {event}. Valid types are: {', '.join(VALID_EVENT_TYPES)}"
            )

    webhook_id = f"wh-{uuid.uuid4().hex[:12]}"
    webhook = database.create_webhook(
        webhook_id=webhook_id,
        url=webhook_data.url,
        events=webhook_data.events,
        secret=webhook_data.secret
    )
    return webhook


@router.get("/events", response_model=List[str])
async def list_event_types(token: str = Depends(require_admin)):
    """List all valid webhook event types"""
    return VALID_EVENT_TYPES


@router.get("/{webhook_id}", response_model=Webhook)
async def get_webhook(webhook_id: str, token: str = Depends(require_admin)):
    """Get a webhook by ID (admin only)"""
    webhook = database.get_webhook(webhook_id)
    if not webhook:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook not found: {webhook_id}"
        )
    return webhook


@router.patch("/{webhook_id}", response_model=Webhook)
async def update_webhook(
    webhook_id: str,
    webhook_data: WebhookUpdate,
    token: str = Depends(require_admin)
):
    """Update a webhook (admin only)"""
    # Validate event types if provided
    if webhook_data.events is not None:
        for event in webhook_data.events:
            if event not in VALID_EVENT_TYPES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid event type: {event}. Valid types are: {', '.join(VALID_EVENT_TYPES)}"
                )

    webhook = database.update_webhook(
        webhook_id=webhook_id,
        url=webhook_data.url,
        events=webhook_data.events,
        secret=webhook_data.secret,
        is_active=webhook_data.is_active
    )
    if not webhook:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook not found: {webhook_id}"
        )
    return webhook


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: str, token: str = Depends(require_admin)):
    """Delete a webhook (admin only)"""
    if not database.delete_webhook(webhook_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook not found: {webhook_id}"
        )


@router.post("/{webhook_id}/test", response_model=WebhookTestResponse)
async def test_webhook(webhook_id: str, token: str = Depends(require_admin)):
    """Send a test event to a webhook (admin only)"""
    webhook = database.get_webhook(webhook_id)
    if not webhook:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Webhook not found: {webhook_id}"
        )

    # Import webhook service and send test event
    from app.core.webhook_service import send_test_webhook

    result = await send_test_webhook(webhook)
    return result
