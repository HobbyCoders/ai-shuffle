"""
Tags management API routes
"""

import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Depends, status, Request

from app.core.models import Tag, TagCreate, TagUpdate, SessionTagsUpdate
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth
from app.api.sessions import check_session_access

router = APIRouter(prefix="/api/v1/tags", tags=["Tags"])


@router.get("", response_model=List[Tag])
async def list_tags(token: str = Depends(require_auth)):
    """List all tags"""
    return await run_sync(database.get_all_tags)


@router.post("", response_model=Tag, status_code=status.HTTP_201_CREATED)
async def create_tag(
    tag_data: TagCreate,
    token: str = Depends(require_auth)
):
    """Create a new tag"""
    tag_id = f"tag-{uuid.uuid4().hex[:8]}"
    tag = await run_sync(
        database.create_tag,
        tag_id=tag_id,
        name=tag_data.name,
        color=tag_data.color
    )
    return tag


@router.get("/{tag_id}", response_model=Tag)
async def get_tag(tag_id: str, token: str = Depends(require_auth)):
    """Get a tag by ID"""
    tag = await run_sync(database.get_tag, tag_id)
    if not tag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tag not found: {tag_id}"
        )
    return tag


@router.patch("/{tag_id}", response_model=Tag)
async def update_tag(
    tag_id: str,
    tag_data: TagUpdate,
    token: str = Depends(require_auth)
):
    """Update a tag"""
    tag = await run_sync(
        database.update_tag,
        tag_id=tag_id,
        name=tag_data.name,
        color=tag_data.color
    )
    if not tag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tag not found: {tag_id}"
        )
    return tag


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(tag_id: str, token: str = Depends(require_auth)):
    """Delete a tag"""
    if not await run_sync(database.delete_tag, tag_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tag not found: {tag_id}"
        )


# ============================================================================
# Session Tag Management
# ============================================================================

def _require_session(session_id: str, request: Request) -> dict:
    """Load a session and enforce the caller's access to it.

    Synchronous by design: every session-tag endpoint below runs this as the
    first step of its single ``run_sync`` closure, so the existence read and the
    work that depends on it stay in ONE thread hop. ``check_session_access`` is
    a pure in-memory check (request state + dict comparison) and its
    HTTPException propagates out of the worker thread unchanged.
    """
    session = database.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session not found: {session_id}"
        )
    check_session_access(request, session)
    return session


@router.get("/session/{session_id}", response_model=List[Tag])
async def get_session_tags(
    request: Request,
    session_id: str,
    token: str = Depends(require_auth)
):
    """Get all tags for a session"""
    def _load():
        _require_session(session_id, request)
        return database.get_session_tags(session_id)

    return await run_sync(_load)


@router.put("/session/{session_id}", response_model=List[Tag])
async def set_session_tags(
    request: Request,
    session_id: str,
    data: SessionTagsUpdate,
    token: str = Depends(require_auth)
):
    """Set all tags for a session (replaces existing tags)"""
    def _apply():
        # Read-decide-write: the tag-existence validation and the replacement
        # write must share one hop, or a tag deleted between them would be
        # written into the session — a race that did not exist before.
        _require_session(session_id, request)

        # Validate that all tag IDs exist
        for tag_id in data.tag_ids:
            if not database.get_tag(tag_id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Tag not found: {tag_id}"
                )

        return database.set_session_tags(session_id, data.tag_ids)

    return await run_sync(_apply)


@router.post("/session/{session_id}/{tag_id}", status_code=status.HTTP_201_CREATED)
async def add_tag_to_session(
    request: Request,
    session_id: str,
    tag_id: str,
    token: str = Depends(require_auth)
):
    """Add a tag to a session"""
    def _apply():
        # Read-decide-write kept atomic: existence checks and the insert.
        _require_session(session_id, request)

        if not database.get_tag(tag_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tag not found: {tag_id}"
            )

        database.add_session_tag(session_id, tag_id)

    await run_sync(_apply)
    return {"status": "ok"}


@router.delete("/session/{session_id}/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tag_from_session(
    request: Request,
    session_id: str,
    tag_id: str,
    token: str = Depends(require_auth)
):
    """Remove a tag from a session"""
    def _apply():
        _require_session(session_id, request)
        database.remove_session_tag(session_id, tag_id)

    await run_sync(_apply)
