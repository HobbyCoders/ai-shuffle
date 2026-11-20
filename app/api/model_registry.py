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
from pydantic import BaseModel, Field, StrictBool

from app.api.auth import require_admin, require_auth
from app.core import claude_accounts, claude_api_models, claude_api_provider, claude_cli_models
from app.core.model_registry import registry_payload
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/models", tags=["Models"])


class ClaudeApiModelEntry(BaseModel):
    """One model enabled for a saved Anthropic API key."""

    id: str = Field(..., min_length=1, max_length=300)
    label: str = Field(default="", max_length=300)


class SaveClaudeApiModelsRequest(BaseModel):
    """Curated model list for a saved Anthropic API key (empty = none enabled)."""

    models: list[ClaudeApiModelEntry] = Field(default_factory=list)


class ToggleClaudeSubscriptionModelRequest(BaseModel):
    """Enable or disable a single Claude subscription model for pickers."""

    model: str = Field(..., min_length=1, max_length=300)
    enabled: StrictBool


@router.get("/claude/api-key/{account_id}")
async def list_claude_api_models(
    account_id: str,
    response: Response,
    token: str = Depends(require_admin),
    refresh: bool = False,
) -> dict:
    """List the Anthropic catalog and curation state for this saved key.

    When ``refresh`` is false and a cached catalog already exists for this key,
    returns it without contacting Anthropic or writing to the database.
    """
    try:
        key = await run_sync(claude_accounts.saved_api_key, account_id)
    except claude_accounts.AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    if not key:
        raise HTTPException(status_code=404, detail="This Anthropic API key is no longer saved.")

    curated = await run_sync(claude_api_provider.curated_models, key)
    cached = None if refresh else await run_sync(claude_api_provider.cached_catalog, key)
    if cached is not None:
        models = cached["models"]
        fetched_at = cached.get("fetched_at")
    else:
        try:
            models = await claude_api_models.list_models(key)
        except claude_api_models.CatalogError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        fetched_at = None
        try:
            fetched_at = await run_sync(claude_api_provider.remember_models, key, models)
        except Exception:  # noqa: BLE001 — the list is still returned
            logger.warning("Could not keep the Claude API model list", exc_info=True)
    response.headers["Cache-Control"] = "no-store"
    return {"models": models, "curated": curated, "fetched_at": fetched_at}


@router.put("/claude/api-key/{account_id}/models")
async def save_claude_api_models(
    account_id: str,
    body: SaveClaudeApiModelsRequest,
    token: str = Depends(require_admin),
) -> dict:
    """Persist the curated enabled models for this saved Anthropic API key."""
    try:
        key = await run_sync(claude_accounts.saved_api_key, account_id)
    except claude_accounts.AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None
    if not key:
        raise HTTPException(status_code=404, detail="This Anthropic API key is no longer saved.")
    entries = []
    for item in body.models:
        model_id = item.id.strip()
        if not model_id:
            raise HTTPException(status_code=422, detail="Model id cannot be blank.")
        entries.append({"id": model_id, "label": item.label.strip() or model_id})
    saved = await run_sync(claude_api_provider.save_curated_models, key, entries)
    return {"models": saved}


@router.get("/claude/subscription")
async def list_claude_subscription_models(
    response: Response,
    token: str = Depends(require_admin),
) -> dict:
    """Return the full (unfiltered) Claude subscription model list with enabled flags."""
    payload = await run_sync(claude_cli_models.subscription_catalog_rows)
    response.headers["Cache-Control"] = "no-store"
    return payload


@router.put("/claude/subscription/models")
async def toggle_claude_subscription_model(
    body: ToggleClaudeSubscriptionModelRequest,
    token: str = Depends(require_admin),
) -> dict:
    """Enable or disable a single Claude subscription model for pickers."""
    model_id = body.model.strip()
    if not model_id:
        raise HTTPException(status_code=422, detail="Model id cannot be blank.")
    try:
        disabled = await run_sync(claude_cli_models.set_model_enabled, model_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"disabled_models": disabled, "model": model_id, "enabled": body.enabled}


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
