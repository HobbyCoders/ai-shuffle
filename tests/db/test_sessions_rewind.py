"""
Tests for rewind-related DB helpers on ``app.db.sessions``.

These cover the new JSONL-UUID plumbing that keeps the ``session_messages``
table in sync with the Claude SDK JSONL transcript when a user rewinds a
conversation:

- ``add_session_message(..., jsonl_uuid=...)`` persists the uuid.
- ``add_session_messages_batch([..., {"jsonl_uuid": "..."}])`` persists the uuid.
- ``set_message_jsonl_uuid(message_id, jsonl_uuid)`` backfills it after the
  SDK echoes a user message with its assigned uuid.
- ``delete_session_messages_after_jsonl_uuid(session_id, target_uuid,
  include_response)`` truncates the DB to match a JSONL truncation point.

The helpers do not yet exist — these tests are intentionally failing to
drive the implementation.
"""

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.db import base
from app.db import database as db
from app.db import sessions as sessions_mod


# =============================================================================
# Fixtures
# =============================================================================

def _create_schema(cursor: sqlite3.Cursor) -> None:
    """Minimal schema needed for rewind DB tests."""
    cursor.execute(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            config JSON NOT NULL DEFAULT '{}',
            mcp_tools JSON DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            last_user_message_at TIMESTAMP,
            api_user_id TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            turn_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE session_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input JSON,
            metadata JSON,
            profile_id TEXT,
            jsonl_uuid TEXT,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute(
        "CREATE INDEX idx_session_messages_session_uuid "
        "ON session_messages(session_id, jsonl_uuid)"
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
    # Use the real commit boundary, including after-commit notifications.
    mock_get_db = base.get_db

    def mock_get_connection():
        return test_db

    def mock_get_cursor(conn):
        # sqlite3.Row already supports dict-style access; the production
        # helper passes psycopg2's RealDictCursor which doesn't exist here.
        return conn.cursor()

    with patch.object(base, "get_connection", lambda: test_db), \
         patch.object(base, "_close_connection", lambda conn: None), \
         patch.object(sessions_mod, "get_db", mock_get_db), \
         patch.object(sessions_mod, "get_cursor", mock_get_cursor), \
         patch.object(sessions_mod, "ph", lambda: "?"), \
         patch.object(db, "get_db", mock_get_db), \
         patch.object(db, "get_connection", mock_get_connection):
        # Seed profile + session
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


def _row_uuid(row) -> str | None:
    """Read jsonl_uuid regardless of whether it's stored in a column or in metadata."""
    direct = row.get("jsonl_uuid") if isinstance(row, dict) else None
    if direct:
        return direct
    meta = row.get("metadata") if isinstance(row, dict) else None
    if isinstance(meta, dict):
        return meta.get("jsonl_uuid")
    return None


# =============================================================================
# add_session_message accepts jsonl_uuid
# =============================================================================

class TestAddSessionMessageUUID:
    def test_add_session_message_persists_jsonl_uuid(self, mock_db):
        """add_session_message should accept and persist a JSONL uuid."""
        msg = db.add_session_message(
            session_id="s1",
            role="user",
            content="hello",
            jsonl_uuid="uuid-user-1",
        )
        assert msg["id"] is not None

        rows = db.get_session_messages("s1")
        assert len(rows) == 1
        assert _row_uuid(rows[0]) == "uuid-user-1"

    def test_add_session_message_without_uuid_still_works(self, mock_db):
        """Backwards compat: no uuid supplied -> row still inserts."""
        msg = db.add_session_message(
            session_id="s1",
            role="user",
            content="legacy row",
        )
        assert msg["id"] is not None
        rows = db.get_session_messages("s1")
        assert len(rows) == 1
        assert _row_uuid(rows[0]) is None


# =============================================================================
# add_session_messages_batch accepts jsonl_uuid per row
# =============================================================================

class TestAddSessionMessagesBatchUUID:
    def test_batch_insert_persists_jsonl_uuid_per_row(self, mock_db):
        rows = db.add_session_messages_batch([
            {"session_id": "s1", "role": "user", "content": "q", "jsonl_uuid": "uuid-u"},
            {"session_id": "s1", "role": "assistant", "content": "a", "jsonl_uuid": "uuid-a"},
        ])
        assert len(rows) == 2

        fetched = db.get_session_messages("s1")
        assert [_row_uuid(r) for r in fetched] == ["uuid-u", "uuid-a"]


# =============================================================================
# set_message_jsonl_uuid (user-echo backfill)
# =============================================================================

class TestSetMessageJsonlUUID:
    def test_sets_uuid_on_existing_row(self, mock_db):
        msg = db.add_session_message(
            session_id="s1", role="user", content="hi"
        )
        assert _row_uuid(db.get_session_messages("s1")[0]) is None

        ok = db.set_message_jsonl_uuid(msg["id"], "uuid-backfilled")
        assert ok is True

        assert _row_uuid(db.get_session_messages("s1")[0]) == "uuid-backfilled"

    def test_returns_false_when_message_missing(self, mock_db):
        ok = db.set_message_jsonl_uuid(999_999, "uuid-nope")
        assert ok is False

    def test_does_not_overwrite_other_metadata(self, mock_db):
        """Backfill must preserve any existing metadata fields (e.g. command_name)."""
        msg = db.add_session_message(
            session_id="s1",
            role="user",
            content="/help",
            metadata={"command_name": "help", "command_args": ""},
        )
        db.set_message_jsonl_uuid(msg["id"], "uuid-cmd")

        row = db.get_session_messages("s1")[0]
        assert _row_uuid(row) == "uuid-cmd"
        meta = row.get("metadata") or {}
        assert meta.get("command_name") == "help"
        assert meta.get("command_args") == ""


# =============================================================================
# delete_session_messages_after_jsonl_uuid
# =============================================================================

class TestDeleteSessionMessagesAfterJsonlUUID:
    def _seed(self, mock_db):
        """Seed 5 rows: user u1, asst a1, user u2, asst a2, user u3."""
        rows = [
            {"session_id": "s1", "role": "user", "content": "u1", "jsonl_uuid": "u1"},
            {"session_id": "s1", "role": "assistant", "content": "a1", "jsonl_uuid": "a1"},
            {"session_id": "s1", "role": "user", "content": "u2", "jsonl_uuid": "u2"},
            {"session_id": "s1", "role": "assistant", "content": "a2", "jsonl_uuid": "a2"},
            {"session_id": "s1", "role": "user", "content": "u3", "jsonl_uuid": "u3"},
        ]
        db.add_session_messages_batch(rows)

    def test_drops_target_and_following_rows(self, mock_db):
        """target=u2, include_response=False: drop u2 itself and everything
        after it (a2, u3), keeping only u1, a1.

        This mirrors the JSONL truncation (``entries[:target_index]``) and the
        frontend's documented intent for a user-message rewind:
        ``include_response=false removes this message and everything after``
        (MessageActions.svelte). Rewinding a user message must make that message
        disappear from the chat — keeping it would reintroduce the reported bug.
        """
        self._seed(mock_db)
        deleted = db.delete_session_messages_after_jsonl_uuid(
            session_id="s1",
            target_uuid="u2",
            include_response=False,
        )
        assert deleted == 3
        remaining = [r["content"] for r in db.get_session_messages("s1")]
        assert remaining == ["u1", "a1"]

    def test_include_response_keeps_following_assistant(self, mock_db):
        """target=u2, include_response=True: keep u1, a1, u2, a2. Delete u3."""
        self._seed(mock_db)
        deleted = db.delete_session_messages_after_jsonl_uuid(
            session_id="s1",
            target_uuid="u2",
            include_response=True,
        )
        assert deleted == 1
        remaining = [r["content"] for r in db.get_session_messages("s1")]
        assert remaining == ["u1", "a1", "u2", "a2"]

    def test_unknown_uuid_returns_zero_without_deleting(self, mock_db):
        """Safety: an unknown target uuid is a no-op; no silent purge."""
        self._seed(mock_db)
        deleted = db.delete_session_messages_after_jsonl_uuid(
            session_id="s1",
            target_uuid="does-not-exist",
            include_response=False,
        )
        assert deleted == 0
        assert len(db.get_session_messages("s1")) == 5

    def test_target_is_last_row_drops_target(self, mock_db):
        """target=u3 (the last row), include_response=False: drop u3 itself.

        Even when the target is the final message, a user-message rewind removes
        it (and there is nothing after to remove), leaving the prior turns.
        """
        self._seed(mock_db)
        deleted = db.delete_session_messages_after_jsonl_uuid(
            session_id="s1",
            target_uuid="u3",
            include_response=False,
        )
        assert deleted == 1
        remaining = [r["content"] for r in db.get_session_messages("s1")]
        assert remaining == ["u1", "a1", "u2", "a2"]

    def test_scoped_to_session_id(self, mock_db):
        """Must not delete messages from other sessions even if uuid collides."""
        # Add a second session with a colliding uuid
        now = datetime.now(timezone.utc).isoformat()
        mock_db.execute(
            "INSERT INTO sessions (id, profile_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("s2", "p1", now, now),
        )
        mock_db.commit()
        db.add_session_messages_batch([
            {"session_id": "s2", "role": "user", "content": "other-u1", "jsonl_uuid": "u1"},
            {"session_id": "s2", "role": "assistant", "content": "other-a1", "jsonl_uuid": "a1"},
        ])
        self._seed(mock_db)

        db.delete_session_messages_after_jsonl_uuid(
            session_id="s1",
            target_uuid="u2",
            include_response=False,
        )
        # Session s2 untouched
        other = [r["content"] for r in db.get_session_messages("s2")]
        assert other == ["other-u1", "other-a1"]
