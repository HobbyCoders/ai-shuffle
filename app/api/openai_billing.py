"""Provider-owned OpenAI Platform billing sign-in."""

from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from pydantic import BaseModel, Field

from app.api.auth import require_admin
from app.api.browser_panel import _browser_running, serve_browser_panel
from app.api.ws_auth import authenticate_websocket
from app.core.config import settings
from app.core.openai_billing_browser import BillingBrowserError, openai_billing_browser
from app.core.provider_usage import _is_openai_platform_row
from app.db import database as db
from app.db.async_database import run_sync

router = APIRouter()


class ConfirmBilling(BaseModel):
    flow_id: str = Field(min_length=1, max_length=200)
    observation_id: str = Field(min_length=1, max_length=200)
    organization_id: str = Field(min_length=1, max_length=200)


class CancelBilling(BaseModel):
    flow_id: str = Field(min_length=1, max_length=200)


async def _provider(provider_id: str, *, active: bool = True) -> dict:
    row = await run_sync(db.get_model_provider, provider_id)
    if row is None:
        raise HTTPException(404, "Provider not found")
    try:
        url = urlsplit(row.get("base_url") or "")
        official = (
            _is_openai_platform_row(row) and url.scheme == "https"
            and url.hostname == "api.openai.com" and url.port in (None, 443)
            and not url.username and not url.password
            and row.get("auth_mode") not in {"codex", "chatgpt"}
        )
    except ValueError:
        official = False
    if not official:
        raise HTTPException(400, "Credit sign-in requires an official OpenAI API provider")
    if active and not row.get("enabled", True):
        raise HTTPException(409, "Enable this provider before connecting credits")
    return row


def _require_local(row: dict) -> None:
    owner = row.get("openai_billing_instance_id")
    if owner and owner != settings.effective_instance_id:
        raise HTTPException(409, "Manage this credit connection on its owning installation")


async def _snapshot(provider_id: str) -> dict:
    snapshots = await run_sync(db.get_all_provider_usage_snapshots)
    return next((s for s in snapshots if s["provider_id"] == provider_id), {})


async def _status(row: dict) -> dict:
    provider_id = row["id"]
    organization_id = row.get("openai_billing_org_id")
    owner = row.get("openai_billing_instance_id")
    state = openai_billing_browser.status(provider_id)
    result = {
        "status": state.get("status", "not_connected"),
        "flow_id": state.get("flow_id"),
        "observation_id": state.get("observation_id"),
        "organization_id": state.get("organization_id"),
        "credits": state.get("credits"),
        "fetched_at": state.get("fetched_at"),
        "error_kind": state.get("error_kind"),
        "owner_instance_id": owner,
        "browser_available": await _browser_running(),
    }
    if organization_id and result["status"] not in {"pending", "ready"}:
        snapshot = await _snapshot(provider_id)
        raw = snapshot.get("raw") or {}
        result.update(
            organization_id=organization_id,
            credits=snapshot.get("credits") if raw.get("organization_id") == organization_id else None,
            fetched_at=raw.get("billing_fetched_at"),
            error_kind=raw.get("usage_error") or result["error_kind"],
        )
        if result["error_kind"]:
            result["status"] = "reconnect_required"
        elif result["status"] == "not_connected":
            result["status"] = "connected"
    if owner and owner != settings.effective_instance_id:
        result.update(status="other_installation", flow_id=None, observation_id=None)
    return result


async def close_provider_billing(provider_id: str, *, local_only: bool = False) -> None:
    await openai_billing_browser.close(provider_id)
    row = await run_sync(
        db.clear_openai_billing_binding, provider_id,
        expected_instance_id=settings.effective_instance_id if local_only else None,
    )
    if local_only and row is None:
        raise HTTPException(409, "Credit tracking changed on another installation")


@router.get("/{provider_id}/openai-billing/status")
async def billing_status(provider_id: str, _: None = Depends(require_admin)):
    return await _status(await _provider(provider_id, active=False))


@router.post("/{provider_id}/openai-billing/connect")
async def connect_billing(provider_id: str, _: None = Depends(require_admin)):
    async with openai_billing_browser.operation_lock(provider_id):
        row = await _provider(provider_id)
        _require_local(row)
        try:
            await openai_billing_browser.start(provider_id)
        except BillingBrowserError:
            raise HTTPException(503, "The billing browser is unavailable") from None
        return await _status(row)


@router.post("/{provider_id}/openai-billing/confirm")
async def confirm_billing(provider_id: str, body: ConfirmBilling, _: None = Depends(require_admin)):
    async with openai_billing_browser.operation_lock(provider_id):
        row = await _provider(provider_id)
        _require_local(row)
        try:
            observation = openai_billing_browser.confirm(
                provider_id, body.flow_id, body.observation_id, body.organization_id,
            )
        except BillingBrowserError:
            raise HTTPException(409, "The billing observation expired or changed; reconnect to confirm") from None
        row = await run_sync(
            db.set_openai_billing_binding, provider_id,
            observation["organization_id"], settings.effective_instance_id,
        )
        if row is None:
            await openai_billing_browser.close(provider_id)
            raise HTTPException(409, "Provider changed while connecting credits")
        saved = await run_sync(
            db.upsert_provider_usage_snapshot,
            provider_id=provider_id, provider_type="openai", plan_type=None,
            windows=[], credits=observation["credits"], raw=observation["raw"],
            fetched_at=observation["fetched_at"],
            expected_binding=(observation["organization_id"], settings.effective_instance_id),
        )
        if saved is False:
            await openai_billing_browser.close(provider_id)
            raise HTTPException(409, "Provider changed while connecting credits")
        return await _status(row)


@router.post("/{provider_id}/openai-billing/cancel")
async def cancel_billing(provider_id: str, body: CancelBilling, _: None = Depends(require_admin)):
    async with openai_billing_browser.operation_lock(provider_id):
        row = await _provider(provider_id, active=False)
        _require_local(row)
        state = openai_billing_browser.status(provider_id)
        if state.get("flow_id") != body.flow_id or state.get("status") == "connected":
            raise HTTPException(409, "This login flow is no longer pending")
        await openai_billing_browser.close(provider_id)
        return await _status(row)


@router.post("/{provider_id}/openai-billing/disconnect")
async def disconnect_billing(provider_id: str, _: None = Depends(require_admin)):
    async with openai_billing_browser.operation_lock(provider_id):
        row = await _provider(provider_id, active=False)
        _require_local(row)
        await close_provider_billing(provider_id, local_only=True)
        return await _status(await _provider(provider_id, active=False))


@router.websocket("/{provider_id}/openai-billing/browser")
async def billing_browser(websocket: WebSocket, provider_id: str, token: Optional[str] = Query(None)):
    auth_ok, api_user = await authenticate_websocket(websocket, token)
    if not auth_ok or api_user is not None:
        await websocket.close(code=4403, reason="Admin authentication required")
        return
    try:
        row = await _provider(provider_id)
        _require_local(row)
    except HTTPException:
        await websocket.close(code=4403, reason="Credit connection unavailable")
        return
    state = openai_billing_browser.status(provider_id)
    if state.get("status") not in {"pending", "ready"} or not state.get("flow_id"):
        await websocket.close(code=4404, reason="Start a credit sign-in first")
        return

    async def login_input(command: dict) -> None:
        await openai_billing_browser.dispatch_input(provider_id, state["flow_id"], command)

    await websocket.accept()
    await serve_browser_panel(
        websocket, openai_billing_browser.panel_key(provider_id),
        login_only=True, login_input=login_input,
    )
