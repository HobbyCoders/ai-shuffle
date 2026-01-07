"""
Notes management API routes - Project-scoped notes for ideas, bugs, planning
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Query

from app.core.models import Note, NoteCreate, NoteUpdate, NoteTag, NoteAutocomplete
from app.db import database
from app.api.auth import require_auth

router = APIRouter(prefix="/api/v1/projects/{project_id}/notes", tags=["Notes"])


def _verify_project_exists(project_id: str) -> None:
    """Verify project exists, raise 404 if not"""
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )


def _verify_note_in_project(note_id: str, project_id: str) -> dict:
    """Verify note exists and belongs to project, raise 404 if not"""
    note = database.get_note(note_id)
    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found: {note_id}"
        )
    if note["project_id"] != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found in project: {note_id}"
        )
    return note


@router.get("", response_model=List[Note])
async def list_notes(
    project_id: str,
    search: Optional[str] = Query(None, description="Search in title and content"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    List all notes for a project.
    Notes are ordered by pinned status (pinned first) then by updated_at descending.
    """
    _verify_project_exists(project_id)
    return database.get_notes_for_project(
        project_id=project_id,
        search=search,
        tag=tag,
        limit=limit,
        offset=offset
    )


@router.post("", response_model=Note, status_code=status.HTTP_201_CREATED)
async def create_note(
    project_id: str,
    note_data: NoteCreate,
    token: str = Depends(require_auth)
):
    """Create a new note in the project"""
    _verify_project_exists(project_id)

    note_id = f"note-{uuid.uuid4().hex[:12]}"
    note = database.create_note(
        note_id=note_id,
        project_id=project_id,
        title=note_data.title,
        content=note_data.content,
        tags=note_data.tags,
        linked_sessions=note_data.linked_sessions,
        is_pinned=note_data.is_pinned
    )
    return note


@router.get("/tags", response_model=List[NoteTag])
async def get_note_tags(
    project_id: str,
    token: str = Depends(require_auth)
):
    """Get all unique tags used in notes for this project with their counts"""
    _verify_project_exists(project_id)
    return database.get_note_tags_for_project(project_id)


@router.get("/autocomplete", response_model=List[NoteAutocomplete])
async def get_notes_autocomplete(
    project_id: str,
    search: Optional[str] = Query(None, description="Search in title and content"),
    limit: int = Query(20, ge=1, le=50),
    token: str = Depends(require_auth)
):
    """Get notes for chat autocomplete (simplified response with preview)"""
    _verify_project_exists(project_id)
    return database.get_notes_autocomplete(
        project_id=project_id,
        search=search,
        limit=limit
    )


@router.get("/{note_id}", response_model=Note)
async def get_note(
    project_id: str,
    note_id: str,
    token: str = Depends(require_auth)
):
    """Get a note by ID"""
    _verify_project_exists(project_id)
    return _verify_note_in_project(note_id, project_id)


@router.patch("/{note_id}", response_model=Note)
async def update_note(
    project_id: str,
    note_id: str,
    note_data: NoteUpdate,
    token: str = Depends(require_auth)
):
    """Update a note"""
    _verify_project_exists(project_id)
    _verify_note_in_project(note_id, project_id)

    note = database.update_note(
        note_id=note_id,
        title=note_data.title,
        content=note_data.content,
        tags=note_data.tags,
        linked_sessions=note_data.linked_sessions,
        is_pinned=note_data.is_pinned
    )
    return note


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    project_id: str,
    note_id: str,
    token: str = Depends(require_auth)
):
    """Delete a note"""
    _verify_project_exists(project_id)
    _verify_note_in_project(note_id, project_id)

    if not database.delete_note(note_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found: {note_id}"
        )
