"""
SQL dialect compatibility layer (Postgres-only).

Provides helper functions that generate SQL fragments for PostgreSQL.
Domain modules call these instead of hard-coding SQL expressions directly.
"""

import logging
from typing import Optional

logger = logging.getLogger("app.db.compat")


# ---------------------------------------------------------------------------
# Placeholder style
# ---------------------------------------------------------------------------

def ph() -> str:
    """Return the parameter placeholder (%s for PostgreSQL)."""
    return "%s"


def phs(n: int) -> str:
    """Return *n* comma-separated placeholders.

    >>> phs(3)
    '%s, %s, %s'
    """
    return ", ".join([ph()] * n)


# ---------------------------------------------------------------------------
# SQL expression helpers
# ---------------------------------------------------------------------------

def sql_date(column: str) -> str:
    """Cast/extract a DATE from a timestamp column."""
    return f"({column})::date"


def sql_strftime(fmt: str, column: str) -> str:
    """Format a timestamp as a string using PostgreSQL to_char."""
    pg_fmt = _strftime_to_pg(fmt)
    return f"to_char({column}, '{pg_fmt}')"


def _strftime_to_pg(fmt: str) -> str:
    """Convert Python strftime-style format to PostgreSQL to_char format."""
    mapping = {
        "%Y-W%W": "IYYY-\"W\"IW",
        "%Y-%m": "YYYY-MM",
        "%Y-%m-%d": "YYYY-MM-DD",
        "%H": "HH24",
        "%M": "MI",
        "%S": "SS",
    }
    if fmt in mapping:
        return mapping[fmt]
    # Fallback: try direct mapping of common tokens
    result = fmt
    result = result.replace("%Y", "YYYY")
    result = result.replace("%m", "MM")
    result = result.replace("%d", "DD")
    result = result.replace("%H", "HH24")
    result = result.replace("%M", "MI")
    result = result.replace("%S", "SS")
    result = result.replace("%W", "IW")
    return result


def sql_ilike(column: str, placeholder: Optional[str] = None) -> str:
    """Case-insensitive LIKE comparison using PostgreSQL ILIKE."""
    param = placeholder or ph()
    return f"{column} ILIKE {param}"


def sql_concat_json_array() -> str:
    """Aggregate values into a JSON array using json_agg."""
    return "json_agg"


def sql_string_agg(column: str, separator: str = ",") -> str:
    """Aggregate strings with a separator using string_agg."""
    return f"string_agg({column}::text, '{separator}')"


def sql_random() -> str:
    """Random function name."""
    return "RANDOM()"


def sql_epoch_seconds(column: str) -> str:
    """Extract epoch seconds from a timestamp."""
    return f"EXTRACT(EPOCH FROM {column})"


def sql_current_timestamp() -> str:
    """Current UTC timestamp expression for use in queries (not DDL defaults)."""
    return "NOW()"


def sql_interval(value: int, unit: str = "days") -> str:
    """Generate an interval expression for date arithmetic.

    Usage: WHERE created_at > {sql_current_timestamp()} - {sql_interval(7, 'days')}
    """
    return f"INTERVAL '{value} {unit}'"


def sql_boolean(val: bool) -> str:
    """Boolean literal."""
    return "TRUE" if val else "FALSE"


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------

def sql_autoincrement_pk() -> str:
    """Auto-incrementing integer primary key."""
    return "BIGSERIAL PRIMARY KEY"


def sql_json_type() -> str:
    """Column type for JSON data."""
    return "JSONB"


def sql_timestamp_default() -> str:
    """Default timestamp for DDL."""
    return "TIMESTAMP WITH TIME ZONE DEFAULT NOW()"


def sql_text_pk() -> str:
    """Text primary key."""
    return "TEXT PRIMARY KEY"


# ---------------------------------------------------------------------------
# INSERT conflict handling
# ---------------------------------------------------------------------------

def sql_insert_or_ignore(table: str, columns: list[str]) -> str:
    """Generate INSERT ... ON CONFLICT DO NOTHING."""
    cols = ", ".join(columns)
    vals = phs(len(columns))
    return f"INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT DO NOTHING"


def sql_insert_or_replace(
    table: str,
    columns: list[str],
    conflict_columns: list[str],
    update_columns: list[str],
) -> str:
    """Generate INSERT ... ON CONFLICT (...) DO UPDATE SET ..."""
    cols = ", ".join(columns)
    vals = phs(len(columns))
    conflict = ", ".join(conflict_columns)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({vals}) "
        f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
    )


# ---------------------------------------------------------------------------
# Schema introspection helpers
# ---------------------------------------------------------------------------

def sql_table_exists_query() -> str:
    """Query to check if a table exists. Bind: (table_name,)."""
    return (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s"
    )


def sql_column_exists_query() -> str:
    """Query to check if a column exists. Bind: (table_name, column_name)."""
    return (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s"
    )


def sql_column_is_nullable_query() -> str:
    """Query to check if column is nullable. Bind: (table_name, column_name).

    Returns row with is_nullable = 'YES' or 'NO'.
    """
    return (
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s"
    )


# ---------------------------------------------------------------------------
# FTS (Full-Text Search) helpers
# ---------------------------------------------------------------------------

def sql_fts_match(content_column: str, query_param: Optional[str] = None) -> str:
    """Full-text search match expression using tsvector."""
    param = query_param or ph()
    return f"{content_column}_tsv @@ plainto_tsquery('english', {param})"


def sql_fts_snippet(content_column: str, query_param: Optional[str] = None) -> str:
    """Full-text search snippet/headline expression using ts_headline."""
    param = query_param or ph()
    return (
        f"ts_headline('english', {content_column}, "
        f"plainto_tsquery('english', {param}), "
        f"'MaxFragments=1,MaxWords=20,MinWords=5')"
    )


def sql_fts_rank() -> str:
    """FTS ranking expression for ORDER BY relevance."""
    return "ts_rank(content_tsv, query)"
