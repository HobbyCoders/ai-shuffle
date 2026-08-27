"""``update_session`` accumulates cache tokens and model calls.

The session row used to carry only two cumulative token columns —
``total_tokens_in`` / ``total_tokens_out`` — and ``total_tokens_in`` is fed the
provider's FRESH input count (usage is normalized Anthropic-style, so cache
reads are excluded from it). Everything else about the prompt went to
``cache_read_tokens`` / ``cache_creation_tokens``, which are OVERWRITE snapshots
of the last call's context size. Nothing added them up, so a cache-heavy session
under-reported its prompt tokens by roughly an order of magnitude.

``turn_count`` had the mirror-image problem: it was incremented by the fluid
path's ``num_turns``, which is the MODEL-CALL count, so a one-turn child claimed
sixteen turns. The model calls now have a column of their own.

Fixture mirrors tests/db/test_sessions_launched_model.py.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.db import database as db
from app.db import sessions as sessions_mod


def _create_schema(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            sdk_session_id TEXT,
            total_cost_usd REAL DEFAULT 0,
            total_tokens_in BIGINT DEFAULT 0,
            total_tokens_out BIGINT DEFAULT 0,
            total_cache_read_tokens BIGINT DEFAULT 0,
            total_cache_creation_tokens BIGINT DEFAULT 0,
            last_input_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            total_model_calls INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _create_schema(conn.cursor())
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    @contextmanager
    def mock_get_db():
        try:
            yield test_db
            test_db.commit()
        except Exception:
            test_db.rollback()
            raise

    def mock_get_cursor(conn):
        return conn.cursor()

    with patch.object(sessions_mod, "get_db", mock_get_db), \
         patch.object(sessions_mod, "get_cursor", mock_get_cursor), \
         patch.object(sessions_mod, "ph", lambda: "?"), \
         patch.object(db, "get_db", mock_get_db):
        now = datetime.now(timezone.utc).isoformat()
        test_db.execute(
            "INSERT INTO sessions (id, profile_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "p1", now, now),
        )
        test_db.commit()
        yield test_db


class TestCumulativeUsage:
    def test_cache_increments_accumulate_across_turns(self, mock_db):
        """The whole point: two cache-heavy turns SUM, they do not overwrite."""
        sessions_mod.update_session(
            "s1",
            tokens_in_increment=15,
            cache_read_increment=180_000,
            cache_creation_increment=12_000,
        )
        sessions_mod.update_session(
            "s1",
            tokens_in_increment=20,
            cache_read_increment=73_000,
            cache_creation_increment=1_500,
        )

        row = sessions_mod.get_session("s1")
        assert row["total_tokens_in"] == 35
        assert row["total_cache_read_tokens"] == 253_000
        assert row["total_cache_creation_tokens"] == 13_500
        # True prompt tokens — the number the fresh-input column alone hid.
        true_prompt = (
            row["total_tokens_in"]
            + row["total_cache_read_tokens"]
            + row["total_cache_creation_tokens"]
        )
        assert true_prompt == 266_535

    def test_snapshot_columns_still_overwrite(self, mock_db):
        """The context bar wants the LAST call's size, so those stay absolute —
        which is exactly why the cumulative columns had to be added beside them."""
        sessions_mod.update_session(
            "s1", cache_read_increment=100, cache_read_tokens=100,
        )
        sessions_mod.update_session(
            "s1", cache_read_increment=250, cache_read_tokens=250,
        )

        row = sessions_mod.get_session("s1")
        assert row["cache_read_tokens"] == 250        # snapshot: last turn
        assert row["total_cache_read_tokens"] == 350  # cumulative: both turns

    def test_turns_and_model_calls_count_separately(self, mock_db):
        """One turn is one turn however many API hops it took."""
        sessions_mod.update_session("s1", turn_increment=1, model_calls_increment=16)
        sessions_mod.update_session("s1", turn_increment=1, model_calls_increment=4)

        row = sessions_mod.get_session("s1")
        assert row["turn_count"] == 2
        assert row["total_model_calls"] == 20

    def test_zero_increments_leave_the_columns_alone(self, mock_db):
        """Truthiness gating, same as the pre-existing increments: a 0 (or a None
        that became 0) must not emit ``col = col + NULL`` and must not bump."""
        sessions_mod.update_session("s1", cache_read_increment=500)
        sessions_mod.update_session("s1", title="renamed")

        row = sessions_mod.get_session("s1")
        assert row["total_cache_read_tokens"] == 500
        assert row["total_cache_creation_tokens"] == 0
        assert row["total_model_calls"] == 0
