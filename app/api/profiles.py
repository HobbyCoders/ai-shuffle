"""
Profile management API routes
"""

from typing import List

from fastapi import APIRouter, HTTPException, Depends, status, Request

from app.core.models import Profile, ProfileCreate, ProfileUpdate
from app.db import database
from app.api.auth import require_auth, require_admin, get_api_user_from_request, is_admin_request

router = APIRouter(prefix="/api/v1/profiles", tags=["Profiles"])


@router.get("", response_model=List[Profile])
async def list_profiles(request: Request, token: str = Depends(require_auth)):
    """List all agent profiles. API users only see their assigned profile."""
    api_user = get_api_user_from_request(request)

    if api_user and api_user.get("profile_id"):
        # API user with assigned profile - only return that profile
        profile = database.get_profile(api_user["profile_id"])
        return [profile] if profile else []

    # Admin or unrestricted API user - return all profiles
    profiles = database.get_all_profiles()
    return profiles


@router.get("/{profile_id}", response_model=Profile)
async def get_profile(request: Request, profile_id: str, token: str = Depends(require_auth)):
    """Get a specific profile. API users can only access their assigned profile."""
    api_user = get_api_user_from_request(request)

    # Check if API user is restricted to a specific profile
    if api_user and api_user.get("profile_id"):
        if api_user["profile_id"] != profile_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile"
            )

    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )
    return profile


@router.post("", response_model=Profile, status_code=status.HTTP_201_CREATED)
async def create_profile(request: ProfileCreate, token: str = Depends(require_admin)):
    """Create a custom profile - Admin only"""
    # Check if ID already exists
    existing = database.get_profile(request.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Profile already exists: {request.id}"
        )

    profile = database.create_profile(
        profile_id=request.id,
        name=request.name,
        description=request.description,
        config=request.config.model_dump(exclude_none=True),
        is_builtin=False
    )

    return profile


@router.put("/{profile_id}", response_model=Profile)
async def update_profile(
    profile_id: str,
    request: ProfileUpdate,
    token: str = Depends(require_admin)
):
    """Update a profile (not built-ins) - Admin only"""
    existing = database.get_profile(profile_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    if existing["is_builtin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify built-in profiles"
        )

    config = None
    if request.config:
        config = request.config.model_dump(exclude_none=True)

    profile = database.update_profile(
        profile_id=profile_id,
        name=request.name,
        description=request.description,
        config=config
    )

    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, token: str = Depends(require_admin)):
    """Delete a profile (not built-ins) - Admin only"""
    existing = database.get_profile(profile_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    if existing["is_builtin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete built-in profiles"
        )

    database.delete_profile(profile_id)
