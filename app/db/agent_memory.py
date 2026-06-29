"""Agent memory file database operations.

Backs the cross-install sync of the Claude Code CLI's per-project memory
directory (``MEMORY.md`` + per-fact ``*.md`` files). Rows are keyed by
``memory_key`` — a project's ``link_group_id`` (stable across installs that
share a git remote) or its ``project_id`` when unlinked — plus the file's
``relative_path`` within the memory dir. A ``deleted`` row is a tombstone so a
delete on one install propagates to the others.

Pure synchronous psycopg2, matching the rest of ``app/db``; callers on the
event loop offload these via ``asyncio.to_thread``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list


class MemoryFileRow(TypedDict):
    """A row from agent_memory_files."""
    id: str
    memory_key: str
    relative_path: str
    content: Optional[str]
    content_hash: Optional[str]
    deleted: bool
    updated_at: object  # psycopg2 returns TIMESTAMPTZ as an aware datetime
    updated_by: Optional[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_memory_files(memory_key: str) -> List[MemoryFileRow]:
    """Return every row for a memory_key, INCLUDING tombstones.

    Tombstones (``deleted = TRUE``) are included so the reconciler can apply
    remote deletions to the local filesystem.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_memory_files WHERE memory_key = %s",
            (memory_key,),
        )
        return rows_to_list(cursor.fetchall())


def get_memory_file(memory_key: str, relative_path: str) -> Optional[MemoryFileRow]:
    """Return a single row by (memory_key, relative_path), or None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM agent_memory_files WHERE memory_key = %s AND relative_path = %s",
            (memory_key, relative_path),
        )
        return row_to_dict(cursor.fetchone())


def upsert_memory_file(
    memory_key: str,
    relative_path: str,
    content: str,
    content_hash: str,
    updated_by: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> None:
    """Insert or update a live (non-deleted) memory file."""
    ts = updated_at or _now()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_memory_files
                (id, memory_key, relative_path, content, content_hash,
                 deleted, updated_at, updated_by)
            VALUES (%s, %s, %s, %s, %s, FALSE, %s, %s)
            ON CONFLICT (memory_key, relative_path) DO UPDATE SET
                content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash,
                deleted = FALSE,
                updated_at = EXCLUDED.updated_at,
                updated_by = EXCLUDED.updated_by
            """,
            (uuid.uuid4().hex, memory_key, relative_path, content,
             content_hash, ts, updated_by),
        )


def tombstone_memory_file(
    memory_key: str,
    relative_path: str,
    updated_by: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> None:
    """Mark a file deleted (tombstone) so the deletion propagates to peers."""
    ts = updated_at or _now()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO agent_memory_files
                (id, memory_key, relative_path, content, content_hash,
                 deleted, updated_at, updated_by)
            VALUES (%s, %s, %s, NULL, NULL, TRUE, %s, %s)
            ON CONFLICT (memory_key, relative_path) DO UPDATE SET
                content = NULL,
                content_hash = NULL,
                deleted = TRUE,
                updated_at = EXCLUDED.updated_at,
                updated_by = EXCLUDED.updated_by
            """,
            (uuid.uuid4().hex, memory_key, relative_path, ts, updated_by),
        )


def delete_memory_key(memory_key: str) -> int:
    """Hard-delete every row for a memory_key. Returns the row count removed.

    Not used by the sync path (which tombstones instead); provided for tests
    and operational cleanup.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM agent_memory_files WHERE memory_key = %s", (memory_key,)
        )
        return cursor.rowcount
