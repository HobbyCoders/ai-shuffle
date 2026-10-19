"""Owner-only write-only balance credentials (mounted under model-providers)."""
import asyncio
import secrets
import time

from fastapi import Depends, HTTPException, WebSocket
from pydantic import BaseModel, SecretStr

from app.api.auth import require_admin
from app.api.openai_billing import router, operation_lock
from app.core import api_key_balances as balances, encryption
from app.core.config import settings
from app.db import database as db, provider_balances
from app.db.async_database import run_sync

# One-shot, short-lived tickets keep browser credentials out of URLs and logs.
_tickets = {}
_active = set()


class BalanceKey(BaseModel):
    key: SecretStr


async def provider(provider_id):
    row = await run_sync(db.get_model_provider, provider_id, include_token=True)
    if not row:
        raise HTTPException(404, "Provider not found")
    if not balances.supported(row):
        raise HTTPException(400, "Requires an official OpenAI or xAI API-key provider")
    return row


async def save(row, key):
    key = key.strip()
    if not key or len(key) > 4096 or any(c.isspace() for c in key):
        raise HTTPException(400, "Enter a key without whitespace")
    if row["provider_type"] == "openai" and not key.startswith("sess-"):
        raise HTTPException(400, "Use a Platform session key (sess-), not an API or ChatGPT key")
    if not row.get("enabled", True):
        raise HTTPException(409, "Enable the provider first")
    encrypted = encryption.encrypt_value(key)
    if not encryption.is_encrypted(encrypted):
        raise HTTPException(503, "Secret storage is locked")
    if not await run_sync(provider_balances.bind, row, settings.effective_instance_id, encrypted):
        raise HTTPException(409, "Provider changed while saving balance access")
    from app.network.usage_budget import resume_authentication
    from app.core.usage_polling import UsageDeferred
    import json
    try:
        previous = json.loads(row.get("balance_snapshot") or "{}")
        team = previous.get("team_id")
        urls = [balances.OPENAI_CREDIT_URL] if row["provider_type"] == "openai" else []
        if row["provider_type"] == "xai":
            await resume_authentication(balances.XAI_KEY_URL, {
                "Authorization": "Bearer " + encryption.decrypt_value(row["auth_token"]),
            })
            if team:
                base = balances.XAI_BILLING_BASE + team
                urls = [base + "/prepaid/balance", base + "/postpaid/invoice/preview"]
        for url in urls:
            await resume_authentication(url, {"Authorization": "Bearer " + key})
    except UsageDeferred:
        raise HTTPException(503, "Key saved. Reconnect the network founder and save again.") from None
    from app.core.provider_usage import provider_usage_monitor
    provider_usage_monitor.request_refresh(force=True)


@router.get("/{provider_id}/balance-access")
async def balance_status(provider_id: str, _: str = Depends(require_admin)):
    return balances.status(await provider(provider_id))


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


@router.post("/{provider_id}/balance-access/sign-in")
async def sign_in(provider_id: str, _: str = Depends(require_admin)):
    row = await provider(provider_id)
    if row["provider_type"] != "openai" or not row.get("enabled", True):
        raise HTTPException(400, "Requires an enabled OpenAI API provider")
    now = time.monotonic()
    for token, (_, expires) in list(_tickets.items()):
        if expires <= now:
            _tickets.pop(token, None)
    if provider_id in _active or any(r["id"] == provider_id for r, _ in _tickets.values()) or len(_tickets) >= 8:
        raise HTTPException(409, "A sign-in is already active; close it first")
    ticket = secrets.token_urlsafe(32)
    _tickets[ticket] = (row, now + 60)
    return {"ticket": ticket}


@router.websocket("/{provider_id}/balance-access/browser")
async def sign_in_browser(websocket: WebSocket, provider_id: str):
    # require_admin's POST checks also enforce same-origin and deny agent previews.
    from starlette.requests import Request
    scope = {**websocket.scope, "type": "http", "method": "POST"}
    try:
        await run_sync(require_admin, Request(scope))
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    acquired = False
    try:
        async with asyncio.timeout(10):
            hello = await websocket.receive_json()
        ticket = _tickets.pop(hello.get("ticket", ""), None)
        if not ticket or ticket[0]["id"] != provider_id or ticket[1] <= time.monotonic() or provider_id in _active:
            await websocket.close(code=1008)
            return
        _active.add(provider_id)
        acquired = True
        from app.core.openai_platform_signin import capture
        async def authorize():
            await run_sync(require_admin, Request(scope))
        async with asyncio.timeout(600):
            key = await capture(websocket, authorize=authorize)
            await authorize()
            async with operation_lock(provider_id):
                current = await provider(provider_id)
                if current.get("revision") != ticket[0].get("revision"):
                    raise HTTPException(409, "Provider changed during sign-in")
                await save(current, key)
            await websocket.send_json({"type": "saved"})
    except Exception:
        # Never stringify browser/CDP exceptions: they can contain auth headers.
        try:
            await websocket.send_json({"type": "error", "message": "Sign-in ended. Retry or paste a Platform session key."})
        except Exception:
            pass
    finally:
        if acquired:
            _active.discard(provider_id)
        try:
            await websocket.close()
        except Exception:
            pass
