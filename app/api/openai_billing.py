"""Write-only OpenAI Admin API configuration. Browser billing is retired."""

import asyncio
from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from pydantic import BaseModel, Field, SecretStr

from app.api.auth import require_admin
from app.core import encryption
from app.core.config import settings
from app.core.provider_usage import _is_openai_platform_row
from app.db import database as db
from app.db.async_database import run_sync

router = APIRouter()


@lru_cache(maxsize=None)
def operation_lock(provider_id):
    return asyncio.Lock()


class AdminUsageConfig(BaseModel):
    admin_key: SecretStr
    organization_id: str = Field(pattern=r"^org-[A-Za-z0-9_-]{1,180}$")


async def _provider(provider_id):
    row = await run_sync(db.get_model_provider, provider_id, include_token=True)
    if row is None:
        raise HTTPException(404, "Provider not found")
    try:
        url = urlsplit(row.get("base_url") or "")
        official = (
            row.get("provider_type") == "openai" and url.scheme == "https"
            and url.hostname == "api.openai.com" and url.port in (None, 443)
            and not url.username and not url.password
            and row.get("auth_mode") not in {"codex", "chatgpt"}
        )
    except ValueError:
        official = False
    if not official:
        raise HTTPException(400, "Usage administration requires an official OpenAI API provider")
    return row


async def _status(row):
    snapshots = await run_sync(db.get_all_provider_usage_snapshots)
    snapshot = next((s for s in snapshots if s["provider_id"] == row["id"]), {})
    raw = snapshot.get("raw") or {}
    configured = bool(row.get("openai_admin_key"))
    return {
        "configured": configured,
        "organization_id": row.get("openai_billing_org_id") if configured else None,
        "source": "OpenAI organization Admin API",
        "scope": "Organization spend; completions token/request usage; current month (UTC)",
        "spend": snapshot.get("credits") if raw.get("source") == "openai_admin" else None,
        "last_error_kind": raw.get("usage_error") if configured else None,
        "fetched_at": raw.get("billing_fetched_at") if configured else None,
    }


async def close_provider_billing(provider_id, *, local_only=False):
    row = await run_sync(db.clear_openai_billing_binding, provider_id,
                         expected_instance_id=settings.effective_instance_id if local_only else None)
    if local_only and row is None:
        raise HTTPException(409, "Usage configuration changed")


@router.get("/{provider_id}/openai-usage")
async def usage_status(provider_id: str, _: None = Depends(require_admin)):
    return await _status(await _provider(provider_id))


@router.put("/{provider_id}/openai-usage")
async def configure_usage(provider_id: str, body: AdminUsageConfig, _: None = Depends(require_admin)):
    async with operation_lock(provider_id):
        row = await _provider(provider_id)
        if not row.get("enabled", True):
            raise HTTPException(409, "Enable the provider before configuring usage")
        key = body.admin_key.get_secret_value().strip()
        if not key or len(key) > 4096 or any(c.isspace() for c in key):
            raise HTTPException(400, "Enter an Admin API key without whitespace")
        encrypted = encryption.encrypt_value(key)
        if not encryption.is_encrypted(encrypted):
            raise HTTPException(503, "Secret storage is locked")
        saved = await run_sync(db.set_openai_billing_binding, provider_id,
                              body.organization_id, settings.effective_instance_id, encrypted)
        if saved is None:
            raise HTTPException(409, "Provider changed while saving usage configuration")
        from app.core.openai_admin_usage import BASE
        from app.core.usage_polling import resume_authentication
        headers = {"Authorization": f"Bearer {key}", "OpenAI-Organization": body.organization_id}
        for endpoint in ("costs", "usage/completions"):
            await run_sync(resume_authentication, BASE + "/" + endpoint, headers)
        from app.core.provider_usage import provider_usage_monitor
        provider_usage_monitor.request_refresh(force=True)
        return await _status(await _provider(provider_id))


@router.delete("/{provider_id}/openai-usage")
async def disconnect_usage(provider_id: str, _: None = Depends(require_admin)):
    async with operation_lock(provider_id):
        await _provider(provider_id)
        await close_provider_billing(provider_id, local_only=True)
        return await _status(await _provider(provider_id))


@router.api_route("/{provider_id}/openai-billing/{operation}", methods=["GET", "POST"])
async def retired_billing(provider_id: str, operation: str, _: None = Depends(require_admin)):
    raise HTTPException(410, "Browser credit tracking was retired. Configure an OpenAI Admin API key for usage and spend.")


@router.websocket("/{provider_id}/openai-billing/browser")
async def retired_browser(websocket: WebSocket, provider_id: str):
    await websocket.close(code=1008)
