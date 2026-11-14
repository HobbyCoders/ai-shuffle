"""
Model registry API — exposes the canonical list of selectable Claude models.

This is the single source of truth consumed by:
  - the frontend (for model dropdowns / badges),
  - MCP tool clients (for createProfile / updateProfile enums),
  - any third-party integration that needs to know which model IDs are valid.

The underlying registry lives in ``app/core/model_registry.py``; this router
is a thin read-only projection of it.
"""

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
