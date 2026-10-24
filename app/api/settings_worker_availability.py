"""Owner-visible launch defaults and a synced availability override for this worker."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, StrictBool

from app.api.auth import require_admin, require_auth
from app.core.worker_availability import Availability, save, snapshot
from app.db.async_database import run_sync

router = APIRouter()


class AvailabilityRequest(BaseModel):
    always_on: StrictBool | None = Field(...)


@router.get("/worker-availability")
async def get_worker_availability(token: str = Depends(require_auth)) -> Availability:
    return await run_sync(snapshot)


@router.put("/worker-availability")
async def put_worker_availability(
    request: AvailabilityRequest, token: str = Depends(require_admin),
) -> Availability:
    return await run_sync(save, request.always_on)
