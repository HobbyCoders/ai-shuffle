"""
Notes management API routes - Global notes for ideas, bugs, planning
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Depends, status, Query, UploadFile, File
from fastapi.responses import FileResponse, Response

from app.core.models import Note, NoteCreate, NoteUpdate, NoteTag, NoteAutocomplete, NoteAttachment
from app.core.note_files import (
    generate_filename, write_note_file, delete_note_file,
    rename_note_file, get_file_path_for_storage, get_absolute_file_path,
    sanitize_attachment_filename, get_unique_attachment_path,
    get_attachment_storage_path, delete_note_attachment_file,
    delete_all_note_attachments, read_note_file, get_note_file_mtime,
    materialize_attachment_file,
)
from app.db import database
from app.api.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notes", tags=["Notes"])

# Max attachment size (25 MB) — notes attachments are reference material, not
# bulk storage. Mirrors the audio-upload ceiling used elsewhere.
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024


def _parse_db_timestamp(value) -> Optional[datetime]:
    """Parse a stored ISO timestamp into a tz-aware datetime (assume UTC)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _reconcile_note_with_disk(note: dict) -> dict:
    """Refresh a note from its on-disk markdown file when edited externally.

    The database is the source of truth, but notes are also materialized as
    ``.md`` files that users (or other tools) may edit directly on disk. When
    the file's modification time is newer than the note's ``updated_at``, the
    file was changed outside the app; re-read it, push the new title/content
    back into the database, and return the refreshed row so the UI reflects the
    external edit. Tags and pin state live only in the DB and are preserved.

    Best-effort: any failure leaves the original DB row untouched.
    """
    file_path = note.get("file_path")
    if not file_path:
        return note

    filename = file_path.split("/")[-1]
    mtime = get_note_file_mtime(filename)
    if mtime is None:
        return note

    db_updated = _parse_db_timestamp(note.get("updated_at"))
    file_mtime = datetime.fromtimestamp(mtime, tz=timezone.utc)
    # Only adopt the file when it is strictly newer than the DB row. The app
    # sets updated_at right after writing the file on its own create/update, so
    # files it wrote are never newer than the row and won't trigger a rewrite.
    if db_updated is not None and file_mtime <= db_updated:
        return note

    parsed = read_note_file(filename)
    if parsed is None:
        return note
    file_title, file_content = parsed

    # If the external edit dropped the leading heading, keep the DB title.
    new_title = file_title or note.get("title") or ""
    new_content = file_content

    if new_title == note.get("title") and new_content == note.get("content"):
        return note

    try:
        updated = database.update_note(
            note_id=note["id"],
            title=new_title,
            content=new_content,
        )
        return updated or note
    except Exception:
        logger.exception("Failed to reconcile note %s from disk", note.get("id"))
        return note


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
    # Note: reconciliation with on-disk files happens only in get_note (the
    # single-note read), not here — that keeps list reads free of the per-note
    # stat/write scan. The open note is refreshed via get_note on the client.
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
    # Adopt any external edit made directly to the on-disk markdown file.
    return _reconcile_note_with_disk(note)


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

    # Remove any uploaded attachment files (DB rows cascade on note delete)
    delete_all_note_attachments(note_id)

    database.delete_note(note_id)


# ============================================================================
# Note Attachments — files uploaded to a note and referenced via @ in the body
# ============================================================================

@router.get("/{note_id}/attachments", response_model=List[NoteAttachment])
async def list_note_attachments(
    note_id: str,
    token: str = Depends(require_auth)
):
    """List all files attached to a note."""
    if not database.get_note(note_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found: {note_id}"
        )
    return database.get_note_attachments(note_id)


@router.post(
    "/{note_id}/attachments",
    response_model=NoteAttachment,
    status_code=status.HTTP_201_CREATED,
)
async def upload_note_attachment(
    note_id: str,
    file: UploadFile = File(...),
    token: str = Depends(require_auth)
):
    """Upload a file and attach it to a note."""
    if not database.get_note(note_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Note not found: {note_id}"
        )

    safe_filename = sanitize_attachment_filename(file.filename or "")
    if not safe_filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )

    content = await file.read()
    if len(content) > MAX_ATTACHMENT_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File exceeds the 25 MB attachment limit"
        )

    file_path = get_unique_attachment_path(note_id, safe_filename)
    try:
        file_path.write_bytes(content)
    except Exception:
        logger.exception("Failed to save note attachment")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save attachment"
        )

    attachment_id = f"natt-{uuid.uuid4().hex[:12]}"
    relative_path = get_attachment_storage_path(note_id, file_path.name)
    # Store the bytes in the shared database (source of truth) so other installs
    # sharing it can serve this attachment; the disk write above is a local cache.
    attachment = database.create_note_attachment(
        attachment_id=attachment_id,
        note_id=note_id,
        filename=file_path.name,
        file_path=relative_path,
        content_type=file.content_type,
        size_bytes=len(content),
        data=content,
    )
    return attachment


@router.get("/{note_id}/attachments/{attachment_id}/download")
async def download_note_attachment(
    note_id: str,
    attachment_id: str,
    token: str = Depends(require_auth)
):
    """Download an attachment file."""
    attachment = database.get_note_attachment(attachment_id)
    if not attachment or attachment["note_id"] != note_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found"
        )

    media_type = attachment.get("content_type") or "application/octet-stream"
    full_path = get_absolute_file_path(attachment["file_path"])

    # Local cache hit (e.g. uploaded on this machine): stream it from disk.
    if full_path.exists() and full_path.is_file():
        return FileResponse(
            path=full_path,
            filename=attachment["filename"],
            media_type=media_type,
        )

    # No local file — this attachment was likely uploaded on another install
    # that shares this database. Serve the bytes stored in the database and
    # repopulate the local cache for next time.
    data = database.get_note_attachment_data(attachment_id)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment file not found on disk or in database"
        )

    cached_path = materialize_attachment_file(attachment["file_path"], data)
    if cached_path is not None:
        return FileResponse(
            path=cached_path,
            filename=attachment["filename"],
            media_type=media_type,
        )

    # Cache write failed (e.g. read-only filesystem); serve from memory. Build a
    # Content-Disposition that can't be broken by quotes/newlines in the name.
    filename = attachment["filename"]
    ascii_name = filename.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.delete(
    "/{note_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_note_attachment_endpoint(
    note_id: str,
    attachment_id: str,
    token: str = Depends(require_auth)
):
    """Delete an attachment (DB row + file on disk)."""
    attachment = database.get_note_attachment(attachment_id)
    if not attachment or attachment["note_id"] != note_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment not found"
        )

    delete_note_attachment_file(attachment["file_path"])
    database.delete_note_attachment(attachment_id)
