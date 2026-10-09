"""
API User management routes (admin only).
"""

import hashlib
import logging
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.auth import require_admin
from app.core import api_user_scope, audit_service, credential_gate
from app.core.models import (
    ApiUserCreate,
    ApiUserCreateResponse,
    ApiUserResponse,
    ApiUserRotateKeyResponse,
    ApiUserUpdate,
)
from app.db import database
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/api-users", tags=["API Users"])
logger = logging.getLogger(__name__)


def _with_warning(user):
    """``user`` plus its ``subscription_warning`` and whether it runs a
    connector that knows the owner's own account (blocking)."""
    if not user:
        return user
    try:
        owner_capable = api_user_scope.owner_sender_connector(user.get("id")) is not None
    except Exception:  # noqa: BLE001 — never break the settings list
        logger.warning("Could not read connectors for API user %s", user.get("id"), exc_info=True)
        owner_capable = False
    return {
        **user,
        "subscription_warning": api_user_scope.subscription_warning(user),
        "owner_sender_connector": owner_capable,
    }


def _refuse_subscription_model(model, user_id=None) -> None:
    """400 when a key would be set to a model on the owner's subscription.

    Its runs would be refused anyway (``credential_gate.requester_refusal``);
    saving it is refused so the setting never looks like it works. The one
    exception is a key running a connector that knows the owner's own
    account: the owner's messages may use the subscription. Blocking.
    """
    if model and credential_gate.selection_uses_subscription(model):
        if api_user_scope.owner_sender_connector(user_id) is not None:
            return
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE,
        )


@router.get("", response_model=List[ApiUserResponse])
async def list_api_users(token: str = Depends(require_admin)):
    """List all API users."""
    return await run_sync(lambda: [_with_warning(u) for u in database.get_all_api_users()])


@router.post("", response_model=ApiUserCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_user_endpoint(
    data: ApiUserCreate,
    token: str = Depends(require_admin),
    request: Request = None,
):
    """Create a new API user and return the plaintext API key (shown once)."""
    await run_sync(_refuse_subscription_model, data.model)
    user_id = secrets.token_hex(4)
    api_key = f"aih_{secrets.token_urlsafe(32)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    user = await run_sync(
        database.create_api_user,
        user_id=user_id,
        name=data.name,
        api_key_hash=api_key_hash,
        project_id=data.project_id,
        profile_ids=data.profile_ids,
        web_login_allowed=data.web_login_allowed,
        description=data.description,
        model=data.model,
        effort=data.effort,
    )

    await _audit_key_change(request, audit_service.AuditEventType.API_KEY_CREATED, user)
    return {**(await run_sync(_with_warning, user)), "api_key": api_key}


@router.get("/{user_id}", response_model=ApiUserResponse)
async def get_api_user_endpoint(user_id: str, token: str = Depends(require_admin)):
    """Get an API user by ID."""
    user = await run_sync(database.get_api_user, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")
    return await run_sync(_with_warning, user)


@router.patch("/{user_id}", response_model=ApiUserResponse)
async def update_api_user_endpoint(
    user_id: str,
    data: ApiUserUpdate,
    token: str = Depends(require_admin),
    request: Request = None,
):
    """Update an API user."""
    kwargs = data.model_dump(exclude_unset=True)
    await run_sync(_refuse_subscription_model, kwargs.get("model"), user_id)
    result = await run_sync(database.update_api_user, user_id, **kwargs)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")
    if "is_active" in kwargs:
        if not kwargs["is_active"]:
            _close_key_sockets(user_id)
        event = (audit_service.AuditEventType.API_KEY_ENABLED if kwargs["is_active"]
                 else audit_service.AuditEventType.API_KEY_DISABLED)
        await _audit_key_change(request, event, result)
    return await run_sync(_with_warning, result)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_user_endpoint(user_id: str, token: str = Depends(require_admin), request: Request = None):
    """Delete an API user."""
    def _delete():
        user = database.get_api_user(user_id)
        if not user or not database.delete_api_user(user_id):
            return None
        return user
    user = await run_sync(_delete)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")
    _close_key_sockets(user_id)
    await _audit_key_change(request, audit_service.AuditEventType.API_KEY_DELETED, user)


@router.post("/{user_id}/rotate-key", response_model=ApiUserRotateKeyResponse)
async def rotate_api_user_key(user_id: str, token: str = Depends(require_admin), request: Request = None):
    """Regenerate the API key for a user. Returns new plaintext key (shown once)."""
    api_key = f"aih_{secrets.token_urlsafe(32)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    def _rotate():
        """Existence check + key swap in ONE hop.

        This is a read-decide-write: splitting the 404 probe and the update
        across two thread hops would open a window in which the user is
        deleted between them, turning a clean 404 into a write against a
        vanished row. The key material is generated on the loop (outside the
        DB work) and only its hash is passed in, so nothing about hashing,
        redaction or logging changes.
        """
        user = database.get_api_user(user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")
        database.update_api_user(user_id, api_key_hash=api_key_hash)
        return user

    user = await run_sync(_rotate)
    _close_key_sockets(user_id)
    await _audit_key_change(request, audit_service.AuditEventType.API_KEY_ROTATED, user)

    return {"id": user_id, "api_key": api_key}


def _close_key_sockets(user_id: str) -> None:
    """Make the key's open sockets re-check it now; a revoked key's close with 4001.

    Chat sockets (``chat_ws_handler``) and every other key socket
    (``ws_key_watch``: sync, browser panel, voice, transcription). Without
    this an idle socket would only notice at its periodic re-check, up to
    30 seconds later.
    """
    from app.api import ws_key_watch
    from app.api.chat_ws_handler import recheck_api_user_sockets
    for signal in (recheck_api_user_sockets, ws_key_watch.recheck):
        try:
            signal(user_id)
        except Exception:  # noqa: BLE001 — the change is committed; the periodic re-check still runs
            logger.warning("Could not signal the API key's open sockets", exc_info=True)


async def _audit_key_change(request: Request, event_type: str, user: dict) -> None:
    """Record the acting admin and key identity, never key material or hashes."""
    def _record():
        admin = database.get_admin() or {}
        audit_service.log_event(
            event_type, request=request, user_id=admin.get("username"),
            details={"api_key_id": user["id"], "api_key_name": user["name"]},
        )
    try:
        await run_sync(_record)
    except Exception:
        # The mutation already succeeded. Never lose a one-time plaintext key
        # response because an ancillary audit lookup failed.
        logger.warning("API key audit recording failed", exc_info=True)
