"""Owner-only saved accounts under the existing Claude auth settings API."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool

from app.api.auth import require_admin
from app.core import claude_accounts as accounts
from app.db.async_database import run_sync

router = APIRouter(prefix="/claude-auth/accounts")
Kind = Literal["api_key", "subscription"]


class KeyRequest(BaseModel):
    """A key save request that never echoes its secret in validation errors."""

    # Any deliberately: Pydantic validation errors must not echo a secret.
    api_key: Any = Field(default=None, repr=False)
    label: str = Field(default="", max_length=120)
    activate: StrictBool = True


class LabelRequest(BaseModel):
    """An owner-supplied display label for a saved account."""

    label: str = Field(default="", max_length=120)


def http_error(exc: accounts.AccountError) -> HTTPException:
    """Translate a safe service error to the account API error envelope."""
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)})


async def _call(function, *args, subscription=False):
    try:
        if subscription:
            return await accounts.mutate_subscription(function, *args)
        return await run_sync(function, *args)
    except accounts.AccountError as exc:
        raise http_error(exc) from None
    except OSError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "storage_unavailable",
                "message": "Claude account storage is unavailable. No saved login was deleted.",
            },
        ) from None


async def _result(account_id=None):
    result = {"accounts": await run_sync(accounts.accounts_payload)}
    # Listing can finish a pending sign-in, which switches the active one.
    await accounts.subscriptions.reap_idle_pooled_clis()
    if account_id:
        result["account_id"] = account_id
    return result


@router.get("")
async def list_accounts(token: str = Depends(require_admin)) -> dict[str, Any]:
    """List masked saved accounts and the current selection for the owner."""
    return await _result()


@router.post("/api-keys")
async def add_key(request: KeyRequest, token: str = Depends(require_admin)) -> dict[str, Any]:
    """Validate and save an Anthropic key, optionally selecting it."""
    if not isinstance(request.api_key, str) or not request.api_key.strip():
        raise HTTPException(
            status_code=400, detail={"code": "invalid_key", "message": "API key cannot be empty."}
        )
    # Same real validation and encrypted-storage gate as the legacy endpoint.
    from app.api.settings import validate_anthropic_key

    await _call(accounts._require_storage)
    await validate_anthropic_key(request.api_key.strip())
    account_id = await _call(accounts.add_api_key, request.api_key, request.label, request.activate)
    return await _result(account_id)


@router.post("/subscription/save-current")
async def save_current(
    request: LabelRequest, token: str = Depends(require_admin)
) -> dict[str, Any]:
    """Save the CLI's current default sign-in (asked with `claude auth status`)."""
    account_id = await _call(accounts.save_current_login, request.label, subscription=True)
    return await _result(account_id)


@router.post("/subscription/add-another")
async def add_another(token: str = Depends(require_admin)) -> dict[str, Any]:
    """Start `claude auth login` into a fresh folder; it becomes active once signed in."""
    try:
        login = await accounts.start_login(external=True)
    except accounts.AccountError as exc:
        raise http_error(exc) from None
    return {**await _result(), "login": login}


@router.post("/subscription/cancel-login")
async def cancel(token: str = Depends(require_admin)) -> dict[str, Any]:
    """Stop a pending add-another sign-in and remove its unfinished folder."""
    try:
        login = await accounts.cancel_login()
    except accounts.AccountError as exc:
        raise http_error(exc) from None
    return {**await _result(), "login": login}


@router.patch("/{kind}/{account_id}")
async def rename(
    kind: Kind,
    account_id: str,
    request: LabelRequest,
    token: str = Depends(require_admin),
) -> dict[str, Any]:
    """Rename a saved account without changing the selected key or auth mode."""
    await _call(accounts.change_account, kind, account_id, "rename", request.label,
                subscription=kind == "subscription")
    return await _result()


@router.post("/{kind}/{account_id}/activate")
async def activate(
    kind: Kind,
    account_id: str,
    token: str = Depends(require_admin),
) -> dict[str, Any]:
    """Select a saved key or sign-in (siblings turn off); the auth mode is unchanged."""
    await _call(
        accounts.change_account, kind, account_id, "activate", subscription=kind == "subscription"
    )
    return await _result()


@router.delete("/{kind}/{account_id}")
async def delete(
    kind: Kind,
    account_id: str,
    token: str = Depends(require_admin),
) -> dict[str, Any]:
    """Delete one saved account. A deleted active key leaves no key active; a
    deleted active sign-in returns Claude Code to its default folder."""
    await _call(
        accounts.change_account, kind, account_id, "delete", subscription=kind == "subscription"
    )
    return await _result()
