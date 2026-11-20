"""
Model registry API — exposes the canonical list of selectable Claude models.

This is the single source of truth consumed by:
  - the frontend (for model dropdowns / badges),
  - MCP tool clients (for createProfile / updateProfile enums),
  - any third-party integration that needs to know which model IDs are valid.

The underlying registry lives in ``app/core/model_registry.py``; this router
is a thin read-only projection of it.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from app.api.auth import require_admin, require_auth
from app.core import claude_accounts, claude_api_models, claude_cli_models
from app.core.model_registry import registry_payload
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/models", tags=["Models"])


@router.get("/claude/api-key/{account_id}")
async def list_claude_api_models(account_id: str, response: Response, token: str = Depends(require_admin)) -> dict:
    """List the official Anthropic catalog for this saved key; do not send a prompt."""
    try:
        key = await run_sync(claude_accounts.saved_api_key, account_id)
    except claude_accounts.AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    if not key:
        raise HTTPException(status_code=404, detail="This Anthropic API key is no longer saved.")
    try:
        models = await claude_api_models.list_models(key)
    except claude_api_models.CatalogError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    try:
        # The active key's list becomes the Claude API provider's models.
        from app.core.claude_api_provider import remember_models

        await run_sync(remember_models, key, models)
    except Exception:  # noqa: BLE001 — the list is still returned
        logger.warning("Could not keep the Claude API model list", exc_info=True)
    response.headers["Cache-Control"] = "no-store"
    return {"models": models}


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
