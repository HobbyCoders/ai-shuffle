"""Rate limit and request log database operations."""

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class RateLimitRow(TypedDict):
    """Full rate limit row from SELECT * FROM rate_limits."""
    id: str
    user_id: Optional[str]
    api_key_id: Optional[str]
    requests_per_minute: int
    requests_per_hour: int
    requests_per_day: int
    concurrent_requests: int
    priority: int
    is_unlimited: bool
    created_at: str
    updated_at: str


class RequestLogEntry(TypedDict):
    """Request log entry."""
    id: str
    user_id: Optional[str]
    api_key_id: Optional[str]
    endpoint: str
    timestamp: str
    duration_ms: Optional[int]
    status: str


# ============================================================================
# Rate Limit Operations
# ============================================================================

def get_rate_limit(rate_limit_id: str) -> Optional[RateLimitRow]:
    """Get a rate limit configuration by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rate_limits WHERE id = ?", (rate_limit_id,))
        return row_to_dict(cursor.fetchone())


def get_rate_limit_for_user(user_id: Optional[str], api_key_id: Optional[str]) -> Optional[RateLimitRow]:
    """
    Get the most specific rate limit configuration for a user.
    Priority: api_key_id specific > user_id specific > default (null/null)
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Try API key specific first
        if api_key_id:
            cursor.execute("SELECT * FROM rate_limits WHERE api_key_id = ?", (api_key_id,))
            result = cursor.fetchone()
            if result:
                return row_to_dict(result)

        # Try user specific
        if user_id:
            cursor.execute("SELECT * FROM rate_limits WHERE user_id = ? AND api_key_id IS NULL", (user_id,))
            result = cursor.fetchone()
            if result:
                return row_to_dict(result)

        # Fall back to default (user_id and api_key_id both NULL)
        cursor.execute("SELECT * FROM rate_limits WHERE user_id IS NULL AND api_key_id IS NULL")
        result = cursor.fetchone()
        return row_to_dict(result) if result else None


def get_all_rate_limits() -> List[RateLimitRow]:
    """Get all rate limit configurations"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM rate_limits ORDER BY priority DESC, created_at ASC")
        return rows_to_list(cursor.fetchall())


def create_rate_limit(
    rate_limit_id: str,
    user_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    requests_per_minute: int = 20,
    requests_per_hour: int = 200,
    requests_per_day: int = 1000,
    concurrent_requests: int = 3,
    priority: int = 0,
    is_unlimited: bool = False
) -> RateLimitRow:
    """Create a new rate limit configuration"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO rate_limits
               (id, user_id, api_key_id, requests_per_minute, requests_per_hour,
                requests_per_day, concurrent_requests, priority, is_unlimited, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rate_limit_id, user_id, api_key_id, requests_per_minute, requests_per_hour,
             requests_per_day, concurrent_requests, priority, is_unlimited, now, now)
        )
    return get_rate_limit(rate_limit_id)


def update_rate_limit(
    rate_limit_id: str,
    requests_per_minute: Optional[int] = None,
    requests_per_hour: Optional[int] = None,
    requests_per_day: Optional[int] = None,
    concurrent_requests: Optional[int] = None,
    priority: Optional[int] = None,
    is_unlimited: Optional[bool] = None
) -> Optional[RateLimitRow]:
    """Update a rate limit configuration"""
    existing = get_rate_limit(rate_limit_id)
    if not existing:
        return None

    updates = ["updated_at = ?"]
    values = [datetime.utcnow().isoformat()]

    if requests_per_minute is not None:
        updates.append("requests_per_minute = ?")
        values.append(requests_per_minute)
    if requests_per_hour is not None:
        updates.append("requests_per_hour = ?")
        values.append(requests_per_hour)
    if requests_per_day is not None:
        updates.append("requests_per_day = ?")
        values.append(requests_per_day)
    if concurrent_requests is not None:
        updates.append("concurrent_requests = ?")
        values.append(concurrent_requests)
    if priority is not None:
        updates.append("priority = ?")
        values.append(priority)
    if is_unlimited is not None:
        updates.append("is_unlimited = ?")
        values.append(is_unlimited)

    values.append(rate_limit_id)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE rate_limits SET {', '.join(updates)} WHERE id = ?",
            values
        )

    return get_rate_limit(rate_limit_id)


def delete_rate_limit(rate_limit_id: str) -> bool:
    """Delete a rate limit configuration"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM rate_limits WHERE id = ?", (rate_limit_id,))
        return cursor.rowcount > 0


# ============================================================================
# Request Log Operations
# ============================================================================

def log_request(
    request_id: str,
    user_id: Optional[str],
    api_key_id: Optional[str],
    endpoint: str,
    status: str = "success",
    duration_ms: Optional[int] = None
) -> RequestLogEntry:
    """Log a request for rate limit tracking"""
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO request_log (id, user_id, api_key_id, endpoint, timestamp, duration_ms, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (request_id, user_id, api_key_id, endpoint, now, duration_ms, status)
        )
    return {
        "id": request_id,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "endpoint": endpoint,
        "timestamp": now,
        "duration_ms": duration_ms,
        "status": status
    }


def get_request_count(
    user_id: Optional[str],
    api_key_id: Optional[str],
    since: datetime,
    endpoint: Optional[str] = None
) -> int:
    """Get the count of requests since a given time"""
    with get_db() as conn:
        cursor = conn.cursor()

        query = "SELECT COUNT(*) as count FROM request_log WHERE timestamp >= ?"
        params = [since.isoformat()]

        if api_key_id:
            query += " AND api_key_id = ?"
            params.append(api_key_id)
        elif user_id:
            query += " AND (user_id = ? OR api_key_id IN (SELECT id FROM api_users WHERE name = ?))"
            params.extend([user_id, user_id])

        if endpoint:
            query += " AND endpoint = ?"
            params.append(endpoint)

        cursor.execute(query, params)
        row = cursor.fetchone()
        return row["count"] if row else 0


def cleanup_old_request_logs(older_than: datetime) -> int:
    """Remove request logs older than a given time"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM request_log WHERE timestamp < ?",
            (older_than.isoformat(),)
        )
        return cursor.rowcount
