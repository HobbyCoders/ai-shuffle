"""
User preferences API routes - for persisting UI state like open tabs
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.db import database
from app.api.auth import require_auth, get_api_user_from_request
from app.core.auth import auth_service

router = APIRouter(prefix="/api/v1/preferences", tags=["Preferences"])


class PreferenceValue(BaseModel):
    """Model for setting a preference"""
    key: str
    value: Any


class PreferenceResponse(BaseModel):
    """Response model for a preference"""
    key: str
    value: Any
    updated_at: Optional[str] = None


def get_user_identity(request: Request) -> tuple[str, str]:
    """Get user type and ID from request for preferences storage"""
    api_user = get_api_user_from_request(request)

    if api_user:
        # API user
        return ("api_user", api_user["id"])
    else:
        # Admin user
        username = auth_service.get_admin_username()
        return ("admin", username or "admin")


@router.get("/{key}")
async def get_preference(
    request: Request,
    key: str,
    token: str = Depends(require_auth)
) -> Optional[PreferenceResponse]:
    """Get a specific preference by key"""
    user_type, user_id = get_user_identity(request)

    pref = database.get_user_preference(user_type, user_id, key)
    if pref:
        return PreferenceResponse(
            key=pref["key"],
            value=pref["value"],
            updated_at=pref.get("updated_at")
        )
    return None


@router.put("/{key}")
async def set_preference(
    request: Request,
    key: str,
    body: PreferenceValue,
    token: str = Depends(require_auth)
) -> PreferenceResponse:
    """Set a preference value"""
    user_type, user_id = get_user_identity(request)

    result = database.set_user_preference(user_type, user_id, key, body.value)
    return PreferenceResponse(
        key=result["key"],
        value=result["value"],
        updated_at=result.get("updated_at")
    )


@router.delete("/{key}")
async def delete_preference(
    request: Request,
    key: str,
    token: str = Depends(require_auth)
) -> dict:
    """Delete a preference"""
    user_type, user_id = get_user_identity(request)

    deleted = database.delete_user_preference(user_type, user_id, key)
    return {"deleted": deleted}
