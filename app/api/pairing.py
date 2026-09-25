"""One-time controller pairing and paired-device administration."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.api.auth import require_admin
from app.core.request_identity import get_client_ip
from app.core import pairing
from app.core.config import settings
from app.db import database
from app.db.async_database import run_sync


router = APIRouter(prefix="/api/v1/pairing", tags=["Pairing"])


class PairingGrantCreate(BaseModel):
    endpoint_url: Optional[str] = Field(default=None, max_length=2048)
    device_label: Optional[str] = Field(default=None, max_length=100)
    purpose: Literal["controller", "runner", "network:join"] = "controller"


class PairingExchange(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=8, max_length=255)
    device_name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=60)
    client_type: Literal["browser", "pwa"] = "browser"


class PairedClientRename(BaseModel):
    device_name: str = Field(min_length=1, max_length=100)


class _ExchangeRateLimiter:
    """Small per-process IP limiter for the unauthenticated exchange route.

    Codes carry 60 bits of entropy and grants also enforce their own attempt
    ceiling in the shared database.  This limiter stops noisy online guessing
    before it consumes database capacity.
    """

    def __init__(self, *, limit: int = 10, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._attempts: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self.limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many pairing attempts. Wait a minute and try again.",
                )
            attempts.append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()


_exchange_limiter = _ExchangeRateLimiter()


def _public_grant(grant: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in grant.items()
        if key not in {"token_hash"}
    }


def _public_client(client: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in client.items()
        if key not in {"session_token_hash"}
    }


@router.get("/endpoints")
async def list_endpoints(
    request: Request,
    _: str = Depends(require_admin),
) -> Dict[str, Any]:
    endpoints = await run_sync(pairing.pairing_endpoints, str(request.base_url))
    return {"endpoints": endpoints}


@router.post("/grants", status_code=status.HTTP_201_CREATED)
async def create_grant(
    body: PairingGrantCreate,
    _: str = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        result = await run_sync(
            pairing.create_grant,
            endpoint_url=body.endpoint_url,
            device_label=body.device_label,
            purpose=body.purpose,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **result,
        "grant": _public_grant(result["grant"]),
    }


@router.get("/grants")
async def list_grants(_: str = Depends(require_admin)) -> Dict[str, Any]:
    grants = await run_sync(database.list_pairing_grants, active_only=True)
    return {"grants": [_public_grant(grant) for grant in grants]}


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_grant(grant_id: str, _: str = Depends(require_admin)) -> Response:
    if not await run_sync(database.revoke_pairing_grant, grant_id):
        raise HTTPException(status_code=404, detail="Active pairing grant not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/exchange")
async def exchange_grant(
    body: PairingExchange,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    client_ip = get_client_ip(request)
    _exchange_limiter.check(client_ip)

    session_token = pairing.generate_controller_session()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.session_expire_days)
    token_hash = await run_sync(pairing.hash_pairing_code, body.code)
    result = await run_sync(
        database.exchange_grant_for_client,
        token_hash=token_hash,
        device_id=body.device_id.strip(),
        device_name=body.device_name.strip(),
        platform=body.platform.strip().lower(),
        client_type=body.client_type,
        session_token_hash=pairing.hash_controller_session(session_token),
        session_expires_at=expires_at,
        target_install_id=settings.effective_instance_id,
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pairing code is invalid, expired, already used, or revoked.",
        )

    _exchange_limiter.clear(client_ip)
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_expire_days * 24 * 60 * 60,
    )
    client = _public_client(result["client"])
    return {
        "status": "ok",
        "client": client,
        "scopes": client.get("scopes") or pairing.DEFAULT_CONTROLLER_SCOPES,
        "expires_at": expires_at.isoformat(),
    }


@router.get("/clients")
async def list_clients(_: str = Depends(require_admin)) -> Dict[str, Any]:
    clients = await run_sync(database.list_paired_clients, include_revoked=False)
    return {"clients": [_public_client(client) for client in clients]}


@router.patch("/clients/{client_id}")
async def rename_client(
    client_id: str,
    body: PairedClientRename,
    _: str = Depends(require_admin),
) -> Dict[str, Any]:
    updated = await run_sync(
        database.rename_paired_client,
        client_id,
        body.device_name.strip(),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Paired device not found")
    return _public_client(updated)


@router.delete("/clients/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_client(client_id: str, _: str = Depends(require_admin)) -> Response:
    if not await run_sync(database.revoke_paired_client, client_id):
        raise HTTPException(status_code=404, detail="Active paired device not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
