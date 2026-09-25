"""Notes database operations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list


# ============================================================================
# TypedDict definitions
# ============================================================================

class NoteRow(TypedDict):
    """Full note row from SELECT * FROM notes."""
    id: str
    title: str
    content: str
    tags: List[str]
    is_pinned: bool
    file_path: Optional[str]
    created_at: str
    updated_at: str


class NoteAutocomplete(TypedDict):
    """Simplified note info for autocomplete."""
    id: str
    title: str
    preview: str
    tags: List[str]
    file_path: Optional[str]


class NoteTagCount(TypedDict):
    """Tag with usage count from notes."""
    name: str
    color: None
    count: int


class NoteAttachmentRow(TypedDict):
    """A file attached to a note."""
    id: str
    note_id: str
    filename: str
    file_path: str
    content_type: Optional[str]
    size_bytes: int
    created_at: str


# ============================================================================
# Notes Operations
# ============================================================================

def get_note(note_id: str) -> Optional[NoteRow]:
    """Get a note by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE id = %s", (note_id,))
        row = row_to_dict(cursor.fetchone())
        if row:
            row["tags"] = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
        return row


def get_all_notes(
    search: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> List[NoteRow]:
    """
    Get all notes with optional search and tag filtering.
    Notes are ordered by pinned status (pinned first) then by updated_at descending.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Build query with filters
        query = "SELECT * FROM notes WHERE 1=1"
        params: List[Any] = []

        if search:
            query += " AND (title ILIKE %s OR content ILIKE %s)"
            search_term = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([search_term, search_term])

        if tag:
            # JSONB array contains check
            query += " AND tags::jsonb @> %s::jsonb"
            params.append(json.dumps([tag]))

        query += " ORDER BY is_pinned DESC, updated_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = rows_to_list(cursor.fetchall())

        # Parse JSON fields
        for row in rows:
            row["tags"] = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]

        return rows


def create_note(
    note_id: str,
    title: str,
    content: str = "",
    tags: Optional[List[str]] = None,
    is_pinned: bool = False,
    file_path: Optional[str] = None
) -> NoteRow:
    """Create a new note"""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO notes (id, title, content, tags, is_pinned, file_path, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                note_id,
                title,
                content,
                json.dumps(tags or []),
                is_pinned,
                file_path,
                now,
                now
            )
        )
    return get_note(note_id)


def update_note(
    note_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    tags: Optional[List[str]] = None,
    is_pinned: Optional[bool] = None,
    file_path: Optional[str] = None
) -> Optional[NoteRow]:
    """Update a note"""
    existing = get_note(note_id)
    if not existing:
        return None

    updates = []
    values = []

    if title is not None:
        updates.append("title = %s")
        values.append(title)
    if content is not None:
        updates.append("content = %s")
        values.append(content)
    if tags is not None:
        updates.append("tags = %s")
        values.append(json.dumps(tags))
    if is_pinned is not None:
        updates.append("is_pinned = %s")
        values.append(is_pinned)
    if file_path is not None:
        updates.append("file_path = %s")
        values.append(file_path)

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(note_id)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE notes SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_note(note_id)


def delete_note(note_id: str) -> bool:
    """Delete a note"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM notes WHERE id = %s", (note_id,))
        return cursor.rowcount > 0


def get_all_note_tags() -> List[NoteTagCount]:
    """
    Get all unique tags used in notes with their counts.
    Returns list of {name: str, count: int}.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # Get all notes
        cursor.execute("SELECT tags FROM notes")
        rows = cursor.fetchall()

        # Count tags
        tag_counts: Dict[str, int] = {}
        for row in rows:
            tags = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        # Sort by count descending, then name
        return sorted(
            [{"name": name, "color": None, "count": count} for name, count in tag_counts.items()],
            key=lambda x: (-x["count"], x["name"])
        )


def get_notes_autocomplete(
    search: Optional[str] = None,
    limit: int = 20
) -> List[NoteAutocomplete]:
    """
    Get notes for autocomplete in chat.
    Returns simplified note info with preview.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Only the preview's worth of each body crosses the wire: one character
        # past the 100-char cut is enough to know whether to add "...". Notes
        # can be large, and this runs on every @-mention keystroke.
        query = (
            "SELECT id, title, SUBSTR(content, 1, 101) AS content_head, tags, file_path "
            "FROM notes WHERE 1=1"
        )
        params: List[Any] = []

        if search:
            query += " AND (title ILIKE %s ESCAPE '\\' OR content ILIKE %s ESCAPE '\\')"
            search_term = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([search_term, search_term])

        query += " ORDER BY is_pinned DESC, updated_at DESC LIMIT %s"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        result = []
        for row in rows:
            head = row["content_head"] or ""
            preview = head[:100] + "..." if len(head) > 100 else head
            tags = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
            result.append({
                "id": row["id"],
                "title": row["title"],
                "preview": preview,
                "tags": tags,
                "file_path": row["file_path"]
            })

        return result


def get_all_note_filenames() -> set:
    """Get the set of all note filenames (extracted from file_path) for collision detection."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT file_path FROM notes WHERE file_path IS NOT NULL")
        filenames = set()
        for row in cursor.fetchall():
            if row["file_path"]:
                # file_path is stored as 'notes/{filename}', extract just the filename
                filenames.add(row["file_path"].split("/")[-1])
        return filenames


def get_notes_without_file_path() -> List[NoteRow]:
    """Get all notes that don't have a file_path set (need materialization)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE file_path IS NULL")
        rows = rows_to_list(cursor.fetchall())
        for row in rows:
            row["tags"] = json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
        return rows


def bulk_update_note_file_paths(updates: Dict[str, str]) -> None:
    """Bulk update file_path for multiple notes. updates is {note_id: file_path}."""
    with get_db() as conn:
        cursor = conn.cursor()
        for note_id, file_path in updates.items():
            cursor.execute(
                "UPDATE notes SET file_path = %s WHERE id = %s",
                (file_path, note_id)
            )


# ============================================================================
# Note Attachments Operations
# ============================================================================

# Metadata columns, excluding the ``data`` BYTEA blob. Reads that only need
# metadata select these explicitly so a list of attachments never drags every
# file's bytes out of the database.
_ATTACHMENT_META_COLS = "id, note_id, filename, file_path, content_type, size_bytes, created_at"


def create_note_attachment(
    attachment_id: str,
    note_id: str,
    filename: str,
    file_path: str,
    content_type: Optional[str] = None,
    size_bytes: int = 0,
    data: Optional[bytes] = None,
) -> NoteAttachmentRow:
    """Persist a note attachment record.

    ``data`` is the file's bytes, stored in the shared database so installs
    sharing it can serve the attachment even when the uploading machine's local
    cache file is absent.
    """
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO note_attachments
                   (id, note_id, filename, file_path, content_type, size_bytes, data, created_at, blob_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (attachment_id, note_id, filename, file_path, content_type, size_bytes, data, now,
             hashlib.sha256(data).hexdigest() if data is not None else None),
        )
        if data is not None:
            from app.db import network as network_store
            network_store.upsert_blob(hashlib.sha256(data).hexdigest(), size=len(data), present=True, _conn=conn)
    return get_note_attachment(attachment_id)


def get_note_attachment(attachment_id: str) -> Optional[NoteAttachmentRow]:
    """Get a single attachment's metadata by ID (without the file bytes)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_ATTACHMENT_META_COLS} FROM note_attachments WHERE id = %s",
            (attachment_id,),
        )
        return row_to_dict(cursor.fetchone())


def get_note_attachments(note_id: str) -> List[NoteAttachmentRow]:
    """List a note's attachment metadata, oldest first (without the file bytes)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_ATTACHMENT_META_COLS} FROM note_attachments "
            "WHERE note_id = %s ORDER BY created_at ASC",
            (note_id,),
        )
        return rows_to_list(cursor.fetchall())


def get_note_attachment_data(attachment_id: str) -> Optional[bytes]:
    """Return an attachment's stored file bytes, or None if absent.

    None means the row has no bytes (e.g. a legacy attachment whose file was
    only ever on another machine's disk and never backfilled).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM note_attachments WHERE id = %s", (attachment_id,))
        row = cursor.fetchone()
        if not row or row["data"] is None:
            return None
        # psycopg2 returns BYTEA as a memoryview; normalize to bytes.
        return bytes(row["data"])


def set_note_attachment_data(attachment_id: str, data: bytes) -> None:
    """Store file bytes for an existing attachment (used to backfill from disk)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE note_attachments SET data = %s, size_bytes = %s WHERE id = %s",
            (data, len(data), attachment_id),
        )


def get_attachments_missing_data() -> List[Dict[str, str]]:
    """Return {id, file_path} for attachments whose bytes are not yet in the DB."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, file_path FROM note_attachments WHERE data IS NULL"
        )
        return rows_to_list(cursor.fetchall())


def delete_note_attachment(attachment_id: str) -> bool:
    """Delete an attachment record. Returns True if a row was removed."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM note_attachments WHERE id = %s", (attachment_id,))
        return cursor.rowcount > 0


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(
    globals(), 'notes',
    'create_note update_note delete_note bulk_update_note_file_paths create_note_attachment set_note_attachment_data delete_note_attachment',
    scope='shared',
)
