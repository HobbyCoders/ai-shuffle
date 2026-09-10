"""
Mobile device API routes for push notifications and action confirmations
"""

import asyncio
import json
import secrets
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Literal

from fastapi import APIRouter, Body, HTTPException, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from app.api.sse_utils import is_client_disconnect
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
from app.core.config import settings
from app.core import pairing
from app.db import database as db
from app.db.mobile_hub import public_device_id
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mobile", tags=["Mobile"])
from app.api.mobile_hub import router as hub_router
router.include_router(hub_router)

# Mobile API key prefix
MOBILE_API_KEY_PREFIX = "aim_"

# Default expiry for mobile API keys (365 days)
MOBILE_API_KEY_EXPIRY_DAYS = 365

# Default expiry for pending actions (5 minutes)
PENDING_ACTION_EXPIRY_MINUTES = 5


# The expired-action sweep is a table-wide UPDATE; one run per process per
# interval is enough because every read filters on expires_at itself.
EXPIRED_ACTION_SWEEP_INTERVAL_S = 60
_last_expired_action_sweep: float | None = None


def _expired_action_sweep_due() -> bool:
    """True at most once per EXPIRED_ACTION_SWEEP_INTERVAL_S per process."""
    global _last_expired_action_sweep
    clock = time.monotonic()
    if (_last_expired_action_sweep is not None
            and clock - _last_expired_action_sweep < EXPIRED_ACTION_SWEEP_INTERVAL_S):
        return False
    _last_expired_action_sweep = clock
    return True


def _reset_expired_action_sweep() -> None:
    """Test hook: make the next sweep check run the sweep."""
    global _last_expired_action_sweep
    _last_expired_action_sweep = None


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

    if device.get("revoked_at"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mobile device credential has been revoked",
        )

    expires_at = device.get("api_key_expires_at")
    if expires_at is None:
        # Legacy rows predate stored expiry. Honor their original documented
        # one-year lifetime instead of silently granting permanent access.
        expires_at = device.get("created_at")
        if expires_at is not None:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            expires_at = expires_at + timedelta(days=MOBILE_API_KEY_EXPIRY_DAYS)
    elif isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expires_at is None or expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mobile device credential has expired; pair this device again",
        )

    if not device.get("scopes"):
        device["scopes"] = list(pairing.DEFAULT_MOBILE_SCOPES)

    from app.core.account_hub import mobile_hub_ownership
    mobile_hub_ownership(device)

    # Update last seen
    db.update_mobile_device_last_seen(device["device_id"])

    return device


def get_device_with_scope(request: Request, required_scope: str) -> dict:
    """Authenticate a mobile key and enforce one grant capability."""
    device = get_device_from_api_key(request)
    if required_scope not in (device.get("scopes") or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This device is missing the {required_scope} capability",
        )
    return device


@router.post("/register", response_model=MobileDeviceRegisterResponse)
async def register_device(request: MobileDeviceRegisterRequest):
    """
    Register a new mobile device.

    Creates a device-specific API key for authentication.
    If the device_id already exists, returns an error.
    """
    device_info = request.device_info

    if not request.pairing_code:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A one-time pairing code is required. Create one from Settings → Devices.",
        )

    # Check if device already exists
    existing = await run_sync(db.get_mobile_device, device_info.device_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device already registered. Use /api/v1/mobile/devices/{device_id} DELETE to unregister first."
        )

    repair = None
    if request.account_repair is not None:
        from app.core import account_hub
        from app.db import mobile_hub as hub_store
        owner = await run_sync(hub_store.ownership, device_info.device_id)
        if (not owner or owner['device_id'] != hub_store.local_device_id(
                owner['server_id'], owner['hub_device_id'])):
            raise HTTPException(409, 'This saved worker authorization cannot be repaired.')
        # Local administration must first unregister this one device. A live
        # account check alone never overrides a worker's revocation decision.
        binding = await run_sync(account_hub.require_active, owner)
        client = await run_sync(db.get_paired_client, owner['paired_client_id'])
        if (not binding or not client or not client.get('revoked_at')
                or client.get('device_id') != owner['device_id']):
            raise HTTPException(409, 'Unregister this phone from this worker before reconnecting.')
        repair = {'owner': owner, 'instance_id': settings.effective_instance_id}

    # Generate API key
    api_key = generate_mobile_api_key()
    api_key_hash = hash_api_key(api_key)

    # Calculate expiry
    expires_at = datetime.now(timezone.utc) + timedelta(days=MOBILE_API_KEY_EXPIRY_DAYS)

    # Consume the one-time grant and create both the generic controller row and
    # mobile push row in a single transaction.
    capabilities_dict = device_info.capabilities.model_dump()
    token_hash = await run_sync(pairing.hash_pairing_code, request.pairing_code)
    result = await run_sync(
        db.exchange_grant_for_mobile,
        token_hash=token_hash,
        device_id=device_info.device_id,
        device_name=device_info.device_name,
        platform=device_info.platform,
        api_key_hash=api_key_hash,
        capabilities=capabilities_dict,
        api_key_expires_at=expires_at,
        target_install_id=settings.effective_instance_id,
        **({'managed_repair': repair} if repair is not None else {}),
    )
    if result and result.get('managed_repair_required'):
        raise HTTPException(409, 'Use this worker’s account reconnect flow with a new pairing code.')
    if result and result.get('repair_invalid'):
        raise HTTPException(409, 'Worker authorization changed. Create a new code on this worker.')
    if result and result.get("duplicate"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Device already registered. Revoke it from Settings → Devices before pairing again.",
        )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pairing code is invalid, expired, already used, or revoked.",
        )

    logger.debug(f"Registered mobile device: {device_info.device_name} ({device_info.platform})")

    registration = MobileDeviceRegisterResponse(
        api_key=api_key,
        device_id=device_info.device_id,
        expires_at=expires_at,
        scopes=result["client"].get("scopes") or pairing.DEFAULT_MOBILE_SCOPES,
    )
    if repair is not None:
        from fastapi.responses import JSONResponse
        return JSONResponse(registration.model_dump(mode='json'), headers={'Cache-Control': 'no-store'})
    return registration


@router.post("/capabilities")
async def update_capabilities(
    request: Request,
    caps_request: MobileDeviceCapabilitiesRequest
):
    """
    Update device capabilities.

    Called when device capabilities change (e.g., biometric becomes available).
    """
    device = await run_sync(get_device_with_scope, request, "controller")

    # Verify device_id matches
    if public_device_id(device) != caps_request.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device ID mismatch"
        )

    capabilities_dict = caps_request.capabilities.model_dump()
    updated = await run_sync(db.update_mobile_device_capabilities, device['device_id'], capabilities_dict)

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
    device = await run_sync(get_device_with_scope, request, "mobile:actions")

    # Sweep expired actions occasionally. This is a table-wide UPDATE across
    # every device, and the read below already filters on expires_at, so it is
    # throttled instead of running on each poll.
    if _expired_action_sweep_due():
        await run_sync(db.cleanup_expired_actions)

    actions = await run_sync(db.get_pending_actions_for_device, device["device_id"])

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
    device = await run_sync(get_device_with_scope, request, "mobile:actions")

    # Get the action
    action = await run_sync(db.get_pending_action, action_id)
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
        await run_sync(db.update_pending_action_status, action_id, "expired")
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
    await run_sync(db.update_pending_action_status, action_id, new_status)

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
    device = await run_sync(get_device_with_scope, request, "mobile:notifications")

    # Verify device_id matches
    if public_device_id(device) != token_request.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device ID mismatch"
        )

    updated = await run_sync(db.update_mobile_device_fcm_token, device['device_id'], token_request.fcm_token)

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
    device = await run_sync(get_device_with_scope, request, "mobile:notifications")

    if public_device_id(device) != up_request.device_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device ID mismatch"
        )

    updated = await run_sync(
        db.update_mobile_device_unifiedpush_endpoint, device['device_id'], up_request.endpoint
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
    await run_sync(get_device_with_scope, request, "mobile:notifications")

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
        except (asyncio.CancelledError, ClientDisconnect, BaseExceptionGroup) as exc:
            # A normal SSE client disconnect must end the stream cleanly, NOT
            # bubble a cancellation out of the generator where it collapses into
            # an anyio ExceptionGroup and surfaces as an unhandled 500 through the
            # BaseHTTPMiddleware stack (issue #627). Anything that is not a pure
            # disconnect/cancellation is re-raised unchanged.
            if not is_client_disconnect(exc):
                raise
            logger.debug("SSE notification stream closed (client disconnect)")
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


class WebSessionMintRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    purpose: Literal['web', 'native'] = 'web'


WEB_RESERVATION_HEADER = 'x-shuffle-web-session-reservation'


@router.post('/web-session/reservations')
async def reserve_web_session(request: Request, response: Response):
    """Prepare a mint without invalidating the phone's existing web session."""
    from app.db.pairing import reserve_paired_client_web_session
    device = await run_sync(get_device_with_scope, request, 'web:session')
    client = await run_sync(db.ensure_paired_client_for_mobile, device)
    if client is None:
        raise HTTPException(401, 'Mobile device credential changed; pair this device again')
    reservation = 'wsm_' + secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=120)
    stored = await run_sync(reserve_paired_client_web_session, client['id'], hash_api_key(reservation),
                            expires_at, mobile_device=device)
    if not stored:
        raise HTTPException(401, 'Mobile device credential changed; pair this device again')
    response.headers['Cache-Control'] = 'no-store'
    return {'reservation': reservation, 'expires_at': expires_at.isoformat(), 'expires_in': 120}


@router.post("/web-session")
async def create_web_session(
    request: Request, response: Response,
    purpose: Literal['web', 'native'] | None = None,
    payload: WebSessionMintRequest | None = Body(default=None),
):
    """
    Mint a web session cookie from a valid mobile API key.

    Lets the Android app load this server's authenticated web UI inside a
    WebView. The returned `session_token` is also injected into the WebView's
    CookieManager by the app.

    The resulting session stays tied to this device. On an administrator-linked
    managed worker, its live-verified owner account can administer the worker;
    direct/legacy pairing remains restricted to controller capabilities.
    """
    # Android supplies JSON; older clients used a query parameter. Resolve both
    # explicitly before minting so a native request never rotates the web slot.
    query_purposes = request.query_params.getlist('purpose')
    if any(value not in ('web', 'native') or value != purpose for value in query_purposes):
        raise HTTPException(status_code=422, detail='Conflicting or invalid session purpose')
    raw_body = await request.body()
    if payload is None and raw_body:
        raise HTTPException(status_code=422, detail='Session request must be a JSON object')
    if payload is not None:
        fields = json.loads(raw_body, object_pairs_hook=list)
        if len(fields) != len({field[0] for field in fields}):
            raise HTTPException(status_code=422, detail='Session request must not repeat fields')
    body_purpose = payload.purpose if payload and 'purpose' in payload.model_fields_set else None
    if purpose and body_purpose and purpose != body_purpose:
        raise HTTPException(status_code=422, detail='Body and query session purposes must match')
    purpose = body_purpose or purpose or 'web'
    reservations = request.headers.getlist(WEB_RESERVATION_HEADER)
    if reservations and (purpose != 'web' or len(reservations) != 1
                         or re.fullmatch(r'wsm_[A-Za-z0-9_-]{43}', reservations[0]) is None):
        raise HTTPException(422, 'Invalid web session reservation')
    # Validates expiry/revocation and bumps last_seen.
    device = await run_sync(get_device_with_scope, request, "web:session")

    client = await run_sync(db.ensure_paired_client_for_mobile, device)
    if client is None:
        raise HTTPException(status_code=401, detail="Mobile device credential changed; pair this device again")
    token = pairing.generate_controller_session()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.session_expire_days)
    from app.db.pairing import WebSessionReservationConflict
    try:
        stored = await run_sync(
            db.set_paired_client_session,
            client["id"],
            pairing.hash_controller_session(token),
            expires_at,
            purpose=purpose,
            mobile_device=device,
            web_reservation_hash=hash_api_key(reservations[0]) if reservations else None,
        )
    except WebSessionReservationConflict:
        raise HTTPException(409, 'Web session reservation expired or was superseded; open this worker again') from None
    if stored is None:
        raise HTTPException(status_code=401, detail="Paired device has been revoked")

    response.headers['Cache-Control'] = 'no-store'
    if purpose == "web":
        response.set_cookie(
            key="session",
            value=token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=settings.session_expire_days * 24 * 60 * 60,
        )

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
    devices = await run_sync(db.get_all_mobile_devices)

    from app.core.models import DeviceCapabilities

    return [
        MobileDevice(
            id=d["id"],
            device_id=d["device_id"],
            device_name=d["device_name"],
            platform=d["platform"],
            capabilities=DeviceCapabilities(**d.get("capabilities", {})),
            fcm_token=d.get("fcm_token"),
            api_key_expires_at=(
                datetime.fromisoformat(d["api_key_expires_at"])
                if isinstance(d.get("api_key_expires_at"), str)
                else d.get("api_key_expires_at")
            ),
            scopes=d.get("scopes") or [],
            revoked_at=(
                datetime.fromisoformat(d["revoked_at"])
                if isinstance(d.get("revoked_at"), str)
                else d.get("revoked_at")
            ),
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
    device = await run_sync(db.get_mobile_device, device_id)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Device not found"
        )

    paired_client_id = device.get("paired_client_id")
    if paired_client_id:
        await run_sync(db.revoke_paired_client, paired_client_id)
    if not await run_sync(db.delete_mobile_device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    logger.debug(f"Deleted mobile device: {device_id}")
