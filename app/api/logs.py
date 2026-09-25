"""
Logs API - Expose backend logs to the UI for debugging.

Provides endpoints to:
- Fetch recent log lines with optional filtering
- Export full log file as download
"""

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.api.auth import require_admin, require_auth, get_api_user_from_request
from app.db import error_logs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Logs"])

# ---------------------------------------------------------------------------
# In-memory ring buffer handler
# ---------------------------------------------------------------------------
# We keep the last N log records in memory so the UI can fetch them without
# needing a log file on disk.  This is lightweight and works regardless of
# deployment mode (Docker, bare-metal, etc.).
# ---------------------------------------------------------------------------

MAX_LOG_LINES = 5_000


class _LogRecord(BaseModel):
    timestamp: str
    level: str
    logger_name: str
    message: str


class _RingBufferHandler(logging.Handler):
    """Stores formatted log records in a fixed-size deque."""

    def __init__(self, capacity: int = MAX_LOG_LINES):
        super().__init__()
        self.buffer: deque[_LogRecord] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(
                _LogRecord(
                    timestamp=datetime.fromtimestamp(
                        record.created, tz=timezone.utc
                    ).isoformat(),
                    level=record.levelname,
                    logger_name=record.name,
                    message=self.format(record),
                )
            )
        except Exception:
            self.handleError(record)


# Singleton handler — attached to the root logger at import time.
ring_handler = _RingBufferHandler(capacity=MAX_LOG_LINES)
ring_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)

_root = logging.getLogger()
_root.addHandler(ring_handler)


# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------


class LogsResponse(BaseModel):
    lines: list[_LogRecord]
    total: int
    returned: int
    levels: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/v1/logs", response_model=LogsResponse)
async def get_logs(
    token: str = Depends(require_admin),
    limit: int = Query(500, ge=1, le=MAX_LOG_LINES),
    level: Optional[str] = Query(None, description="Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"),
    search: Optional[str] = Query(None, description="Case-insensitive substring search in message"),
) -> LogsResponse:
    """
    Fetch recent backend log lines (admin only).

    Returns the most recent `limit` lines, optionally filtered by level and
    search term.
    """
    buf = ring_handler.buffer

    # Collect unique levels for the filter dropdown
    all_levels = sorted({r.level for r in buf})

    # Apply filters
    filtered: list[_LogRecord] = []
    level_upper = level.upper() if level else None
    search_lower = search.lower() if search else None

    for record in reversed(buf):  # newest first
        if level_upper and record.level != level_upper:
            continue
        if search_lower and search_lower not in record.message.lower():
            continue
        filtered.append(record)
        if len(filtered) >= limit:
            break

    return LogsResponse(
        lines=filtered,
        total=len(buf),
        returned=len(filtered),
        levels=all_levels,
    )


@router.get("/api/v1/logs/export")
async def export_logs(
    token: str = Depends(require_admin),
) -> PlainTextResponse:
    """
    Export all buffered log lines as a downloadable text file (admin only).
    """
    buf = ring_handler.buffer
    text = "\n".join(r.message for r in buf)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"ai-shuffle-logs-{timestamp}.log"

    return PlainTextResponse(
        content=text,
        media_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ---------------------------------------------------------------------------
# Persistent error log (error_log table)
# ---------------------------------------------------------------------------
# Backend ERROR+ logs are captured automatically via ErrorLogDBHandler. The
# endpoints below let the SPA report client-side errors and let admins (and the
# GetAppErrors MCP tool, indirectly) read the durable error history.
# ---------------------------------------------------------------------------

# Sources a client is allowed to report (backend errors are captured server-side).
_CLIENT_SOURCES = {"frontend", "ws", "api"}

# Any caller, API keys included, can write here; the store clips message and
# detail itself, so bound the other free-form fields before they reach it.
_MAX_URL = 2048
_MAX_LEVEL = 16
_MAX_SESSION_ID = 200
_MAX_CONTEXT_BYTES = 16 * 1024


def _bounded_context(context: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The context as sent, or a size marker when its JSON is too large."""
    if not context:
        return context
    size = len(json.dumps(context, default=str))
    if size > _MAX_CONTEXT_BYTES:
        return {"truncated": True, "original_bytes": size}
    return context


class ClientErrorIn(BaseModel):
    message: str
    source: str = "frontend"
    level: str = "ERROR"
    detail: Optional[str] = None
    url: Optional[str] = None
    status_code: Optional[int] = None
    session_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None


class ErrorLogResponse(BaseModel):
    errors: list[dict[str, Any]]
    returned: int


@router.post("/api/v1/logs/client-error")
async def report_client_error(
    payload: ClientErrorIn,
    request: Request,
    token: str = Depends(require_auth),
) -> dict[str, Any]:
    """
    Record a client-side (frontend / WebSocket / API) error into the durable
    error log. Authenticated via cookie or API key.

    Called by the SPA's global error handlers and WS error handlers so frontend
    errors land in the same store agents read via GetAppErrors.
    """
    api_user = get_api_user_from_request(request)
    user_id = api_user.get("id") if api_user else "admin"
    source = payload.source if payload.source in _CLIENT_SOURCES else "frontend"

    error_id = await asyncio.to_thread(
        error_logs.record_error,
        source,
        payload.message,
        level=(payload.level or "ERROR").upper()[:_MAX_LEVEL],
        detail=payload.detail,
        url=payload.url[:_MAX_URL] if payload.url else payload.url,
        status_code=payload.status_code,
        session_id=payload.session_id[:_MAX_SESSION_ID] if payload.session_id else payload.session_id,
        user_id=user_id,
        context=_bounded_context(payload.context),
    )
    return {"ok": True, "id": error_id}


@router.get("/api/v1/logs/errors", response_model=ErrorLogResponse)
async def get_error_logs(
    token: str = Depends(require_admin),
    limit: int = Query(100, ge=1, le=500),
    source: Optional[str] = Query(None, description="Filter by source (frontend/backend/ws/api)"),
    level: Optional[str] = Query(None, description="Filter by level (ERROR/WARNING/CRITICAL)"),
    session_id: Optional[str] = Query(None, description="Filter by chat session id"),
) -> ErrorLogResponse:
    """Fetch recent persisted errors, most-recently-seen first (admin only)."""
    errors = await asyncio.to_thread(
        error_logs.get_recent_errors,
        limit=limit,
        source=source,
        level=level,
        session_id=session_id,
    )
    return ErrorLogResponse(errors=errors, returned=len(errors))
