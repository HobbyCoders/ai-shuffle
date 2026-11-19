"""
Model registry API — exposes the canonical list of selectable Claude models.

This is the single source of truth consumed by:
  - the frontend (for model dropdowns / badges),
  - MCP tool clients (for createProfile / updateProfile enums),
  - any third-party integration that needs to know which model IDs are valid.

The underlying registry lives in ``app/core/model_registry.py``; this router
is a thin read-only projection of it.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import require_admin, require_auth
from app.core import claude_cli_models
from app.core.model_registry import registry_payload
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/models", tags=["Models"])


@router.get("")
async def list_models(token: str = Depends(require_auth)) -> dict:
    """Return the full model registry.

    Payload shape::

        {
          "models": [
            {"value": "sonnet",
             "label": "Sonnet",
             "family": "sonnet",
             "context_window": 200000},
            ...
          ],
          "default": "sonnet",
          "inherit_value": "",
          "legacy_map": {"claude-sonnet-4-6": "sonnet", ...},
          "providers": [
            {"id": "prov-1",
             "name": "OpenRouter",
             "provider_type": "openrouter",
             "models": [{"id": "openai/gpt-5.6", "label": "GPT-5.6"}],
             "default_model": "openai/gpt-5.6"},
            ...
          ]
        }

    ``providers`` lists only enabled ``model_providers`` rows and never
    includes the encrypted ``auth_token`` blob.
    """
    # The default reads usage history and provider rows: off the event loop.
    return await run_sync(registry_payload)


class ClaudeEnabledModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=300)
    enabled: bool


@router.post("/claude/refresh")
async def refresh_claude_models(token: str = Depends(require_admin)) -> dict:
    """Ask this worker's Claude Code CLI for its model list now.

    Sends only the ``initialize`` handshake (no prompt, no usage) and returns
    the refreshed registry. 409 outside a subscription sign-in or when the CLI
    cannot answer.
    """
    try:
        await run_sync(claude_cli_models.refresh)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await run_sync(registry_payload)


@router.put("/claude/enabled")
async def set_claude_enabled_model(
    request: ClaudeEnabledModelRequest, token: str = Depends(require_admin)
) -> dict:
    """Turn one versioned Claude Code model on or off in the pickers (the aliases
    always are). Other enabled ids are kept, including ones this worker's CLI
    does not list."""
    try:
        saved = await run_sync(claude_cli_models.set_model_enabled, request.model, request.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"models": saved}


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Saved Anthropic API key not found.")


def _storage_error(exc: Any) -> HTTPException:
    """A safe ``claude_accounts.AccountError`` (locked or unreadable storage)."""
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/claude/api-keys/{account_id}")
async def claude_api_key_models(account_id: str, token: str = Depends(require_admin)) -> dict:
    """The models one saved Anthropic API key was last listed with (Settings >
    Claude > API key > Models), whether it is the selected key, and the checked
    versions. ``catalog`` is null before its first listing. Never the key."""
    from app.core import claude_api_models
    from app.core.claude_accounts import AccountError

    try:
        return await run_sync(claude_api_models.account_payload, account_id)
    except KeyError:
        raise _not_found() from None
    except AccountError as exc:
        raise _storage_error(exc) from None


@router.post("/claude/api-keys/{account_id}/refresh")
async def refresh_claude_api_key_models(account_id: str, token: str = Depends(require_admin)) -> dict:
    """Ask Anthropic's Models API which models this saved key can use, now.

    No prompt and no usage. 409 with a plain reason when Anthropic cannot
    answer; the reason never contains the key or Anthropic's reply body."""
    from app.core import claude_api_models
    from app.core.claude_accounts import AccountError

    try:
        return await run_sync(claude_api_models.refresh_account, account_id)
    except KeyError:
        raise _not_found() from None
    except AccountError as exc:
        raise _storage_error(exc) from None
    except claude_api_models.CatalogError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.put("/claude/api-enabled")
async def set_claude_api_enabled_model(
    request: ClaudeEnabledModelRequest, token: str = Depends(require_admin)
) -> dict:
    """Check or uncheck one full Claude id for the API-key pickers. The pickers
    offer it while the selected key's list contains it; a chat that already
    picked it keeps running it either way."""
    from app.core import claude_api_models

    try:
        saved = await run_sync(claude_api_models.set_model_enabled, request.model, request.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"models": saved}
