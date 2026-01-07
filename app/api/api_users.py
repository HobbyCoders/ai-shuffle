"""
API User management routes - Admin only
"""

import secrets
import hashlib
import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Depends, status

from app.core.models import ApiUser, ApiUserCreate, ApiUserUpdate, ApiUserWithKey
from app.api.auth import require_admin
from app.db import database as db

router = APIRouter(prefix="/api/v1/api-users", tags=["API Users"])


def generate_api_key() -> str:
    """Generate a secure API key"""
    return f"aih_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage"""
    return hashlib.sha256(api_key.encode()).hexdigest()


@router.get("", response_model=List[ApiUser])
async def list_api_users(token: str = Depends(require_admin)):
    """List all API users - Admin only"""
    users = db.get_all_api_users()
    return users


@router.get("/{user_id}", response_model=ApiUser)
async def get_api_user(user_id: str, token: str = Depends(require_admin)):
    """Get an API user by ID - Admin only"""
    user = db.get_api_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API user not found"
        )
    return user


@router.post("", response_model=ApiUserWithKey, status_code=status.HTTP_201_CREATED)
async def create_api_user(request: ApiUserCreate, token: str = Depends(require_admin)):
    """Create a new API user and return the API key (shown only once)"""
    # Validate project exists if provided
    if request.project_id:
        project = db.get_project(request.project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project not found"
            )

    # Validate profile exists if provided
    if request.profile_id:
        profile = db.get_profile(request.profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Profile not found"
            )

    # Generate ID and API key
    user_id = str(uuid.uuid4())[:8]
    api_key = generate_api_key()
    api_key_hash = hash_api_key(api_key)

    # Create user
    user = db.create_api_user(
        user_id=user_id,
        name=request.name,
        api_key_hash=api_key_hash,
        project_id=request.project_id,
        profile_id=request.profile_id,
        description=request.description,
        web_login_allowed=request.web_login_allowed
    )

    # Return with the plaintext API key (only time it's shown)
    return {**user, "api_key": api_key}


@router.put("/{user_id}", response_model=ApiUser)
async def update_api_user(
    user_id: str,
    request: ApiUserUpdate,
    token: str = Depends(require_admin)
):
    """Update an API user - Admin only"""
    existing = db.get_api_user(user_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API user not found"
        )

    # Validate project exists if provided
    if request.project_id:
        project = db.get_project(request.project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Project not found"
            )

    # Validate profile exists if provided
    if request.profile_id:
        profile = db.get_profile(request.profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Profile not found"
            )

    user = db.update_api_user(
        user_id=user_id,
        name=request.name,
        project_id=request.project_id,
        profile_id=request.profile_id,
        description=request.description,
        is_active=request.is_active,
        web_login_allowed=request.web_login_allowed
    )

    return user


@router.post("/{user_id}/regenerate-key", response_model=ApiUserWithKey)
async def regenerate_api_key(user_id: str, token: str = Depends(require_admin)):
    """Regenerate the API key for an API user - Admin only"""
    existing = db.get_api_user(user_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API user not found"
        )

    # Generate new API key
    api_key = generate_api_key()
    api_key_hash = hash_api_key(api_key)

    user = db.update_api_user_key(user_id, api_key_hash)

    return {**user, "api_key": api_key}


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_user(user_id: str, token: str = Depends(require_admin)):
    """Delete an API user - Admin only"""
    if not db.delete_api_user(user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API user not found"
        )
