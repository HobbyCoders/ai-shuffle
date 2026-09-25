"""
Model Catalog API — Dynamic fal.ai model discovery

Provides endpoints to browse, search, and filter the full fal.ai model library.
The catalog is fetched from fal.ai's platform API and cached locally.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.auth import require_auth
from app.services.fal_catalog import catalog_service
from app.services.fal_schema import schema_service
from app.api.settings import get_decrypted_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/catalog", tags=["Model Catalog"])


@router.get("/models")
async def list_models(
    q: Optional[str] = Query(None, description="Search by name, description, or endpoint ID"),
    category: Optional[str] = Query(None, description="Filter by fal.ai category (e.g., text-to-image)"),
    tool_type: Optional[str] = Query(None, description="Filter by tool type (e.g., image_generation)"),
    output_type: Optional[str] = Query(None, description="Filter by output type (image, video, 3d, audio)"),
    _: str = Depends(require_auth),
):
    """
    List all available fal.ai models with optional search and filtering.

    Refreshes the catalog in the background if it's stale (>1 hour old).
    """
    # Auto-refresh if cache is stale
    if catalog_service.is_stale:
        api_key = get_decrypted_api_key("fal_api_key")
        try:
            await catalog_service.refresh(api_key)
        except Exception as e:
            logger.warning(f"Background catalog refresh failed: {e}")

    models = catalog_service.search(
        q=q,
        category=category,
        tool_type=tool_type,
        output_type=output_type,
    )

    return {
        "models": [m.to_dict() for m in models],
        "total": len(models),
        "catalog_size": catalog_service.model_count,
    }


@router.get("/schema")
async def get_model_schema(
    endpoint_id: str = Query(..., description="fal.ai endpoint ID to fetch the input schema for"),
    refresh: bool = Query(False, description="Bypass the schema cache and re-fetch"),
    _: str = Depends(require_auth),
):
    """Return the normalized OpenAPI input schema for a model.

    Drives the dynamic, model-specific parameter form in the UI. Returns
    ``found: False`` (not an error) when the model has no discoverable schema,
    so callers can fall back to a plain prompt input.
    """
    api_key = get_decrypted_api_key("fal_api_key")
    schema = await schema_service.get_schema(endpoint_id, api_key, force=refresh)
    if not schema:
        return {"found": False, "endpoint_id": endpoint_id}
    return {"found": True, "schema": schema.to_dict()}


@router.get("/models/{endpoint_id:path}")
async def get_model(
    endpoint_id: str,
    _: str = Depends(require_auth),
):
    """Get a specific model by its fal.ai endpoint ID."""
    model = catalog_service.get_model(endpoint_id)
    if not model:
        return {"error": f"Model '{endpoint_id}' not found in catalog", "found": False}
    return {"model": model.to_dict(), "found": True}


@router.get("/categories")
async def list_categories(
    _: str = Depends(require_auth),
):
    """List all available model categories with counts."""
    return {
        "categories": catalog_service.get_categories(),
        "total_models": catalog_service.model_count,
    }


@router.post("/refresh")
async def refresh_catalog(
    _: str = Depends(require_auth),
):
    """Force refresh the model catalog from fal.ai."""
    api_key = get_decrypted_api_key("fal_api_key")
    count = await catalog_service.refresh(api_key)
    return {
        "success": True,
        "models_fetched": count,
        "categories": catalog_service.get_categories(),
    }
