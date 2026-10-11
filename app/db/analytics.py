"""Usage logging and analytics database operations."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, logger
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


def _created_at_range(
    column: str, start_date: Optional[str], end_date: Optional[str]
) -> tuple[str, List[Any]]:
    """Inclusive YYYY-MM-DD range filter on a timestamp column, as SQL + params.

    Compares the raw column against day boundaries (``>= start``,
    ``< end + 1 day``) instead of casting it with ``::date``, so PostgreSQL can
    serve the Stats date range from ``idx_usage_log_created``; the cast form
    forces a sequential scan of the whole ledger. The days are the same: a bare
    'YYYY-MM-DD' compared with a timestamptz is midnight in the session time
    zone, as is the ``::date`` cast. An end date that does not parse keeps the
    cast form rather than guessing a boundary.
    """
    sql = ""
    params: List[Any] = []
    if start_date:
        sql += f" AND {column} >= %s"
        params.append(start_date)
    if end_date:
        try:
            next_day = datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1)
        except ValueError:
            sql += f" AND {sql_date(column)} <= %s"
            params.append(end_date)
        else:
            sql += f" AND {column} < %s"
            params.append(next_day.isoformat())
    return sql, params


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


class SessionUsageEntry(TypedDict):
    """Per-turn usage entry with cumulative cost."""
    cost_usd: float
    cumulative_cost_usd: float
    duration_ms: int
    tokens_in: int
    tokens_out: int
    model: Optional[str]
    turn_index: int
    #: True when any token count booked for this turn was estimated by the
    #: harness (the provider reported no usage, or the reply was interrupted).
    usage_estimated: bool


class UsageStats(TypedDict):
    """Aggregate usage statistics."""
    total_sessions: int
    total_queries: int
    total_tokens_in: int
    total_tokens_out: int
    total_cost_usd: float
    #: Turns whose token counts were estimated (see ``usage_estimated``).
    estimated_queries: int


class AnalyticsUsageStats(TypedDict):
    """Analytics usage stats for a date range."""
    total_tokens_in: int
    total_tokens_out: int
    total_cost_usd: float
    query_count: int
    session_count: int
    #: How many of ``query_count`` booked estimated token counts.
    estimated_query_count: int


class CostBreakdownEntry(TypedDict):
    """Cost breakdown grouped by profile/user/date."""
    key: str
    name: Optional[str]
    total_tokens_in: int
    total_tokens_out: int
    total_cost_usd: float
    query_count: int
    estimated_query_count: int


class UsageTrendEntry(TypedDict):
    """Usage trend data point."""
    date: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    query_count: int
    estimated_query_count: int


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
    #: True when any of the session's booked turns has estimated token counts.
    usage_estimated: bool


# ============================================================================
# Usage Log Operations
# ============================================================================

#: ``COUNT`` of the rows whose ``usage_estimated`` flag is set, in a form both
#: PostgreSQL (BOOLEAN) and the SQLite test shim (0/1) evaluate the same way.
#: ``.format(col="ul.usage_estimated")`` names the column.
_ESTIMATED_COUNT_SQL = "COALESCE(SUM(CASE WHEN {col} THEN 1 ELSE 0 END), 0)"

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
    usage_estimated: bool = False,
    provider_id: Optional[str] = None,
    provider_type: Optional[str] = None,
    billing: Optional[str] = None,
):
    """Log usage for tracking.

    ``tokens_in`` is FRESH input only (usage is normalized Anthropic-style, so
    cache reads are excluded from it). A turn's true prompt size is
    ``tokens_in + cache_read_tokens + cache_creation_tokens``.

    ``usage_estimated`` marks a turn whose token counts the harness guessed
    (the provider reported no usage, or the reply was interrupted). The
    amounts are booked all the same; the flag only labels them.

    ``provider_id``/``provider_type``/``billing`` attribute the row to the
    provider leg that served it (see ``app.core.usage_attribution``).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO usage_log (session_id, profile_id, model, tokens_in, tokens_out,
                                      cache_read_tokens, cache_creation_tokens,
                                      cost_usd, duration_ms, usage_estimated,
                                      provider_id, provider_type, billing)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (session_id, profile_id, model, tokens_in, tokens_out,
             cache_read_tokens or 0, cache_creation_tokens or 0,
             cost_usd, duration_ms, bool(usage_estimated),
             provider_id, provider_type, billing)
        )

    # Update in-memory usage cache (avoids DB re-read on next load)
    if session_id:
        from app.core.cost_utils import append_usage_entry
        append_usage_entry(session_id, cost_usd, duration_ms, tokens_in, tokens_out, model,
                           usage_estimated=bool(usage_estimated))


def settle_turn_usage(
    session_id: str, settlement_key: str, *, profile_id=None, model=None,
    tokens_in=0, tokens_out=0, cache_read_tokens=0, cache_creation_tokens=0,
    cost_usd=0, duration_ms=0, model_calls=0, turn_increment=0,
    usage_estimated=False, provider_id=None, provider_type=None, billing=None,
) -> bool:
    """Claim an execution attempt and book its ledger/counters in one commit.

    The key is captured by the producer, never inferred from token equality or
    whichever turn happens to be live during finalization. A unique INSERT waits
    for competing transactions in PostgreSQL; a rollback releases the claim too.

    ``usage_estimated`` rides the ledger row only: the session counters and
    every budget add the estimated amounts like any other (owner decision);
    the flag marks the turn as an estimate in the usage displays.

    ``provider_id``/``provider_type``/``billing`` ride the ledger row only and
    attribute it to the provider leg that served the turn.
    """
    from app.db.sessions import update_session

    if not settlement_key:
        raise ValueError("Usage settlement requires an execution identity")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO usage_log (settlement_key, session_id, profile_id, model, "
            "tokens_in, tokens_out, cache_read_tokens, cache_creation_tokens, "
            "cost_usd, duration_ms, usage_estimated, provider_id, provider_type, billing) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (settlement_key) DO NOTHING RETURNING id",
            (settlement_key, session_id, profile_id, model, tokens_in or 0,
             tokens_out or 0, cache_read_tokens or 0, cache_creation_tokens or 0,
             cost_usd or 0, duration_ms or 0, bool(usage_estimated),
             provider_id, provider_type, billing),
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


#: Turn sources whose model the owner chose by typing into a chat. Same set as
#: ``app.core.model_override.USER_SOURCES`` (kept literal here so the db layer
#: does not import app.core): wakes, automations, Task workers, connectors,
#: peer messages and resumes are not counted.
_CHOSEN_TURN_SOURCES = ("user", "queue", "worktree", "api")

#: Length of a ``turns.id`` minted by ``turns.enqueue_turn`` /
#: ``record_adhoc_turn``: ``"turn-" + uuid4().hex``.
_TURN_ID_LEN = 37


def get_most_used_models(since: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Booked turns per ``(provider_id, provider_type, model)`` since ``since``.

    Counts only turns the owner picked a model for: ``usage_log`` rows settled
    for a ``turns`` row whose ``source`` is a typed message
    (:data:`_CHOSEN_TURN_SOURCES`), in a list-visible session that is not a
    delegated worker (``is_worker``), not hidden (wake-word command sessions),
    not an automation run (``automation_id``) and not a voice call
    (``mode = 'voice'``). One-shot text generation (titles, commit text,
    suggestions) books with no session or turn and never matches.

    The turn is found from the settlement key
    (``query_persistence.usage_settlement_key``: ``["<session>","<turn>",…]``),
    so the turn id is the fixed-length slice after the session id and the join
    is a primary-key lookup. Rows without a settlement key (older bookings,
    one-shot generation) are not counted.

    Ordered by turns, then most recent use. ``since`` is a UTC timestamp string.
    """
    placeholders = ", ".join(["%s"] * len(_CHOSEN_TURN_SOURCES))
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT ul.provider_id, ul.provider_type, ul.model,
                       COUNT(*) AS turn_count, MAX(ul.created_at) AS last_used
                FROM usage_log ul
                JOIN turns t
                  ON t.id = substr(ul.settlement_key, length(ul.session_id) + 6, {_TURN_ID_LEN})
                 AND t.session_id = ul.session_id
                JOIN sessions s ON s.id = ul.session_id
                WHERE ul.created_at >= %s
                  AND ul.settlement_key IS NOT NULL
                  AND ul.model IS NOT NULL AND ul.model <> ''
                  AND t.source IN ({placeholders})
                  AND s.automation_id IS NULL
                  AND COALESCE(s.is_worker, FALSE) = FALSE
                  AND COALESCE(s.hidden, FALSE) = FALSE
                  AND COALESCE(s.mode, '') <> 'voice'
                GROUP BY ul.provider_id, ul.provider_type, ul.model
                ORDER BY turn_count DESC, last_used DESC
                LIMIT %s""",
            (since, *_CHOSEN_TURN_SOURCES, int(limit)),
        )
        return [dict(row) for row in cursor.fetchall()]


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
            SELECT cost_usd, duration_ms, tokens_in, tokens_out, model, usage_estimated
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
                "turn_index": turn_index,
                "usage_estimated": bool(row["usage_estimated"]),
            })
        return result


def get_usage_stats() -> UsageStats:
    """Get aggregate usage statistics"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT
                COUNT(*) as total_queries,
                COALESCE(SUM(tokens_in), 0) as total_tokens_in,
                COALESCE(SUM(tokens_out), 0) as total_tokens_out,
                COALESCE(SUM(cost_usd), 0) as total_cost_usd,
                {_ESTIMATED_COUNT_SQL.format(col="usage_estimated")} as estimated_queries
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
            "total_cost_usd": row["total_cost_usd"],
            "estimated_queries": int(row["estimated_queries"] or 0),
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
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter, params = _created_at_range("ul.created_at", start_date, end_date)

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
                COUNT(*) as query_count,
                {_ESTIMATED_COUNT_SQL.format(col="ul.usage_estimated")} as estimated_query_count
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
            "session_count": session_row["session_count"],
            "estimated_query_count": int(usage_row["estimated_query_count"] or 0),
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
    with get_db() as conn:
        cursor = conn.cursor()

        # Build date filter clause
        date_filter, params = _created_at_range("ul.created_at", start_date, end_date)

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
                    COUNT(*) as query_count,
                    {_ESTIMATED_COUNT_SQL.format(col="ul.usage_estimated")} as estimated_query_count
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
                    COUNT(*) as query_count,
                    {_ESTIMATED_COUNT_SQL.format(col="ul.usage_estimated")} as estimated_query_count
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
                    COUNT(*) as query_count,
                    {_ESTIMATED_COUNT_SQL.format(col="ul.usage_estimated")} as estimated_query_count
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
                "query_count": row["query_count"],
                "estimated_query_count": int(row["estimated_query_count"] or 0),
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
    with get_db() as conn:
        cursor = conn.cursor()

        # Build filter clauses
        date_filter, params = _created_at_range("u.created_at", start_date, end_date)

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
                COUNT(*) as query_count,
                {_ESTIMATED_COUNT_SQL.format(col="u.usage_estimated")} as estimated_query_count
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
                "query_count": row["query_count"],
                "estimated_query_count": int(row["estimated_query_count"] or 0),
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
                s.created_at,
                -- Any booked turn with guessed token counts marks the session.
                EXISTS (
                    SELECT 1 FROM usage_log ul
                    WHERE ul.session_id = s.id AND ul.usage_estimated
                ) AS usage_estimated
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
                "created_at": _to_iso(row["created_at"]),
                "usage_estimated": bool(row["usage_estimated"]),
            })
        return result


# ============================================================================
# Per-provider breakdown
# ============================================================================

#: Breakdown key of the native/delegated Claude leg.
CLAUDE_PROVIDER_KEY = "claude"
#: Breakdown key of rows that name no provider and belong to no session
#: (one-shot generation booked before provider attribution existed).
UNATTRIBUTED_PROVIDER_KEY = "unattributed"

#: The provider a usage row is attributed to. Rows booked with attribution
#: carry it; older rows fall back to their session's CURRENT binding (counted
#: as ``inferred``), and older session-less rows are unattributed.
_PROVIDER_KEY_SQL = f"""CASE
    WHEN ul.provider_type IS NOT NULL
        THEN COALESCE(ul.provider_id, '{CLAUDE_PROVIDER_KEY}')
    WHEN s.id IS NOT NULL
        THEN COALESCE(s.provider_id, '{CLAUDE_PROVIDER_KEY}')
    ELSE '{UNATTRIBUTED_PROVIDER_KEY}'
END"""


class ProviderUsageEntry(TypedDict, total=False):
    """Usage booked on one provider leg (optionally on one day)."""
    key: str
    name: str
    provider_type: Optional[str]
    billing: str
    total_tokens_in: int
    total_tokens_out: int
    cache_read_tokens: int
    cache_creation_tokens: int
    total_cost_usd: float
    cost_status: str
    query_count: int
    estimated_query_count: int
    unpriced_query_count: int
    subscription_query_count: int
    api_query_count: int
    inferred_query_count: int
    date: str


def _provider_display_name(key: str, name: Optional[str]) -> str:
    if key == CLAUDE_PROVIDER_KEY:
        return "Claude"
    if key == UNATTRIBUTED_PROVIDER_KEY:
        return "Unattributed"
    return name or "Deleted provider"


def get_manual_balance_credits(row: dict) -> dict | None:
    """Entered balance less recorded app costs; not a provider balance reading."""
    balance, set_at = row.get("manual_balance_usd"), row.get("manual_balance_set_at")
    if balance is None or set_at is None:
        return None
    items = get_analytics_provider_breakdown(provider_id=row["id"], since_ms=set_at)
    spent = sum(item["total_cost_usd"] for item in items)
    return {
        "balance_remaining": float(balance) - spent,
        "manual_balance": float(balance),
        "estimated_spend": spent,
        "set_at": set_at,
        "unpriced_turns": sum(item["unpriced_query_count"] for item in items),
    }


def get_analytics_provider_breakdown(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    api_user_id: Optional[str] = None,
    by_date: bool = False,
    *,
    provider_id: Optional[str] = None,
    since_ms: Optional[int] = None,
) -> List[ProviderUsageEntry]:
    """Usage per provider leg for a date range (inclusive, YYYY-MM-DD).

    ``by_date`` splits each provider into one entry per day (the CSV export).
    Token and cost sums cover every booked turn. Three things are reported
    next to them rather than folded into a number:

    * ``billing``: whether the provider is paid per token or by subscription
      (a subscription's cost is an API-price estimate, not a bill);
    * ``cost_status``: ``unknown`` when no turn had a price (the model is in
      no price table, so the cost is missing rather than zero), ``partial``
      when some turns lacked one;
    * ``inferred_query_count``: turns booked before attribution existed,
      attributed from the session's current provider binding.
    """
    from app.core.usage_attribution import claude_billing, provider_billing

    date_expr = sql_date("ul.created_at")
    where, params = _created_at_range("ul.created_at", start_date, end_date)
    if api_user_id:
        where += " AND s.api_user_id = %s"
        params.append(api_user_id)

    # Balance tracking requires exact recorded attribution, not the legacy
    # fallback to a session's current provider, and a millisecond cutoff.
    if provider_id is not None:
        where += " AND ul.provider_id = %s"
        params.append(provider_id)
    if since_ms is not None:
        where += " AND ul.created_at >= %s"
        params.append(datetime.fromtimestamp(since_ms / 1000, timezone.utc).isoformat())

    inner_day = f", {date_expr} AS day" if by_date else ""
    outer_day = ", t.day AS day" if by_date else ""
    group_day = ", t.day" if by_date else ""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT
                t.pkey AS key{outer_day},
                mp.name AS provider_name,
                mp.provider_type AS current_type,
                mp.auth_mode AS current_auth_mode,
                MAX(t.provider_type) AS recorded_type,
                COALESCE(SUM(t.tokens_in), 0) AS total_tokens_in,
                COALESCE(SUM(t.tokens_out), 0) AS total_tokens_out,
                COALESCE(SUM(t.cache_read), 0) AS cache_read_tokens,
                COALESCE(SUM(t.cache_creation), 0) AS cache_creation_tokens,
                COALESCE(SUM(t.cost), 0) AS total_cost_usd,
                COUNT(*) AS query_count,
                COALESCE(SUM(t.estimated), 0) AS estimated_query_count,
                COALESCE(SUM(t.unpriced), 0) AS unpriced_query_count,
                COALESCE(SUM(t.sub), 0) AS subscription_query_count,
                COALESCE(SUM(t.api), 0) AS api_query_count,
                COALESCE(SUM(t.inferred), 0) AS inferred_query_count
            FROM (
                SELECT
                    {_PROVIDER_KEY_SQL} AS pkey{inner_day},
                    ul.provider_type AS provider_type,
                    COALESCE(ul.tokens_in, 0) AS tokens_in,
                    COALESCE(ul.tokens_out, 0) AS tokens_out,
                    COALESCE(ul.cache_read_tokens, 0) AS cache_read,
                    COALESCE(ul.cache_creation_tokens, 0) AS cache_creation,
                    COALESCE(ul.cost_usd, 0) AS cost,
                    CASE WHEN ul.usage_estimated THEN 1 ELSE 0 END AS estimated,
                    CASE WHEN COALESCE(ul.cost_usd, 0) = 0
                          AND COALESCE(ul.tokens_in, 0) + COALESCE(ul.tokens_out, 0)
                              + COALESCE(ul.cache_read_tokens, 0)
                              + COALESCE(ul.cache_creation_tokens, 0) > 0
                         THEN 1 ELSE 0 END AS unpriced,
                    CASE WHEN ul.billing = 'subscription' THEN 1 ELSE 0 END AS sub,
                    CASE WHEN ul.billing = 'api' THEN 1 ELSE 0 END AS api,
                    CASE WHEN ul.provider_type IS NULL AND s.id IS NOT NULL
                         THEN 1 ELSE 0 END AS inferred
                FROM usage_log ul
                LEFT JOIN sessions s ON ul.session_id = s.id
                WHERE 1=1 {where}
            ) t
            LEFT JOIN model_providers mp ON mp.id = t.pkey
            GROUP BY t.pkey{group_day}, mp.name, mp.provider_type, mp.auth_mode
        """, params)
        rows = cursor.fetchall()

    claude_bill: Optional[str] = None
    claude_bill_read = False
    result: List[ProviderUsageEntry] = []
    for row in rows:
        key = str(row["key"])
        query_count = int(row["query_count"] or 0)
        sub = int(row["subscription_query_count"] or 0)
        api = int(row["api_query_count"] or 0)
        unpriced = int(row["unpriced_query_count"] or 0)

        # Rows with no recorded billing (the inferred ones, or rows booked
        # while a provider lookup failed) take the provider's CURRENT billing.
        unbilled = query_count - sub - api
        if unbilled > 0:
            current: Optional[str] = None
            if key == CLAUDE_PROVIDER_KEY:
                if not claude_bill_read:
                    claude_bill, claude_bill_read = claude_billing(), True
                current = claude_bill
            elif row["current_type"]:
                current = provider_billing(row["current_type"], row["current_auth_mode"])
            if current == "subscription":
                sub += unbilled
            elif current == "api":
                api += unbilled

        if sub and api:
            billing = "mixed"
        elif sub:
            billing = "subscription"
        elif api:
            billing = "api"
        else:
            billing = "unknown"

        if unpriced == 0:
            cost_status = "estimated"
        elif unpriced >= query_count:
            cost_status = "unknown"
        else:
            cost_status = "partial"

        if key == CLAUDE_PROVIDER_KEY:
            provider_type: Optional[str] = CLAUDE_PROVIDER_KEY
        else:
            provider_type = row["current_type"] or row["recorded_type"]

        entry: ProviderUsageEntry = {
            "key": key,
            "name": _provider_display_name(key, row["provider_name"]),
            "provider_type": provider_type,
            "billing": billing,
            "total_tokens_in": int(row["total_tokens_in"] or 0),
            "total_tokens_out": int(row["total_tokens_out"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "cache_creation_tokens": int(row["cache_creation_tokens"] or 0),
            "total_cost_usd": round(float(row["total_cost_usd"] or 0), 6),
            "cost_status": cost_status,
            "query_count": query_count,
            "estimated_query_count": int(row["estimated_query_count"] or 0),
            "unpriced_query_count": unpriced,
            "subscription_query_count": sub,
            "api_query_count": api,
            "inferred_query_count": int(row["inferred_query_count"] or 0),
        }
        if by_date:
            entry["date"] = _to_iso(row["day"])
        result.append(entry)

    if by_date:
        result.sort(key=lambda e: (e["date"], -e["total_cost_usd"], e["name"]))
    else:
        result.sort(key=lambda e: (-e["total_cost_usd"], -e["query_count"], e["name"]))
    return result
