"""
Logs API - Expose backend logs to the UI for debugging.

Provides endpoints to:
- Fetch recent log lines with optional filtering
- Export full log file as download
"""

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.api.auth import require_admin

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
