"""Content-addressed artifact registry — the ``artifacts`` table (v2 core).

Blobs are deduplicated by their content ``hash`` (the primary key). Upserts are
idempotent: the first writer's metadata wins and subsequent upserts of the same
hash are no-ops that still return the stored row.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.db.base import get_db

logger = logging.getLogger(__name__)


def upsert_artifact(
    hash: str,
    size: int,
    mime: Optional[str],
    filename: Optional[str],
    created_by_session: Optional[str],
) -> Dict[str, Any]:
    """Register an artifact by content hash (idempotent). Returns the stored
    row — on a repeat hash the original metadata is preserved."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO artifacts (hash, size, mime, filename, created_by_session) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (hash) DO NOTHING",
            (hash, size, mime, filename, created_by_session),
        )
        cursor.execute("SELECT * FROM artifacts WHERE hash=%s", (hash,))
        row = cursor.fetchone()
    return dict(row)


def get_artifact(hash: str) -> Optional[Dict[str, Any]]:
    """Fetch an artifact by content hash, or None."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM artifacts WHERE hash=%s", (hash,))
        row = cursor.fetchone()
    return dict(row) if row else None
