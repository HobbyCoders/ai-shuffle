"""Native generation account settings; no credential material in this API."""
from fastapi import APIRouter, Depends, HTTPException
from app.api.auth import require_admin, require_auth
from app.core.native_generation_settings import NativeGenerationSettings, account_view, list_accounts, save
from app.db.async_database import run_sync

router = APIRouter()


@router.get("/native-generation")
async def get_native_generation(token: str = Depends(require_auth)):
    try:
        return await run_sync(list_accounts)
    except ValueError:
        raise HTTPException(422, "Invalid stored Native generation settings.") from None


@router.get("/native-generation/{provider_id}")
async def get_native_generation_account(provider_id: str, token: str = Depends(require_auth)):
    try:
        return await run_sync(account_view, provider_id)
    except ValueError:
        raise HTTPException(422, "Unknown account or invalid Native generation settings.") from None


@router.put("/native-generation/{provider_id}")
async def put_native_generation_account(provider_id: str, request: NativeGenerationSettings,
                                        token: str = Depends(require_admin)):
    try:
        await run_sync(save, provider_id, request)
        return await run_sync(account_view, provider_id)
    except ValueError:
        raise HTTPException(422, "Unsupported Native generation options; use verified account capabilities.") from None
