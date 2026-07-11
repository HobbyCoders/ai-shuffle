"""
Tests for persisting the effective launched model on ``sessions.launched_model``
via ``app.db.sessions.update_session``.

The effective model a CLI/SDK session was launched (or switched) with is written
to the durable ``sessions.launched_model`` column at spawn/link time so an
out-of-process reader (the Unraid host-op guard) can authoritatively read the
model WITHOUT waiting for turn-end ``session_messages.metadata['model']``
commits. This proves the write path lands the value on the row.

Fixture mirrors tests/db/test_delete_orphan_user_turn.py.
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
        CREATE TABLE profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            config JSON NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Includes the new nullable ``launched_model`` column (fresh-DB definition
    # mirrors app/db/schema_postgres.py). Older rows leave it NULL.
    cursor.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            title TEXT,
            status TEXT DEFAULT 'active',
            worktree_id TEXT,
            sdk_session_id TEXT,
            launched_model TEXT,
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
            "INSERT INTO profiles (id, name, config) VALUES (?, ?, ?)",
            ("p1", "Test", "{}"),
        )
        test_db.execute(
            "INSERT INTO sessions (id, profile_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("s1", "p1", now, now),
        )
        test_db.commit()
        yield test_db


class TestUpdateSessionLaunchedModel:
    def test_persists_launched_model(self, mock_db):
        """update_session(launched_model=...) lands on the row."""
        assert sessions_mod.get_session("s1")["launched_model"] is None

        sessions_mod.update_session("s1", launched_model="claude-fable-5")

        assert sessions_mod.get_session("s1")["launched_model"] == "claude-fable-5"

    def test_none_leaves_column_unchanged(self, mock_db):
        """Passing launched_model=None must not clobber a previously set value."""
        sessions_mod.update_session("s1", launched_model="claude-fable-5")
        # A later unrelated update (launched_model omitted / None) keeps it.
        sessions_mod.update_session("s1", title="renamed")

        assert sessions_mod.get_session("s1")["launched_model"] == "claude-fable-5"
