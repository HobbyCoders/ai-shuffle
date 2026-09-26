"""The ``upload_blobs`` table — DB-mediated cross-install upload delivery.

Modeled byte-for-byte on the ``note_attachments`` pattern (see
``app/db/notes.py``): the bytes in shared Postgres are the source of truth and
local disk is a cache. A staged upload lives on the disk of whichever install
served the upload endpoint; when the turn that references it is placed on
ANOTHER install, the file's bytes are lifted into a row here at SEND time and
materialized into the executing install's session working dir at SPAWN time,
after which ``data`` is NULLed and ``materialized_at`` stamped.

``source_path`` is the UPLOADER-absolute path exactly as embedded in the prompt
— that is what makes the spawn-side prompt rewrite a plain exact-string
replacement rather than a fuzzy match.

Metadata reads exclude the ``data`` BYTEA so listing a turn's blobs never drags
every file's bytes out of the database; :func:`get_blob_data` is the explicit,
single-row way to fetch bytes.

This module is a PURE DB layer: plain functions over ``get_db()``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.db.base import get_db, row_to_dict, rows_to_list

logger = logging.getLogger(__name__)

# Metadata columns, excluding the ``data`` BYTEA blob (note_attachments idiom).
_META_COLS = (
    "id, project_v2_id, session_id, source_path, filename, size, "
    "created_at, materialized_at"
)


def create_blob(
    project_v2_id: Optional[str],
    session_id: Optional[str],
    source_path: str,
    filename: str,
    size: int,
    data: bytes,
) -> str:
    """Persist one upload blob and return its id."""
    blob_id = f"ub-{uuid.uuid4().hex}"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO upload_blobs "
            "(id, project_v2_id, session_id, source_path, filename, size, data) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (blob_id, project_v2_id, session_id, source_path, filename, size, data),
        )
    return blob_id


def get_blob(blob_id: str) -> Optional[Dict[str, Any]]:
    """One blob's METADATA (no bytes), or None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_META_COLS} FROM upload_blobs WHERE id = %s", (blob_id,)
        )
        return row_to_dict(cursor.fetchone())


def get_blobs(blob_ids: List[str]) -> List[Dict[str, Any]]:
    """Metadata (no bytes) for the given ids, in the ORDER GIVEN.

    Order is preserved in Python rather than in SQL so the caller's prompt-
    rewrite mapping is deterministic regardless of insertion timing.
    """
    if not blob_ids:
        return []
    by_id: Dict[str, Dict[str, Any]] = {}
    with get_db() as conn:
        cursor = conn.cursor()
        for blob_id in blob_ids:
            cursor.execute(
                f"SELECT {_META_COLS} FROM upload_blobs WHERE id = %s", (blob_id,)
            )
            row = row_to_dict(cursor.fetchone())
            if row is not None:
                by_id[blob_id] = row
    return [by_id[b] for b in blob_ids if b in by_id]


def list_blobs_for_session(session_id: str) -> List[Dict[str, Any]]:
    """Metadata (no bytes) for every blob lifted for a session, oldest first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT {_META_COLS} FROM upload_blobs WHERE session_id = %s "
            "ORDER BY created_at ASC, id ASC",
            (session_id,),
        )
        return rows_to_list(cursor.fetchall())


def get_blob_data(blob_id: str) -> Optional[bytes]:
    """Return a blob's stored bytes, or None when the row is absent or already
    materialized (``data`` NULLed)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM upload_blobs WHERE id = %s", (blob_id,))
        row = cursor.fetchone()
        if not row or row["data"] is None:
            return None
        # psycopg2 returns BYTEA as a memoryview; normalize to bytes.
        return bytes(row["data"])


def clear_data(blob_id: str) -> None:
    """Drop a blob's bytes once it has been written to the executing install's
    disk, stamping ``materialized_at``. The metadata row survives so a later
    materialization attempt can tell "already delivered" from "never existed"."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE upload_blobs SET data = NULL, materialized_at = NOW() "
            "WHERE id = %s",
            (blob_id,),
        )


def purge_old(older_than_days: int = 7) -> int:
    """Delete blob rows older than ``older_than_days`` (the required sweeper —
    the bytes column means this table must never grow unboundedly). Returns the
    number of rows removed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM upload_blobs WHERE created_at < %s", (cutoff,))
        return cursor.rowcount or 0
