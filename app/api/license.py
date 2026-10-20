"""Read-only Cloud plan information for Settings, available to signed-in users."""
from fastapi import APIRouter, Depends

from app.api.auth import require_auth
from app.core import license as licensing
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/license", tags=["Cloud"])


@router.get("")
async def license_status(_=Depends(require_auth)):
    return await run_sync(licensing.status)
