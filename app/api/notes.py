"""
Notes management API routes - Global notes for ideas, bugs, planning
"""

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Query

from app.core.models import Note, NoteCreate, NoteUpdate, NoteTag, NoteAutocomplete
from app.core.note_files import (
    generate_filename, write_note_file, delete_note_file,
    rename_note_file, get_file_path_for_storage, get_absolute_file_path
)
from app.db import database
from app.api.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notes", tags=["Notes"])


@router.get("", response_model=List[Note])
async def list_notes(
    search: Optional[str] = Query(None, description="Search in title and content"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    token: str = Depends(require_auth)
):
    """
    List all notes globally.
    Notes are ordered by pinned status (pinned first) then by updated_at descending.
    """
    return database.get_all_notes(
        search=search,
        tag=tag,
        limit=limit,
        offset=offset
    )


@router.post("", response_model=Note, status_code=status.HTTP_201_CREATED)
async def create_note(
    note_data: NoteCreate,
    token: str = Depends(require_auth)
):
    """Create a new note"""
    note_id = f"note-{uuid.uuid4().hex[:12]}"

    # Write physical markdown file
    existing_filenames = database.get_all_note_filenames()
    filename = generate_filename(note_data.title, note_id, existing_filenames)
    write_note_file(filename, note_data.title, note_data.content or "")
    file_path = get_file_path_for_storage(filename)

    note = database.create_note(
        note_id=note_id,
        title=note_data.title,
        content=note_data.content,
        tags=note_data.tags,
        is_pinned=note_data.is_pinned,
        file_path=file_path
    )
    return note


@router.get("/tags", response_model=List[NoteTag])
async def get_note_tags(
    token: str = Depends(require_auth)
):
    """Get all unique tags used in notes with their counts"""
    return database.get_all_note_tags()


@router.get("/autocomplete", response_model=List[NoteAutocomplete])
async def get_notes_autocomplete(
    search: Optional[str] = Query(None, description="Search in title and content"),
    limit: int = Query(20, ge=1, le=50),
    token: str = Depends(require_auth)
):
    """Get notes for chat autocomplete (simplified response with preview)"""
    notes = database.get_notes_autocomplete(
        search=search,
        limit=limit
    )
    # Compute absolute paths for the frontend
    for note in notes:
        if note.get("file_path"):
            note["absolute_path"] = str(get_absolute_file_path(note["file_path"]))
    return notes


@router.get("/{note_id}", response_model=Note)
async def get_note(
    note_id: str,
    token: str = Depends(require_auth)
):
    """Get a note by ID"""
    note = database.get_note(note_id)
    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found: {note_id}"
        )
    return note


@router.patch("/{note_id}", response_model=Note)
async def update_note(
    note_id: str,
    note_data: NoteUpdate,
    token: str = Depends(require_auth)
):
    """Update a note"""
    existing = database.get_note(note_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found: {note_id}"
        )

    title = note_data.title if note_data.title is not None else existing["title"]
    content = note_data.content if note_data.content is not None else existing["content"]
    title_changed = note_data.title is not None and note_data.title != existing["title"]
    content_changed = note_data.content is not None and note_data.content != existing["content"]

    new_file_path = None
    if title_changed:
        # Title changed — need a new filename
        old_filename = existing["file_path"].split("/")[-1] if existing.get("file_path") else None
        existing_filenames = database.get_all_note_filenames()
        new_filename = generate_filename(title, note_id, existing_filenames)

        if old_filename:
            rename_note_file(old_filename, new_filename, title, content)
        else:
            write_note_file(new_filename, title, content)

        new_file_path = get_file_path_for_storage(new_filename)
    elif content_changed and existing.get("file_path"):
        # Content changed but title didn't — rewrite file in place
        filename = existing["file_path"].split("/")[-1]
        write_note_file(filename, title, content)

    note = database.update_note(
        note_id=note_id,
        title=note_data.title,
        content=note_data.content,
        tags=note_data.tags,
        is_pinned=note_data.is_pinned,
        file_path=new_file_path
    )
    return note


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: str,
    token: str = Depends(require_auth)
):
    """Delete a note"""
    existing = database.get_note(note_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found: {note_id}"
        )

    # Delete the physical file
    if existing.get("file_path"):
        filename = existing["file_path"].split("/")[-1]
        delete_note_file(filename)

    database.delete_note(note_id)
