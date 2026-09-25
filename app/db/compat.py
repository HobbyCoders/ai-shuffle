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
