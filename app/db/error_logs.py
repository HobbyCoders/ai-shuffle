"""
Error log database operations.

Durable, queryable record of every error in the app:

- **Backend** unhandled exceptions and ERROR-level logs, captured at the
  logging layer by ``app/core/error_log_handler.py``.
- **Frontend / API / WebSocket** errors, POSTed to
  ``/api/v1/logs/client-error`` (see ``app/api/logs.py``).

Read by the admin logs UI (``GET /api/v1/logs/errors``) and — the whole point —
by the ``GetAppErrors`` MCP tool, so an agent can debug what the user just hit
("I got this error, check the logs and fix it").

Repeated errors that share a ``fingerprint`` within ``_DEDUPE_WINDOW_SECONDS``
collapse onto one row (``occurrence_count`` bumped, ``updated_at`` refreshed)
so a render loop or crash storm can't flood the table.

All functions here are defensive: error logging must never raise back into the
caller, and read paths must never crash a tool call.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from app.db.base import get_db, rows_to_list, logger

# Repeats of the same fingerprint seen within this window collapse onto the
# existing row instead of inserting a new one.
_DEDUPE_WINDOW_SECONDS = 300

# Opportunistic pruning: every Nth fresh insert, trim the table back down.
_PRUNE_EVERY = 200
_insert_count = 0

VALID_SOURCES = {"frontend", "backend", "ws", "api"}

# Bound stored sizes so a giant payload can't bloat a single row.
_MAX_MESSAGE = 2000
_MAX_DETAIL = 20000


def _fingerprint(source: str, message: str, detail: Optional[str]) -> str:
    """Stable short hash of (source, message, first line of detail) for dedupe."""
    first_line = ""
    if detail and detail.strip():
        first_line = detail.strip().splitlines()[0]
    raw = f"{source}\x1f{message}\x1f{first_line}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def record_error(
    source: str,
    message: str,
    *,
    level: str = "ERROR",
    detail: Optional[str] = None,
    logger_name: Optional[str] = None,
    url: Optional[str] = None,
    status_code: Optional[int] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Persist an error and return its row id (or None on failure).

    Repeated errors sharing a fingerprint within ``_DEDUPE_WINDOW_SECONDS``
    collapse onto the existing row. Never raises — error logging must not break
    the caller or the request/log path it runs in.
    """
    global _insert_count
    try:
        message = (message or "").strip()[:_MAX_MESSAGE] or "(no message)"
        if detail:
            detail = detail[:_MAX_DETAIL]
        source = source if source in VALID_SOURCES else "backend"
        fp = _fingerprint(source, message, detail)
        ctx_json = json.dumps(context) if context else None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_DEDUPE_WINDOW_SECONDS)

        with get_db() as conn:
            cursor = conn.cursor()
            # Collapse onto a recent identical error if one exists.
            cursor.execute(
                """UPDATE error_log
                      SET occurrence_count = occurrence_count + 1,
                          updated_at = NOW()
                    WHERE id = (
                        SELECT id FROM error_log
                         WHERE fingerprint = %s
                           AND updated_at > %s
                         ORDER BY updated_at DESC
                         LIMIT 1
                    )
                    RETURNING id""",
                (fp, cutoff),
            )
            row = cursor.fetchone()
            if row:
                return row["id"]

            error_id = str(uuid.uuid4())
            cursor.execute(
                """INSERT INTO error_log
                      (id, source, level, message, detail, logger_name, url,
                       status_code, session_id, user_id, context, fingerprint)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (error_id, source, level, message, detail, logger_name, url,
                 status_code, session_id, user_id, ctx_json, fp),
            )

        _insert_count += 1
        if _insert_count % _PRUNE_EVERY == 0:
            prune_errors()
        return error_id
    except Exception:
        # Last resort: never let error logging raise. Use logger.debug (not
        # error) so this can't feed back through the ErrorLogDBHandler.
        logger.debug("record_error failed", exc_info=True)
        return None


def get_recent_errors(
    limit: int = 50,
    source: Optional[str] = None,
    level: Optional[str] = None,
    session_id: Optional[str] = None,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return recent errors, most-recently-seen first.

    JSON-serializable: timestamps become ISO strings and ``context`` is a dict.
    Returns ``[]`` on any failure rather than raising.
    """
    try:
        query = "SELECT * FROM error_log WHERE 1=1"
        params: List[Any] = []
        if source:
            query += " AND source = %s"
            params.append(source)
        if level:
            query += " AND level = %s"
            params.append(level.upper())
        if session_id:
            query += " AND session_id = %s"
            params.append(session_id)
        if since:
            query += " AND updated_at > %s"
            params.append(since)
        query += " ORDER BY updated_at DESC LIMIT %s"
        params.append(max(1, min(limit, 500)))

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = rows_to_list(cursor.fetchall())

        for row in rows:
            for key in ("created_at", "updated_at"):
                val = row.get(key)
                if val is not None and hasattr(val, "isoformat"):
                    row[key] = val.isoformat()
            ctx = row.get("context")
            if isinstance(ctx, str):
                try:
                    row["context"] = json.loads(ctx)
                except (ValueError, TypeError):
                    pass
        return rows
    except Exception:
        logger.debug("get_recent_errors failed", exc_info=True)
        return []


def prune_errors(keep_days: int = 7, max_rows: int = 5000) -> int:
    """Bound table growth.

    Deletes rows older than ``keep_days``, then trims to the newest
    ``max_rows``. Returns rows deleted. Never raises.
    """
    deleted = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM error_log WHERE updated_at < %s", (cutoff,))
            deleted += cursor.rowcount or 0
            cursor.execute(
                """DELETE FROM error_log
                    WHERE id IN (
                        SELECT id FROM error_log
                         ORDER BY updated_at DESC
                         OFFSET %s
                    )""",
                (max_rows,),
            )
            deleted += cursor.rowcount or 0
    except Exception:
        logger.debug("prune_errors failed", exc_info=True)
    return deleted
