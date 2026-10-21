"""Native-image defaults; no credential or paid-provider fallback."""
from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_admin, require_auth
from app.core.native_image_settings import NativeImageSettings, capabilities, load, save
from app.db.async_database import run_sync

router = APIRouter()


def _view(provider: str):
    return {"provider": provider, "settings": load(provider).model_dump(),
            "capabilities": capabilities(provider)}


@router.get("/native-images")
async def get_native_images(token: str = Depends(require_auth)):
    try:
        return await run_sync(lambda: {p: _view(p) for p in ("chatgpt", "grok")})
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid stored native image settings.") from None


@router.get("/native-images/{provider}")
async def get_native_image_settings(provider: str, token: str = Depends(require_auth)):
    try:
        return await run_sync(_view, provider)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown provider or invalid stored native image settings.") from None


@router.put("/native-images/{provider}")
async def put_native_image_settings(provider: str, request: NativeImageSettings,
                                    token: str = Depends(require_admin)):
    try:
        await run_sync(save, provider, request)
        return await run_sync(_view, provider)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unsupported native image options; use the provider capability table.") from None
