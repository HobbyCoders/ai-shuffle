"""
Model registry API — exposes the canonical list of selectable Claude models.

This is the single source of truth consumed by:
  - the frontend (for model dropdowns / badges),
  - MCP tool clients (for createProfile / updateProfile enums),
  - any third-party integration that needs to know which model IDs are valid.

The underlying registry lives in ``app/core/model_registry.py``; this router
is a thin read-only projection of it.
"""

from fastapi import APIRouter, Depends

from app.api.auth import require_auth
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
