"""``usage_log.usage_estimated`` (schema v90): booking, reads and aggregates.

A turn whose token counts the harness guessed (the provider reported no
usage, or the reply was interrupted) is booked like any other, and flagged.
The flag reaches the per-session usage list (the cost footer under each
reply), the analytics aggregates behind the Stats page and the status
widget's totals; nothing is ever subtracted because of it.
"""

import pytest

from app.core.cost_utils import attach_cost_metadata, invalidate_usage_cache
from app.db import database
from app.db.base import column_exists


@pytest.fixture(autouse=True)
def _fresh_usage_cache():
    invalidate_usage_cache()
    yield
    invalidate_usage_cache()


def _book(sid, key, *, estimated, cost=0.01, tokens_in=10, tokens_out=2):
    return database.settle_turn_usage(
        sid, key, profile_id="default", model="m", tokens_in=tokens_in,
        tokens_out=tokens_out, cost_usd=cost, duration_ms=5, model_calls=1,
        turn_increment=1, usage_estimated=estimated,
    )


def test_the_column_is_part_of_the_fresh_schema_and_defaults_to_false(mock_db):
    from tests.pg_shim import route_db_to

    with route_db_to(mock_db) as shim:
        assert column_exists(shim.cursor(), "usage_log", "usage_estimated")
    database.create_session(session_id="s-default", profile_id="default")
    database.log_usage("s-default", "default", "m", 5, 1, 0.001, 3)
    row = mock_db.execute(
        "SELECT usage_estimated FROM usage_log WHERE session_id=?", ("s-default",)
    ).fetchone()
    assert not row[0]


def test_log_usage_and_settlement_book_the_flag_without_changing_the_amounts(mock_db):
    sid = "s-flag"
    database.create_session(session_id=sid, profile_id="default")
    database.log_usage(sid, "default", "m", 7, 3, 0.002, 4, usage_estimated=True)
    assert _book(sid, "k-estimated", estimated=True) is True
    assert _book(sid, "k-measured", estimated=False) is True

    flags = [bool(r[0]) for r in mock_db.execute(
        "SELECT usage_estimated FROM usage_log WHERE session_id=? ORDER BY id", (sid,)
    ).fetchall()]
    assert flags == [True, True, False]
    # Budgets and caps keep counting estimated amounts: both settled turns land.
    row = database.get_session(sid)
    assert row["total_tokens_in"] == 20
    assert row["total_cost_usd"] == pytest.approx(0.02)


def test_per_session_usage_and_the_cost_footer_carry_the_flag(mock_db):
    sid = "s-footer"
    database.create_session(session_id=sid, profile_id="default")
    _book(sid, "k1", estimated=False)
    _book(sid, "k2", estimated=True)

    entries = database.get_all_session_usage(sid)
    assert [e["usage_estimated"] for e in entries] == [False, True]

    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "type": "text", "content": "measured"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "type": "text", "content": "guessed"},
    ]
    attach_cost_metadata(messages, sid)
    assert "usage_estimated" not in messages[1]["metadata"]
    assert messages[3]["metadata"]["usage_estimated"] is True
    assert messages[3]["metadata"]["cost_usd"] == pytest.approx(0.01)


def test_the_usage_cache_appends_the_flag_for_a_later_log_usage(mock_db):
    sid = "s-cache"
    database.create_session(session_id=sid, profile_id="default")
    _book(sid, "k1", estimated=False)
    database.get_all_session_usage(sid)
    from app.core.cost_utils import get_cached_usage
    get_cached_usage(sid)  # populate
    database.log_usage(sid, "default", "m", 1, 1, 0.001, 1, usage_estimated=True)
    assert [e["usage_estimated"] for e in get_cached_usage(sid)] == [False, True]


def test_analytics_aggregates_count_estimated_turns_and_flag_sessions(mock_db):
    a, b = "s-agg-a", "s-agg-b"
    database.create_session(session_id=a, profile_id="default")
    database.create_session(session_id=b, profile_id="default")
    _book(a, "a1", estimated=False)
    _book(a, "a2", estimated=True)
    _book(b, "b1", estimated=False)

    stats = database.get_analytics_usage_stats()
    assert stats["query_count"] == 3
    assert stats["estimated_query_count"] == 1
    assert stats["total_tokens_in"] == 30  # the estimate is inside the total

    by_profile = database.get_analytics_cost_breakdown(group_by="profile")
    assert [(r["query_count"], r["estimated_query_count"]) for r in by_profile] == [(3, 1)]
    by_date = database.get_analytics_cost_breakdown(group_by="date")
    assert sum(r["estimated_query_count"] for r in by_date) == 1

    trends = database.get_analytics_usage_trends(interval="day")
    assert sum(p["estimated_query_count"] for p in trends) == 1

    top = {s["session_id"]: s["usage_estimated"] for s in database.get_analytics_top_sessions()}
    assert top == {a: True, b: False}

    totals = database.get_usage_stats()
    assert totals["total_queries"] == 3 and totals["estimated_queries"] == 1


def test_a_database_without_estimates_reports_zero_everywhere(mock_db):
    sid = "s-none"
    database.create_session(session_id=sid, profile_id="default")
    _book(sid, "k1", estimated=False)
    assert database.get_analytics_usage_stats()["estimated_query_count"] == 0
    assert database.get_usage_stats()["estimated_queries"] == 0
    assert database.get_analytics_top_sessions()[0]["usage_estimated"] is False
