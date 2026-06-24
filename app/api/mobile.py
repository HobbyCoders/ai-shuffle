"""
Mobile device API routes for push notifications and action confirmations
"""

import asyncio
import json
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, HTTPException, Depends, Request, Response, status
from fastapi.responses import StreamingResponse

from app.services.mobile_push_hub import mobile_push_hub
from app.core.models import (
    MobileDevice,
    MobileDeviceRegisterRequest,
    MobileDeviceRegisterResponse,
    MobileDeviceCapabilitiesRequest,
    PendingAction,
    ActionConfirmRequest,
    PushTokenRequest,
    UnifiedPushEndpointRequest,
)
from app.api.auth import require_admin, get_api_key, hash_api_key
from app.core.auth import auth_service
from app.core.config import settings
from app.db import database as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mobile", tags=["Mobile"])

# Mobile API key prefix
MOBILE_API_KEY_PREFIX = "aim_"

# Default expiry for mobile API keys (365 days)
MOBILE_API_KEY_EXPIRY_DAYS = 365

# Default expiry for pending actions (5 minutes)
PENDING_ACTION_EXPIRY_MINUTES = 5


def generate_mobile_api_key() -> str:
    """Generate a secure mobile API key"""
    return f"{MOBILE_API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def get_device_from_api_key(request: Request) -> dict:
    """
    Extract and validate mobile device from API key.
    Returns the device dict or raises 401.
    """
    api_key = get_api_key(request)
    if not api_key or not api_key.startswith(MOBILE_API_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid mobile API key required"
        )

    key_hash = hash_api_key(api_key)
    device = db.get_mobile_device_by_api_key_hash(key_hash)

    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid mobile API key"
        )

    # Update last seen
    db.update_mobile_device_last_seen(device["device_id"])

    return device


@router.post("/register", response_model=MobileDeviceRegisterResponse)
async def register_device(request: MobileDeviceRegisterRequest):
    """
    Register a new mobile device.

    Creates a device-specific API key for authentication.
    If the device_id already exists, returns an error.
    """
    device_info = request.device_info

    # Check if device already exists
    existing = db.get_mobile_device(device_info.device_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device already registered. Use /api/v1/mobile/devices/{device_id} DELETE to unregister first."
        )

    # Generate API key
    api_key = generate_mobile_api_key()
    api_key_hash = hash_api_key(api_key)

    # Calculate expiry
    expires_at = datetime.now(timezone.utc) + timedelta(days=MOBILE_API_KEY_EXPIRY_DAYS)

    # Create device record
    capabilities_dict = device_info.capabilities.model_dump()
    db.create_mobile_device(
        device_id=device_info.device_id,
        device_name=device_info.device_name,
        platform=device_info.platform,
        api_key_hash=api_key_hash,
        capabilities=capabilities_dict
    )

    logger.debug(f"Registered mobile device: {device_info.device_name} ({device_info.platform})")

    return MobileDeviceRegisterResponse(
        api_key=api_key,
        device_id=device_info.device_id,
        expires_at=expires_at
    )


@router.post("/capabilities")
async def update_capabilities(
    request: Request,
    caps_request: MobileDeviceCapabilitiesRequest
):
    """
    Update device capabilities.

    Called when device capabilities change (e.g., biometric becomes available).
    """
    device = get_device_from_api_key(request)

    # Verify device_id matches
    if device["device_id"] != caps_request.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device ID mismatch"
        )

    capabilities_dict = caps_request.capabilities.model_dump()
    updated = db.update_mobile_device_capabilities(caps_request.device_id, capabilities_dict)

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )

    return {"status": "ok", "message": "Capabilities updated"}


@router.get("/pending", response_model=List[PendingAction])
async def get_pending_actions(request: Request):
    """
    Get pending actions for the authenticated device.

    Returns a list of actions waiting for confirmation.
    """
    device = get_device_from_api_key(request)

    # Clean up expired actions first
    db.cleanup_expired_actions()

    actions = db.get_pending_actions_for_device(device["device_id"])

    return [
        PendingAction(
            id=a["id"],
            device_id=a["device_id"],
            action_type=a["action_type"],
            params=a.get("params", {}),
            security_level=a.get("security_level", "normal"),
            created_at=datetime.fromisoformat(a["created_at"]) if isinstance(a["created_at"], str) else a["created_at"],
            expires_at=datetime.fromisoformat(a["expires_at"]) if isinstance(a["expires_at"], str) else a["expires_at"],
            status=a["status"]
        )
        for a in actions
    ]


@router.post("/confirm/{action_id}")
async def confirm_action(
    action_id: str,
    confirm_request: ActionConfirmRequest,
    request: Request
):
    """
    Confirm or reject a pending action.

    The action must belong to the authenticated device.
    """
    device = get_device_from_api_key(request)

    # Get the action
    action = db.get_pending_action(action_id)
    if not action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action not found"
        )

    # Verify action belongs to this device
    if action["device_id"] != device["device_id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Action does not belong to this device"
        )

    # Check if action is still pending
    if action["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Action already {action['status']}"
        )

    # Check if action has expired
    expires_at = action["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if datetime.now(timezone.utc) > expires_at:
        db.update_pending_action_status(action_id, "expired")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Action has expired"
        )

    # Check biometric requirement for high/critical security levels
    security_level = action.get("security_level", "normal")
    if security_level in ("high", "critical") and not confirm_request.biometric_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Biometric verification required for {security_level} security actions"
        )

    # Update status
    new_status = "approved" if confirm_request.approved else "rejected"
    db.update_pending_action_status(action_id, new_status)

    logger.debug(f"Action {action_id} {new_status} by device {device['device_id']}")

    return {
        "status": "ok",
        "action_id": action_id,
        "result": new_status,
        "biometric_verified": confirm_request.biometric_verified
    }


@router.post("/push/token")
async def register_push_token(
    request: Request,
    token_request: PushTokenRequest
):
    """
    Register or update FCM push notification token.

    Called when the app obtains a new FCM token.
    """
    device = get_device_from_api_key(request)

    # Verify device_id matches
    if device["device_id"] != token_request.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device ID mismatch"
        )

    updated = db.update_mobile_device_fcm_token(token_request.device_id, token_request.fcm_token)

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )

    logger.debug(f"Updated FCM token for device {token_request.device_id}")

    return {"status": "ok", "message": "Push token registered"}


@router.post("/push/unifiedpush")
async def register_unifiedpush_endpoint(
    request: Request,
    up_request: UnifiedPushEndpointRequest,
):
    """
    Register or update the device's UnifiedPush endpoint URL.

    The Android app obtains this URL from its UnifiedPush distributor (e.g. ntfy)
    and registers it so this server can deliver notifications without Firebase.
    """
    device = get_device_from_api_key(request)

    if device["device_id"] != up_request.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device ID mismatch"
        )

    updated = db.update_mobile_device_unifiedpush_endpoint(
        up_request.device_id, up_request.endpoint
    )

    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )

    logger.debug(f"Updated UnifiedPush endpoint for device {up_request.device_id}")

    return {"status": "ok", "message": "UnifiedPush endpoint registered"}


@router.get("/notifications/stream")
async def notifications_stream(request: Request):
    """
    Server-Sent Events stream of notification events for this device.

    The Android app's foreground service holds this connection open and raises a
    local notification for each event, delivering push without Firebase *and*
    without a separate UnifiedPush distributor app. Authenticated via the aim_
    Bearer key.

    Emits ``event: notification`` frames whose data is the JSON event
    ``{type, title, body, data}``, plus periodic ``: keepalive`` comments so idle
    connections survive proxy/timeout cutoffs.
    """
    # Validates the aim_ key (raises 401 otherwise) and bumps last_seen.
    get_device_from_api_key(request)

    async def event_generator():
        queue = mobile_push_hub.subscribe()
        try:
            # Initial comment flushes proxy buffers and confirms the stream.
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: notification\ndata: {json.dumps(event)}\n\n"
        finally:
            mobile_push_hub.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/web-session")
async def create_web_session(request: Request, response: Response):
    """
    Mint a web session cookie from a valid mobile API key.

    Lets the Android app load this server's authenticated web UI inside a
    WebView. The returned `session_token` is also injected into the WebView's
    CookieManager by the app.

    SECURITY: on a self-hosted instance, any holder of a valid device key is the
    owner, so this grants a full web session (equivalent to admin login).
    """
    # Validates the aim_ key (raises 401 otherwise) and bumps last_seen.
    get_device_from_api_key(request)

    token = auth_service.create_session()

    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_expire_days * 24 * 60 * 60,
    )

    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.session_expire_days)

    return {
        "status": "ok",
        "session_token": token,
        "expires_at": expires_at.isoformat(),
    }


@router.get("/devices", response_model=List[MobileDevice])
async def list_devices(token: str = Depends(require_admin)):
    """
    List all registered mobile devices.

    Admin only endpoint.
    """
    devices = db.get_all_mobile_devices()

    from app.core.models import DeviceCapabilities

    return [
        MobileDevice(
            id=d["id"],
            device_id=d["device_id"],
            device_name=d["device_name"],
            platform=d["platform"],
            capabilities=DeviceCapabilities(**d.get("capabilities", {})),
            fcm_token=d.get("fcm_token"),
            created_at=datetime.fromisoformat(d["created_at"]) if isinstance(d["created_at"], str) else d["created_at"],
            last_seen=datetime.fromisoformat(d["last_seen"]) if d.get("last_seen") and isinstance(d["last_seen"], str) else d.get("last_seen")
        )
        for d in devices
    ]


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(device_id: str, token: str = Depends(require_admin)):
    """
    Remove a registered mobile device.

    Admin only endpoint. This also removes all pending actions and trusted actions
    for the device (via CASCADE delete).
    """
    if not db.delete_mobile_device(device_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )

    logger.debug(f"Deleted mobile device: {device_id}")
