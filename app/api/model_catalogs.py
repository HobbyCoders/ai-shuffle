"""Read the replicated discovery catalog without contacting the provider."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_admin
from app.core import model_catalogs
from app.db import database as db
from app.db.async_database import run_sync

router = APIRouter()


@router.get("/{provider_id}/model-catalog")
async def get_catalog(provider_id: str, token: str = Depends(require_admin)):
    if await run_sync(db.get_model_provider, provider_id) is None:
        raise HTTPException(status_code=404, detail="Model provider not found")
    return await run_sync(model_catalogs.read, provider_id)


async def store_catalog(provider_id: str, response):
    try:
        await run_sync(model_catalogs.save, provider_id, response.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return response
