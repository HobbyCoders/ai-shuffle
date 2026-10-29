"""Read the replicated discovery catalog without contacting the provider."""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_admin
from app.core import model_catalogs
from app.db import database as db
from app.db.async_database import run_sync

if TYPE_CHECKING:
    from app.api.model_providers import FetchModelsResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/{provider_id}/model-catalog")
async def get_catalog(provider_id: str, token: str = Depends(require_admin)) -> dict[str, Any] | None:
    """Return stored discovery for an existing provider without upstream I/O."""
    if await run_sync(db.get_model_provider, provider_id) is None:
        raise HTTPException(status_code=404, detail="Model provider not found")
    return await run_sync(model_catalogs.read, provider_id)


async def store_catalog(provider_id: str, response: FetchModelsResponse) -> FetchModelsResponse:
    """Share discovery best-effort, returning the complete live result regardless."""
    try:
        snapshot = await run_sync(model_catalogs.save, provider_id, response.model_dump())
        if snapshot:
            return response.model_copy(update={field: snapshot[field] for field in (
                "fetched_at", "fetched_by_device", "fetched_by_name",
            )})
    except Exception:  # A sharing failure must not discard successful discovery.
        # Exceptions may contain credential material: log the provider ID only.
        logger.warning("Could not share model catalog for provider %s", provider_id)
    return response
