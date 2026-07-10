"""API routes for multiple Anthropic (Claude Code OAuth) accounts.

Admin-gated CRUD + switching for Claude accounts. Mounted under
``/api/v1/auth/claude/accounts``. Credential blobs are NEVER returned; only
display metadata plus a per-install ``is_active`` flag.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.auth import require_admin
from app.core import account_failover
from app.core import claude_accounts as accounts
from app.core import encryption
from app.core.oauth import direct_oauth
from app.core.usage_limits import schedule_usage_refresh
from app.db import database as db
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/auth/claude/accounts",
    tags=["Claude Accounts"],
)


def _epoch_ms(value) -> int | None:
    """Normalize a timestamp column to epoch milliseconds (int), or None.

    ``last_used_at``/``created_at`` are TIMESTAMPTZ columns: psycopg returns
    ``datetime`` objects in production, while the SQLite pg-shim used in tests
    returns ISO-8601 strings. Convert both to epoch ms so the wire contract is a
    stable ``int | null`` the frontend can subtract from ``Date.now()`` without
    producing NaN. Already-numeric values pass through.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _public_account(row: dict, active_id: str | None) -> dict:
    """Shape a DB row into the public API representation (no credentials)."""
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row.get("email"),
        "subscription_type": row.get("subscription_type"),
        "is_active": row["id"] == active_id,
        "expires_at": row.get("expires_at"),
        "has_refresh_token": bool(row.get("has_refresh_token")),
        "sync_pending": bool(row.get("sync_pending")),
        "last_used_at": _epoch_ms(row.get("last_used_at")),
        "created_at": _epoch_ms(row.get("created_at")),
    }


@router.get("")
@router.get("/")
async def list_accounts(token: str = Depends(require_admin)):
    """List all Claude accounts (shared) plus this install's active pointer."""
    encryption_ready = encryption.is_encryption_ready()
    # Seed the default account lazily when encryption is available (idempotent).
    if encryption_ready:
        accounts.seed_from_credentials_file()

    active_id = accounts.get_active_account_id()
    rows = db.get_all_claude_accounts()
    return {
        "accounts": [_public_account(r, active_id) for r in rows],
        "active_account_id": active_id,
        "encryption_ready": encryption_ready,
        "auto_switch_on_limit": account_failover.get_auto_switch_enabled(),
    }


@router.put("/auto-switch")
async def set_auto_switch(request: Request, token: str = Depends(require_admin)):
    """Toggle auto-switch-to-another-account on usage limit for this install.

    Registered ABOVE the parameterized ``/{account_id}`` routes so the literal
    path wins. Body: ``{"enabled": bool}``.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body must include a boolean 'enabled' field.",
        )
    account_failover.set_auto_switch_enabled(enabled)
    return {"success": True, "auto_switch_on_limit": enabled}


@router.post("/start")
async def start_add_account(token: str = Depends(require_admin)):
    """Start an OAuth flow for adding an account (no file deletion / no write)."""
    result = direct_oauth.start_oauth_flow()
    return {
        "success": True,
        "oauth_url": result["oauth_url"],
        "state": result["state"],
    }


@router.post("/complete")
async def complete_add_account(request: Request, token: str = Depends(require_admin)):
    """Complete adding an account. Body: {code, state, name?, make_active?}."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    code = body.get("code")
    state = body.get("state")
    name = body.get("name")
    make_active = bool(body.get("make_active", False))

    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Authorization code is required")
    if not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="State parameter is required")

    result = await accounts.add_account(code, state, name=name, make_active=make_active)

    if not result.get("success"):
        err = result.get("error")
        if err == "encryption_locked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Encryption is locked. Log in again as admin (or set "
                       "ADMIN_PASSWORD) before adding a Claude account.",
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not add account: {err}",
        )

    active_account_id = accounts.get_active_account_id()
    # If this newly-added account became active, re-poll usage for it.
    if make_active:
        schedule_usage_refresh()

    return {
        "success": True,
        "account": result.get("account"),
        "active_account_id": active_account_id,
    }


@router.post("/{account_id}/activate")
async def activate_account(account_id: str, token: str = Depends(require_admin)):
    """Switch the active account to ``account_id`` for this install."""
    # Run the switch off the event loop: it now performs a (blocking) token
    # refresh when the target's access token is expired/near-expiry.
    result = await run_sync(accounts.switch_account, account_id)
    if not result.get("success"):
        err = result.get("error")
        if err == "encryption_locked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Encryption is locked. Log in again as admin (or set "
                       "ADMIN_PASSWORD) before switching Claude accounts.",
            )
        if err == "not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Account not found")
        if err == "decrypt_failed":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not decrypt the account's credentials. Re-add this account.",
            )
        if err == "reauth_required":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account's session has expired and it has no refresh "
                       "token. Re-add this account to sign in again.",
            )
        if err == "refresh_failed":
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not refresh this account's expired token. Try again, "
                       "or re-add the account if the problem persists.",
            )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"Could not switch account: {err}")
    # Re-poll usage for the newly-active account and broadcast to all clients.
    schedule_usage_refresh()
    return {"success": True, "active_account_id": result["active_account_id"]}


@router.patch("/{account_id}")
async def rename_account(account_id: str, request: Request,
                         token: str = Depends(require_admin)):
    """Rename an account. Body: {name}."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="A non-empty name is required")

    result = accounts.rename_account(account_id, name)
    if not result.get("success"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Account not found")
    active_id = accounts.get_active_account_id()
    return {"success": True, "account": _public_account(result["account"], active_id)}


@router.delete("/{account_id}")
async def delete_account(account_id: str, token: str = Depends(require_admin)):
    """Delete an account (active-with-siblings → 409; sole active → logout)."""
    result = accounts.delete_account(account_id)
    if not result.get("success"):
        err = result.get("error")
        if err == "not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Account not found")
        if err == "active_with_siblings":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete the active account while others exist. "
                       "Switch to another account first.",
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Could not delete account: {err}")
    return {"success": True}
