"""Regression tests: analytics DB functions must return ISO date strings.

PostgreSQL returns native ``datetime.date``/``datetime.datetime`` objects for
``::date`` casts and timestamp columns. The analytics API response models
(``UsageTrend.date``, ``TopSession.created_at``, ``CostBreakdownItem.name``)
declare these fields as ``str``, so passing native date/datetime objects through
raised ``pydantic_core.ValidationError`` -> HTTP 500.

See branch fix/error-exception-asgi-application-exception. The existing
``tests/api/test_analytics.py`` mocks the database layer with already-stringified
dates, so it never reproduced the failure; these tests exercise the DB functions
themselves with the native objects Postgres actually returns.
"""

import datetime
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from app.db import analytics


@contextmanager
def _fake_db(rows):
    """A get_db() stand-in whose cursor.fetchall() yields the given rows."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    conn.cursor.return_value = cursor
    yield conn


@pytest.fixture
def patch_db(monkeypatch):
    def _apply(rows):
        monkeypatch.setattr(analytics, "get_db", lambda: _fake_db(rows))
    return _apply


def test_usage_trends_day_interval_coerces_date_to_iso_string(patch_db):
    """Postgres ``::date`` -> datetime.date must come back as 'YYYY-MM-DD'."""
    patch_db([{
        "date": datetime.date(2026, 6, 20),
        "tokens_in": 10, "tokens_out": 20, "cost_usd": 1.5, "query_count": 3,
     "estimated_query_count": 0,
    }])
    result = analytics.get_analytics_usage_trends(
        "2026-06-01", "2026-06-30", interval="day"
    )
    assert result[0]["date"] == "2026-06-20"
    assert isinstance(result[0]["date"], str)


def test_usage_trends_week_interval_passes_string_through(patch_db):
    """The to_char week/month buckets are already strings and must be unchanged."""
    patch_db([{
        "date": "2026-W25",
        "tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "query_count": 0,
     "estimated_query_count": 0,
    }])
    result = analytics.get_analytics_usage_trends(
        "2026-06-01", "2026-06-30", interval="week"
    )
    assert result[0]["date"] == "2026-W25"


def test_top_sessions_coerces_datetime_to_iso_string(patch_db):
    """A TIMESTAMPTZ column -> datetime.datetime must serialize to an ISO string."""
    tz = datetime.timezone(datetime.timedelta(hours=-7))
    patch_db([{
        "session_id": "s1", "title": "t", "profile_id": "p", "profile_name": "P",
        "total_cost_usd": 2.0, "total_tokens_in": 5, "total_tokens_out": 6,
        "created_at": datetime.datetime(2026, 6, 25, 17, 0, 0, tzinfo=tz), "usage_estimated": False,
    }])
    result = analytics.get_analytics_top_sessions("2026-06-01", "2026-06-30")
    assert result[0]["created_at"] == "2026-06-25T17:00:00-07:00"
    assert isinstance(result[0]["created_at"], str)


def test_top_sessions_passes_none_created_at_through(patch_db):
    """``created_at`` is Optional[str]; a missing value must remain None, not crash."""
    patch_db([{
        "session_id": "s1", "title": None, "profile_id": None, "profile_name": None,
        "total_cost_usd": 1.0, "total_tokens_in": 0, "total_tokens_out": 0,
        "created_at": None, "usage_estimated": False,
    }])
    result = analytics.get_analytics_top_sessions("2026-06-01", "2026-06-30")
    assert result[0]["created_at"] is None


def test_cost_breakdown_by_date_coerces_name_to_iso_string(patch_db):
    """group_by='date' puts a native date in both key and name; both must be strings."""
    patch_db([{
        "key": datetime.date(2026, 6, 20),
        "name": datetime.date(2026, 6, 20),
        "total_tokens_in": 1, "total_tokens_out": 2,
        "total_cost_usd": 3.0, "query_count": 4,
     "estimated_query_count": 0,
    }])
    result = analytics.get_analytics_cost_breakdown(
        "2026-06-01", "2026-06-30", group_by="date"
    )
    assert result[0]["key"] == "2026-06-20"
    assert result[0]["name"] == "2026-06-20"
    assert isinstance(result[0]["name"], str)
