"""Write-only local OpenRouter management credential and cache-only status."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr

from app.api.auth import require_admin
from app.core import encryption
from app.core.openrouter_usage import (
    CREDITS_URL,
    SETTING_PREFIX,
    SNAPSHOT_PREFIX,
    supported,
)
from app.db import database as db
from app.db.async_database import run_sync

router = APIRouter()


class ManagementUsageConfig(BaseModel):
    management_key: SecretStr


async def _provider(provider_id):
    row = await run_sync(db.get_model_provider, provider_id)
    if row is None:
        raise HTTPException(404, "Provider not found")
    if not supported(row):
        raise HTTPException(400, "Account credits require an official OpenRouter provider")
    return row


def _save(provider_id, encrypted):
    # The provider lock orders save/delete against provider removal and observations.
    with db.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM model_providers WHERE id = %s FOR UPDATE", (provider_id,))
        row = cursor.fetchone()
        if not row or not supported(row) or not row.get("enabled", True):
            return False
        cursor.execute("""INSERT INTO system_settings (key, value, updated_at, revision)
            VALUES (%s, %s, %s, %s) ON CONFLICT(key) DO UPDATE SET
            value = EXCLUDED.value, updated_at = EXCLUDED.updated_at, revision = EXCLUDED.revision""",
                       (SETTING_PREFIX + provider_id, encrypted,
                        datetime.now(timezone.utc).isoformat(), str(uuid4())))
        cursor.execute("DELETE FROM provider_usage_snapshots WHERE provider_id = %s",
                       (SNAPSHOT_PREFIX + provider_id,))
    return True


async def _status(provider_id):
    configured = bool(await run_sync(db.get_system_setting_revision, SETTING_PREFIX + provider_id))
    snapshots = await run_sync(db.get_all_provider_usage_snapshots)
    snapshot = next((s for s in snapshots if s["provider_id"] == SNAPSHOT_PREFIX + provider_id), {})
    return {"configured": configured, "source": "OpenRouter management API",
            "scope": "Account credit ledger (USD)", "credits": snapshot.get("credits"),
            "fetched_at": snapshot.get("fetched_at"),
            "last_error_kind": (snapshot.get("raw") or {}).get("usage_error")}


@router.get("/{provider_id}/openrouter-usage")
async def usage_status(provider_id: str, _: None = Depends(require_admin)):
    await _provider(provider_id)
    return await _status(provider_id)


@router.put("/{provider_id}/openrouter-usage")
async def configure_usage(provider_id: str, body: ManagementUsageConfig, _: None = Depends(require_admin)):
    await _provider(provider_id)
    key = body.management_key.get_secret_value().strip()
    if not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise HTTPException(400, "Enter a management key without whitespace")
    encrypted = encryption.encrypt_value(key)
    if not encryption.is_encrypted(encrypted):
        raise HTTPException(503, "Secret storage is locked")
    if not await run_sync(_save, provider_id, encrypted):
        raise HTTPException(409, "Enable the official OpenRouter provider before saving usage access")
    from app.core.usage_polling import UsageDeferred
    from app.network.usage_budget import resume_authentication

    try:
        await resume_authentication(CREDITS_URL, {"Authorization": f"Bearer {key}"})
    except UsageDeferred:
        raise HTTPException(503, "Key saved. Reconnect the network founder and save again to resume usage polling.") from None
    from app.core.provider_usage import provider_usage_monitor

    provider_usage_monitor.request_refresh(force=True)
    return await _status(provider_id)


@router.delete("/{provider_id}/openrouter-usage")
async def disconnect_usage(provider_id: str, _: None = Depends(require_admin)):
    # Allow removal even after editing the provider's type or endpoint.
    if await run_sync(db.get_model_provider, provider_id) is None:
        raise HTTPException(404, "Provider not found")
    await run_sync(db.delete_system_setting, SETTING_PREFIX + provider_id)
    return await _status(provider_id)
