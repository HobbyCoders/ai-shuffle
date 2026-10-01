"""Per-provider usage attribution and the Stats provider breakdown (#1614).

Every usage writer stamps the provider leg that served the turn
(``usage_log.provider_id/provider_type/billing``), the breakdown groups the
ledger by it, and the maintenance pass no longer prunes the ledger at 7 days
(which made the 30/90-day Stats ranges report one week).
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core import claude_auth_mode
from app.core import query_persistence as qp
from app.core.cost_utils import invalidate_usage_cache
from app.core.usage_attribution import usage_attribution
from app.db import database, turns, session_events
from app.db.base import column_exists


@pytest.fixture(autouse=True)
def _fresh_usage_cache():
    invalidate_usage_cache()
    yield
    invalidate_usage_cache()


@pytest.fixture
def claude_mode(monkeypatch):
    """Pin the Claude Code auth mode the native leg is billed under."""
    def _set(mode):
        monkeypatch.setattr(claude_auth_mode, "current_mode", lambda: mode)
    _set(claude_auth_mode.MODE_SUBSCRIPTION)
    return _set


def _provider(pid, name, ptype, auth_mode=None):
    database.create_model_provider(pid, name, ptype, "https://example.invalid")
    if auth_mode:
        database.update_model_provider(pid, auth_mode=auth_mode)


def _rows(mock_db, sid):
    return [tuple(r) for r in mock_db.execute(
        "SELECT provider_id, provider_type, billing FROM usage_log "
        "WHERE session_id=? ORDER BY id", (sid,),
    ).fetchall()]


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def test_the_columns_are_part_of_the_fresh_schema(mock_db):
    from tests.pg_shim import route_db_to

    with route_db_to(mock_db) as shim:
        for column in ("provider_id", "provider_type", "billing"):
            assert column_exists(shim.cursor(), "usage_log", column)


def test_native_claude_leg_is_billed_by_the_claude_auth_mode(mock_db, claude_mode):
    assert usage_attribution(None) == {
        "provider_id": None, "provider_type": "claude", "billing": "subscription",
    }
    claude_mode(claude_auth_mode.MODE_API_KEY)
    assert usage_attribution(None)["billing"] == "api"


@pytest.mark.parametrize("ptype,auth_mode,billing", [
    ("chatgpt", None, "subscription"),
    ("custom", "chatgpt", "subscription"),
    ("grok", None, "subscription"),
    ("openai", None, "api"),
    ("xai", None, "api"),
    ("openrouter", None, "api"),
    ("custom", None, "api"),
])
def test_provider_rows_are_billed_by_their_shape(mock_db, ptype, auth_mode, billing):
    _provider("p-1", "Row", ptype, auth_mode)
    assert usage_attribution("p-1") == {
        "provider_id": "p-1", "provider_type": ptype, "billing": billing,
    }


def test_a_deleted_provider_books_as_unknown_instead_of_guessing(mock_db):
    assert usage_attribution("gone") == {
        "provider_id": "gone", "provider_type": "unknown", "billing": None,
    }


def test_log_usage_and_settlement_store_the_attribution(mock_db):
    sid = "s-attr"
    database.create_session(session_id=sid, profile_id="default")
    database.log_usage(sid, "default", "m", 1, 1, 0.0, 1,
                       provider_id="p-x", provider_type="openai", billing="api")
    database.settle_turn_usage(sid, "k1", profile_id="default", model="m",
                               tokens_in=1, provider_type="claude",
                               billing="subscription")
    # A caller that predates attribution still books (legacy NULLs).
    database.log_usage(sid, "default", "m", 1, 1, 0.0, 1)
    assert _rows(mock_db, sid) == [
        ("p-x", "openai", "api"),
        (None, "claude", "subscription"),
        (None, None, None),
    ]


@pytest.mark.asyncio
async def test_turn_settlement_books_the_leg_the_turn_ran_on(mock_db, claude_mode):
    """The metadata carries the turn's resolved binding; it wins over the
    session row (which a later switch may already have moved)."""
    _provider("p-codex", "Codex", "chatgpt")
    sid = "s-turn"
    database.create_session(session_id=sid, profile_id="default", provider_id=None)
    meta = {"usage_settlement_key": "k-turn", qp.USAGE_PROVIDER_KEY: "p-codex",
            "tokens_in": 5, "tokens_out": 1, "total_cost_usd": 0.0, "num_turns": 1}
    await qp._persist_turn_results(sid, None, meta, "default")

    meta2 = {"usage_settlement_key": "k-turn-2", qp.USAGE_PROVIDER_KEY: None,
             "tokens_in": 5, "tokens_out": 1, "total_cost_usd": 0.01, "num_turns": 1}
    await qp._persist_turn_results(sid, None, meta2, "default")
    assert _rows(mock_db, sid) == [
        ("p-codex", "chatgpt", "subscription"),
        (None, "claude", "subscription"),
    ]


@pytest.mark.asyncio
async def test_settlement_without_a_stamped_leg_falls_back_to_the_session(mock_db):
    _provider("p-or", "OpenRouter", "openrouter")
    sid = "s-fallback"
    database.create_session(session_id=sid, profile_id="default", provider_id="p-or")
    meta = {"usage_settlement_key": "k-fb", "tokens_in": 3, "tokens_out": 1,
            "total_cost_usd": 0.001, "num_turns": 1}
    await qp._persist_turn_results(sid, None, meta, "default")
    assert _rows(mock_db, sid) == [("p-or", "openrouter", "api")]


@pytest.mark.asyncio
async def test_a_failed_attribution_still_books_the_turn(mock_db, monkeypatch):
    """Attribution only labels the row; it must never cost the settlement."""
    import app.core.usage_attribution as ua

    def boom(_provider_id):
        raise RuntimeError("lookup exploded")

    monkeypatch.setattr(ua, "usage_attribution", boom)
    sid = "s-boom"
    database.create_session(session_id=sid, profile_id="default")
    meta = {"usage_settlement_key": "k-boom", qp.USAGE_PROVIDER_KEY: "p-1",
            "tokens_in": 3, "tokens_out": 1, "total_cost_usd": 0.001, "num_turns": 1}
    await qp._persist_turn_results(sid, None, meta, "default")
    assert _rows(mock_db, sid) == [(None, None, None)]
    assert database.get_session(sid)["total_tokens_in"] == 3


@pytest.mark.asyncio
async def test_the_kill_path_attributes_its_recovered_usage(mock_db):
    _provider("p-xai", "xAI", "xai")
    sid = "s-killed"
    database.create_session(session_id=sid, profile_id="default", provider_id="p-xai")
    turn = turns.enqueue_turn(sid, "user", prompt="go")
    turns.start_turn(turn["id"], instance_id="test")
    session_events.append_events(sid, [{
        "type": "turn.usage", "turn_id": turn["id"],
        "payload": {"usage": {"model_call_index": 1, "input_tokens": 9,
                              "output_tokens": 2, "cost_usd": 0.003}},
    }])
    assert await qp.settle_killed_session(sid) is True
    assert _rows(mock_db, sid) == [("p-xai", "xai", "api")]


# ---------------------------------------------------------------------------
# Breakdown query
# ---------------------------------------------------------------------------

def _book(sid, key, **kw):
    base = dict(profile_id="default", model="m", tokens_in=100, tokens_out=10,
                cost_usd=0.01, model_calls=1, turn_increment=1)
    base.update(kw)
    assert database.settle_turn_usage(sid, key, **base)


def _by_key(items):
    return {i["key"]: i for i in items}


def test_breakdown_groups_turns_tokens_and_cost_per_provider(mock_db, claude_mode):
    _provider("p-codex", "My Codex", "chatgpt")
    _provider("p-oai", "OpenAI key", "openai")
    _provider("p-local", "Local vLLM", "custom")
    database.create_session(session_id="s1", profile_id="default")
    _book("s1", "a", provider_type="claude", billing="subscription", cost_usd=0.5,
          cache_read_tokens=1000)
    _book("s1", "b", provider_type="claude", billing="subscription", cost_usd=0.25,
          usage_estimated=True)
    _book("s1", "c", provider_id="p-codex", provider_type="chatgpt",
          billing="subscription", cost_usd=0.1)
    _book("s1", "d", provider_id="p-oai", provider_type="openai", billing="api",
          cost_usd=0.2)
    # A local model nobody priced: tokens, no cost. Unknown, not $0.
    _book("s1", "e", provider_id="p-local", provider_type="custom", billing="api",
          cost_usd=0)

    items = _by_key(database.get_analytics_provider_breakdown())
    assert set(items) == {"claude", "p-codex", "p-oai", "p-local"}

    claude = items["claude"]
    assert claude["name"] == "Claude"
    assert claude["query_count"] == 2
    assert claude["total_tokens_in"] == 200
    assert claude["cache_read_tokens"] == 1000
    assert claude["total_cost_usd"] == pytest.approx(0.75)
    assert claude["billing"] == "subscription"
    assert claude["estimated_query_count"] == 1
    assert claude["cost_status"] == "estimated"

    assert items["p-codex"]["name"] == "My Codex"
    assert items["p-codex"]["billing"] == "subscription"
    assert items["p-oai"]["billing"] == "api"
    assert items["p-local"]["cost_status"] == "unknown"
    assert items["p-local"]["unpriced_query_count"] == 1
    # Sorted by cost, highest first.
    assert [i["key"] for i in database.get_analytics_provider_breakdown()][0] == "claude"


def test_partial_pricing_and_mixed_billing_are_reported(mock_db):
    _provider("p-1", "Row", "openai")
    database.create_session(session_id="s1", profile_id="default")
    _book("s1", "a", provider_id="p-1", provider_type="openai", billing="api", cost_usd=0.2)
    _book("s1", "b", provider_id="p-1", provider_type="openai", billing="api", cost_usd=0)
    _book("s1", "c", provider_id="p-1", provider_type="openai", billing="subscription",
          cost_usd=0.1)
    item = _by_key(database.get_analytics_provider_breakdown())["p-1"]
    assert item["cost_status"] == "partial"
    assert item["unpriced_query_count"] == 1
    assert item["billing"] == "mixed"
    # A turn that used no tokens and cost nothing is a real $0, not unpriced.
    _book("s1", "d", provider_id="p-1", provider_type="openai", billing="api",
          cost_usd=0, tokens_in=0, tokens_out=0)
    item = _by_key(database.get_analytics_provider_breakdown())["p-1"]
    assert item["unpriced_query_count"] == 1


def test_rows_booked_before_attribution_are_inferred_from_the_session(mock_db, claude_mode):
    _provider("p-grok", "SuperGrok", "grok")
    database.create_session(session_id="s-native", profile_id="default", provider_id=None)
    database.create_session(session_id="s-grok", profile_id="default", provider_id="p-grok")
    database.log_usage("s-native", "default", "sonnet", 10, 1, 0.02, 1)
    database.log_usage("s-grok", "default", "grok-5", 10, 1, 0.0, 1)
    database.log_usage(None, None, "haiku", 10, 1, 0.001, 1)  # old generation row

    items = _by_key(database.get_analytics_provider_breakdown())
    assert items["claude"]["inferred_query_count"] == 1
    assert items["claude"]["billing"] == "subscription"
    assert items["p-grok"]["inferred_query_count"] == 1
    assert items["p-grok"]["billing"] == "subscription"
    assert items["p-grok"]["cost_status"] == "unknown"
    assert items["unattributed"]["name"] == "Unattributed"
    assert items["unattributed"]["billing"] == "unknown"
    assert items["unattributed"]["inferred_query_count"] == 0


def test_a_deleted_provider_keeps_its_usage_under_its_id(mock_db):
    database.create_session(session_id="s1", profile_id="default")
    _book("s1", "a", provider_id="p-gone", provider_type="openrouter", billing="api")
    item = _by_key(database.get_analytics_provider_breakdown())["p-gone"]
    assert item["name"] == "Deleted provider"
    assert item["provider_type"] == "openrouter"
    assert item["billing"] == "api"


def test_breakdown_honours_the_date_range_user_filter_and_daily_split(mock_db):
    database.create_api_user("u-1", "User", "hash-u-1")
    database.create_session(session_id="s-mine", profile_id="default", api_user_id="u-1")
    database.create_session(session_id="s-other", profile_id="default")
    _book("s-mine", "a", provider_type="claude", billing="api")
    _book("s-mine", "b", provider_type="claude", billing="api")
    _book("s-other", "c", provider_type="claude", billing="api")
    mock_db.execute(
        "UPDATE usage_log SET created_at='2026-09-01 12:00:00' WHERE settlement_key='a'")
    mock_db.execute(
        "UPDATE usage_log SET created_at='2026-09-03 12:00:00' WHERE settlement_key IN ('b','c')")
    mock_db.commit()

    in_range = database.get_analytics_provider_breakdown("2026-09-02", "2026-09-30")
    assert [i["query_count"] for i in in_range] == [2]
    mine = database.get_analytics_provider_breakdown(api_user_id="u-1")
    assert [i["query_count"] for i in mine] == [2]

    daily = database.get_analytics_provider_breakdown("2026-09-01", "2026-09-30", by_date=True)
    # [:10]: the SQLite shim drops the ::date cast, so its day is the raw
    # timestamp; PostgreSQL returns a date that _to_iso renders as YYYY-MM-DD.
    assert [(i["date"][:10], i["query_count"]) for i in daily] == [
        ("2026-09-01", 1), ("2026-09-03", 2),
    ]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_maintenance_keeps_usage_older_than_a_week(mock_db):
    """The ledger behind the 30/90-day ranges must survive the log sweep."""
    database.create_session(session_id="s-old", profile_id="default")
    database.log_usage("s-old", "default", "m", 1, 1, 0.01, 1)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    mock_db.execute("UPDATE usage_log SET created_at=?", (old,))
    mock_db.commit()

    results = database.run_database_maintenance(skip_vacuum=True)

    assert "cleaned_usage_log" not in results
    assert mock_db.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0] == 1
