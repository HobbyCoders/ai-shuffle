"""
API User management routes (admin only).
"""

import hashlib
import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import require_admin
from app.core.models import (
    ApiUserCreate,
    ApiUserCreateResponse,
    ApiUserResponse,
    ApiUserRotateKeyResponse,
    ApiUserUpdate,
)
from app.db import database

router = APIRouter(prefix="/api/v1/api-users", tags=["API Users"])


@router.get("", response_model=List[ApiUserResponse])
async def list_api_users(token: str = Depends(require_admin)):
    """List all API users."""
    return database.get_all_api_users()


@router.post("", response_model=ApiUserCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_user_endpoint(
    data: ApiUserCreate,
    token: str = Depends(require_admin),
):
    """Create a new API user and return the plaintext API key (shown once)."""
    user_id = secrets.token_hex(4)
    api_key = f"aih_{secrets.token_urlsafe(32)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    user = database.create_api_user(
        user_id=user_id,
        name=data.name,
        api_key_hash=api_key_hash,
        project_id=data.project_id,
        profile_ids=data.profile_ids,
        web_login_allowed=data.web_login_allowed,
        description=data.description,
    )

    return {**user, "api_key": api_key}


@router.get("/{user_id}", response_model=ApiUserResponse)
async def get_api_user_endpoint(user_id: str, token: str = Depends(require_admin)):
    """Get an API user by ID."""
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")
    return user


@router.patch("/{user_id}", response_model=ApiUserResponse)
async def update_api_user_endpoint(
    user_id: str,
    data: ApiUserUpdate,
    token: str = Depends(require_admin),
):
    """Update an API user."""
    kwargs = data.model_dump(exclude_unset=True)
    result = database.update_api_user(user_id, **kwargs)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")
    return result


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_user_endpoint(user_id: str, token: str = Depends(require_admin)):
    """Delete an API user."""
    if not database.delete_api_user(user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")


@router.post("/{user_id}/rotate-key", response_model=ApiUserRotateKeyResponse)
async def rotate_api_user_key(user_id: str, token: str = Depends(require_admin)):
    """Regenerate the API key for a user. Returns new plaintext key (shown once)."""
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API user not found")

    api_key = f"aih_{secrets.token_urlsafe(32)}"
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    database.update_api_user(user_id, api_key_hash=api_key_hash)

    return {"id": user_id, "api_key": api_key}
