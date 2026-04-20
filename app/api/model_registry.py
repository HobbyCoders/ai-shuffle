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

router = APIRouter(prefix="/api/v1/models", tags=["Models"])


@router.get("")
async def list_models(token: str = Depends(require_auth)) -> dict:
    """Return the full model registry.

    Payload shape::

        {
          "models": [
            {"value": "claude-sonnet-4-6",
             "label": "Sonnet 4.6",
             "family": "sonnet",
             "context_window": 200000,
             "fast_mode_eligible": false},
            ...
          ],
          "default": "claude-sonnet-4-6",
          "inherit_value": "",
          "legacy_map": {"sonnet": "claude-sonnet-4-6", ...}
        }
    """
    return registry_payload()
