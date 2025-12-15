"""
Session templates management API routes
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Query

from app.core.models import Template, TemplateCreate, TemplateUpdate
from app.db import database
from app.api.auth import require_auth

router = APIRouter(prefix="/api/v1/templates", tags=["Templates"])


@router.get("", response_model=List[Template])
async def list_templates(
    profile_id: Optional[str] = Query(None, description="Filter by profile ID"),
    category: Optional[str] = Query(None, description="Filter by category"),
    token: str = Depends(require_auth)
):
    """
    List all templates.

    Optionally filter by profile_id (includes global templates) or category.
    """
    return database.get_all_templates(profile_id=profile_id, category=category)


@router.get("/categories", response_model=List[str])
async def list_template_categories(token: str = Depends(require_auth)):
    """Get all unique template categories"""
    return database.get_template_categories()


@router.post("", response_model=Template, status_code=status.HTTP_201_CREATED)
async def create_template(
    template_data: TemplateCreate,
    token: str = Depends(require_auth)
):
    """Create a new session template"""
    template_id = f"tmpl-{uuid.uuid4().hex[:8]}"

    # Validate profile_id if provided
    if template_data.profile_id:
        profile = database.get_profile(template_data.profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Profile not found: {template_data.profile_id}"
            )

    template = database.create_template(
        template_id=template_id,
        name=template_data.name,
        description=template_data.description,
        prompt=template_data.prompt,
        profile_id=template_data.profile_id,
        icon=template_data.icon,
        category=template_data.category
    )
    return template


@router.get("/{template_id}", response_model=Template)
async def get_template(template_id: str, token: str = Depends(require_auth)):
    """Get a template by ID"""
    template = database.get_template(template_id)
    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}"
        )
    return template


@router.patch("/{template_id}", response_model=Template)
async def update_template(
    template_id: str,
    template_data: TemplateUpdate,
    token: str = Depends(require_auth)
):
    """Update a template"""
    existing = database.get_template(template_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}"
        )

    if existing.get("is_builtin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify builtin templates"
        )

    # Validate profile_id if provided
    if template_data.profile_id:
        profile = database.get_profile(template_data.profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Profile not found: {template_data.profile_id}"
            )

    template = database.update_template(
        template_id=template_id,
        name=template_data.name,
        description=template_data.description,
        prompt=template_data.prompt,
        profile_id=template_data.profile_id,
        icon=template_data.icon,
        category=template_data.category
    )

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}"
        )

    return template


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(template_id: str, token: str = Depends(require_auth)):
    """Delete a template"""
    existing = database.get_template(template_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}"
        )

    if existing.get("is_builtin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete builtin templates"
        )

    if not database.delete_template(template_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Template not found: {template_id}"
        )
