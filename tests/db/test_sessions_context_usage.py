"""
Tests for persisting/reading the SDK context-usage snapshot on
``sessions.context_usage_json`` via ``app.db.sessions``.

``set_session_context_usage`` wraps the raw SDK payload as
``{"captured_at": <iso>, "data": {...}}`` and writes it to the nullable
``context_usage_json`` JSONB column; ``get_session_context_usage_snapshot``
reads it back (decoding the TEXT the SQLite test shim stores for JSONB). This
lets the context-usage endpoint serve the last breakdown after the SDK process
is evicted from the local pool.

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
        CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            config JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Includes the new nullable ``context_usage_json`` column (fresh-DB
    # definition mirrors app/db/schema_postgres.py). Older rows leave it NULL.
    cursor.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            title TEXT,
            status TEXT DEFAULT 'active',
            worktree_id TEXT,
            sdk_session_id TEXT,
            context_usage_json JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


@pytest.fixture
def test_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
            "INSERT INTO agents (id, name, config) VALUES (?, ?, ?)",
            ("p1", "Test", "{}"),
        )
        test_db.execute(
            "INSERT INTO sessions (id, profile_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "p1", now, now),
        )
        test_db.commit()
        yield test_db


class TestSessionContextUsageSnapshot:
    def test_absent_returns_none(self, mock_db):
        """No snapshot written yet -> None."""
        assert sessions_mod.get_session_context_usage_snapshot("s1") is None

    def test_roundtrip_wraps_with_captured_at(self, mock_db):
        """set_ wraps the raw payload; get_ returns {captured_at, data}."""
        usage = {"totalTokens": 1200, "maxTokens": 200000}

        sessions_mod.set_session_context_usage("s1", usage)
        snap = sessions_mod.get_session_context_usage_snapshot("s1")

        assert snap is not None
        assert snap["data"] == usage
        assert isinstance(snap["captured_at"], str) and snap["captured_at"]

    def test_overwrites_prior_snapshot(self, mock_db):
        """A later capture replaces the stored payload."""
        sessions_mod.set_session_context_usage("s1", {"totalTokens": 1})
        sessions_mod.set_session_context_usage("s1", {"totalTokens": 999})

        snap = sessions_mod.get_session_context_usage_snapshot("s1")
        assert snap["data"] == {"totalTokens": 999}

    def test_missing_session_returns_none(self, mock_db):
        """Unknown session id -> None (no row)."""
        assert sessions_mod.get_session_context_usage_snapshot("nope") is None
