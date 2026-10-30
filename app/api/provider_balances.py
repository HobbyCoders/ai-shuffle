"""Owner-only write-only xAI management credentials (under model-providers)."""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, SecretStr

from app.api.auth import require_admin
from app.core import api_key_balances as balances, encryption
from app.core.config import settings
from app.core.provider_locks import operation_lock
from app.db import database as db, provider_balances
from app.db.async_database import run_sync

router = APIRouter()


class BalanceKey(BaseModel):
    key: SecretStr


async def provider(provider_id):
    row = await run_sync(db.get_model_provider, provider_id, include_token=True)
    if not row:
        raise HTTPException(404, "Provider not found")
    if not balances.supported(row):
        raise HTTPException(400, "Requires an official xAI API-key provider")
    return row


async def save(row, key):
    key = key.strip()
    if not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise HTTPException(400, "Enter a key without whitespace")
    if not row.get("enabled", True):
        raise HTTPException(409, "Enable the provider first")
    encrypted = encryption.encrypt_value(key)
    if not encryption.is_encrypted(encrypted):
        raise HTTPException(503, "Secret storage is locked")
    if not await run_sync(provider_balances.bind, row, settings.effective_instance_id, encrypted):
        raise HTTPException(409, "Provider changed while saving balance access")
    from app.network.usage_budget import resume_authentication
    from app.core.usage_polling import UsageDeferred
    try:
        previous = json.loads(row.get("balance_snapshot") or "{}")
        team = previous.get("team_id")
        await resume_authentication(balances.XAI_KEY_URL, {
            "Authorization": "Bearer " + encryption.decrypt_value(row["auth_token"]),
        })
        if team:
            base = balances.XAI_BILLING_BASE + team
            for endpoint in ("/prepaid/balance", "/postpaid/invoice/preview"):
                await resume_authentication(base + endpoint, {"Authorization": "Bearer " + key})
    except UsageDeferred:
        raise HTTPException(503, "Key saved. Reconnect the network founder and save again.") from None
    from app.core.provider_usage import provider_usage_monitor
    provider_usage_monitor.request_refresh(force=True)


@router.get("/{provider_id}/balance-access")
async def balance_status(provider_id: str, _: str = Depends(require_admin)):
    row = await provider(provider_id)
    from app.core.provider_usage import provider_usage_monitor, FORCED_REFRESH_COOLDOWN_SECONDS
    provider_usage_monitor.request_refresh(force=True, cooldown=FORCED_REFRESH_COOLDOWN_SECONDS)
    return {**balances.status(row), "refreshing": provider_usage_monitor.refreshing}


@router.put("/{provider_id}/balance-access")
async def balance_configure(provider_id: str, body: BalanceKey, _: str = Depends(require_admin)):
    async with operation_lock(provider_id):
        await save(await provider(provider_id), body.key.get_secret_value())
        return balances.status(await provider(provider_id))


@router.delete("/{provider_id}/balance-access")
async def balance_disconnect(provider_id: str, _: str = Depends(require_admin)):
    async with operation_lock(provider_id):
        row = await provider(provider_id)
        if not await run_sync(provider_balances.bind, row, settings.effective_instance_id, None):
            raise HTTPException(409, "Provider changed while removing balance access")
        return balances.status(await provider(provider_id))
