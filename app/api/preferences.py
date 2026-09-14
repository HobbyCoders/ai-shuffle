"""
User preferences API routes - for persisting UI state like open tabs
"""

from datetime import datetime
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, Field, field_serializer

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth
from app.core import preview_access
from app.core.preference_identity import request_preference_identity
from app.db.settings import PreferenceConflict

router = APIRouter(prefix="/api/v1/preferences", tags=["Preferences"])


class PreferenceValue(BaseModel):
    """Model for setting a preference"""
    key: str
    value: Any
    expected_version: Optional[str] = None


class PreferenceResponse(BaseModel):
    """Response model for a preference"""
    key: str
    value: Any
    updated_at: Optional[Union[str, datetime]] = None
    version: Optional[str] = None

    @field_serializer("updated_at")
    def serialize_updated_at(self, v: Any) -> Optional[str]:
        if isinstance(v, datetime):
            return v.isoformat()
        return v


class AllPreferencesResponse(BaseModel):
    """Response model for all preferences"""
    preferences: dict[str, Any]
    versions: dict[str, Optional[str]] = Field(default_factory=dict)


def get_user_identity(request: Request) -> tuple[str, str]:
    """Use the same verified identity as the global sync subscription."""
    return request_preference_identity(request)


def require_preference_write(request: Request, token: str = Depends(require_auth)) -> str:
    # Preview UI interactions must not broadcast an agent's temporary layout
    # choices to the owner's other windows/workers. Check the cookie even when
    # require_auth admitted the browser as a direct LAN owner first.
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot change account preferences.")
    return token


@router.get("/all")
async def get_all_preferences(
    request: Request,
    token: str = Depends(require_auth)
) -> AllPreferencesResponse:
    """Get all preferences for the current user in a single request.

    Returns a dictionary mapping preference keys to their values.
    This is more efficient than fetching preferences individually on app startup.
    """
    user_type, user_id = get_user_identity(request)

    # ONE thread hop: this endpoint is hit on nearly every page load, and the
    # synchronous psycopg2 read used to run straight on the event loop.
    prefs = await run_sync(database.get_all_user_preferences, user_type, user_id)
    # Convert list of preference objects to a key->value dictionary
    preferences_dict = {p["key"]: p["value"] for p in prefs}

    versions = {p['key']: p.get('revision') for p in prefs}
    return AllPreferencesResponse(preferences=preferences_dict, versions=versions)


@router.get("/{key}")
async def get_preference(
    request: Request,
    key: str,
    token: str = Depends(require_auth)
) -> Optional[PreferenceResponse]:
    """Get a specific preference by key"""
    user_type, user_id = get_user_identity(request)

    pref = await run_sync(database.get_user_preference, user_type, user_id, key)
    if pref:
        return PreferenceResponse(
            key=pref["key"],
            value=pref["value"],
            updated_at=pref.get("updated_at"),
            version=pref.get("revision"),
        )
    return None


@router.put("/{key}")
async def set_preference(
    request: Request,
    key: str,
    body: PreferenceValue,
    only_if_missing: bool = False,
    token: str = Depends(require_preference_write)
) -> PreferenceResponse:
    """Set a preference value"""
    user_type, user_id = get_user_identity(request)

    options = {'expected_version': body.expected_version} if 'expected_version' in body.model_fields_set else {}
    try:
        result = await run_sync(database.set_user_preference, user_type, user_id, key, body.value,
                                only_if_missing=only_if_missing, **options)
    except PreferenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PreferenceResponse(
        key=result["key"],
        value=result["value"],
        updated_at=result.get("updated_at"),
        version=result.get("revision"),
    )


@router.post("/{key}")
async def set_preference_post(
    request: Request,
    key: str,
    body: PreferenceValue,
    only_if_missing: bool = False,
    token: str = Depends(require_preference_write)
) -> PreferenceResponse:
    """Set a preference value via POST.

    This endpoint mirrors PUT functionality but accepts POST requests.
    Needed for navigator.sendBeacon() which only supports POST method.
    sendBeacon is used for reliable preference saves during page unload.
    """
    user_type, user_id = get_user_identity(request)

    options = {'expected_version': body.expected_version} if 'expected_version' in body.model_fields_set else {}
    try:
        result = await run_sync(database.set_user_preference, user_type, user_id, key, body.value,
                                only_if_missing=only_if_missing, **options)
    except PreferenceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PreferenceResponse(
        key=result["key"],
        value=result["value"],
        updated_at=result.get("updated_at"),
        version=result.get("revision"),
    )


@router.delete("/{key}")
async def delete_preference(
    request: Request,
    key: str,
    token: str = Depends(require_preference_write)
) -> dict:
    """Delete a preference"""
    user_type, user_id = get_user_identity(request)

    deleted = await run_sync(database.delete_user_preference, user_type, user_id, key)
    return {"deleted": deleted}
