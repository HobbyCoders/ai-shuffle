"""
Mobile device API routes: pairing, key renewal, web sessions and devices
"""

import json
import secrets
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Literal

from fastapi import APIRouter, Body, HTTPException, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict

from app.core.models import (
    MobileDevice,
    MobileDeviceRegisterRequest,
    MobileDeviceRegisterResponse,
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

from app.core.device_keys import KEY_LIFETIME_DAYS, key_expiry

MOBILE_API_KEY_EXPIRY_DAYS = KEY_LIFETIME_DAYS

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

    return validate_mobile_device(device)


def validate_mobile_device(device: dict) -> dict:
    if device.get("revoked_at"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Mobile device credential has been revoked",
        )

    if key_expiry(device) <= datetime.now(timezone.utc):
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


@router.post('/key/renew')
def renew_device_key(request: Request):
    from app.db.mobile import mobile_key_renewal_device, renew_mobile_key
    key = get_api_key(request)
    if not key or not key.startswith(MOBILE_API_KEY_PREFIX):
        raise HTTPException(401, 'Valid mobile API key required')
    key_hash = hash_api_key(key)
    device = db.get_mobile_device_by_api_key_hash(key_hash) or mobile_key_renewal_device(key_hash)
    if device is None:
        raise HTTPException(401, 'Invalid or expired mobile API key; pair this device again')
    validate_mobile_device(device)
    client = db.ensure_paired_client_for_mobile(device)
    if not client:
        raise HTTPException(401, 'Device credential changed; pair this device again')
    device['paired_client_id'] = client['id']
    try:
        replacement, expiry = renew_mobile_key(device, key_hash, generate_mobile_api_key())
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from None
    except RuntimeError:
        raise HTTPException(503, 'Device key renewal unavailable; the existing key is unchanged') from None
    from fastapi.responses import JSONResponse
    return JSONResponse({'api_key': replacement or key, 'device_id': public_device_id(device),
                         'expires_at': expiry.isoformat(), 'rotated': replacement is not None},
                        headers={'Cache-Control': 'no-store'})


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
            detail="A one-time pairing code is required. Create one from Settings > Devices.",
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
            detail="Device already registered. Revoke it from Settings > Devices before pairing again.",
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
    expires_at = min(datetime.now(timezone.utc) + timedelta(days=settings.session_expire_days),
                     key_expiry(device))
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
            max_age=max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds())),
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
        # Every socket the device holds closes now (4001), not at its next re-check.
        from app.api.ws_key_watch import close_paired_client_sockets
        close_paired_client_sockets(paired_client_id)
    if not await run_sync(db.delete_mobile_device, device_id):
        raise HTTPException(status_code=404, detail="Device not found")

    logger.debug(f"Deleted mobile device: {device_id}")
