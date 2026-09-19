"""Usage logging and analytics database operations."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger
from app.db.compat import sql_date, sql_strftime

# Whitelisted values for dynamic query parameters to prevent SQL injection.
# These are the ONLY allowed values for group_by and interval parameters.
_ALLOWED_GROUP_BY = frozenset({"profile", "user", "date"})
_ALLOWED_INTERVALS = frozenset({"day", "week", "month"})


def _interval_date_expressions() -> Dict[str, str]:
    """Return interval-to-SQL-expression mapping for the active backend."""
    return {
        "day": sql_date("u.created_at"),
        "week": sql_strftime("%Y-W%W", "u.created_at"),
        "month": sql_strftime("%Y-%m", "u.created_at"),
    }


def _to_iso(value: Any) -> Any:
    """Normalize a DB date/datetime value to an ISO-format string.

    PostgreSQL returns native ``date``/``datetime`` objects for ``::date`` casts
    and timestamp columns, but the analytics API contract (and its Pydantic
    response models) expects ISO strings. ``None`` and existing strings (e.g. the
    ``to_char`` week/month buckets) pass through unchanged.
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


# ============================================================================
# TypedDict definitions
# ============================================================================

class LastSessionUsage(TypedDict):
    """Last usage entry for a session (renamed fields for API compat)."""
    total_cost_usd: float
    duration_ms: int
    tokens_in: int
    tokens_out: int
    model: Optional[str]


class SessionUsageEntry(TypedDict):
    """Per-turn usage entry with cumulative cost."""
    cost_usd: float
    cumulative_cost_usd: float
    duration_ms: int
    tokens_in: int
    tokens_out: int
    model: Optional[str]
    turn_index: int


class UsageStats(TypedDict):
    """Aggregate usage statistics."""
    total_sessions: int
    total_queries: int
    total_tokens_in: int
    total_tokens_out: int
    total_cost_usd: float


class AnalyticsUsageStats(TypedDict):
    """Analytics usage stats for a date range."""
    total_tokens_in: int
    total_tokens_out: int
    total_cost_usd: float
    query_count: int
    session_count: int


class CostBreakdownEntry(TypedDict):
    """Cost breakdown grouped by profile/user/date."""
    key: str
    name: Optional[str]
    total_tokens_in: int
    total_tokens_out: int
    total_cost_usd: float
    query_count: int


class UsageTrendEntry(TypedDict):
    """Usage trend data point."""
    date: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    query_count: int


class TopSessionEntry(TypedDict):
    """Top session by cost."""
    session_id: str
    title: Optional[str]
    profile_id: Optional[str]
    profile_name: Optional[str]
    total_cost_usd: float
    total_tokens_in: int
    total_tokens_out: int
    created_at: str


# ============================================================================
# Usage Log Operations
# ============================================================================

def log_usage(
    session_id: Optional[str],
    profile_id: Optional[str],
    model: Optional[str],
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    duration_ms: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
):
    """Log usage for tracking.

    ``tokens_in`` is FRESH input only (usage is normalized Anthropic-style, so
    cache reads are excluded from it). A turn's true prompt size is
    ``tokens_in + cache_read_tokens + cache_creation_tokens``.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO usage_log (session_id, profile_id, model, tokens_in, tokens_out,
                                      cache_read_tokens, cache_creation_tokens,
                                      cost_usd, duration_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (session_id, profile_id, model, tokens_in, tokens_out,
             cache_read_tokens or 0, cache_creation_tokens or 0,
             cost_usd, duration_ms)
        )

    # Update in-memory usage cache (avoids DB re-read on next load)
    if session_id:
        from app.core.cost_utils import append_usage_entry
        append_usage_entry(session_id, cost_usd, duration_ms, tokens_in, tokens_out, model)


def settle_turn_usage(
    session_id: str, settlement_key: str, *, profile_id=None, model=None,
    tokens_in=0, tokens_out=0, cache_read_tokens=0, cache_creation_tokens=0,
    cost_usd=0, duration_ms=0, model_calls=0, turn_increment=0,
) -> bool:
    """Claim an execution attempt and book its ledger/counters in one commit.

    The key is captured by the producer, never inferred from token equality or
    whichever turn happens to be live during finalization. A unique INSERT waits
    for competing transactions in PostgreSQL; a rollback releases the claim too.
    """
    from app.db.sessions import update_session

    if not settlement_key:
        raise ValueError("Usage settlement requires an execution identity")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO usage_log (settlement_key, session_id, profile_id, model, "
            "tokens_in, tokens_out, cache_read_tokens, cache_creation_tokens, "
            "cost_usd, duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (settlement_key) DO NOTHING RETURNING id",
            (settlement_key, session_id, profile_id, model, tokens_in or 0,
             tokens_out or 0, cache_read_tokens or 0, cache_creation_tokens or 0,
             cost_usd or 0, duration_ms or 0),
        )
        if cursor.fetchone() is None:
            return False
        row = update_session(
            session_id, cost_increment=cost_usd or 0,
            tokens_in_increment=tokens_in or 0, tokens_out_increment=tokens_out or 0,
            cache_read_increment=cache_read_tokens or 0,
            cache_creation_increment=cache_creation_tokens or 0,
            model_calls_increment=model_calls or 0, turn_increment=turn_increment,
            _conn=conn,
        )
        if row is None:
            raise ValueError(f"Unknown usage session: {session_id}")
    # Only committed bookings invalidate the cache; never append speculatively.
    from app.core.cost_utils import invalidate_usage_cache
    invalidate_usage_cache(session_id)
    return True


def get_last_session_usage(session_id: str) -> Optional[LastSessionUsage]:
    """Get the most recent usage entry for a session.

    Returns cost and duration info for the last query turn in the session.
    This is used to populate metadata for the last assistant message when
    loading chat history (since JSONL files don't store cost/duration).

    Returns:
        Dict with cost_usd, duration_ms, tokens_in, tokens_out, model
        or None if no usage found
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cost_usd, duration_ms, tokens_in, tokens_out, model
            FROM usage_log
            WHERE session_id = %s
            ORDER BY id DESC
            LIMIT 1
        """, (session_id,))
        row = cursor.fetchone()
        if row:
            return {
                "total_cost_usd": row["cost_usd"],
                "duration_ms": row["duration_ms"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "model": row["model"]
            }
        return None


def get_all_session_usage(session_id: str) -> List[SessionUsageEntry]:
    """Get ALL usage entries for a session, ordered by turn.

    Returns list of usage entries, one per conversation turn.
    The Nth entry corresponds to the Nth user->assistant turn.
    Includes both per-turn cost and cumulative cost running total.

    Args:
        session_id: The session to get usage for

    Returns:
        List of dicts with: cost_usd, cumulative_cost_usd, duration_ms,
        tokens_in, tokens_out, model, turn_index
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cost_usd, duration_ms, tokens_in, tokens_out, model
            FROM usage_log
            WHERE session_id = %s
            ORDER BY id ASC
        """, (session_id,))
        rows = cursor.fetchall()

        result = []
        cumulative_cost = 0.0
        for turn_index, row in enumerate(rows):
            cost = row["cost_usd"] or 0.0
            cumulative_cost += cost
            result.append({
                "cost_usd": cost,
                "cumulative_cost_usd": cumulative_cost,
                "duration_ms": row["duration_ms"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "model": row["model"],
                "turn_index": turn_index
            })
        return result


def get_usage_stats() -> UsageStats:
    """Get aggregate usage statistics"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COUNT(*) as total_queries,
                COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                COALESCE(SUM(tokens_out), 0) as total_tokens_out,
                COALESCE(SUM(cost_usd), 0) as total_cost_usd
            FROM usage_log
        """)
        row = cursor.fetchone()

        cursor.execute("SELECT COUNT(*) as count FROM sessions")
        sessions_row = cursor.fetchone()

        return {
            "total_sessions": sessions_row["count"],
            "total_queries": row["total_queries"],
            "total_tokens_in": row["total_tokens_in"],
            "total_tokens_out": row["total_tokens_out"],
            "total_cost_usd": row["total_cost_usd"]
        }


# ============================================================================
# Analytics Operations
# ============================================================================

def get_analytics_usage_stats(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    api_user_id: Optional[str] = None
) -> AnalyticsUsageStats:
    """
    Get aggregate usage statistics for a date range.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        Dictionary with total_tokens_in, total_tokens_out, total_cost_usd, session_count, query_count
    """
    _date_expr = sql_date("ul.created_at")
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter = ""
        params = []
        if start_date:
            date_filter += f" AND {_date_expr} >= %s"
            params.append(start_date)
        if end_date:
            date_filter += f" AND {_date_expr} <= %s"
            params.append(end_date)

        # Add user filter if specified
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = %s"
            params.append(api_user_id)

        # Get usage log aggregates (join with sessions for user filtering)
        cursor.execute(f"""
            SELECT
                COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                COUNT(*) as query_count
            FROM usage_log ul
            LEFT JOIN sessions s ON ul.session_id = s.id
            WHERE 1=1 {date_filter} {user_filter}
        """, params)
        usage_row = cursor.fetchone()

        # Get session count for the same period
        cursor.execute(f"""
            SELECT COUNT(DISTINCT ul.session_id) as session_count
            FROM usage_log ul
            LEFT JOIN sessions s ON ul.session_id = s.id
            WHERE ul.session_id IS NOT NULL {date_filter} {user_filter}
        """, params)
        session_row = cursor.fetchone()

        return {
            "total_tokens_in": usage_row["total_tokens_in"],
            "total_tokens_out": usage_row["total_tokens_out"],
            "total_cost_usd": round(usage_row["total_cost_usd"], 6),
            "query_count": usage_row["query_count"],
            "session_count": session_row["session_count"]
        }


def get_analytics_cost_breakdown(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    group_by: str = "profile",
    api_user_id: Optional[str] = None
) -> List[CostBreakdownEntry]:
    """
    Get cost breakdown grouped by profile, user, or date.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        group_by: Grouping - 'profile', 'user', or 'date'
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        List of dictionaries with key, name, total_cost_usd, total_tokens_in, total_tokens_out, query_count
    """
    _date_expr = sql_date("ul.created_at")
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter = ""
        params = []
        if start_date:
            date_filter += f" AND {_date_expr} >= %s"
            params.append(start_date)
        if end_date:
            date_filter += f" AND {_date_expr} <= %s"
            params.append(end_date)

        # Add user filter if specified
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = %s"
            params.append(api_user_id)

        # Validate group_by against whitelist (defense-in-depth against SQL injection)
        if group_by not in _ALLOWED_GROUP_BY:
            logger.warning(f"Invalid group_by value rejected: {group_by!r}")
            return []

        if group_by == "profile":
            cursor.execute(f"""
                SELECT
                    COALESCE(ul.profile_id, 'unknown') as key,
                    p.name as name,
                    COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                    COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                    COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                    COUNT(*) as query_count
                FROM usage_log ul
                LEFT JOIN agents p ON ul.profile_id = p.id
                LEFT JOIN sessions s ON ul.session_id = s.id
                WHERE 1=1 {date_filter} {user_filter}
                GROUP BY ul.profile_id, p.name
                ORDER BY total_cost_usd DESC
            """, params)

        elif group_by == "user":
            # Join with sessions to get api_user_id, then with api_users for name
            cursor.execute(f"""
                SELECT
                    COALESCE(s.api_user_id, 'admin') as key,
                    CASE
                        WHEN s.api_user_id IS NULL THEN 'Admin'
                        ELSE COALESCE(au.name, 'Unknown User')
                    END as name,
                    COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                    COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                    COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                    COUNT(*) as query_count
                FROM usage_log ul
                LEFT JOIN sessions s ON ul.session_id = s.id
                LEFT JOIN api_users au ON s.api_user_id = au.id
                WHERE 1=1 {date_filter} {user_filter}
                GROUP BY s.api_user_id, au.name
                ORDER BY total_cost_usd DESC
            """, params)

        elif group_by == "date":
            _group_date_expr = sql_date("ul.created_at")
            cursor.execute(f"""
                SELECT
                    {_group_date_expr} as key,
                    {_group_date_expr} as name,
                    COALESCE(SUM(ul.tokens_in), 0) as total_tokens_in,
                    COALESCE(SUM(ul.tokens_out), 0) as total_tokens_out,
                    COALESCE(SUM(ul.cost_usd), 0) as total_cost_usd,
                    COUNT(*) as query_count
                FROM usage_log ul
                LEFT JOIN sessions s ON ul.session_id = s.id
                WHERE 1=1 {date_filter} {user_filter}
                GROUP BY {_group_date_expr}
                ORDER BY {_group_date_expr} DESC
            """, params)

        else:
            return []

        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "key": str(row["key"]) if row["key"] else "unknown",
                "name": _to_iso(row["name"]),
                "total_tokens_in": row["total_tokens_in"],
                "total_tokens_out": row["total_tokens_out"],
                "total_cost_usd": round(row["total_cost_usd"], 6),
                "query_count": row["query_count"]
            })
        return result


def get_analytics_usage_trends(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "day",
    api_user_id: Optional[str] = None
) -> List[UsageTrendEntry]:
    """
    Get usage trends over time.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        interval: Time interval - 'day', 'week', or 'month'
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        List of dictionaries with date, tokens_in, tokens_out, cost_usd, query_count
    """
    _date_expr = sql_date("u.created_at")
    with get_db() as conn:
        cursor = conn.cursor()

        # Build filter clauses
        date_filter = ""
        params = []
        if start_date:
            date_filter += f" AND {_date_expr} >= %s"
            params.append(start_date)
        if end_date:
            date_filter += f" AND {_date_expr} <= %s"
            params.append(end_date)

        # User filter for API users (join with sessions to get api_user_id)
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = %s"
            params.append(api_user_id)

        # Choose date grouping based on interval — looked up from a safe whitelist.
        # This prevents SQL injection even if `interval` were to contain malicious input.
        if interval not in _ALLOWED_INTERVALS:
            logger.warning(f"Invalid interval value rejected: {interval!r}")
            return []
        date_expr = _interval_date_expressions()[interval]

        # LEFT JOIN (like the stats/breakdown queries) so session-less rows —
        # internal generation spend, which has no session — still show up.
        cursor.execute(f"""
            SELECT
                {date_expr} as date,
                COALESCE(SUM(u.tokens_in), 0) as tokens_in,
                COALESCE(SUM(u.tokens_out), 0) as tokens_out,
                COALESCE(SUM(u.cost_usd), 0) as cost_usd,
                COUNT(*) as query_count
            FROM usage_log u
            LEFT JOIN sessions s ON u.session_id = s.id
            WHERE 1=1 {date_filter} {user_filter}
            GROUP BY {date_expr}
            ORDER BY date ASC
        """, params)

        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "date": _to_iso(row["date"]),
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "cost_usd": round(row["cost_usd"], 6),
                "query_count": row["query_count"]
            })
        return result


def get_analytics_top_sessions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 10,
    api_user_id: Optional[str] = None
) -> List[TopSessionEntry]:
    """
    Get top sessions by cost.

    Args:
        start_date: Start date (ISO format YYYY-MM-DD), inclusive
        end_date: End date (ISO format YYYY-MM-DD), inclusive
        limit: Maximum number of sessions to return
        api_user_id: Optional API user ID to filter by (None = all users)

    Returns:
        List of dictionaries with session_id, title, profile_id, profile_name, total_cost_usd, etc.
    """
    _date_expr = sql_date("s.created_at")
    with get_db() as conn:
        cursor = conn.cursor()

        # Build filter clauses
        date_filter = ""
        params = []
        if start_date:
            date_filter += f" AND {_date_expr} >= %s"
            params.append(start_date)
        if end_date:
            date_filter += f" AND {_date_expr} <= %s"
            params.append(end_date)

        # User filter for API users
        user_filter = ""
        if api_user_id:
            user_filter = " AND s.api_user_id = %s"
            params.append(api_user_id)

        params.append(limit)

        cursor.execute(f"""
            SELECT
                s.id as session_id,
                s.title,
                s.profile_id,
                p.name as profile_name,
                s.total_cost_usd,
                -- TRUE prompt tokens. s.total_tokens_in is FRESH input only, so
                -- on a cache-heavy session it under-reports by roughly an order
                -- of magnitude; the cumulative cache columns are the rest.
                COALESCE(s.total_tokens_in, 0)
                    + COALESCE(s.total_cache_read_tokens, 0)
                    + COALESCE(s.total_cache_creation_tokens, 0)
                    AS total_tokens_in,
                s.total_tokens_out,
                s.created_at
            FROM sessions s
            LEFT JOIN agents p ON s.profile_id = p.id
            WHERE s.total_cost_usd > 0 {date_filter} {user_filter}
            ORDER BY s.total_cost_usd DESC
            LIMIT %s
        """, params)

        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "session_id": row["session_id"],
                "title": row["title"],
                "profile_id": row["profile_id"],
                "profile_name": row["profile_name"],
                "total_cost_usd": round(row["total_cost_usd"], 6) if row["total_cost_usd"] else 0,
                "total_tokens_in": row["total_tokens_in"] or 0,
                "total_tokens_out": row["total_tokens_out"] or 0,
                "created_at": _to_iso(row["created_at"])
            })
        return result
